"""
arch4/brain/rf_quality_gate.py
════════════════════════════════════════════════════════════════════════════════
RF Quality Gate — centralizes all RF quality checks.

Evaluated ONCE per tick at the top of decide(). The result persists on `self`
between ticks, so consumers always have either:
  - Current tick's evaluation, OR
  - Previous tick's evaluation (if current tick hasn't run decide() yet)

Consumers must use this typed verdict. Missing verdicts are reported as
contract failures and hold gate-dependent decisions safely.

Usage in decide():
    self._rf_quality = evaluate_rf_quality(
        modem=modem,
        cfg=RFQualityConfig(...),  # thresholds from config
    )

The verdict is frozen (immutable) after evaluation. Consumers read, never write.
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from arch4.controller_frames import CellularIntelligenceGeneration


# ── Configuration for RF quality thresholds ───────────────────────────────────

@dataclass(frozen=True)
class RFQualityConfig:
    """Configurable thresholds for RF quality gates.
    
    Some gates use fixed thresholds (industry standards), others are
    configurable per deployment. This config holds the configurable ones.
    """
    # Congestion RF gate
    congestion_rf_ok_sinr_db: float = 6.0
    congestion_rf_ok_rsrp_dbm: float = -90.0
    
    # Low RF gate (for congestion dampening)
    low_rf_sinr_gate_db: float = 4.0
    
    # Feasibility gate (minimum for any action)
    sinr_feasibility_gate_db: float = 10.0
    
    # CA restore gate
    ca_restore_sinr_db: float = 8.0
    
    # Stable link for cell optimization
    stable_sinr_db: float = 8.0
    
    # NR rollback threshold
    nr_rollback_sinr_db: float = 3.0
    
    # Night training thresholds
    night_probe_bad_sinr_db: float = 5.0
    night_probe_bad_rsrp_dbm: float = -100.0
    
    # Escape probe RF cliff threshold
    escape_probe_rf_cliff_sinr_min_db: float = 3.0


# ── Fixed thresholds (not configurable) ───────────────────────────────────────

# Priority rotation requires excellent serving RF. Scheduler signals
# (CQI/MCS/QAM) are GUI-only and must not drive decisions.
_PRIORITY_ROTATE_SINR_MIN_DB = 15.0

# Preemptive switch floor
_PREEMPTIVE_SWITCH_SINR_FLOOR_DB = 10.0
_PREEMPTIVE_SWITCH_RSRP_FLOOR_DBM = -100.0

# Neighbor RSRP validity bounds
_NEIGHBOR_RSRP_MIN_DBM = -140.0
_NEIGHBOR_RSRP_MAX_DBM = -44.0
_NEIGHBOR_RSRP_QUALITY_FLOOR_DBM = -110.0

# Tier classification boundaries
_TIER_EXCELLENT_SINR_DB = 15.0
_TIER_EXCELLENT_RSRP_DBM = -80.0
_TIER_GOOD_SINR_DB = 8.0
_TIER_GOOD_RSRP_DBM = -90.0
_TIER_MARGINAL_SINR_DB = 3.0
_TIER_MARGINAL_RSRP_DBM = -100.0


# ── RF Quality Verdicts ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class RFQualityVerdicts:
    """Immutable per-tick RF quality evaluation.
    
    Evaluated once at the top of decide(). All decision lanes read this
    instead of independently checking modem.sinr_db / modem.rsrp_dbm.
    
    Raw values (for diagnostics):
        sinr_db, rsrp_dbm — current serving cell RF
        nr_sinr_db — NR (5G) SINR if NSA active
        radio_connected — is radio attached
        cqi — retained as a compatibility diagnostic, not used by RF gates
    
    Tier classification:
        tier — "excellent" | "good" | "marginal" | "bad" | "dead"
    
    Gate verdicts (named booleans for each consumer):
        ca_restore_ok — RF good enough to attempt CA restoration
        priority_rotate_ok — RF excellent, can try priority rotation
        preemptive_switch_ok — RF above floor for preemptive cell switch
        congestion_rf_ok — RF good enough that congestion isn't RF-induced
        feasibility_ok — RF above minimum for any proactive action
        low_rf_active — RF below low threshold (dampens congestion signals)
        stable_for_cell_opt — RF stable enough for cell optimization
        nr_rollback_needed — NR SINR too weak, should rollback to LTE
        night_probe_trigger — RF bad enough to trigger night probe
        escape_probe_rf_ok — RF good enough for escape probe bypass
        neighbor_rsrp_valid — helper for neighbor RSRP validity check
    """
    # ── Raw values ────────────────────────────────────────────────────
    sinr_db: float = 0.0
    rsrp_dbm: float = -120.0
    nr_sinr_db: float = 0.0
    radio_connected: bool = False
    cqi: int = 0
    
    # ── Tier classification ───────────────────────────────────────────
    tier: str = "dead"
    
    # ── Gate verdicts ─────────────────────────────────────────────────
    ca_restore_ok: bool = False
    priority_rotate_ok: bool = False
    preemptive_switch_ok: bool = False
    congestion_rf_ok: bool = False
    feasibility_ok: bool = False
    low_rf_active: bool = True
    stable_for_cell_opt: bool = False
    nr_rollback_needed: bool = False
    night_probe_trigger: bool = False
    escape_probe_rf_ok: bool = False
    
    # ── Convenience methods ───────────────────────────────────────────
    
    def is_neighbor_rsrp_valid(self, rsrp_dbm: float) -> bool:
        """Check if a neighbor's RSRP is within valid measurement range."""
        return _NEIGHBOR_RSRP_MIN_DBM <= rsrp_dbm <= _NEIGHBOR_RSRP_MAX_DBM
    
    def is_neighbor_rsrp_quality(self, rsrp_dbm: float) -> bool:
        """Check if a neighbor's RSRP indicates decent quality."""
        return rsrp_dbm > _NEIGHBOR_RSRP_QUALITY_FLOOR_DBM
    
    @property
    def is_usable(self) -> bool:
        """True if RF is at least marginal (can attempt operations)."""
        return self.tier in {"excellent", "good", "marginal"}
    
    @property
    def is_good_or_better(self) -> bool:
        """True if RF is good or excellent."""
        return self.tier in {"excellent", "good"}
    
    @property
    def is_excellent(self) -> bool:
        """True if RF is excellent."""
        return self.tier == "excellent"


