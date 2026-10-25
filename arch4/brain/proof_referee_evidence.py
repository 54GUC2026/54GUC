"""Pure evidence and context grading for delayed ROI proof/referee closure."""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

from typing import Any, Mapping, Optional

from arch4.action_outcome_contract import action_outcome_contract_from_staged_roi
from arch4.brain.ca_contract import (
    cart_ld_activity as contract_cart_ld_activity,
    cast_ld as contract_cast_ld,
    cast_ld_valid as contract_cast_ld_valid,
)
from arch4.brain.proof_referee_context_owner import staged_roi_context_status
from arch4.signal_processing import ca_demand_load_mbps


OWNER = "arch4.brain.proof_referee_evidence"


def staged_roi_label(
    *,
    original: Any,
    modem_now: Optional[CellularIntelligenceGeneration],
    computed_now: Optional[DerivedSignalFrames],
    extra: dict[str, Any],
    incident_assessment: Optional[dict[str, Any]] = None,
) -> tuple[str, str, dict[str, Any]]:
    health_stage = str(extra.get("health_stage", "") or "").strip().lower()
    incident = dict(incident_assessment or {})
    if not incident:
        incident = extra.get("incident_assessment") if isinstance(extra.get("incident_assessment"), dict) else {}
    incident_class = str((incident or {}).get("incident_class", "") or "").strip().lower()
    self_induced = bool((incident or {}).get("self_induced", False))
    cart_activity_raw = (
        float(computed_now.quality.cart_activity_per_hour or 0.0)
        if computed_now is not None
        else 0.0
    )
    cast_raw = (
        float(computed_now.quality.cast or 0.0)
        if computed_now is not None
        else 0.0
    )
    cast_ld_valid = contract_cast_ld_valid(computed_now) if computed_now is not None else False
    cart_ld_activity = contract_cart_ld_activity(computed_now) if computed_now is not None else 0.0
    cast_ld = contract_cast_ld(computed_now) if computed_now is not None else 0.0
    cq_now = (
        float(computed_now.quality.cq_cell or 0.0)
        if computed_now is not None
        else 0.0
    )
    original_meta_raw = getattr(original, "objective_metadata", {}) or {}
    original_meta = dict(original_meta_raw) if isinstance(original_meta_raw, Mapping) else {}
    baseline_raw = original_meta.get("delayed_reward_baseline") or {}
    baseline = dict(baseline_raw) if isinstance(baseline_raw, Mapping) else {}
    cq_before = float(baseline.get("cq_before", getattr(original, "cq_cell", 0.0)) or 0.0)
    congestion_before = float(baseline.get("congestion_before", 0.0) or 0.0)
    congestion_before_valid = bool(baseline.get("congestion_before_valid", False))
    congestion_now = (
        float(computed_now.quality.congestion_pct or 0.0)
        if computed_now is not None
        else 0.0
    )
    congestion_now_valid = (
        bool(computed_now.quality.congestion_pct_valid)
        if computed_now is not None
        else False
    )
    rtt_now = (
        float(computed_now.latency.rtt_sliding_avg or 0.0)
        if computed_now is not None
        else 0.0
    )
    rtt_before = float(getattr(original, "rtt_sliding_avg", 0.0) or 0.0)
    sinr_now = (
        float(modem_now.cellular.sinr_db or 0.0)
        if modem_now is not None
        else 0.0
    )
    sinr_before = float(getattr(original, "sinr_db", 0.0) or 0.0)
    rsrq_now = (
        float(modem_now.cellular.rsrq_db or 0.0)
        if modem_now is not None
        else 0.0
    )
    rsrq_before = float(getattr(original, "rsrq_db", 0.0) or 0.0)
    tp_now = (
        float(modem_now.traffic.dl_mbps or 0.0)
        if modem_now is not None
        else 0.0
    )
    tp_ul_now = (
        float(modem_now.traffic.ul_mbps or 0.0)
        if modem_now is not None
        else 0.0
    )
    tp_before = float(getattr(original, "throughput_before_mbps", 0.0) or 0.0)
    ca_demand_load = ca_demand_load_mbps(tp_now, tp_ul_now)
    ca_ld_observation_active = bool(cast_ld_valid)
    reasons: list[str] = []

    if incident_class == "post_action_instability" or (self_induced and incident_class):
        reasons.append(f"incident:{incident_class}")
    if health_stage in {"warning", "degraded_hold", "degraded_persist", "critical"}:
        reasons.append(f"health:{health_stage}")
    if ca_ld_observation_active and cart_ld_activity >= 60.0:
        reasons.append(f"cart_ld_activity_high:{cart_ld_activity:.1f}/h")
    if ca_ld_observation_active and cast_ld < 75.0:
        reasons.append(f"cast_ld_low:{cast_ld:.1f}%")
    if cq_before > 0.0 and cq_now > 0.0 and (cq_now - cq_before) <= -0.08:
        reasons.append(f"cq_drop:{cq_now - cq_before:.2f}")
    if rtt_before > 0.0 and rtt_now >= max(rtt_before + 60.0, rtt_before * 1.8):
        reasons.append(f"rtt_worse:{rtt_before:.0f}->{rtt_now:.0f}ms")
    if sinr_before and sinr_now and (sinr_now - sinr_before) <= -3.0:
        reasons.append(f"sinr_drop:{sinr_now - sinr_before:.1f}dB")
    if rsrq_before and rsrq_now and (rsrq_now - rsrq_before) <= -3.0:
        reasons.append(f"rsrq_drop:{rsrq_now - rsrq_before:.1f}dB")

    evidence = {
        "health_stage": health_stage,
        "incident_class": incident_class,
        "self_induced": self_induced,
        "cast_ld_valid": bool(cast_ld_valid),
        "cart_ld_activity": round(cart_ld_activity, 3),
        "cast_ld": round(cast_ld, 3),
        "cart_activity_raw": round(cart_activity_raw, 3),
        "cast_raw": round(cast_raw, 3),
        "cq_before": round(cq_before, 4),
        "cq_now": round(cq_now, 4),
        "congestion_before": round(congestion_before, 3),
        "congestion_now": round(congestion_now, 3),
        "congestion_before_valid": congestion_before_valid,
        "congestion_now_valid": congestion_now_valid,
        "rtt_before": round(rtt_before, 3),
        "rtt_now": round(rtt_now, 3),
        "sinr_before": round(sinr_before, 3),
        "sinr_now": round(sinr_now, 3),
        "rsrq_before": round(rsrq_before, 3),
        "rsrq_now": round(rsrq_now, 3),
        "tp_before": round(tp_before, 3),
        "tp_now": round(tp_now, 3),
        "tp_ul_now": round(tp_ul_now, 3),
        "ca_demand_load_mbps": round(ca_demand_load, 3),
        "ca_ld_observation_active": bool(ca_ld_observation_active),
    }
    context_status, context_reason, context_evidence = staged_roi_context_status(
        original=original,
        modem_now=modem_now,
    )
    evidence.update(context_evidence)
    valid_evidence, validity_reason = staged_roi_evidence_valid(
        modem_now=modem_now,
        computed_now=computed_now,
        extra=extra,
        health_stage=health_stage,
    )
    evidence["grade_evidence_valid"] = bool(valid_evidence)
    evidence["grade_validity_reason"] = validity_reason
    if context_status != "same":
        return "neutral", context_reason, evidence
    if not valid_evidence:
        return "neutral", validity_reason, evidence
    if reasons:
        return "bad", ";".join(reasons), evidence
    if (
        ca_ld_observation_active
        and health_stage in {"healthy", "normal", ""}
        and cart_ld_activity <= 20.0
        and cast_ld >= 90.0
    ):
        return "good", "stable_after_horizon", evidence
    return "neutral", "mixed_or_insufficient_horizon_evidence", evidence


