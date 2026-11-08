"""Core-owned downstream LAN diagnostic monitor."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Dict, List, Optional

from arch4.interfaces import Arch4Config
from arch4.modem.utils import RttProbeResult, measure_host_ping_rtt
from arch4.operational_service_contracts import (
    DOWNSTREAM_HEALTH_INTERVAL_MAX_S,
    DOWNSTREAM_HEALTH_INTERVAL_MIN_S,
)

@dataclass
class DownstreamHealthStatus:
    enabled: bool = False
    target: str = ""
    label: str = "Downstream router"
    stage: str = "disabled"
    ok: Optional[bool] = None
    fail_streak: int = 0
    success_streak: int = 0
    last_rtt_ms: Optional[float] = None
    last_probe_ts: float = 0.0
    last_change_ts: float = 0.0
    message: str = "Downstream health monitor is off."


class DownstreamHealthMonitor:
    """Developer LAN and router monitor. It uses host ping and publishes status only. Keep
    it separate from WAN health: LAN status must never become evidence for a modem band
    action.
    """

    def __init__(self, cfg: Arch4Config):
        self.cfg = cfg
        self._ping_count = max(
            1,
            min(10, int(getattr(cfg, "downstream_health_ping_count", 1) or 1)),
        )
        self._interval_s = max(
            DOWNSTREAM_HEALTH_INTERVAL_MIN_S,
            min(
                DOWNSTREAM_HEALTH_INTERVAL_MAX_S,
                float(getattr(cfg, "downstream_health_interval_s", 30.0) or 30.0),
            ),
        )
        self._st = DownstreamHealthStatus(
            enabled=bool(getattr(cfg, "downstream_health_enabled", False)),
            target=str(getattr(cfg, "downstream_health_target", "") or "").strip(),
            label=str(getattr(cfg, "downstream_health_label", "Downstream router") or "Downstream router"),
        )
        self._events: List[Dict[str, Any]] = []
        self._set_stage(self._initial_stage(), now=time.time(), emit=False)

    def refresh_thresholds(self) -> None:
        """ACK config binding without overwriting operator runtime state."""

    def configure(
        self,
        *,
        enabled: Optional[bool] = None,
        target: Optional[str] = None,
        label: Optional[str] = None,
        ping_count: Optional[int] = None,
        interval_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        st = self._st
        before = self.get_status()
        changed = False
        if enabled is not None and bool(enabled) != st.enabled:
            st.enabled = bool(enabled)
            st.last_probe_ts = 0.0
            changed = True
        if target is not None:
            target_text = str(target or "").strip()
            if target_text != st.target:
                st.target = target_text
                st.fail_streak = 0
                st.success_streak = 0
                st.last_rtt_ms = None
                st.last_probe_ts = 0.0
                changed = True
        if label is not None:
            label_text = str(label or "").strip() or "Downstream router"
            if label_text != st.label:
                st.label = label_text
                changed = True
        if ping_count is not None:
            count_value = max(1, min(10, int(ping_count or 1)))
            if self._ping_count != count_value:
                self._ping_count = count_value
                st.last_probe_ts = 0.0
                changed = True
        if interval_s is not None:
            interval_value = max(
                DOWNSTREAM_HEALTH_INTERVAL_MIN_S,
                min(
                    DOWNSTREAM_HEALTH_INTERVAL_MAX_S,
                    float(interval_s or DOWNSTREAM_HEALTH_INTERVAL_MIN_S),
                ),
            )
            if self._interval_s != interval_value:
                self._interval_s = interval_value
                st.last_probe_ts = 0.0
                changed = True
        self._set_stage(self._initial_stage(), now=time.time(), emit=changed)
        status = self.get_status()
        if changed:
            self._events.append(
                {
                    "kind": "downstream_health_config",
                    "enabled": bool(st.enabled),
                    "target": st.target,
                    "label": st.label,
                    "ping_count": int(self._ping_count),
                    "interval_s": float(self._interval_s),
                    "stage": st.stage,
                    "previous_stage": before.get("stage"),
                    "message": st.message,
                    "ts": time.time(),
                }
            )
        return status

    def update(self, now: float | None = None) -> Dict[str, Any]:
        now = float(now or time.time())
        st = self._st
        if not st.enabled or not st.target:
            self._set_stage(self._initial_stage(), now=now)
            return self.get_status(now=now)

        interval_s = float(self._interval_s)
        if st.last_probe_ts and (now - float(st.last_probe_ts)) < interval_s:
            return self.get_status(now=now)

        timeout_s = max(0.3, float(getattr(self.cfg, "downstream_health_timeout_s", 1.0) or 1.0))
        rtt_max_ms = max(1.0, float(getattr(self.cfg, "downstream_health_rtt_max_ms", 1000.0) or 1000.0))
        ping_count = int(self._ping_count)
        result = measure_host_ping_rtt(target=st.target, count=ping_count, timeout_s=timeout_s)
        if not isinstance(result, RttProbeResult):
            raise TypeError("downstream_typed_probe_result_required")
        rtt_ms = result.value_ms
        st.last_probe_ts = now
        st.last_rtt_ms = float(rtt_ms) if rtt_ms is not None else None
        sample_ok = bool(rtt_ms is not None and float(rtt_ms) <= rtt_max_ms)
        if sample_ok:
            st.success_streak += 1
            st.fail_streak = 0
        else:
            st.fail_streak += 1
            st.success_streak = 0

        fail_threshold = max(1, int(getattr(self.cfg, "downstream_health_fail_threshold", 3) or 3))
        recover_threshold = max(1, int(getattr(self.cfg, "downstream_health_recover_threshold", 1) or 1))
        if sample_ok and st.success_streak >= recover_threshold:
            self._set_stage("ok", now=now)
        elif sample_ok:
            self._set_stage("recovering", now=now)
        elif st.fail_streak >= fail_threshold:
            self._set_stage("down", now=now)
        else:
            self._set_stage("warning", now=now)
        return self.get_status(now=now)

    def get_status(self, now: float | None = None) -> Dict[str, Any]:
        st = self._st
        return {
            "enabled": bool(st.enabled),
            "target": st.target,
            "label": st.label,
            "ping_count": int(self._ping_count),
            "interval_s": float(self._interval_s),
            "stage": st.stage,
            "ok": st.ok,
            "fail_streak": int(st.fail_streak),
            "success_streak": int(st.success_streak),
            "last_rtt_ms": st.last_rtt_ms,
            "last_probe_ts": float(st.last_probe_ts),
            "last_change_ts": float(st.last_change_ts),
            "message": st.message,
            "classification": "downstream_lan_health",
            "action_hint": "observe_only",
            "drives_radio_actions": False,
            "radio_evidence": False,
            "source": "host_ping",
            "ts": float(now or time.time()),
        }

    def drain_runtime_events(self) -> List[Dict[str, Any]]:
        out = list(self._events)
        self._events.clear()
        return out

    def _initial_stage(self) -> str:
        if not self._st.enabled:
            return "disabled"
        if not self._st.target:
            return "no_target"
        return "pending"

    def _set_stage(self, stage: str, *, now: float, emit: bool = True) -> None:
        stage = str(stage or "unknown")
        st = self._st
        before = st.stage
        st.stage = stage
        st.ok = True if stage == "ok" else False if stage in {"warning", "down", "no_target"} else None
        st.message = self._message_for_stage(stage)
        if before != stage:
            st.last_change_ts = now
            if emit:
                self._events.append(
                    {
                        "kind": "downstream_health_stage_change",
                        "stage": stage,
                        "previous_stage": before,
                        "enabled": bool(st.enabled),
                        "target": st.target,
                        "message": st.message,
                        "classification": "downstream_lan_health",
                        "drives_radio_actions": False,
                        "ts": now,
                    }
                )

    def _message_for_stage(self, stage: str) -> str:
        st = self._st
        label = st.label or "Downstream router"
        if stage == "disabled":
            return "Downstream health monitor is off."
        if stage == "no_target":
            return "Downstream health monitor is on, but target is empty."
        if stage == "pending":
            return f"{label} monitor waiting for first check."
        if stage == "ok":
            rtt = f" ({st.last_rtt_ms:.0f} ms)" if st.last_rtt_ms is not None else ""
            return f"{label} reachable{rtt}."
        if stage == "recovering":
            return f"{label} responded; waiting for recovery confirmation."
        if stage == "warning":
            return f"{label} missed {st.fail_streak} check(s); observing only."
        if stage == "down":
            return f"{label} unreachable; this is LAN/downstream only, no PBAND/CA action."
        return f"{label} status unknown."


__all__ = ("DownstreamHealthMonitor",)
