"""
arch4/brain/tick_safety.py
════════════════════════════════════════════════════════════════
Per-tick safety verdict — evaluated ONCE at the top of decide(),
consumed by all decision lanes and guard checks.

Replaces the old repeated per-lane exploration-hold evaluation pattern. The
decision lanes now consume one tick-safety verdict instead of independently
re-reading incident state, RF cliff cooldowns, post-fault windows, known outage
signals, etc.

Usage in decide():
    self._tick_safety = evaluate_tick_safety(
        modem=modem, computed=computed, engine=self, now=now,
    )

Usage in consumers:
    if self._tick_safety.exploration_blocked:
        return None   # universal hold — all lanes
    if self._tick_safety.band_probe_blocked:
        return None   # lane-specific gate

The verdict is frozen (immutable) after evaluation. Consumers read,
never write. This guarantees consistent decisions within a tick.
"""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from arch4.brain.tick_safety_hold_owner import (
    known_outage_hold_for_tick,
    throughput_hold_for_tick,
)



# ── RF quality tiers ─────────────────────────────────────────────────────

def classify_rf_tier(modem: Optional[CellularIntelligenceGeneration]) -> str:
    """Classify RF quality into a tier.

    Tiers (from best to worst):
        excellent — SINR ≥ 15 dB, RSRP > -80 dBm
        good      — SINR ≥ 8 dB, RSRP > -90 dBm
        marginal  — SINR ≥ 3 dB, RSRP > -100 dBm
        bad       — SINR < 3 dB or RSRP ≤ -100 dBm
        dead      — radio not connected or modem absent
    """
    if modem is None:
        return "dead"
    if not bool(getattr(modem.cellular, 'radio_connected', False)):
        return "dead"
    sinr = float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0)
    rsrp = float(getattr(modem.cellular, 'rsrp_dbm', -120.0) or -120.0)
    if sinr >= 15.0 and rsrp > -80.0:
        return "excellent"
    if sinr >= 8.0 and rsrp > -90.0:
        return "good"
    if sinr >= 3.0 and rsrp > -100.0:
        return "marginal"
    return "bad"


def classify_traffic_state(modem: Optional[CellularIntelligenceGeneration]) -> str:
    """Classify traffic state.

    Returns modem.traffic_state if available, otherwise derives from bytes/sec.
    """
    if modem is None:
        return "idle"
    return str(getattr(modem.traffic, 'traffic_state', "idle") or "idle")


@dataclass(frozen=True)
class TickSafetyVerdicts:
    """Immutable per-tick safety evaluation.

    Evaluated once at the top of decide(). All decision lanes read this
    instead of independently re-evaluating the same conditions.

    Boolean fields:
        exploration_blocked — universal hold: ALL exploration lanes blocked.
            True when incident demands immediate recovery, or health checks
            are in hold-down.

        rf_cliff_hold — RF cliff cooldown is active. Exploration should wait
            unless escape-hatch probe overrides.

        post_fault_hold — post-fault dampening window active. Same escape
            semantics as rf_cliff_hold.

        known_outage_hold — known outage window detected for this context.

        throughput_hold — current throughput is high enough that probing
            would add risk for no gain.

    String/tier fields:
        rf_tier — "excellent" | "good" | "marginal" | "bad" | "dead"
        traffic_state — "idle" | "download" | "active"

    Gate details (for escape-hatch and telemetry):
        incident_gate — gate name if incident is suppressing ("critical_incident",
            "incident_exploration_hold", or "")
        incident_class — incident class name if suppressing
        rf_cliff_remaining_s — seconds remaining on RF cliff cooldown
        post_fault_remaining_s — seconds remaining on post-fault hold
        post_fault_class — fault class if post-fault hold is active
    """
    # ── Universal holds ──────────────────────────────────────────
    exploration_blocked: bool = False
    incident_gate: str = ""
    incident_class: str = ""

    # ── Per-gate holds (can be overridden by escape hatch) ───────
    rf_cliff_hold: bool = False
    rf_cliff_remaining_s: float = 0.0

    post_fault_hold: bool = False
    post_fault_remaining_s: float = 0.0
    post_fault_class: str = ""

    known_outage_hold: bool = False

    throughput_hold: bool = False

    # ── Classifications ──────────────────────────────────────────
    rf_tier: str = "good"
    traffic_state: str = "idle"

    # ── Computed convenience booleans ─────────────────────────────
    @property
    def any_hold_active(self) -> bool:
        """True if any hold is blocking exploration."""
        return (
            self.exploration_blocked
            or self.rf_cliff_hold
            or self.post_fault_hold
            or self.known_outage_hold
        )

    @property
    def bypassable_hold_active(self) -> bool:
        """True if a hold is active but eligible for escape-hatch bypass."""
        return (self.rf_cliff_hold or self.post_fault_hold) and not self.exploration_blocked

    def is_lane_blocked(self, lane: str) -> bool:
        """Check if a specific lane is blocked.

        Universal holds block all lanes. Per-gate holds block all lanes
        equally (per Joe's direction: maximum common denominator).
        """
        return self.any_hold_active

    def blocking_gate(self) -> str:
        """Return the name of the active blocking gate, or empty string."""
        if self.exploration_blocked:
            return self.incident_gate or "exploration_blocked"
        if self.rf_cliff_hold:
            return "rf_cliff_cooldown"
        if self.post_fault_hold:
            return "post_fault_cooldown"
        if self.known_outage_hold:
            return "known_outage"
        return ""


