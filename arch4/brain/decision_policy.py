from arch4.controller_frames import DerivedSignalFrames
"""
Decision policy mixin extracted from arch4/brain/decision.py.

── Seed 2: Dependency Boundary Contract ─────────────────────────────────────

This mixin owns provider/modem-specific policy overrides and exploration logic.
It is a POLICY LAYER — it filters and adjusts actions proposed by state dispatch.

What it accepts (stable contract inputs):
  - CellularIntelligenceGeneration, DerivedSignalFrames: raw observations from the input layer
  - SystemState: current FSM state
  - LeverAction (optional): action proposed by state dispatch

What it returns:
  - A LeverAction (or None) after applying policy filters

What it does NOT do:
  - Make modem hardware calls
  - Read from filesystem or network
  - Own cooldown state for rate limiting (that belongs in DecisionPipelineMixin)

Dependency rule:
  - May import from arch4.policy.* (policy helpers)
  - May import from arch4.interfaces (contracts)
  - Must NOT import from arch4.modem.* (hardware) or arch4.ngui.* (presentation)
─────────────────────────────────────────────────────────────────────────────
"""

from arch4.controller_frames import CellularIntelligenceGeneration

import logging
import time
from typing import Optional, List

from arch4.operator_profiles import FAMILY_HUAWEI_HILINK, FAMILY_ROUTEROS_AT
from arch4.interfaces import (
LeverAction,
    LeverType,
    is_fault_state,
)
from arch4.policy.a1_b818_windows import (
    a1_restricted_band_pool,
    filter_a1_restricted_bands,
    hour_in_window,
    is_a1_overnight_profile,
    is_a1_hard_night_freeze_window,
    is_a1_restricted_night_window,
    policy_provider_plmn,
)
from arch4.policy.family_rotate_policy import (
    allow_family_priority_rotate,
    family_priority_rotate_order,
    note_family_priority_rotate_reverted,
    note_family_priority_rotate_stable,
    record_family_priority_rotate_attempt,
)
from arch4.policy.emergency_backend_policy import (
    emergency_backend,
    make_emergency_backend_action,
)
from arch4.policy.family_objective_policy import (
    check_family_explore_objective_block,
    is_family_cell_lock_objective,
    note_family_cell_lock_retry,
    note_family_explore_result,
)

log = logging.getLogger("arch4.brain.decision")