def _classify_rf_tier(sinr_db: float, rsrp_dbm: float, radio_connected: bool) -> str:
    """Classify RF quality into a tier.
    
    Tiers (from best to worst):
        excellent — SINR ≥ 15 dB, RSRP > -80 dBm
        good      — SINR ≥ 8 dB, RSRP > -90 dBm
        marginal  — SINR ≥ 3 dB, RSRP > -100 dBm
        bad       — SINR < 3 dB or RSRP ≤ -100 dBm
        dead      — radio not connected
    """
    if not radio_connected:
        return "dead"
    if sinr_db >= _TIER_EXCELLENT_SINR_DB and rsrp_dbm > _TIER_EXCELLENT_RSRP_DBM:
        return "excellent"
    if sinr_db >= _TIER_GOOD_SINR_DB and rsrp_dbm > _TIER_GOOD_RSRP_DBM:
        return "good"
    if sinr_db >= _TIER_MARGINAL_SINR_DB and rsrp_dbm > _TIER_MARGINAL_RSRP_DBM:
        return "marginal"
    return "bad"


def evaluate_rf_quality(
    *,
    modem: Optional[CellularIntelligenceGeneration],
    cfg: Optional[RFQualityConfig] = None,
) -> RFQualityVerdicts:
    """Evaluate all RF quality gates once for this tick.
    
    Called at the top of decide(). The result is stored on the engine
    and read by all decision lanes.
    
    Args:
        modem: current modem observation (may be None)
        cfg: RF quality thresholds (uses defaults if None)
    """
    if cfg is None:
        cfg = RFQualityConfig()
    
    # Extract raw values
    if modem is None:
        return RFQualityVerdicts()  # All defaults (dead, no gates pass)
    if not isinstance(modem, CellularIntelligenceGeneration):
        raise TypeError(
            "rf_quality_gate_requires_cellular_intelligence_generation"
        )
    
    radio_connected = bool(getattr(modem.cellular, 'radio_connected', False))
    if not radio_connected:
        return RFQualityVerdicts(radio_connected=False, tier="dead")
    
    sinr_db = float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0)
    rsrp_dbm = float(getattr(modem.cellular, 'rsrp_dbm', -120.0) or -120.0)
    nr_sinr_db = float(getattr(modem.cellular, 'nr_sinr_db', 0.0) or 0.0)
    cqi = 0
    
    # Classify tier
    tier = _classify_rf_tier(sinr_db, rsrp_dbm, radio_connected)
    
    # ── Evaluate gates ────────────────────────────────────────────────
    
    # CA restore: SINR above threshold
    ca_restore_ok = sinr_db >= cfg.ca_restore_sinr_db
    
    # Priority rotate: excellent RF only. Scheduler signals are display-only.
    priority_rotate_ok = (
        sinr_db >= _PRIORITY_ROTATE_SINR_MIN_DB
        and rsrp_dbm >= _TIER_GOOD_RSRP_DBM
    )
    
    # Preemptive switch: above floor thresholds
    preemptive_switch_ok = (
        sinr_db >= _PREEMPTIVE_SWITCH_SINR_FLOOR_DB
        and rsrp_dbm >= _PREEMPTIVE_SWITCH_RSRP_FLOOR_DBM
    )
    
    # Congestion RF: RF good enough that congestion isn't RF-induced
    congestion_rf_ok = (
        sinr_db >= cfg.congestion_rf_ok_sinr_db
        and rsrp_dbm >= cfg.congestion_rf_ok_rsrp_dbm
    )
    
    # Feasibility: minimum for any proactive action
    feasibility_ok = sinr_db >= cfg.sinr_feasibility_gate_db
    
    # Low RF: below threshold (dampens congestion detection)
    low_rf_active = sinr_db <= cfg.low_rf_sinr_gate_db
    
    # Stable for cell opt
    stable_for_cell_opt = sinr_db >= cfg.stable_sinr_db
    
    # NR rollback: NR SINR too weak
    nr_rollback_needed = nr_sinr_db < cfg.nr_rollback_sinr_db
    
    # Night probe trigger: RF bad enough
    night_probe_trigger = (
        sinr_db <= cfg.night_probe_bad_sinr_db
        or rsrp_dbm <= cfg.night_probe_bad_rsrp_dbm
    )
    
    # Escape probe RF: good enough to attempt escape
    escape_probe_rf_ok = sinr_db > cfg.escape_probe_rf_cliff_sinr_min_db
    
    return RFQualityVerdicts(
        sinr_db=sinr_db,
        rsrp_dbm=rsrp_dbm,
        nr_sinr_db=nr_sinr_db,
        radio_connected=radio_connected,
        cqi=cqi,
        tier=tier,
        ca_restore_ok=ca_restore_ok,
        priority_rotate_ok=priority_rotate_ok,
        preemptive_switch_ok=preemptive_switch_ok,
        congestion_rf_ok=congestion_rf_ok,
        feasibility_ok=feasibility_ok,
        low_rf_active=low_rf_active,
        stable_for_cell_opt=stable_for_cell_opt,
        nr_rollback_needed=nr_rollback_needed,
        night_probe_trigger=night_probe_trigger,
        escape_probe_rf_ok=escape_probe_rf_ok,
    )


