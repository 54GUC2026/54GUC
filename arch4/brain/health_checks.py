"""
arch4/brain/health_checks.py

ARCH6v1.2 health-check framework.
Uses runtime WAN proof (RTT/data traffic) with debounce/hysteresis.

── Seed 3: Health-Gating Position in Decision Order ─────────────────────────

Health checks run at Lane 4, gate "health_hold" — the FOURTH guard in the
action pipeline. This means:

  Evaluated AFTER:
    - objective_lifecycle (reject no-effect actions)
    - manual_action_hold (respect operator band-hold)
    - incident_hold (respect incident assessor decision)

  Evaluated BEFORE:
    - no_effect_precheck, a1_restricted_filter, learning_guard, etc.

  Consequence: if health is degraded, actions are suppressed even if the
  decision engine and incident assessor both want to act. Health hold is
  intentionally conservative — it waits for the WAN path to stabilize
  before executing modem changes.

  Health hold does NOT block:
    - E_* fault-state actions (apply_emergency_guard bypasses most guards)
    - Manual actions from the GUI (manual_action_hold has separate logic)

  Health stages (in order of severity):
    healthy → degraded (ping failures accumulating) → hold_down (active block)
                                                     → recovering (failures clearing)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

from dataclasses import dataclass
from typing import Dict, Any, List, Optional
import time

from arch4.interfaces import Arch4Config
from arch4.config_views import build_health_check_config
from arch4.modem.utils import measure_host_ping_rtt


@dataclass
class HealthStatus:
    ping_ok: bool = True
    ping_fail_streak: int = 0
    ping_success_streak: int = 0
    overall_ok: bool = True
    hold_down_until_ts: float = 0.0
    last_change_ts: float = 0.0
    degraded_since_ts: float = 0.0
    stage: str = "healthy"
    action_hint: str = "none"
    controlled_settle_active: bool = False
    controlled_settle_reason: str = ""
    controlled_settle_until_ts: float = 0.0
    controlled_settle_miss_streak: int = 0
    controlled_settle_miss_count: int = 0
    last_controlled_settle_miss_ts: float = 0.0


@dataclass
class TargetHealth:
    ok: bool = True
    fail_streak: int = 0
    success_streak: int = 0
    last_change_ts: float = 0.0


class HealthCheckEngine:
    def __init__(self, cfg: Arch4Config):
        self.cfg = cfg
        self.health_cfg = build_health_check_config(cfg)
        self._st = HealthStatus()
        self._events: List[Dict[str, Any]] = []
        self._targets: Dict[str, TargetHealth] = {}
        self._multi_target_active = bool(self.health_cfg.multi_target_enabled)

    def refresh_thresholds(self) -> None:
        self.health_cfg = build_health_check_config(self.cfg)

    def on_modem_read_failed(self, now: float | None = None) -> None:
        now = float(now or time.time())
        self._update_with_sample(sample_ok=False, rtt_ms=None, now=now)

    def update(
        self,
        modem: CellularIntelligenceGeneration,
        rtt_ms: float | None,
        now: float | None = None,
        probe_results: Optional[Dict[str, bool]] = None,
        controlled_settle: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = float(now or time.time())
        data_active = bool(
            float(getattr(modem.traffic, 'rx_bytes_per_sec', 0.0) or 0.0) > 0.0
            or float(getattr(modem.traffic, 'tx_bytes_per_sec', 0.0) or 0.0) > 0.0
        )
        ping_active = bool(
            (rtt_ms is not None)
            and (float(rtt_ms) <= float(self.health_cfg.ping_rtt_max_ms))
        )
        sample_ok = bool(ping_active or data_active)
        if probe_results:
            self._multi_target_active = True
            sample_ok = self._update_multi_target(probe_results, modem, now=now)
        else:
            self._multi_target_active = bool(self.health_cfg.multi_target_enabled)
        self._update_with_sample(
            sample_ok=sample_ok,
            rtt_ms=rtt_ms,
            now=now,
            controlled_settle=controlled_settle,
            radio_connected=bool(getattr(modem.cellular, 'radio_connected', False)),
        )
        return self.get_status()

    def _update_multi_target(self, probe_results: Dict[str, bool], modem: CellularIntelligenceGeneration, now: float) -> bool:
        ok_count = 0
        non_ping_present = False
        non_ping_ok = False
        for name, ok in probe_results.items():
            tg = self._targets.get(name)
            if tg is None:
                tg = TargetHealth(ok=bool(ok), last_change_ts=now)
                self._targets[name] = tg
            before = tg.ok
            if ok:
                tg.success_streak += 1
                tg.fail_streak = 0
            else:
                tg.fail_streak += 1
                tg.success_streak = 0
            tg.ok = bool(ok)
            if tg.ok != before:
                tg.last_change_ts = now
            if tg.ok:
                ok_count += 1
            if str(name).startswith(("dns:", "http:")):
                non_ping_present = True
                if tg.ok:
                    non_ping_ok = True
        min_ok = max(1, int(self.health_cfg.multi_min_ok_targets))
        if non_ping_present and not non_ping_ok:
            return False
        return bool(ok_count >= min_ok)

    def _update_with_sample(
        self,
        sample_ok: bool,
        rtt_ms: float | None,
        now: float,
        *,
        controlled_settle: Optional[Dict[str, Any]] = None,
        radio_connected: bool = False,
    ) -> None:
        st = self._st

        settle = dict(controlled_settle or {})
        settle_until_ts = float(settle.get("until_ts", 0.0) or 0.0)
        settle_active = bool(settle.get("active", False)) and (
            settle_until_ts <= 0.0 or settle_until_ts > now
        )
        controlled_sample = bool(
            (not sample_ok)
            and settle_active
            and bool(st.overall_ok)
        )
        st.controlled_settle_active = bool(settle_active)
        st.controlled_settle_reason = str(settle.get("reason", "") or "")
        st.controlled_settle_until_ts = settle_until_ts

        if sample_ok:
            st.ping_success_streak += 1
            st.ping_fail_streak = 0
            st.controlled_settle_miss_streak = 0
        elif controlled_sample:
            st.ping_success_streak = 0
            st.controlled_settle_miss_streak += 1
            st.controlled_settle_miss_count += 1
            st.last_controlled_settle_miss_ts = now
        else:
            st.ping_fail_streak += 1
            st.ping_success_streak = 0
            st.controlled_settle_miss_streak = 0

        st.ping_ok = sample_ok
        before = st.overall_ok
        before_stage = st.stage

        if controlled_sample:
            if st.controlled_settle_miss_streak == 1:
                self._events.append(
                    {
                        "kind": "health_controlled_settle_miss",
                        "reason": st.controlled_settle_reason,
                        "controlled_settle_until_ts": st.controlled_settle_until_ts,
                        "overall_ok": st.overall_ok,
                        "ping_ok": st.ping_ok,
                        "ping_fail_streak": st.ping_fail_streak,
                        "controlled_settle_miss_streak": st.controlled_settle_miss_streak,
                        "rtt_ms": float(rtt_ms) if rtt_ms is not None else None,
                        "ts": now,
                    }
                )
        elif st.overall_ok:
            if st.ping_fail_streak >= int(self.health_cfg.ping_fail_threshold):
                st.overall_ok = False
                st.hold_down_until_ts = now + float(self.health_cfg.hold_down_s)
                st.last_change_ts = now
                st.degraded_since_ts = now
        else:
            if now >= st.hold_down_until_ts and st.ping_success_streak >= int(self.health_cfg.ping_recover_threshold):
                st.overall_ok = True
                st.last_change_ts = now
                st.degraded_since_ts = 0.0
                st.hold_down_until_ts = 0.0

        stage_meta = self._stage_meta(now)
        st.stage = str(stage_meta["stage"])
        st.action_hint = str(stage_meta["action_hint"])

        if st.stage != before_stage:
            self._events.append(
                {
                    "kind": "health_stage_change",
                    "stage": st.stage,
                    "action_hint": st.action_hint,
                    "hold_down_active": bool(stage_meta["hold_down_active"]),
                    "recovery_candidate": bool(stage_meta["recovery_candidate"]),
                    "degraded_for_s": float(stage_meta["degraded_for_s"]),
                    "overall_ok": st.overall_ok,
                    "ping_ok": st.ping_ok,
                    "ping_fail_streak": st.ping_fail_streak,
                    "ping_success_streak": st.ping_success_streak,
                    "hold_down_until_ts": st.hold_down_until_ts,
                    "rtt_ms": float(rtt_ms) if rtt_ms is not None else None,
                    "ts": now,
                }
            )

        if st.overall_ok != before:
            self._events.append(
                {
                    "kind": "health_state_change",
                    "overall_ok": st.overall_ok,
                    "ping_ok": st.ping_ok,
                    "ping_fail_streak": st.ping_fail_streak,
                    "ping_success_streak": st.ping_success_streak,
                    "hold_down_until_ts": st.hold_down_until_ts,
                    "stage": st.stage,
                    "action_hint": st.action_hint,
                    "hold_down_active": bool(stage_meta["hold_down_active"]),
                    "recovery_candidate": bool(stage_meta["recovery_candidate"]),
                    "degraded_for_s": float(stage_meta["degraded_for_s"]),
                    "rtt_ms": float(rtt_ms) if rtt_ms is not None else None,
                    "ts": now,
                }
            )

    def _stage_meta(self, now: float) -> Dict[str, Any]:
        st = self._st
        degraded_for_s = 0.0
        if st.degraded_since_ts > 0.0:
            degraded_for_s = max(0.0, now - float(st.degraded_since_ts))
        hold_down_active = bool((not st.overall_ok) and now < float(st.hold_down_until_ts))

        if st.overall_ok:
            if st.ping_fail_streak > 0:
                return {
                    "stage": "warning",
                    "action_hint": "observe",
                    "hold_down_active": False,
                    "recovery_candidate": False,
                    "degraded_for_s": 0.0,
                }
            return {
                "stage": "healthy",
                "action_hint": "none",
                "hold_down_active": False,
                "recovery_candidate": False,
                "degraded_for_s": 0.0,
            }

        if hold_down_active:
            return {
                "stage": "degraded_hold",
                "action_hint": "observe_hold",
                "hold_down_active": True,
                "recovery_candidate": False,
                "degraded_for_s": degraded_for_s,
            }

        return {
            "stage": "degraded_persist",
            "action_hint": "escalation_candidate",
            "hold_down_active": False,
            "recovery_candidate": True,
            "degraded_for_s": degraded_for_s,
        }

    def get_status(self, now: float | None = None) -> Dict[str, Any]:
        now = float(now or time.time())
        st = self._st
        stage_meta = self._stage_meta(now)
        targets = {}
        for name, tg in self._targets.items():
            targets[name] = {
                "ok": bool(tg.ok),
                "fail_streak": int(tg.fail_streak),
                "success_streak": int(tg.success_streak),
                "last_change_ts": float(tg.last_change_ts),
            }
        return {
            "overall_ok": bool(st.overall_ok),
            "ping_ok": bool(st.ping_ok),
            "ping_fail_streak": int(st.ping_fail_streak),
            "ping_success_streak": int(st.ping_success_streak),
            "hold_down_until_ts": float(st.hold_down_until_ts),
            "last_change_ts": float(st.last_change_ts),
            "degraded_since_ts": float(st.degraded_since_ts),
            "stage": str(stage_meta["stage"]),
            "action_hint": str(stage_meta["action_hint"]),
            "hold_down_active": bool(stage_meta["hold_down_active"]),
            "recovery_candidate": bool(stage_meta["recovery_candidate"]),
            "degraded_for_s": float(stage_meta["degraded_for_s"]),
            "controlled_settle_active": bool(st.controlled_settle_active),
            "controlled_settle_reason": st.controlled_settle_reason,
            "controlled_settle_until_ts": float(st.controlled_settle_until_ts),
            "controlled_settle_miss_streak": int(st.controlled_settle_miss_streak),
            "controlled_settle_miss_count": int(st.controlled_settle_miss_count),
            "last_controlled_settle_miss_ts": float(st.last_controlled_settle_miss_ts),
            "multi_target_enabled": bool(self._multi_target_active or self.health_cfg.multi_target_enabled),
            "targets": targets,
        }

    def drain_runtime_events(self) -> List[Dict[str, Any]]:
        out = list(self._events)
        self._events.clear()
        return out


__all__ = ("HealthCheckEngine",)