def evaluate_tick_safety(
    *,
    modem: Optional[CellularIntelligenceGeneration],
    computed: Optional[DerivedSignalFrames],
    engine: Any,
    now: Optional[float] = None,
) -> TickSafetyVerdicts:
    """Evaluate all safety gates once for this tick.

    Called at the top of decide(). The result is stored on the engine
    and read by all decision lanes and guards.

    Args:
        modem: current modem observation (may be None)
        computed: current computed signals (may be None)
        engine: DecisionEngine instance (reads incident state, state context, etc.)
        now: current timestamp (default: time.time())
    """
    t = now if now is not None else time.time()

    rf_tier = classify_rf_tier(modem)
    traffic = classify_traffic_state(modem)

    # ── Incident hold ────────────────────────────────────────────
    from arch4.policy.incident_action_policy import (
        incident_class_name,
        incident_requires_immediate_recovery,
    )
    assessment = getattr(engine, "_incident_assessment", {}) or {}
    inc_class = incident_class_name(assessment)
    exploration_blocked = False
    incident_gate = ""

    if inc_class != "healthy" and bool(assessment.get("suppress_exploration", False)):
        exploration_blocked = True
        if incident_requires_immediate_recovery(assessment):
            incident_gate = "critical_incident"
        else:
            incident_gate = "incident_exploration_hold"

    # ── RF cliff hold ────────────────────────────────────────────
    rf_cliff_hold = False
    rf_cliff_remaining_s = 0.0

    posture = getattr(
        getattr(engine, "posture_authority", None),
        "current_posture",
        None,
    )
    rf_cliff_event_at = float(
        getattr(posture, "rf_cliff_event_at", 0.0) or 0.0
    )
    if rf_cliff_event_at > 0.0:
        from arch4.brain.decision_support import DecisionSupportMixin
        cooldown_s = DecisionSupportMixin._CELL_OPT_RF_CLIFF_COOLDOWN_S
        remaining = (rf_cliff_event_at + cooldown_s) - t
        if remaining > 0.0:
            rf_cliff_hold = True
            rf_cliff_remaining_s = remaining

    # ── Post-fault hold ──────────────────────────────────────────
    post_fault_hold = False
    post_fault_remaining_s = 0.0
    post_fault_class = ""

    pf_until = float(getattr(posture, "post_fault_until", 0.0) or 0.0)
    pf_class = str(getattr(posture, "post_fault_class", "") or "")
    if t < pf_until and pf_class in {"BAD_RF", "E_RF_COLLAPSE", "E_RADIO_DOWN"}:
        post_fault_hold = True
        post_fault_remaining_s = pf_until - t
        post_fault_class = pf_class

    # ── Known outage hold ────────────────────────────────────────
    known_outage_hold = known_outage_hold_for_tick(engine, modem)

    # ── Throughput hold ──────────────────────────────────────────
    throughput_hold = throughput_hold_for_tick(engine, modem, computed)

    return TickSafetyVerdicts(
        exploration_blocked=exploration_blocked,
        incident_gate=incident_gate,
        incident_class=inc_class,
        rf_cliff_hold=rf_cliff_hold,
        rf_cliff_remaining_s=rf_cliff_remaining_s,
        post_fault_hold=post_fault_hold,
        post_fault_remaining_s=post_fault_remaining_s,
        post_fault_class=post_fault_class,
        known_outage_hold=known_outage_hold,
        throughput_hold=throughput_hold,
        rf_tier=rf_tier,
        traffic_state=traffic,
    )