def staged_roi_runtime_ready(
    *,
    modem_now: Optional[CellularIntelligenceGeneration],
    computed_now: Optional[DerivedSignalFrames],
    extra: dict[str, Any],
    incident_assessment: Optional[dict[str, Any]] = None,
) -> tuple[bool, str]:
    health_stage = str(extra.get("health_stage", "") or "").strip().lower()
    valid, reason = staged_roi_evidence_valid(
        modem_now=modem_now,
        computed_now=computed_now,
        extra=extra,
        health_stage=health_stage,
    )
    if not valid:
        return False, reason

    incident = dict(incident_assessment or {})
    if not incident:
        incident = extra.get("incident_assessment") if isinstance(extra.get("incident_assessment"), dict) else {}
    incident_class = str((incident or {}).get("incident_class", "") or "").strip().lower()
    incident_subtype = str((incident or {}).get("incident_subtype", "") or "").strip().lower()
    if incident_class in {"startup_bootstrap_observe", "radio_disconnected"}:
        return False, f"runtime_not_ready:{incident_class}"
    if "bootstrap_grace" in incident_subtype or "startup" in incident_subtype:
        return False, f"runtime_not_ready:{incident_subtype}"

    runtime_health = extra.get("runtime_health") if isinstance(extra.get("runtime_health"), dict) else {}
    health_checks = extra.get("health_checks") if isinstance(extra.get("health_checks"), dict) else {}
    for source in (runtime_health, health_checks):
        stage = str(source.get("stage", "") or "").strip().lower()
        if stage in {"stale", "live_register_stale", "connection_lost"}:
            return False, f"runtime_not_ready:{stage}"
        if bool(source.get("startup_bootstrap_grace_active", False)):
            return False, "runtime_not_ready:startup_grace"
        reason_text = str(source.get("reason", "") or source.get("action_hint", "") or "").strip().lower()
        if "startup" in reason_text or "connect_settle" in reason_text:
            return False, f"runtime_not_ready:{reason_text}"

    return True, "runtime_ready"


def staged_roi_evidence_valid(
    *,
    modem_now: Optional[CellularIntelligenceGeneration],
    computed_now: Optional[DerivedSignalFrames],
    extra: dict[str, Any],
    health_stage: str,
) -> tuple[bool, str]:
    if modem_now is None:
        return False, "invalid_grade_evidence:missing_modem"
    if computed_now is None:
        return False, "invalid_grade_evidence:missing_computed"
    if bool(extra.get("sample_stale", False) or extra.get("live_register_stale", False)):
        return False, "invalid_grade_evidence:stale_sample"
    if health_stage in {"stale", "live_register_stale"}:
        return False, f"invalid_grade_evidence:{health_stage}"
    return True, "valid_grade_evidence"


def staged_roi_action_outcome_contract(label: str, reason: str) -> dict[str, Any]:
    return action_outcome_contract_from_staged_roi(label=label, reason=reason)


def staged_roi_learning_grade_actionable(outcome_contract: Mapping[str, Any]) -> bool:
    contract = outcome_contract if isinstance(outcome_contract, Mapping) else {}
    label_norm = str(contract.get("learning_outcome_label", "") or "").strip().lower()
    return bool(contract.get("learning_ltm_allowed", False)) and label_norm in {"good", "bad"}


__all__ = [
    "OWNER",
    "staged_roi_action_outcome_contract",
    "staged_roi_evidence_valid",
    "staged_roi_label",
    "staged_roi_learning_grade_actionable",
    "staged_roi_runtime_ready",
]
