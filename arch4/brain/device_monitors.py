"""
Device monitor lane adapters.

Monitors are advisory only: they emit status/events, never direct modem actions.

── What belongs here vs DecisionEngine ──────────────────────────────────────

  DeviceMonitor subclasses:
    ✓ Observe modem state, radio health, timing conditions
    ✓ Emit runtime events and status flags (e.g. radio hold, morning sequence)
    ✓ Surface status via extra["device_monitor"] for runtime/telemetry consumers
    ✗ NEVER call modem.execute_lever() or return a LeverAction
    ✗ NEVER mutate state that DecisionEngine reads for decisions

  DecisionEngine (brain/decision.py + mixins):
    ✓ Sole source of LeverActions
    ✓ Night band training (DecisionNightTrainingMixin)
    ✓ Band selection, cell lock, restart decisions
    ✓ Receives (modem, computed) — does NOT read extra["device_monitor"]

  Important: device_monitor status goes into extra for runtime/logging consumption,
  but decide() does not read extra. If decision logic needs device state, it
  must be plumbed explicitly (e.g. via note_health_status or similar).

  The distinction matters for B818/Huawei:
    HuaweiHiLinkDeviceMonitor wraps HuaweiHiLinkSupervisor, which tracks
    morning sequence and reboot conditions. It reports these as status flags
    for runtime observability and telemetry. The decision pipeline has its own
    channels for receiving health/incident context when needed.
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Protocol

from arch4.interfaces import LeverAction, LeverType
from arch4.b818.supervisor import HuaweiHiLinkSupervisor
from arch4.modem_live.profile_catalog import (
    FAMILY_HUAWEI_HILINK,
    FAMILY_OPENWRT_MM,
    FAMILY_ROUTEROS_AT,
    FAMILY_ZTE_WEBUI,
    FAMILY_ZTE_ZWRT,
    modem_backend_family,
)


class DeviceMonitor(Protocol):
    def observe(self, modem: Any, extra: Dict[str, Any]) -> None:
        ...

    def note_action_result(self, action: LeverAction, success: bool, now: float) -> None:
        ...

    def drain_runtime_events(self) -> list[Dict[str, Any]]:
        ...

    def get_runtime_status(self) -> Dict[str, Any]:
        ...


class NoopDeviceMonitor:
    """Placeholder monitor for backends without dedicated monitor logic yet."""

    def __init__(self, backend: str):
        self.backend = str(backend or "").lower()
        self._counts = {"observe_calls": 0}
        self._started_at = time.time()

    def observe(self, modem: Any, extra: Dict[str, Any]) -> None:
        del modem, extra
        self._counts["observe_calls"] += 1

    def note_action_result(self, action: LeverAction, success: bool, now: float) -> None:
        del action, success, now

    def drain_runtime_events(self) -> list[Dict[str, Any]]:
        return []

    def get_runtime_status(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "mode": "placeholder_noop",
            "uptime_s": max(0.0, time.time() - self._started_at),
            "counts": dict(self._counts),
        }


class MikroTikDeviceMonitor:
    """MikroTik monitor lane.

    RouterOS owns radio recovery on MikroTik routers. This monitor reports link
    down/recovered timing only; it never recommends restart/reboot recovery.
    """

    def __init__(
        self,
        backend: str = "mikrotik",
        *,
        restart_recommend_after_s: float = 10.0,
        restart_cooldown_s: float = 120.0,
    ):
        self.backend = str(backend or "").lower()
        self._radio_down_notice_after_s = max(0.0, float(restart_recommend_after_s))
        self._hold_notice_cooldown_s = max(0.0, float(restart_cooldown_s))
        self._started_at = time.time()
        self._runtime_events: list[Dict[str, Any]] = []
        self._counts = {
            "hold_notices": 0,
            "link_down_events": 0,
            "link_recovered_events": 0,
        }
        self._last_ok_ts: float = 0.0
        self._emergency_active: bool = False
        self._reboot_done_at: float = 0.0
        self._hold_notice_active: bool = False
        self._hold_notice_cooldown_until: float = 0.0

    def _emit(self, kind: str, **payload) -> None:
        self._runtime_events.append({"kind": kind, "backend": self.backend, **payload})

    def observe(self, modem: Any, extra: Dict[str, Any]) -> None:
        del extra
        now = time.time()

        connected_ok = bool(modem is not None and getattr(modem.cellular, 'radio_connected', False))
        if connected_ok:
            if self._emergency_active and self._last_ok_ts > 0.0:
                down_for = max(0.0, now - self._last_ok_ts)
                self._counts["link_recovered_events"] += 1
                self._emit("mikrotik_monitor_recovered", down_for_s=round(down_for, 1))
            self._last_ok_ts = now
            self._emergency_active = False
            self._hold_notice_active = False
            return

        if self._last_ok_ts == 0.0:
            self._last_ok_ts = now
        if not self._emergency_active:
            self._emergency_active = True
            self._counts["link_down_events"] += 1
            self._emit("mikrotik_monitor_link_down")

        down_for = max(0.0, now - self._last_ok_ts)
        if (
            not self._hold_notice_active
            and now >= self._hold_notice_cooldown_until
            and down_for >= self._radio_down_notice_after_s
        ):
            self._hold_notice_active = True
            self._counts["hold_notices"] += 1
            self._emit(
                "mikrotik_monitor_hold_routeros_recovery",
                reason="radio down persistent; RouterOS owns recovery",
                down_for_s=round(down_for, 1),
                threshold_s=int(self._radio_down_notice_after_s),
            )

    def note_action_result(self, action: LeverAction, success: bool, now: float) -> None:
        wants_restart = (
            action.lever == LeverType.RESTART
            or (action.lever == LeverType.UE_CAPABILITY and bool(action.params.get("restart")))
        )
        if not wants_restart:
            return
        self._reboot_done_at = float(now)
        self._hold_notice_cooldown_until = float(now) + self._hold_notice_cooldown_s
        self._hold_notice_active = False
        self._emit(
            "mikrotik_monitor_restart_observed",
            success=bool(success),
            cooldown_s=int(self._hold_notice_cooldown_s),
        )

    def drain_runtime_events(self) -> list[Dict[str, Any]]:
        out = list(self._runtime_events)
        self._runtime_events.clear()
        return out

    def get_runtime_status(self) -> Dict[str, Any]:
        now = time.time()
        last_ok_age = max(0.0, now - self._last_ok_ts) if self._last_ok_ts > 0.0 else 0.0
        return {
            "backend": self.backend,
            "mode": "mikrotik_monitor",
            "uptime_s": max(0.0, now - self._started_at),
            "radio_down_notice_after_s": self._radio_down_notice_after_s,
            "routeros_recovery_hold_active": bool(self._hold_notice_active),
            "emergency_active": bool(self._emergency_active),
            "restart_recommend_active": False,
            "reboot_pending": False,
            "restart_recommendation_id": "",
            "restart_cooldown_active": bool(now < self._hold_notice_cooldown_until),
            "reboot_cooldown_active": bool(now < self._hold_notice_cooldown_until),
            "morning_sequence_recommend_active": False,
            "last_ok_age_s": round(last_ok_age, 1),
            "counts": dict(self._counts),
        }


class HuaweiHiLinkDeviceMonitor:
    """Monitor lane adapter for Huawei HiLink modems (B818, B535, and family).

    Wraps HuaweiHiLinkSupervisor as a pure observation/recommendation layer.

    What this class does:
      - Tracks radio health (emergency_active, reconnect/reboot pending)
      - Tracks morning sequence conditions (should the Brain run B20→B3→B1?)
      - Emits runtime events for the event journal
      - Exposes get_runtime_status() → extra["device_monitor"] for runtime/Brain

    What this class does NOT do:
      - Issue any LeverActions or modem commands
      - Make band selection decisions (that is DecisionEngine's job)
      - Run night band training (that is DecisionNightTrainingMixin's job)

    The PolicyProfile flags that relate to this monitor:
      b818_keep_device_monitor=True  → this monitor is created and runs
      b818_use_full_brain=True       → DecisionEngine runs full brain path

    Both flags are independent. Setting b818_keep_device_monitor=False disables
    this monitor but leaves DecisionEngine running unchanged.
    """

    def __init__(self, backend: str = "b818"):
        backend_name = str(backend or "b818").strip().lower()
        self._impl = HuaweiHiLinkSupervisor(
            backend=backend_name,
            event_prefix="hilink_monitor",
            enable_morning_sequence=True,
        )

    def observe(self, modem: Any, extra: Dict[str, Any]) -> None:
        self._impl.observe(modem, extra)

    def note_action_result(self, action: LeverAction, success: bool, now: float) -> None:
        self._impl.note_action_result(action, success, now)

    def drain_runtime_events(self) -> list[Dict[str, Any]]:
        return self._impl.drain_runtime_events()

    def get_runtime_status(self) -> Dict[str, Any]:
        return self._impl.get_runtime_status()


def create_device_monitor(backend: str) -> Optional[DeviceMonitor]:
    """Create the appropriate device monitor for a modem backend.

    Monitor routing by backend:

      b818 / b535 / huawei (FAMILY_HUAWEI_HILINK)
        → HuaweiHiLinkDeviceMonitor
          Tracks morning sequence and radio-down reboot conditions.
          Night band training is NOT here — it is in DecisionNightTrainingMixin.

      mikrotik (FAMILY_ROUTEROS_AT)
        → MikroTikDeviceMonitor
          Tracks radio health and recommends restart if link is persistently down.

      openwrt / zte_goform / zte_zwrt (FAMILY_OPENWRT_MM / ZTE families)
        → NoopDeviceMonitor
          Placeholder — emits no events, provides basic status only.

      model (replay/sim) or unknown
        → None (no monitor)
    """
    b = str(backend or "").strip().lower()
    family = modem_backend_family(b)
    if family == FAMILY_HUAWEI_HILINK:
        return HuaweiHiLinkDeviceMonitor(b)
    if family == FAMILY_ROUTEROS_AT:
        return MikroTikDeviceMonitor(b)
    if family in {FAMILY_OPENWRT_MM, FAMILY_ZTE_WEBUI, FAMILY_ZTE_ZWRT}:
        return NoopDeviceMonitor(b)
    return None
