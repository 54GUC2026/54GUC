"""Facts-only calibration confidence owner.

Calibration may tell the rest of 54GUC how trustworthy its current facts are.
It must not decide modem actions or publish policy advice.  Self-learning,
Brain, TSAR, and Command Authority own those later steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class CalibrationFactConfidence:
    score: float
    level: str
    basis: dict[str, Any]
    package: dict[str, Any]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value or 0.0)))


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value or {}) if isinstance(value, Mapping) else {}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def fact_confidence_level(score: float) -> str:
    val = _clamp01(score)
    if val >= 0.95:
        return "strong"
    if val >= 0.70:
        return "usable"
    if val >= 0.45:
        return "cautious"
    if val > 0.0:
        return "weak"
    return "none"


def _state_floor(*, state_name: str, freshness: str, trust_level: str) -> float:
    state = str(state_name or "").strip().lower()
    fresh = str(freshness or "").strip().lower()
    trust = str(trust_level or "").strip().lower()
    if state == "stale" and fresh == "scope_mismatch":
        return 0.0
    if state == "active" and fresh == "fresh":
        return 1.0
    if state == "active":
        return 0.85
    if state == "stale":
        return 0.65
    if state in {"collecting", "provisional"}:
        return 0.55
    if state == "invalid":
        return 0.20
    if state == "disabled":
        return 0.0
    if trust == "estimated":
        return 0.50
    if trust == "trusted":
        return 0.85
    return 0.25


def calibration_fact_confidence(
    *,
    calibration_state: Mapping[str, Any] | None,
    calibration: Mapping[str, Any] | None = None,
    state_name: str = "",
    freshness: str = "",
    trust_level: str = "",
) -> CalibrationFactConfidence:
    """Score calibration fact trust from evidence, not action intent.

    The score is deliberately allowed to rise during collection when enough
    clean facts exist.  That gives self-learning and prediction useful
    confidence earlier, while `baseline_valid` and command authority still gate
    actual control.
    """

    state = _mapping(calibration_state)
    calib = _mapping(calibration)
    name = str(state_name or state.get("state", "") or "").strip().lower()
    fresh = str(freshness or state.get("freshness", "") or "").strip().lower()
    trust = str(trust_level or "").strip().lower()

    state_floor = _state_floor(state_name=name, freshness=fresh, trust_level=trust)
    if state_floor <= 0.0:
        package = _fact_package(
            score=0.0,
            level="none",
            basis={
                "owner": "calibration_fact_confidence",
                "state_floor": 0.0,
                "sample_progress": 0.0,
                "candidate_sample_count": 0,
                "calibration_sample_count": 0,
                "primary_metric_confidence": 0.0,
                "past_context_confidence": 0.0,
                "load_penalty": 0.0,
                "evidence_score": 0.0,
                "reason": "terminal_or_unavailable",
            },
            state_name=name,
            freshness=fresh,
            state=state,
            calibration=calib,
        )
        return CalibrationFactConfidence(
            score=0.0,
            level="none",
            basis=package["basis"],
            package=package,
        )

    candidate_samples = max(
        _int(state.get("candidate_session_sample_count")),
        _int(state.get("candidate_sample_count")),
    )
    calibration_samples = _int(calib.get("sample_count"))
    baseline_samples = max(
        _int(state.get("baseline_context_sample_count")),
        _int(state.get("active_context_sample_count")),
    )
    progress_pct = _clamp01(_float(state.get("candidate_session_progress_pct")) / 100.0)
    active_saved_setup = bool(
        name == "active"
        and fresh == "fresh"
        and trust == "trusted"
        and baseline_samples >= 10
    )
    sample_progress = max(
        progress_pct,
        _clamp01(float(candidate_samples) / 30.0),
        _clamp01(float(calibration_samples) / 72.0),
        _clamp01(float(baseline_samples) / 30.0) if active_saved_setup else 0.0,
    )
    primary_metric_confidence = _clamp01(
        state.get(
            "candidate_primary_metric_confidence",
            1.0 if active_saved_setup else 0.0,
        )
    )
    if active_saved_setup and primary_metric_confidence <= 0.0:
        primary_metric_confidence = 1.0
    past_context_confidence = _clamp01(float(_int(state.get("baseline_context_sample_count"))) / 60.0)
    evidence_score = (
        0.55
        + 0.24 * sample_progress
        + 0.16 * primary_metric_confidence
        + 0.05 * past_context_confidence
    )
    load_ratio = max(
        _float(state.get("candidate_network_load_biased_ratio")),
        1.0 if bool(calib.get("network_load_biased", False)) else 0.0,
    )
    load_penalty = min(0.18, 0.18 * _clamp01(load_ratio))

    if name in {"collecting", "provisional"}:
        score = max(state_floor, evidence_score)
        score = min(score, 0.82)
    elif name == "active" and fresh == "fresh":
        score = 1.0
    elif name == "active":
        score = max(state_floor, min(0.92, state_floor + 0.07 * past_context_confidence))
    elif name == "stale":
        score = max(state_floor, min(0.72, state_floor + 0.05 * past_context_confidence))
    else:
        score = state_floor

    if name != "active" or fresh != "fresh":
        score = max(0.0, score - load_penalty)
    score = _clamp01(score)
    level = fact_confidence_level(score)
    basis = {
        "owner": "calibration_fact_confidence",
        "state_floor": float(state_floor),
        "sample_progress": float(sample_progress),
        "candidate_sample_count": int(candidate_samples),
        "calibration_sample_count": int(calibration_samples),
        "baseline_context_sample_count": int(baseline_samples),
        "baseline_context_evidence_usable": bool(active_saved_setup),
        "primary_metric_confidence": float(primary_metric_confidence),
        "past_context_confidence": float(past_context_confidence),
        "load_penalty": float(load_penalty),
        "evidence_score": float(_clamp01(evidence_score)),
        "reason": "evidence_weighted_fact_confidence",
    }
    return CalibrationFactConfidence(
        score=float(score),
        level=level,
        basis=basis,
        package=_fact_package(
            score=score,
            level=level,
            basis=basis,
            state_name=name,
            freshness=fresh,
            state=state,
            calibration=calib,
        ),
    )


def _fact_package(
    *,
    score: float,
    level: str,
    basis: Mapping[str, Any],
    state_name: str,
    freshness: str,
    state: Mapping[str, Any] | None = None,
    calibration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    st = _mapping(state)
    calib = _mapping(calibration)
    confidence = _clamp01(score)
    evidence_count = max(
        _int(basis.get("candidate_sample_count")),
        _int(basis.get("calibration_sample_count")),
        _int(basis.get("baseline_context_sample_count"))
        if bool(basis.get("baseline_context_evidence_usable", False))
        else 0,
    )
    sample_progress = _clamp01(basis.get("sample_progress", 0.0))
    primary_metric_confidence = _clamp01(basis.get("primary_metric_confidence", 0.0))
    past_context_confidence = _clamp01(basis.get("past_context_confidence", 0.0))
    load_penalty = _clamp01(basis.get("load_penalty", 0.0))
    clean_for_learning = bool(load_penalty <= 0.02 and evidence_count >= 10 and confidence >= 0.55)
    strong_enough_for_prediction = bool(
        confidence >= 0.70
        and sample_progress >= 0.66
        and primary_metric_confidence >= 0.55
        and load_penalty <= 0.08
    )
    strong_enough_for_preemption_facts = bool(
        confidence >= 0.78
        and sample_progress >= 0.80
        and primary_metric_confidence >= 0.70
        and past_context_confidence >= 0.35
        and load_penalty <= 0.04
    )
    missing: list[str] = []
    if evidence_count < 10:
        missing.append("more_clean_samples")
    if sample_progress < 0.66:
        missing.append("collection_progress")
    if primary_metric_confidence < 0.55:
        missing.append("primary_metric_confidence")
    if past_context_confidence < 0.35:
        missing.append("past_context")
    if load_penalty > 0.08:
        missing.append("quiet_sample_window")
    if state_name in {"disabled", "invalid"}:
        missing.append("active_calibration_session")

    self_learning_strength = confidence * (1.0 - min(0.35, load_penalty))
    crystal_ball_strength = min(
        confidence,
        (0.55 * sample_progress)
        + (0.30 * primary_metric_confidence)
        + (0.15 * past_context_confidence),
    )
    referee_strength = min(1.0, (confidence * 0.70) + (past_context_confidence * 0.30))
    context_key = _text(
        st.get("baseline_context_key")
        or st.get("active_context_key")
        or st.get("candidate_context_key")
        or calib.get("current_context_key")
    )
    profile_id = _text(
        st.get("active_profile_id")
        or st.get("candidate_profile_id")
        or calib.get("active_profile_id")
        or calib.get("profile_id")
    )
    profile_version = _int(
        st.get("active_profile_version")
        or st.get("candidate_profile_version")
        or calib.get("active_profile_version")
    )
    sample_count = int(evidence_count)
    fresh_sample_count = max(
        _int(st.get("fresh_sample_count")),
        _int(st.get("candidate_fresh_sample_count")),
        _int(st.get("candidate_session_fresh_sample_count")),
        _int(st.get("active_context_fresh_sample_count")),
    )
    if fresh_sample_count <= 0 and bool(basis.get("baseline_context_evidence_usable", False)):
        fresh_sample_count = sample_count
    stale = bool(
        state_name == "stale"
        or freshness in {"stale", "scope_mismatch", "timed_out", "stalled", "paused"}
        or _bool(st.get("sample_stale"))
        or _bool(st.get("candidate_analysis_stale"))
    )
    quiet_qualified = bool(load_penalty <= 0.02)
    dirty = bool(stale or not clean_for_learning or load_penalty > 0.08)
    if state_name in {"disabled", "invalid"}:
        validity = "invalid"
    elif stale:
        validity = "stale"
    elif clean_for_learning:
        validity = "clean"
    elif state_name in {"collecting", "provisional"}:
        validity = "collecting"
    else:
        validity = "limited"
    source_ts = _first_float(
        st.get("sample_ts"),
        st.get("source_ts"),
        st.get("updated_at"),
        st.get("ts"),
        calib.get("sample_ts"),
        calib.get("source_ts"),
        calib.get("updated_at"),
        calib.get("ts"),
    )
    facts = {
        "value": _fact_value_snapshot(st, calib, confidence=confidence),
        "baseline": {
            "context_key": context_key,
            "sample_count": _int(basis.get("baseline_context_sample_count")),
            "valid": bool(basis.get("baseline_context_evidence_usable", False)),
            "profile_id": profile_id,
            "profile_version": int(profile_version),
        },
        "delta": {
            "valid": not stale and state_name not in {"disabled", "invalid"},
            "values": _mapping(st.get("delta_values") or calib.get("delta_values")),
            "state": _text(st.get("delta_state") or calib.get("delta_state") or state_name),
        },
        "sample_count": sample_count,
        "fresh_sample_count": int(fresh_sample_count),
        "sample_fresh": bool(fresh_sample_count > 0 and not stale),
        "quiet_qualified": quiet_qualified,
        "confidence": float(confidence),
        "context_key": context_key,
        "modem_profile": {
            "profile_id": profile_id,
            "profile_version": int(profile_version),
        },
        "source_ts": float(source_ts),
        "sample_ts": float(source_ts),
        "dirty": dirty,
        "stale": stale,
        "validity": validity,
    }

    return {
        "schema": "calibration_fact_package.v1",
        "owner": "calibration_fact_confidence",
        "state": str(state_name or ""),
        "freshness": str(freshness or ""),
        "confidence": float(confidence),
        "level": str(level or ""),
        "evidence_count": int(evidence_count),
        "clean_for_learning": clean_for_learning,
        "strong_enough_for_prediction": strong_enough_for_prediction,
        "strong_enough_for_preemption_facts": strong_enough_for_preemption_facts,
        "self_learning_input_strength": float(_clamp01(self_learning_strength)),
        "crystal_ball_input_strength": float(_clamp01(crystal_ball_strength)),
        "referee_input_strength": float(_clamp01(referee_strength)),
        "minimum_next_samples": max(0, 30 - int(evidence_count)),
        "missing_evidence": missing,
        "facts": facts,
        "basis": dict(basis),
        "action_authority": "none",
        "reason": "facts_only_confidence_package",
    }


def _fact_value_snapshot(
    state: Mapping[str, Any],
    calibration: Mapping[str, Any],
    *,
    confidence: float,
) -> dict[str, Any]:
    out: dict[str, Any] = {"confidence": float(confidence)}
    for key in (
        "expected_rsrq_db",
        "expected_rtt_ms",
        "expected_sinr_db",
        "current_rsrq_db",
        "current_rtt_ms",
        "current_sinr_db",
        "rsrq_delta_db",
        "rtt_delta_ms",
        "jitter_pct",
    ):
        if key in state:
            out[key] = state[key]
        elif key in calibration:
            out[key] = calibration[key]
    return out


def _first_float(*values: Any) -> float:
    for value in values:
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return bool(value)


__all__ = [
    "CalibrationFactConfidence",
    "calibration_fact_confidence",
    "fact_confidence_level",
]
