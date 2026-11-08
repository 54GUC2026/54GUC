"""Owner contract for congestion threshold mutation.

Congestion formulas stay in the processors. Runtime threshold changes must
arrive as an owner-stamped policy object, not loose kwargs from whichever caller
happens to hold the congestion module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from arch4.coerce import CONGESTION_HIGH_ENTER_PCT, CONGESTION_MILD_ENTER_PCT
from arch4.interfaces import DEFAULT_CONGESTION_RSRQ_GAP_THRESHOLD_DB


CONGESTION_THRESHOLD_POLICY_OWNER = "arch4.core.signal_computer.SignalComputer"


@dataclass(frozen=True, slots=True)
class CongestionThresholdPolicy:
    owner: str
    source: str
    rtt_warning_ms: float
    rtt_cliff_ms: float
    rtt_floor_ms: float
    rtt_ceiling_ms: float
    rsrq_gap_threshold_db: float
    rsrq_ceiling_db: float
    moderate_pct: float
    high_pct: float
    rsrq_moderate_db: float
    rsrq_high_db: float
    local_rf_ok_sinr_db: float
    local_rf_ok_rsrp_dbm: float
    local_self_load_dl_mbps: float


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def build_congestion_threshold_policy(
    *,
    signal_cfg: Any,
    config: Any,
    hard_thresholds: Mapping[str, Any] | None = None,
    source: str,
) -> CongestionThresholdPolicy:
    hard = hard_thresholds if isinstance(hard_thresholds, Mapping) else {}
    rtt_warning_ms = _float(
        hard.get("rtt_warning_ms") or getattr(signal_cfg, "rtt_warning_ms", 150.0),
        150.0,
    )
    rtt_cliff_ms = _float(
        hard.get("rtt_cliff_ms") or getattr(signal_cfg, "rtt_cliff_ms", 400.0),
        400.0,
    )
    return CongestionThresholdPolicy(
        owner=CONGESTION_THRESHOLD_POLICY_OWNER,
        source=str(source or "unknown"),
        rtt_warning_ms=rtt_warning_ms,
        rtt_cliff_ms=rtt_cliff_ms,
        rtt_floor_ms=_float(getattr(signal_cfg, "congestion_rtt_floor_ms", 5.0), 5.0),
        rtt_ceiling_ms=_float(getattr(signal_cfg, "congestion_rtt_ceiling_ms", 150.0), 150.0),
        rsrq_gap_threshold_db=_float(
            getattr(signal_cfg, "congestion_rsrq_gap_threshold_db", DEFAULT_CONGESTION_RSRQ_GAP_THRESHOLD_DB),
            DEFAULT_CONGESTION_RSRQ_GAP_THRESHOLD_DB,
        ),
        rsrq_ceiling_db=_float(getattr(signal_cfg, "congestion_rsrq_ceiling_db", 6.0), 6.0),
        moderate_pct=CONGESTION_MILD_ENTER_PCT,
        high_pct=CONGESTION_HIGH_ENTER_PCT,
        rsrq_moderate_db=_float(getattr(config, "congestion_qualifier_rsrq_gap_db", 4.0), 4.0),
        rsrq_high_db=6.0,
        local_rf_ok_sinr_db=_float(getattr(config, "congestion_rf_ok_sinr_db", 6.0), 6.0),
        local_rf_ok_rsrp_dbm=_float(
            getattr(config, "congestion_rf_ok_rsrp_dbm", -90.0),
            -90.0,
        ),
        local_self_load_dl_mbps=_float(
            getattr(config, "congestion_self_load_dl_mbps", 10.0),
            10.0,
        ),
    )


def require_congestion_threshold_policy(policy: Any) -> CongestionThresholdPolicy:
    if not isinstance(policy, CongestionThresholdPolicy):
        raise TypeError("congestion_threshold_update_requires_policy")
    if policy.owner != CONGESTION_THRESHOLD_POLICY_OWNER:
        raise ValueError("congestion_threshold_policy_owner_mismatch")
    return policy
