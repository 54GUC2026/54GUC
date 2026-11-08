"""Shared canonical DL bottleneck assessment from delivered-load evidence.

This module consumes existing RF/CA/traffic/congestion surfaces and the
controlled speed-test delivered-capacity estimate.  It never turns ordinary
live traffic into speed-test proof.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from arch4.core_contracts.signals import SignalSamplePort
from arch4.interfaces import ComputedSignals


DL_BOTTLENECK_SCHEMA_VERSION = "dl_bottleneck_v1"
DL_BOTTLENECK_OWNER = "signal_processing.dl_bottleneck"


@dataclass(frozen=True, slots=True)
class DlBottleneckAssessment:
    state: str = "unknown"
    reason: str = "missing_controlled_speedtest_evidence"
    proof_level: str = "unknown"
    evidence: dict[str, Any] = field(default_factory=dict)
    missing_evidence: tuple[str, ...] = ("controlled_speedtest_dl_samples",)
    rejected_explanations: tuple[str, ...] = ()
    schema_version: str = DL_BOTTLENECK_SCHEMA_VERSION
    owner: str = DL_BOTTLENECK_OWNER

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "owner": self.owner,
            "state": self.state,
            "reason": self.reason,
            "proof_level": self.proof_level,
            "evidence": dict(self.evidence),
            "missing_evidence": list(self.missing_evidence),
            "rejected_explanations": list(self.rejected_explanations),
        }


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _bool(value: Any) -> bool:
    return bool(value)


def assess_dl_bottleneck(
    modem: SignalSamplePort,
    computed: ComputedSignals,
    *,
    ca_active: bool,
) -> DlBottleneckAssessment:
    """Return one conservative DL bottleneck assessment.

    Unknown is the correct answer until controlled speed-test evidence exists.
    """

    delivered_valid = _bool(
        getattr(computed, "delivered_capacity_valid", False)
    )
    ratio = max(
        0.0,
        min(1.0, _float(getattr(computed, "delivered_capacity_ratio", 0.0))),
    )
    best_dl = max(
        0.0,
        _float(getattr(computed, "delivered_capacity_best_dl_mbps", 0.0)),
    )
    ceiling = max(
        0.0,
        _float(getattr(computed, "delivered_capacity_ceiling_mbps", 0.0)),
    )
    sample_count = int(
        _float(getattr(computed, "delivered_capacity_sample_count", 0))
    )
    source = str(getattr(computed, "delivered_capacity_source", "") or "")
    evidence = {
        "delivered_capacity_valid": delivered_valid,
        "delivered_capacity_ratio": ratio if delivered_valid else None,
        "delivered_capacity_best_dl_mbps": best_dl if delivered_valid else None,
        "delivered_capacity_ceiling_mbps": ceiling if delivered_valid else None,
        "delivered_capacity_sample_count": sample_count,
        "delivered_capacity_source": source or "needs_controlled_speed_tests",
    }
    if not delivered_valid:
        return DlBottleneckAssessment(evidence=evidence)

    sinr = _float(getattr(modem, "sinr_db", 0.0))
    rsrp = _float(getattr(modem, "rsrp_dbm", 0.0))
    rsrq = _float(getattr(modem, "rsrq_db", 0.0))
    cq = _float(getattr(computed, "cq_cell", 0.0))
    rtt = _float(
        getattr(
            computed,
            "rtt_sliding_avg",
            getattr(computed, "rtt_ms", 0.0),
        )
    )
    congestion = _float(getattr(computed, "congestion_pct", 0.0))
    congestion_valid = _bool(
        getattr(computed, "congestion_pct_valid", False)
    )
    traffic_valid = _bool(getattr(modem, "traffic_valid", True))
    packet_session = getattr(modem, "packet_session_connected", None)
    ca_active = _bool(ca_active)
    cart_ld = (
        bool(int(getattr(computed, "cart_ld", 0) or 0))
        if bool(getattr(computed, "cart_ld_valid", False))
        else None
    )
    evidence.update(
        {
            "sinr_db": sinr,
            "rsrp_dbm": rsrp,
            "rsrq_db": rsrq,
            "cq_cell": cq,
            "rtt_ms": rtt,
            "congestion_pct": congestion if congestion_valid else None,
            "congestion_pct_valid": congestion_valid,
            "traffic_valid": traffic_valid,
            "packet_session_connected": packet_session,
            "ca_active": ca_active,
            "cart_ld": cart_ld,
        }
    )

    if ratio >= 0.55:
        return DlBottleneckAssessment(
            state="no_dl_bottleneck_proven",
            reason="controlled delivered capacity is acceptable for the LTE ceiling",
            proof_level="proven",
            evidence=evidence,
            missing_evidence=(),
            rejected_explanations=("delivered_capacity_poor",),
        )

    if not traffic_valid or packet_session is False:
        return DlBottleneckAssessment(
            state="transport_session_failure",
            reason="delivered capacity is poor and traffic/session truth is invalid",
            proof_level="inferred",
            evidence=evidence,
            missing_evidence=("path_validation",),
            rejected_explanations=("pure_rf_limit",),
        )

    if cart_ld is False or (not ca_active and ceiling >= 80.0):
        return DlBottleneckAssessment(
            state="ca_bandwidth_grant_limit",
            reason="delivered capacity is poor while CA/bandwidth grant evidence is absent",
            proof_level="inferred",
            evidence=evidence,
            missing_evidence=("scheduler_resource_share",),
            rejected_explanations=(),
        )

    rf_sufficient = bool(
        sinr >= 8.0
        and rsrp >= -100.0
        and rsrq >= -13.5
        and (cq >= 0.55 or cq <= 0.0)
    )
    rf_bad = bool(sinr < 3.0 or rsrp <= -110.0 or rsrq <= -14.0 or (0.0 < cq <= 0.45))
    congestion_low = bool(congestion_valid and congestion <= 65.0)
    rtt_ok = bool(rtt <= 150.0 or rtt <= 0.0)

    if rf_bad:
        return DlBottleneckAssessment(
            state="radio_limit",
            reason="delivered capacity is poor and RF evidence is weak",
            proof_level="inferred",
            evidence=evidence,
            missing_evidence=("scheduler_resource_share",),
            rejected_explanations=(),
        )

    if rf_sufficient and congestion_low and rtt_ok:
        return DlBottleneckAssessment(
            state="hidden_capacity_limit",
            reason="delivered capacity is poor while RF, RTT, and congestion evidence look usable",
            proof_level="inferred",
            evidence=evidence,
            missing_evidence=(
                "scheduler_resource_share",
                "backhaul_or_server_path",
                "operator_policy",
            ),
            rejected_explanations=("rf_degradation", "attached_cell_congestion"),
        )

    missing: list[str] = []
    if not congestion_valid:
        missing.append("congestion_evidence")
    if not rf_sufficient and not rf_bad:
        missing.append("rf_sufficiency_decision")
    if not rtt_ok:
        missing.append("latency_path_classification")
    return DlBottleneckAssessment(
        state="unknown",
        reason="delivered capacity is poor but required causal evidence is incomplete",
        proof_level="unknown",
        evidence=evidence,
        missing_evidence=tuple(missing or ["scheduler_resource_share"]),
        rejected_explanations=(),
    )
