"""Shared RF truth assessment.

RF collapse is a diagnosis, not a label for any single ugly number.  This
module is intentionally small and pure so the state machine, incident tracker,
and GUI-facing health broadcast can share one evidence rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from arch4.brain.outcome_profile import has_ca_capacity, is_bad_rf_capacity_winner
from arch4.controller_frames import (
    CellularIntelligenceGeneration,
    DerivedSignalFrames,
)


BAD_RF_COLLAPSE_RSRP_DBM = -110.0
BAD_RF_COLLAPSE_RSRQ_DB = -14.0


@dataclass(frozen=True)
class RfTruthVerdict:
    verdict: str
    confirmed_bad_rf: bool
    rules_out_bad_rf: bool
    confidence: float
    reason: str
    bad_evidence: tuple[str, ...] = field(default_factory=tuple)
    good_evidence: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confirmed_bad_rf": bool(self.confirmed_bad_rf),
            "rules_out_bad_rf": bool(self.rules_out_bad_rf),
            "confidence": float(self.confidence),
            "reason": self.reason,
            "bad_evidence": list(self.bad_evidence),
            "good_evidence": list(self.good_evidence),
        }


def assess_rf_truth(
    *,
    modem: CellularIntelligenceGeneration,
    computed: DerivedSignalFrames,
    cfg: Any = None,
    low_sinr_persistent: bool | None = None,
) -> RfTruthVerdict:
    if not bool(modem.cellular.radio_connected):
        return RfTruthVerdict(
            verdict="unknown",
            confirmed_bad_rf=False,
            rules_out_bad_rf=False,
            confidence=0.0,
            reason="radio disconnected; RF registers are not authoritative",
        )

    sinr = float(modem.cellular.sinr_db or 0.0)
    rsrp = float(modem.cellular.rsrp_dbm or 0.0)
    rsrq = float(modem.cellular.rsrq_db or 0.0)
    cq = float(computed.quality.cq_cell or 0.0)
    rtt = float(
        computed.latency.rtt_sliding_avg
        or computed.latency.rtt_ms
        or 0.0
    )
    congestion = float(computed.quality.congestion_pct or 0.0)
    ca_active = bool(modem.cellular.ca_active)
    ca_capacity = bool(has_ca_capacity(modem))
    capacity_winner = bool(is_bad_rf_capacity_winner(modem, computed, cfg))

    if low_sinr_persistent is None:
        low_sinr = sinr <= 3.0
    else:
        low_sinr = bool(low_sinr_persistent)
    weak_rsrp = rsrp <= BAD_RF_COLLAPSE_RSRP_DBM
    weak_rsrq = rsrq <= BAD_RF_COLLAPSE_RSRQ_DB
    weak_cq = bool(cq > 0.0 and cq <= 0.45)
    outcome_bad = bool(
        (rtt > 0.0 and rtt >= 180.0)
        or (congestion > 0.0 and congestion >= 75.0)
    )

    good: list[str] = []
    if capacity_winner:
        good.append("capacity_outcome_usable")
    if ca_active and rsrp > BAD_RF_COLLAPSE_RSRP_DBM and (rsrq > BAD_RF_COLLAPSE_RSRQ_DB or cq >= 0.45 or cq <= 0.0):
        good.append("ca_active_with_usable_serving_rf")
    if rsrp >= -100.0 and rsrq >= -13.5 and (cq >= 0.45 or cq <= 0.0):
        good.append("strong_serving_rf")
    if ca_capacity and not outcome_bad and sinr >= 0.0 and rsrq > BAD_RF_COLLAPSE_RSRQ_DB:
        good.append("ca_capacity_without_bad_outcome")

    if good:
        return RfTruthVerdict(
            verdict="usable",
            confirmed_bad_rf=False,
            rules_out_bad_rf=True,
            confidence=0.90 if capacity_winner or "strong_serving_rf" in good else 0.80,
            reason="usable RF evidence blocks BAD_RF",
            good_evidence=tuple(good),
        )

    bad: list[str] = []
    if low_sinr:
        bad.append("persistent_low_sinr")
    if weak_rsrp:
        bad.append("weak_rsrp")
    if weak_rsrq:
        bad.append("weak_rsrq")
    if weak_cq:
        bad.append("weak_cq")
    if outcome_bad:
        bad.append("bad_user_outcome")

    hard_raw_collapse = bool(low_sinr and weak_rsrp and weak_rsrq)
    confirmed = bool(
        hard_raw_collapse
        or (len(bad) >= 4 and outcome_bad)
        or (len([x for x in bad if x != "bad_user_outcome"]) >= 2 and outcome_bad and not ca_capacity)
        or (low_sinr and len(bad) >= 3 and not ca_capacity)
    )
    if confirmed:
        return RfTruthVerdict(
            verdict="bad",
            confirmed_bad_rf=True,
            rules_out_bad_rf=False,
            confidence=0.90 if hard_raw_collapse else 0.82,
            reason="multiple RF indicators prove collapse",
            bad_evidence=tuple(bad),
        )

    if bad:
        return RfTruthVerdict(
            verdict="watch",
            confirmed_bad_rf=False,
            rules_out_bad_rf=False,
            confidence=0.55,
            reason="RF symptoms present but not enough to prove BAD_RF",
            bad_evidence=tuple(bad),
        )

    return RfTruthVerdict(
        verdict="good",
        confirmed_bad_rf=False,
        rules_out_bad_rf=True,
        confidence=0.75,
        reason="no RF-collapse evidence",
        good_evidence=("no_bad_rf_evidence",),
    )
