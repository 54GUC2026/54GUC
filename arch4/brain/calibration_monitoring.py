"""Background calibration monitoring publication owner."""

from __future__ import annotations

import time
from typing import Any, Dict, Mapping

from arch4.brain.calibration_workflow import PHASE_BACKGROUND

BACKGROUND_MONITORING_EVENT_MIN_INTERVAL_S = 60 * 60.0
BACKGROUND_ANALYSIS_EVENT_MIN_INTERVAL_S = 5 * 60.0


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _session_outcome(report: Mapping[str, Any]) -> Dict[str, Any]:
    return _mapping(report.get("session_outcome_report"))


def build_background_monitoring_recommendation(report: Mapping[str, Any]) -> Dict[str, str]:
    """Return the public background-monitoring recommendation.

    CalibrationAuthority owns when a background candidate is analyzed. This
    module owns the recommendation vocabulary and prevents that shape from
    becoming private authority logic or GUI wording logic.
    """

    source = _mapping(report)
    verdict = str(source.get("verdict", "") or "")
    comparison = _mapping(source.get("comparison"))
    evidence_grade = str(_session_outcome(source).get("evidence_grade", "") or "")
    context_ready = bool(comparison.get("context_ready", True))
    if not context_ready:
        return {
            "recommendation": "collect_same_context_window",
            "message": (
                "Cell changed during the background check. "
                "54GUC is collecting a clean same-cell result."
            ),
            "conclusion": "context_shifted",
            "physical_setup_claim": "not_asserted",
            "evidence_grade": evidence_grade or "not_comparable",
        }
    if verdict in {"worse", "drift_detected"}:
        return {
            "recommendation": "recalibrate_if_persistent",
            "message": (
                "Background result is worse than the saved setup. "
                "Hold position and run Antenna Guide again only if this persists."
            ),
            "conclusion": "candidate_worse_vs_reference",
            "physical_setup_claim": "not_asserted",
            "evidence_grade": evidence_grade or "negative",
        }
    return {
        "recommendation": "observe",
        "message": "Monitoring session complete. 54GUC will keep observing.",
        "conclusion": "monitoring_complete",
        "physical_setup_claim": "not_asserted",
        "evidence_grade": evidence_grade,
    }


def _monitoring_signature(*, verdict: str, monitoring: Mapping[str, Any]) -> Dict[str, str]:
    return {
        "verdict": str(verdict or ""),
        "recommendation": str(monitoring.get("recommendation", "") or "observe"),
        "conclusion": str(monitoring.get("conclusion", "") or ""),
        "evidence_grade": str(monitoring.get("evidence_grade", "") or ""),
    }


def _signature_text(signature: Mapping[str, str]) -> str:
    return "|".join(f"{key}={signature[key]}" for key in sorted(signature))


def build_background_monitoring_event(
    *,
    candidate_id: str,
    profile: Dict[str, Any],
    verdict: str,
    monitoring: Mapping[str, Any],
    now_ts: float,
) -> Dict[str, Any] | None:
    """Return a deduped runtime event payload, or None when unchanged."""

    runtime_review = profile.setdefault("runtime_review", {})
    if not isinstance(runtime_review, dict):
        runtime_review = {}
        profile["runtime_review"] = runtime_review
    signature = _monitoring_signature(verdict=verdict, monitoring=monitoring)
    sig_text = _signature_text(signature)
    last_sig = str(runtime_review.get("last_monitoring_event_signature", "") or "")
    last_ts = float(runtime_review.get("last_monitoring_event_ts", 0.0) or 0.0)
    if sig_text == last_sig and float(now_ts) - last_ts < BACKGROUND_MONITORING_EVENT_MIN_INTERVAL_S:
        return None
    runtime_review["last_monitoring_event_signature"] = sig_text
    runtime_review["last_monitoring_event_ts"] = float(now_ts)
    return {
        "kind": "calibration_monitoring_recommendation",
        "candidate_id": str(candidate_id or ""),
        "verdict": signature["verdict"],
        "recommendation": signature["recommendation"],
        "message": str(monitoring.get("message", "") or "Monitoring session complete. 54GUC will keep observing."),
        "conclusion": signature["conclusion"],
        "physical_setup_claim": str(monitoring.get("physical_setup_claim", "") or "not_asserted"),
        "evidence_grade": signature["evidence_grade"],
    }


def build_calibration_analysis_done_event(
    *,
    candidate_id: str,
    profile: Dict[str, Any],
    workflow_mode: str,
    report: Mapping[str, Any],
    sample_count: int,
    source_raw_sample_count: int,
    now_ts: float | None = None,
) -> Dict[str, Any] | None:
    """Return a deduped calibration-analysis event payload, or None."""

    runtime_review = profile.setdefault("runtime_review", {})
    if not isinstance(runtime_review, dict):
        runtime_review = {}
        profile["runtime_review"] = runtime_review
    source = _mapping(report)
    verdict = str(source.get("verdict", "") or "")
    recommendation = str(source.get("recommendation", "") or "")
    evidence_grade = str(_session_outcome(source).get("evidence_grade", "") or "")
    blocking_gates = source.get("blocking_gates", [])
    if not isinstance(blocking_gates, list):
        blocking_gates = []
    signature = {
        "verdict": verdict,
        "recommendation": recommendation,
        "evidence_grade": evidence_grade,
        "coverage_ok": "1" if bool(source.get("coverage_ok", False)) else "0",
        "approval_ready": "1" if bool(source.get("approval_ready", False)) else "0",
        "gates": ",".join(str(item) for item in blocking_gates[:4]),
    }
    sig_text = _signature_text(signature)
    now_value = float(time.time() if now_ts is None else now_ts)
    if str(workflow_mode or "") == PHASE_BACKGROUND:
        last_sig = str(runtime_review.get("last_analysis_event_signature", "") or "")
        last_ts = float(runtime_review.get("last_analysis_event_ts", 0.0) or 0.0)
        if sig_text == last_sig and now_value - last_ts < BACKGROUND_ANALYSIS_EVENT_MIN_INTERVAL_S:
            return None
        runtime_review["last_analysis_event_signature"] = sig_text
        runtime_review["last_analysis_event_ts"] = now_value
    return {
        "kind": "calibration_analysis_done",
        "candidate_id": str(candidate_id or ""),
        "sample_count": int(sample_count),
        "source_raw_sample_count": int(source_raw_sample_count),
        "verdict": verdict,
        "recommendation": recommendation,
    }


__all__ = [
    "build_background_monitoring_event",
    "build_background_monitoring_recommendation",
    "build_calibration_analysis_done_event",
]
