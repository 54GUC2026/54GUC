"""
arch4/brain/exploration_pressure.py
════════════════════════════════════════════════════════════════════════
ExplorationPressure — composite band exploration urgency.

Philosophy: "Leave nothing on the table, but don't disturb the table."
────────────────────────────────────────────────────────────────────────
The brain is an aggressive autopilot. Its job is to find and hold the
best prime band + CA configuration at all times. However:

  1. Band probing IS a disruption — every band switch causes a brief
     re-attach that interrupts the session. It must have a reason.

  2. Once all candidates are tested and none are better, the brain is
     QUIET. Not because a timer ran out — because there is nothing left
     to test. Silence is the correct answer when all options are known.

  3. Traffic must be respected. An active session (two-way or download)
     adds probe resistance. But resistance accumulates, not blocks —
     if enough pressure builds, the brain probes anyway.

  4. The 90-second minimum cooloff is always respected. Urgency does
     not shorten this. It only determines whether the brain probes AGAIN
     after the cooloff, and what it targets.

Probe pressure model
────────────────────
  probe_pressure: float  — accumulates every tick when signals are bad
  probe_resistance: float — accumulates when traffic is active
  
  Probe fires when: probe_pressure > PROBE_THRESHOLD
  After probe:      probe_pressure reset, resistance spike for 90s

Signal weights (additive per tick, clamped 0.0-1.0 total)
──────────────────────────────────────────────────────────
  rf_spike             +0.40/tick  sudden SINR drop — act now
  rtt_alarm            +0.020/tick RTT above severe threshold
  rsrq_load_gap_large  +0.018/tick RSRQ gap > 6 dB (cell very loaded)
  cliff_ahead              +0.012/tick historical pattern predicts congestion
  cliff_ahead          +0.012/tick history: congestion incoming
  rsrq_load_gap_wide   +0.010/tick RSRQ gap > 3 dB (cell filling up)
  rtt_elevated         +0.008/tick RTT above warning threshold
  ca_lost_under_load   +0.012/tick CA dropped while traffic was running
  load_trend_rising    +0.005/tick history: cell load rising
  rsrq_degrading       +0.005/tick RF gradually worsening

  Natural decay:       -0.003/tick when no signals active (pressure fades)

Resistance per tick (traffic state)
────────────────────────────────────
  active:   +0.025/tick (two-way session — highest resistance)
  download: +0.012/tick (one-sided — medium resistance)
  idle:     +0.000/tick (no resistance — pressure builds freely)

Thresholds
──────────
  PROBE_THRESHOLD = 0.60   fire a probe when pressure exceeds this
  RESISTANCE_SPIKE = 3.0   after probe, resistance set to this (drains slowly)
  RESISTANCE_DRAIN = 0.035 resistance drains per tick during cooloff
  
  At 2-second tick interval, 90s cooloff = 45 ticks.
  RESISTANCE_SPIKE / RESISTANCE_DRAIN = 3.0 / 0.035 ≈ 86 ticks ≈ 172s
  Actual minimum is enforced separately by the 90s cooloff timer.

Compound bonuses (one-time, applied when multiple signals confirm together)
────────────────────────────────────────────────────────────────────────────
  rtt_elevated + rsrq_load_gap_wide   → +0.05 one-time bonus
  cliff_ahead + load_trend_rising     → +0.05 one-time bonus
  cliff_ahead + load_trend_rising   → +0.05 one-time bonus
════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, TYPE_CHECKING

from arch4.brain.congestion_signals import congestion_rtt_level
from arch4.brain.ca_contract import cart_ld_active, cart_ld_valid

if TYPE_CHECKING:
    from arch4.controller_frames import CellularIntelligenceGeneration

# ── Thresholds ────────────────────────────────────────────────────────
PROBE_THRESHOLD    = 0.60   # fire probe when pressure exceeds this
RESISTANCE_SPIKE   = 3.0    # resistance after a probe (drains to 0 slowly)
RESISTANCE_DRAIN   = 0.020  # resistance drains per tick (~150 ticks = ~300s at 2s/tick)
PRESSURE_MAX       = 1.5    # cap on pressure accumulation (prevents runaway)
PRESSURE_NATURAL_DECAY = 0.003  # decay per tick when no signals active
POST_FAULT_DAMPING = 0.20
POST_HARD_FAULT_DAMPING = 0.35


@dataclass
class ExplorationPressure:
    """Stateful composite exploration urgency tracker.

    Instantiated once per DecisionEngine. update() called every tick.
    should_probe() returns True when the brain should consider a probe.
    record_probe() called when a probe is executed.
    """

    # Current state
    pressure: float = 0.0
    resistance: float = 0.0

    # Last tick signal flags (for decision logic and logging)
    rf_spike: bool = False
    rtt_elevated: bool = False
    rtt_alarm: bool = False
    rsrq_degrading: bool = False
    rsrq_load_gap_wide: bool = False    # gap > 3 dB
    rsrq_load_gap_large: bool = False   # gap > 6 dB
    cliff_ahead: bool = False
    load_trend_rising: bool = False
    ca_lost_under_load: bool = False
    traffic_state: str = "idle"
    post_fault_recent: bool = False
    post_fault_class: str = ""

    # Driving reasons this tick (for action reason strings)
    active_reasons: List[str] = field(default_factory=list)

    # Timestamps
    _last_probe_ts: float = field(default=0.0, repr=False)
    _last_update_ts: float = field(default=0.0, repr=False)

    def update(
        self,
        modem: "CellularIntelligenceGeneration",
        computed: "DerivedSignalFrames",
        *,
        cliff_ahead: bool = False,
        load_trend: str = "unknown",
        incident_assessment: Optional[Any] = None,
        post_fault_recent: bool = False,
        post_fault_class: str = "",
        now: Optional[float] = None,
    ) -> None:
        """Update pressure and resistance from current tick signals.

        Call this once per tick before checking should_probe().
        """
        if now is None:
            now = time.time()
        self._last_update_ts = now

        # ── Read signals ───────────────────────────────────────────────
        rtt_pressure = congestion_rtt_level(
            computed,
            baseline_level=str(getattr(computed.latency, 'rtt_pressure_level', "normal") or "normal"),
        )
        # Use quiet-window RTT for pressure — self-traffic inflates RTT
        # from bufferbloat, not cell congestion. Changing bands won't fix it.
        self.rtt_alarm    = rtt_pressure == "alarm"
        self.rtt_elevated = self.rtt_alarm or rtt_pressure in {"busy", "elevated"}
        self.rsrq_degrading      = bool(getattr(computed.quality, 'rsrq_degrading', False))
        rsrq_gap                 = float(getattr(computed.quality, 'rsrq_gap', 0.0) or 0.0)
        rsrq_gap_valid           = bool(getattr(computed.quality, 'rsrq_gap_valid', False))
        self.rsrq_load_gap_wide  = rsrq_gap_valid and rsrq_gap > 3.0
        self.rsrq_load_gap_large = rsrq_gap_valid and rsrq_gap > 6.0
        self.cliff_ahead         = cliff_ahead
        self.load_trend_rising   = load_trend == "rising"
        self.traffic_state       = str(getattr(modem.traffic, 'traffic_state', "idle") or "idle")
        self.post_fault_recent   = bool(post_fault_recent)
        self.post_fault_class    = str(post_fault_class or "")

        # RF spike from incident assessment
        inc_class = str((incident_assessment or {}).get("incident_class", "") or "")
        self.rf_spike = inc_class in {"rf_event_spike", "rf_collapse"}

        # CA lost under load
        self.ca_lost_under_load = (
            cart_ld_valid(computed)
            and not cart_ld_active(computed)
            and bool(getattr(modem.traffic, 'has_traffic', False))
            and self.traffic_state != "idle"
        )

        # ── Accumulate pressure ────────────────────────────────────────
        delta = 0.0
        reasons = []

        if self.rf_spike:
            delta += 0.40
            reasons.append("RF spike")
        if self.rtt_alarm:
            delta += 0.020
            reasons.append("RTT alarm")
        elif self.rtt_elevated:
            delta += 0.008
            reasons.append("RTT elevated")
        if self.rsrq_load_gap_large:
            delta += 0.018
            reasons.append("RSRQ gap large")
        elif self.rsrq_load_gap_wide:
            delta += 0.010
            reasons.append("RSRQ gap wide")
        if self.cliff_ahead:
            delta += 0.012
            reasons.append("cliff ahead")
        if self.ca_lost_under_load:
            delta += 0.012
            reasons.append("CA lost under load")
        if self.load_trend_rising:
            delta += 0.005
            reasons.append("load rising")
        if self.rsrq_degrading:
            delta += 0.005
            reasons.append("RSRQ degrading")

        # When RF is bad and the user is idle, the brain should become more
        # proactive instead of being trapped in passive observation.
        if self.traffic_state == "idle" and (
            self.rf_spike or self.post_fault_class in {"BAD_RF", "E_RF_COLLAPSE", "E_RADIO_DOWN"}
        ):
            delta += 0.06
            reasons.append("idle bad-RF proactive bias")

        # Compound bonuses — multiple signals confirming same root cause
        if self.rtt_elevated and self.rsrq_load_gap_wide:
            delta += 0.05  # congestion from two independent signals
        if self.cliff_ahead and self.load_trend_rising:
            delta += 0.05  # history agrees from two directions

        # Natural decay when nothing is wrong
        if delta == 0.0:
            delta = -PRESSURE_NATURAL_DECAY

        self.pressure = max(0.0, min(PRESSURE_MAX, self.pressure + delta))
        self.active_reasons = reasons

        # ── Accumulate resistance from traffic ─────────────────────────
        if self.traffic_state in {"active", "upload"}:
            self.resistance += 0.025
        elif self.traffic_state == "download":
            self.resistance += 0.012
        # idle: no resistance

        # Drain resistance every tick (post-probe cooloff decays naturally)
        self.resistance = max(0.0, self.resistance - RESISTANCE_DRAIN)

    def should_probe(self) -> bool:
        """True when accumulated pressure exceeds resistance threshold.

        This is a SUGGESTION to the brain. The brain still decides:
          - Is there a meaningful candidate to probe?
          - Is the mandatory 90s cooloff elapsed?
          - Does LTM/STM support the probe?
        """
        return self.net_pressure >= PROBE_THRESHOLD

    def record_probe(self, now: Optional[float] = None) -> None:
        """Call when a probe action is executed.

        Resets pressure, applies resistance spike to enforce cooloff.
        The 90s minimum is also enforced separately by aggressiveness.
        """
        if now is None:
            now = time.time()
        self._last_probe_ts = now
        self.pressure = 0.0
        self.resistance = RESISTANCE_SPIKE  # drains over ~86 ticks

    def reason_str(self) -> str:
        """Short summary for action reason strings."""
        return " | ".join(self.active_reasons) if self.active_reasons else "routine"

    def pilot_hint(self) -> str:
        """One plain-English sentence explaining why the brain is exploring."""
        if not self.active_reasons:
            return "Routine monitoring check."
        if self.rf_spike:
            return "Sudden signal drop — brain searching for a stable band."
        if self.rtt_alarm and self.rsrq_load_gap_large:
            return "Cell is heavily congested. Brain urgently looking for a better configuration."
        if self.cliff_ahead and self.load_trend_rising:
            return "Network load rising and history predicts it will get worse. Brain is preparing."
        if self.cliff_ahead:
            return "Historical data predicts congestion soon. Brain is pre-switching."
        if self.rtt_alarm:
            return "Network latency is severe. Brain searching for alternatives."
        if self.rsrq_load_gap_large:
            return "Cell is becoming congested (RSRQ gap widening). Brain probing alternatives."
        if self.ca_lost_under_load:
            return "Multi-band (CA) dropped under load. Brain trying to restore it."
        if self.rsrq_degrading:
            return "Signal quality gradually degrading. Brain watching and probing."
        if self.rtt_elevated:
            return "Network latency rising. Brain checking alternatives."
        return "Routine optimisation check."

    @property
    def net_pressure(self) -> float:
        damping = 0.0
        if self.post_fault_recent:
            if self.post_fault_class in {"BAD_RF", "E_RF_COLLAPSE", "E_RADIO_DOWN"}:
                damping = POST_HARD_FAULT_DAMPING
            else:
                damping = POST_FAULT_DAMPING
        if self.traffic_state == "idle" and (
            self.rf_spike or self.post_fault_class in {"BAD_RF", "E_RF_COLLAPSE", "E_RADIO_DOWN"}
        ):
            damping *= 0.4
        return max(0.0, self.pressure - self.resistance - damping)
