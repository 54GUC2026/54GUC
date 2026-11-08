"""
arch4/b818/supervisor.py
═══════════════════════════════════════════════════════════════
B818 supervisor monitor lane (non-actuating).

Responsibilities:
  - observe modem/control context for health and sequence conditions
  - emit recommendations/events for runtime observability and telemetry
  - never return direct modem actions

All modem actuation belongs to Brain decision path.

── What lives HERE vs in DecisionEngine ─────────────────────

  HuaweiHiLinkSupervisor (this file):
    ✓ Radio health tracking (emergency_active, reboot_pending)
    ✓ Morning sequence detection (should B20→B3→B1 run at startup?)
    ✓ Reboot cooldown tracking
    ✓ Event emission for the journal
    ✓ Status surfaced via extra["device_monitor"] for runtime/telemetry
    ✗ No band selection decisions
    ✗ No night band training
    ✗ No LeverAction emission

  DecisionEngine + DecisionNightTrainingMixin (brain/decision*.py):
    ✓ All band selection (BAND_SELECTION lever)
    ✓ Cell lock (CELL_LOCK lever)
    ✓ Modem restart (RESTART lever)
    ✓ Night band training 00:00–06:00 (probing, hold, recheck)
    ✓ Morning band restore sequence
    ✓ Anti-thrash and forced schedule (A1+B818 policy)

  Important dataflow note:
    - This supervisor's status goes into extra["device_monitor"]
    - Runtime export/telemetry paths read extra["device_monitor"]
    - decide() receives (modem, computed), NOT extra
    - If decision logic needs supervisor state, it must be plumbed
      explicitly through note_health_status() or similar channels

  The night training loop is entirely in DecisionNightTrainingMixin and
  is NOT reflected in this supervisor's state.
─────────────────────────────────────────────────────────────
"""

from dataclasses import dataclass
import time
from typing import Optional, Dict, Any, List

from arch4.interfaces import LeverAction, LeverType


@dataclass
class SequenceState:
    active: bool = False
    attempt: int = 0
    retry_at: float = 0.0
    last_run_date: Optional[tuple] = None


class HuaweiHiLinkSupervisor:
    def __init__(
        self,
        retry_delay_s: float = 1800.0,
        *,
        backend: str = "b818",
        event_prefix: str = "",
        enable_morning_sequence: bool = True,
    ):
        self._retry_delay_s = retry_delay_s
        self._backend = str(backend or "b818").strip().lower()
        self._event_prefix = (
            str(event_prefix or "").strip()
            or ("b818_monitor" if self._backend == "b818" else "huawei_monitor")
        )
        self._enable_morning_sequence = bool(enable_morning_sequence)
        self._last_ok_ts: float = 0.0
        self._emergency_active: bool = False
        self._reboot_pending: bool = False
        self._restart_recommendation_id: str = ""
        self._reboot_done_at: float = 0.0
        self._reboot_cooldown_until: float = 0.0
        self._sequence = SequenceState()
        self._runtime_events: List[dict] = []
        self._counts = {
            "recommend_restart": 0,
            "recommend_morning_sequence": 0,
        }

    def _emit(self, kind: str, **payload) -> None:
        self._runtime_events.append({"kind": kind, "backend": self._backend, **payload})

    def drain_runtime_events(self) -> List[dict]:
        ev = self._runtime_events[:]
        self._runtime_events.clear()
        return ev

    def get_runtime_status(self) -> Dict[str, Any]:
        return {
            "backend": self._backend,
            "mode": self._event_prefix,
            "emergency_active": bool(self._emergency_active),
            "reboot_pending": bool(self._reboot_pending),
            "restart_recommendation_id": self._restart_recommendation_id,
            "reboot_cooldown_active": bool(time.time() < self._reboot_cooldown_until),
            "morning_sequence_recommend_active": bool(self._enable_morning_sequence and self._sequence.active),
            "counts": dict(self._counts),
        }

    def note_modem_ok(self, now: float) -> None:
        self._last_ok_ts = now
        if self._emergency_active:
            self._emergency_active = False
            self._reboot_pending = False
            self._restart_recommendation_id = ""
            self._reboot_done_at = 0.0
            self._emit(f"{self._event_prefix}_recovered")

    def note_action_result(self, action: LeverAction, success: bool, now: float) -> None:
        if action.lever in {LeverType.RESTART, LeverType.UE_CAPABILITY} and action.params.get("restart"):
            self._reboot_pending = False
            self._restart_recommendation_id = ""
            self._reboot_done_at = now
            self._reboot_cooldown_until = now + 120.0
            if success:
                self._last_ok_ts = now
                self._emit(f"{self._event_prefix}_restart_executed", success=True)
            else:
                self._emit(f"{self._event_prefix}_restart_executed", success=False)

    def _maybe_recommend_daily_sequence(self, now: float) -> None:
        if not self._enable_morning_sequence:
            return
        lt = time.localtime(now)
        date_key = (lt.tm_year, lt.tm_mon, lt.tm_mday)
        if lt.tm_hour == 6 and lt.tm_min == 1 and self._sequence.last_run_date != date_key:
            self._sequence.last_run_date = date_key
            self._sequence.active = True
            self._sequence.attempt = 0
            self._counts["recommend_morning_sequence"] += 1
            self._emit(f"{self._event_prefix}_recommend_morning_sequence")

    def observe(self, modem, extra: Optional[Dict[str, Any]]) -> None:
        del extra
        now = time.time()
        connected_ok = modem is not None and modem.cellular.radio_connected

        if connected_ok:
            self.note_modem_ok(now)
        else:
            if self._last_ok_ts == 0.0:
                self._last_ok_ts = now
            if (now - self._last_ok_ts) > 10.0:
                self._emergency_active = True
                if not self._reboot_pending and now >= self._reboot_cooldown_until:
                    self._reboot_pending = True
                    self._counts["recommend_restart"] += 1
                    self._restart_recommendation_id = (
                        f"{self._event_prefix}:{self._backend}:"
                        f"{self._last_ok_ts:.6f}:"
                        f"{self._counts['recommend_restart']}"
                    )
                    self._emit(f"{self._event_prefix}_recommend_restart", reason="modem down >10s")

        # Recommendation only; Brain owns the actual sequence actions.
        self._maybe_recommend_daily_sequence(now)

        # Sequence recommendation can be cleared once radio is healthy later in the morning.
        if self._enable_morning_sequence and self._sequence.active and connected_ok and modem and modem.cellular.primary_band == 3:
            self._sequence.active = False
            self._emit(f"{self._event_prefix}_morning_sequence_satisfied")
