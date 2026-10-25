"""
arch4/brain/traffic_gate.py
════════════════════════════════════════════════════════════════════════════════
Traffic Gate — centralizes all traffic state checks.

Evaluated ONCE per tick at the top of decide(). The result persists on `self`
between ticks, so consumers always have either:
  - Current tick's evaluation, OR
  - Previous tick's evaluation (if current tick hasn't run decide() yet)

Consumers must use this typed verdict. Missing verdicts are reported as
contract failures and hold gate-dependent decisions safely.

Usage in decide():
    self._traffic = evaluate_traffic(
        modem=modem,
        cfg=TrafficConfig(...),  # thresholds from config
    )

The verdict is frozen (immutable) after evaluation. Consumers read, never write.
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from arch4.controller_frames import CellularIntelligenceGeneration


# ── Configuration for traffic thresholds ──────────────────────────────────────

@dataclass(frozen=True)
class TrafficConfig:
    """Configurable thresholds for traffic gates.

    Some gates use fixed thresholds, others are configurable per deployment.
    """
    # Priority rotation floor (minimum throughput to consider rotation)
    priority_rotate_floor_mbps: float = 0.5

    # Soft congestion minimum (must have this much traffic to detect congestion)
    soft_congestion_min_dl_mbps: float = 1.0

    # Throughput hold threshold (high enough throughput that probing is risky)
    throughput_hold_mbps: float = 5.0

    # Idle session timeout (seconds of uptime before considering idle actions)
    idle_session_min_uptime_s: float = 60.0


# ── Traffic Verdicts ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TrafficVerdicts:
    """Immutable per-tick traffic evaluation.

    Evaluated once at the top of decide(). All decision lanes read this
    instead of independently checking modem.has_traffic / throughput_dl_mbps.

    Raw values (for diagnostics and telemetry):
        throughput_dl_mbps — current download throughput
        throughput_ul_mbps — current upload throughput
        traffic_state — "idle" | "download" | "active" | "upload" | "invalid"
        session_uptime_s — seconds since session started

    Gate verdicts (named booleans for each consumer):
        has_traffic — modem reports active traffic
        has_load — has_traffic AND traffic_state != "idle"
        above_priority_floor — throughput >= 0.5 Mbps (priority rotation)
        above_soft_cong_min — throughput >= soft_congestion_min threshold
        above_throughput_hold — throughput high enough to hold exploration
        idle_session_mature — no traffic AND session uptime > threshold
    """
    # ── Raw values ────────────────────────────────────────────────────
    throughput_dl_mbps: float = 0.0
    throughput_ul_mbps: float = 0.0
    traffic_state: str = "idle"
    traffic_valid: bool = True
    session_uptime_s: float = 0.0

    # ── Gate verdicts ─────────────────────────────────────────────────
    has_traffic: bool = False
    has_load: bool = False
    above_priority_floor: bool = False
    above_soft_cong_min: bool = False
    above_throughput_hold: bool = False
    idle_session_mature: bool = False

    # ── Convenience properties ────────────────────────────────────────

    @property
    def is_idle(self) -> bool:
        """True if traffic state is idle."""
        return self.traffic_state == "idle"

    @property
    def is_downloading(self) -> bool:
        """True if actively downloading."""
        return self.traffic_state == "download"

    @property
    def is_active(self) -> bool:
        """True if traffic state indicates active use."""
        return self.traffic_state in {"download", "active", "upload"}

    @property
    def can_probe_safely(self) -> bool:
        """True if traffic is low enough that probing won't disrupt user."""
        return not self.above_throughput_hold


def evaluate_traffic(
    *,
    modem: Optional[CellularIntelligenceGeneration],
    cfg: Optional[TrafficConfig] = None,
) -> TrafficVerdicts:
    """Evaluate all traffic gates once for this tick.

    Called at the top of decide(). The result is stored on the engine
    and read by all decision lanes.

    Args:
        modem: current modem observation (may be None)
        cfg: traffic thresholds (uses defaults if None)
    """
    if cfg is None:
        cfg = TrafficConfig()

    # Extract raw values
    if modem is None:
        return TrafficVerdicts()  # All defaults (no traffic)
    if not isinstance(modem, CellularIntelligenceGeneration):
        raise TypeError("traffic_gate_requires_cellular_intelligence_generation")

    has_traffic = bool(getattr(modem.traffic, 'has_traffic', False))
    traffic_state = str(getattr(modem.traffic, 'traffic_state', "idle") or "idle")
    traffic_valid = bool(getattr(modem.traffic, 'traffic_valid', True))
    throughput_dl_mbps = float(getattr(modem.traffic, 'dl_mbps', 0.0) or 0.0)
    throughput_ul_mbps = float(getattr(modem.traffic, 'ul_mbps', 0.0) or 0.0)
    session_uptime_s = float(getattr(modem.action, 'session_uptime_s', 0.0) or 0.0)

    # ── Evaluate gates ────────────────────────────────────────────────

    # Has load: traffic present AND not idle
    has_load = has_traffic and traffic_state != "idle"

    # Priority rotation floor
    above_priority_floor = throughput_dl_mbps >= cfg.priority_rotate_floor_mbps

    # Soft congestion minimum
    above_soft_cong_min = (
        has_traffic
        and throughput_dl_mbps >= cfg.soft_congestion_min_dl_mbps
    )

    # Throughput hold (too much traffic to risk probing)
    above_throughput_hold = throughput_dl_mbps >= cfg.throughput_hold_mbps

    # Idle session mature (no traffic for long enough to try idle actions)
    idle_session_mature = (
        traffic_valid
        and not has_traffic
        and session_uptime_s > cfg.idle_session_min_uptime_s
    )

    return TrafficVerdicts(
        throughput_dl_mbps=throughput_dl_mbps,
        throughput_ul_mbps=throughput_ul_mbps,
        traffic_state=traffic_state,
        traffic_valid=traffic_valid,
        session_uptime_s=session_uptime_s,
        has_traffic=has_traffic,
        has_load=has_load,
        above_priority_floor=above_priority_floor,
        above_soft_cong_min=above_soft_cong_min,
        above_throughput_hold=above_throughput_hold,
        idle_session_mature=idle_session_mature,
    )


def build_traffic_config_from_engine(engine: Any) -> TrafficConfig:
    """Build TrafficConfig from engine's config views.

    Helper to extract threshold values from the various config views
    that the engine has access to.
    """
    # Pipeline policy config
    pipeline_cfg = getattr(engine, "_pipeline_policy_config", lambda: None)()
    soft = getattr(engine, "_thresholds", {}).get("soft", {})

    # State policy config
    state_cfg = getattr(engine, "_state_policy_config", lambda: None)()

    return TrafficConfig(
        priority_rotate_floor_mbps=0.5,  # Fixed threshold
        soft_congestion_min_dl_mbps=float(
            soft.get("soft_congestion_min_dl_mbps",
                     getattr(pipeline_cfg, "soft_congestion_min_dl_mbps", 1.0) if pipeline_cfg else 1.0)
        ),
        throughput_hold_mbps=float(
            getattr(state_cfg, "throughput_hold_mbps", 5.0) if state_cfg else 5.0
        ),
        idle_session_min_uptime_s=60.0,  # Fixed threshold
    )
