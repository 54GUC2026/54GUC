"""
arch4/brain/prediction_confidence.py
====================================
Derived prediction-confidence contract shared by runtime status and decision use.
"""

from __future__ import annotations

from typing import Any, Dict


def compute_prediction_confidence_contract(
    *,
    prediction: Dict[str, Any] | None,
    prediction_modifier: float,
    prediction_posture: str,
    learned_confidence_kind: str = "",
    learned_confidence_reason: str = "",
) -> Dict[str, Any]:
    pred = prediction if isinstance(prediction, dict) else {}
    ready = bool(pred.get("ready", False))
    if not ready:
        return {
            "prediction_confidence_score": 0.0,
            "prediction_confidence_label": "none",
            "prediction_confidence_reason_code": "not_ready",
            "prediction_confidence_summary": "Prediction confidence is not available yet.",
        }

    build_records = int(pred.get("build_records", 60) or 60)
    slots = int(pred.get("slots", 24) or 24)
    drift_detected = bool(pred.get("drift_detected", False))

    score = 0.35
    score += min(0.35, float(build_records) / 200.0 * 0.35)
    score += min(0.15, float(slots) / 60.0 * 0.15)
    score *= max(0.5, min(1.2, float(prediction_modifier or 1.0)))
    if drift_detected:
        score -= 0.15
    score = max(0.0, min(1.0, score))

    if score >= 0.75:
        label = "high"
    elif score >= 0.45:
        label = "medium"
    else:
        label = "low"

    posture = str(prediction_posture or "normal").strip().lower()
    kind = str(learned_confidence_kind or "").strip().lower()
    reason = str(learned_confidence_reason or "").strip()
    reason_code = "normal"
    if posture in {"cautious", "conservative"} and kind == "authority_observe_monitor":
        reason_code = "observe_monitor_fallback"
        summary = "Prediction confidence is low because 54GUC repeatedly had to fall back to observe/monitor."
    elif posture in {"cautious", "conservative"} and reason:
        reason_code = "learned_reason"
        summary = f"Prediction confidence is {label} because {reason.lower()}"
    elif posture == "boosted":
        reason_code = "strong_recent_results"
        summary = f"Prediction confidence is {label} after strong recent results."
    elif drift_detected:
        reason_code = "forecast_drift"
        summary = f"Prediction confidence is {label} because the forecast pattern is drifting."
    else:
        summary = f"Prediction confidence is {label}."

    return {
        "prediction_confidence_score": round(score, 3),
        "prediction_confidence_label": label,
        "prediction_confidence_reason_code": reason_code,
        "prediction_confidence_summary": summary,
    }