class DecisionPolicyMixin:
    @staticmethod
    def _decision_policy_log():
        return log

    def _is_in_night_training_window(self, now: Optional[float] = None) -> bool:
        """Return True if local time is within the configured night-training window."""
        ts = time.time() if now is None else now
        hour = time.localtime(ts).tm_hour
        start_h = self.decision_policy_cfg.night_training_start_hour
        end_h = self.decision_policy_cfg.night_training_end_hour
        if start_h <= end_h:
            return start_h <= hour < end_h
        return hour >= start_h or hour < end_h

    def _is_a1_overnight_profile(self) -> bool:
        return is_a1_overnight_profile(self)

    def _policy_provider_plmn(self) -> str:
        return policy_provider_plmn(self)

    @staticmethod
    def _hour_in_window(hour: int, start_h: int, end_h: int) -> bool:
        return hour_in_window(hour, start_h, end_h)

    def _active_bands(self, modem: CellularIntelligenceGeneration) -> List[int]:
        # Keep one active-band source of truth in decision pipeline.
        return self._current_active_bands(modem)

    def _is_a1_restricted_night_window(
        self,
        now: Optional[float] = None,
        modem: Optional[CellularIntelligenceGeneration] = None,
    ) -> bool:
        return is_a1_restricted_night_window(self, now=now, modem=modem)

    def _is_a1_hard_night_freeze_window(
        self,
        now: Optional[float] = None,
    ) -> bool:
        return is_a1_hard_night_freeze_window(self, now=now)

    def _a1_restricted_band_pool(self, modem: CellularIntelligenceGeneration, now: Optional[float] = None) -> List[int]:
        return a1_restricted_band_pool(self, modem, now=now)

    def _filter_a1_restricted_bands(
        self, bands: List[int], modem: CellularIntelligenceGeneration, now: Optional[float] = None
    ) -> List[int]:
        return filter_a1_restricted_bands(self, bands, modem, now=now)

    def _decide_a1_overnight_recovery(
        self, modem: CellularIntelligenceGeneration, now: float
    ) -> tuple[Optional[LeverAction], bool]:
        del modem, now
        raise RuntimeError("old_provider_overnight_action_owner_deleted")

    def _suppress_a1_provider_daytime_band_action(
        self, action: Optional[LeverAction], *, now: Optional[float] = None
    ) -> Optional[LeverAction]:
        del action, now
        raise RuntimeError("old_provider_daytime_filter_owner_deleted")

    def _decide_provider_overnight_recovery(
        self, modem: CellularIntelligenceGeneration, now: float
    ) -> tuple[Optional[LeverAction], bool]:
        del modem, now
        raise RuntimeError("old_provider_overnight_action_owner_deleted")

    def _suppress_b818_daytime_band_action(self, action: Optional[LeverAction]) -> Optional[LeverAction]:
        del action
        raise RuntimeError("old_provider_daytime_filter_owner_deleted")

    def _filter_daytime_band_action_for_backend(
        self, action: Optional[LeverAction]
    ) -> Optional[LeverAction]:
        del action
        raise RuntimeError("old_provider_daytime_filter_owner_deleted")

    def _guard_emergency_action(self, action: Optional[LeverAction]) -> Optional[LeverAction]:
        """
        Independent emergency governor:
        - minimum cooldown between emergency actions
        - window budget with global cooldown on burst
        """
        if not action:
            return None
        if not is_fault_state(self.posture_authority.state):
            return action
        # Systematic BAD_RF probing has its own slow dwell/cooldown pacing.
        # If the generic emergency guard blocks it here, the v6.4 probe lane
        # can never start after the step-ladder budget is exhausted.
        if str(getattr(action, "intent", "") or "") in {"systematic_probe", "systematic_probe_hold"}:
            return action
        # Fast emergency restart path has its own dedicated cooldown gate.
        if action.lever == LeverType.RESTART and action.params.get("emergency_fast"):
            return action
        now = time.time()
        if not self._availability_scalar_probe_allowed(
            cooldown="emergency",
            reason_code="emergency",
            reason="emergency action cooldown active",
            source="emergency_action",
            now=now,
        ):
            return None

        window_s = max(0.0, self.decision_policy_cfg.emergency_action_window_s)
        max_in_window = max(1, self.decision_policy_cfg.emergency_action_max_per_window)
        self._emergency_actions_window = [
            t for t in self._emergency_actions_window if (now - t) <= window_s
        ]
        if len(self._emergency_actions_window) >= max_in_window:
            cooldown_s = max(0.0, self.decision_policy_cfg.emergency_action_global_cooldown_s)
            self.cooldowns.set("emergency", hold_s=cooldown_s, now=now)
            self._emit_runtime_event(
                "emergency_action_cooldown_started",
                cooldown_s=int(cooldown_s),
                actions_in_window=len(self._emergency_actions_window),
                window_s=int(window_s),
            )
            log.warning(
                "Emergency action guard: %d actions within %ds; suppressing further emergency actions for %.0f min",
                len(self._emergency_actions_window), int(window_s), cooldown_s / 60.0,
            )
            return None

        self._emergency_actions_window.append(now)
        self.cooldowns.set(
            "emergency",
            hold_s=max(0.0, self.decision_policy_cfg.emergency_action_cooldown_s),
            now=now,
        )
        return action

    def _decide_hilink_radio_drop_recovery(self, modem: CellularIntelligenceGeneration, now: float) -> tuple[Optional[LeverAction], bool]:
        del modem, now
        raise RuntimeError("old_provider_radio_drop_action_owner_deleted")

    def _decide_provider_radio_drop_recovery(
        self, modem: CellularIntelligenceGeneration, now: float
    ) -> tuple[Optional[LeverAction], bool]:
        del modem, now
        raise RuntimeError("old_provider_radio_drop_action_owner_deleted")

    def _decide_a1_b818_morning_sequence(
        self,
        modem: CellularIntelligenceGeneration,
        now: float,
    ) -> Optional[LeverAction]:
        del modem, now
        raise RuntimeError("old_provider_morning_action_owner_deleted")

    def _decide_a1_provider_morning_sequence(
        self,
        modem: CellularIntelligenceGeneration,
        now: float,
    ) -> Optional[LeverAction]:
        del modem, now
        raise RuntimeError("old_provider_morning_action_owner_deleted")

    def _decide_provider_morning_sequence(
        self,
        modem: CellularIntelligenceGeneration,
        now: float,
    ) -> Optional[LeverAction]:
        del modem, now
        raise RuntimeError("old_provider_morning_action_owner_deleted")

    def _provider_morning_routine_guard_status(self, now: float) -> dict:
        del now
        raise RuntimeError("old_provider_morning_guard_owner_deleted")

    def _note_hilink_overnight_action(self, action: LeverAction, now: float) -> None:
        del action, now
        raise RuntimeError("old_provider_result_writer_deleted")

    def _note_a1_overnight_action(self, action: LeverAction, now: float) -> None:
        self._note_hilink_overnight_action(action, now)

    def _apply_hilink_restricted_band_filter(
        self,
        action: Optional[LeverAction],
        modem: CellularIntelligenceGeneration,
        now: float,
    ) -> Optional[LeverAction]:
        del action, modem, now
        raise RuntimeError("old_post_owner_provider_restricted_filter_deleted")

    def _apply_a1_restricted_band_filter(
        self,
        action: Optional[LeverAction],
        modem: CellularIntelligenceGeneration,
        now: float,
    ) -> Optional[LeverAction]:
        del action, modem, now
        raise RuntimeError("old_post_owner_provider_restricted_filter_deleted")

    def _guard_hilink_antithrash(
        self,
        action: Optional[LeverAction],
        modem: CellularIntelligenceGeneration,
        now: float,
    ) -> Optional[LeverAction]:
        del action, modem, now
        raise RuntimeError("old_provider_antithrash_guard_owner_deleted")

    def _guard_a1_antithrash(
        self,
        action: Optional[LeverAction],
        modem: CellularIntelligenceGeneration,
        now: float,
    ) -> Optional[LeverAction]:
        del action, modem, now
        raise RuntimeError("old_provider_antithrash_guard_owner_deleted")

    def _allow_hilink_priority_rotate(self, now: float) -> bool:
        return allow_family_priority_rotate(self, family=FAMILY_HUAWEI_HILINK, now=now)

    def _note_hilink_priority_rotate_stable(self, target_band: int, now: float) -> None:
        note_family_priority_rotate_stable(self, target_band=target_band, now=now, family=FAMILY_HUAWEI_HILINK)

    def _note_hilink_priority_rotate_reverted(
        self,
        target_band: int,
        current_band: int,
        now: float,
    ) -> None:
        note_family_priority_rotate_reverted(
            self,
            target_band=target_band,
            current_band=current_band,
            now=now,
            family=FAMILY_HUAWEI_HILINK,
        )

    def _allow_mikrotik_priority_rotate(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        now: float,
    ) -> bool:
        return allow_family_priority_rotate(
            self,
            family=FAMILY_ROUTEROS_AT,
            modem=modem,
            computed=computed,
            now=now,
        )

    def _allow_routeros_priority_rotate(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        now: float,
    ) -> bool:
        return self._allow_mikrotik_priority_rotate(modem, computed, now)

    def _mikrotik_priority_rotate_order(self, active_bands: List[int], current_primary: int) -> List[int]:
        return family_priority_rotate_order(
            self,
            active_bands,
            current_primary,
            family=FAMILY_ROUTEROS_AT,
        )

    def _routeros_priority_rotate_order(self, active_bands: List[int], current_primary: int) -> List[int]:
        return self._mikrotik_priority_rotate_order(active_bands, current_primary)

    def _record_mikrotik_priority_rotate_attempt(self, now: float) -> None:
        record_family_priority_rotate_attempt(self, now=now, family=FAMILY_ROUTEROS_AT)

    def _record_routeros_priority_rotate_attempt(self, now: float) -> None:
        self._record_mikrotik_priority_rotate_attempt(now)

    def _note_mikrotik_priority_rotate_stable(self, target_band: int, now: float) -> None:
        note_family_priority_rotate_stable(self, target_band=target_band, now=now, family=FAMILY_ROUTEROS_AT)

    def _note_routeros_priority_rotate_stable(self, target_band: int, now: float) -> None:
        self._note_mikrotik_priority_rotate_stable(target_band, now)

    def _note_mikrotik_priority_rotate_reverted(
        self,
        target_band: int,
        current_band: int,
        now: float,
    ) -> None:
        note_family_priority_rotate_reverted(
            self,
            target_band=target_band,
            current_band=current_band,
            now=now,
            family=FAMILY_ROUTEROS_AT,
        )

    def _note_routeros_priority_rotate_reverted(
        self,
        target_band: int,
        current_band: int,
        now: float,
    ) -> None:
        self._note_mikrotik_priority_rotate_reverted(target_band, current_band, now)

    def _check_routeros_explore_objective_block(
        self,
        action: LeverAction,
        now: float,
    ) -> tuple[bool, str]:
        return check_family_explore_objective_block(
            self,
            action=action,
            now=now,
            family=FAMILY_ROUTEROS_AT,
        )

    def _check_mikrotik_explore_objective_block(
        self,
        action: LeverAction,
        now: float,
    ) -> tuple[bool, str]:
        return self._check_routeros_explore_objective_block(action, now)

    def _note_routeros_explore_result(
        self,
        action: LeverAction,
        *,
        attached: bool,
        now: float,
        target_sig: str,
    ) -> None:
        note_family_explore_result(
            self,
            action=action,
            attached=attached,
            now=now,
            target_sig=target_sig,
            family=FAMILY_ROUTEROS_AT,
        )

    def _note_mikrotik_explore_result(
        self,
        action: LeverAction,
        *,
        attached: bool,
        now: float,
        target_sig: str,
    ) -> None:
        self._note_routeros_explore_result(
            action,
            attached=attached,
            now=now,
            target_sig=target_sig,
        )

    @staticmethod
    def _is_routeros_cell_lock_objective(action: LeverAction) -> bool:
        return is_family_cell_lock_objective(action, family=FAMILY_ROUTEROS_AT)

    @staticmethod
    def _is_mikrotik_cell_lock_objective(action: LeverAction) -> bool:
        return DecisionPolicyMixin._is_routeros_cell_lock_objective(action)

    def _note_routeros_cell_lock_retry(
        self,
        action: LeverAction,
        *,
        objective_id: str,
        requested_pci: int,
        requested_earfcn: int,
        now: float,
    ) -> None:
        note_family_cell_lock_retry(
            self,
            action=action,
            objective_id=objective_id,
            requested_pci=requested_pci,
            requested_earfcn=requested_earfcn,
            now=now,
            family=FAMILY_ROUTEROS_AT,
        )

    def _note_mikrotik_cell_lock_retry(
        self,
        action: LeverAction,
        *,
        objective_id: str,
        requested_pci: int,
        requested_earfcn: int,
        now: float,
    ) -> None:
        self._note_routeros_cell_lock_retry(
            action,
            objective_id=objective_id,
            requested_pci=requested_pci,
            requested_earfcn=requested_earfcn,
            now=now,
        )

    def _emergency_backend(self) -> str:
        return emergency_backend(self)

    def _make_emergency_backend_action(self, emergency_class: str) -> Optional[LeverAction]:
        return make_emergency_backend_action(self, emergency_class=emergency_class)