def build_rf_quality_config_from_engine(engine: Any) -> RFQualityConfig:
    """Build RFQualityConfig from engine's config views.
    
    Helper to extract threshold values from the various config views
    that the engine has access to.
    """
    # Pipeline policy config
    pipeline_cfg = getattr(engine, "_pipeline_policy_config", lambda: None)()
    soft = getattr(engine, "_thresholds", {}).get("soft", {})
    hard = getattr(engine, "_thresholds", {}).get("hard", {})
    
    # State policy config
    state_cfg = getattr(engine, "_state_policy_config", lambda: None)()
    
    # Escape probe config
    escape_cfg = getattr(engine, "_escape_probe_cfg", lambda: None)()
    
    # Night training config
    night_cfg = getattr(engine, "night_training_cfg", None)
    
    return RFQualityConfig(
        congestion_rf_ok_sinr_db=float(
            soft.get("congestion_rf_ok_sinr_db", 
                     getattr(pipeline_cfg, "congestion_rf_ok_sinr_db", 6.0) if pipeline_cfg else 6.0)
        ),
        congestion_rf_ok_rsrp_dbm=float(
            soft.get("congestion_rf_ok_rsrp_dbm",
                     getattr(pipeline_cfg, "congestion_rf_ok_rsrp_dbm", -90.0) if pipeline_cfg else -90.0)
        ),
        low_rf_sinr_gate_db=float(
            soft.get("v5_low_rf_sinr_gate_db",
                     getattr(pipeline_cfg, "v5_low_rf_sinr_gate_db", 4.0) if pipeline_cfg else 4.0)
        ),
        sinr_feasibility_gate_db=float(
            hard.get("sinr_feasibility_gate_db",
                     getattr(pipeline_cfg, "sinr_feasibility_gate_db", 10.0) if pipeline_cfg else 10.0)
        ),
        ca_restore_sinr_db=float(
            getattr(state_cfg, "exploring_ca_restore_sinr_db", 8.0) if state_cfg else 8.0
        ),
        stable_sinr_db=float(
            getattr(state_cfg, "bouncer_stable_sinr_db", 8.0) if state_cfg else 8.0
        ),
        nr_rollback_sinr_db=float(
            getattr(engine, "_NR_ROLLBACK_SINR_DB", 3.0)
        ),
        night_probe_bad_sinr_db=float(
            getattr(night_cfg, "night_probe_trigger_bad_rf_sinr_db", 5.0) if night_cfg else 5.0
        ),
        night_probe_bad_rsrp_dbm=float(
            getattr(night_cfg, "night_probe_trigger_bad_rsrp_dbm", -100.0) if night_cfg else -100.0
        ),
        escape_probe_rf_cliff_sinr_min_db=float(
            getattr(escape_cfg, "escape_probe_rf_cliff_sinr_min_db", 3.0) if escape_cfg else 3.0
        ),
    )
