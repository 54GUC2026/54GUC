"""
Comparable Brain vector contract helpers.

The four vectors remain advisory inputs. This module only normalizes their
export shape so TSAR can compare evidence, blockers, recommendations, and
candidate actions without each vector speaking a different dialect.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

from arch4.brain.candidate_action_contract import stamp_candidate_execution_contract


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return _lower(value) in {"1", "true", "yes", "on", "y"}


def _dedupe_text(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _learning_feedback_blockers(kind: Any) -> list[str]:
    text = _text(kind)
    if not text or text == "warning":
        return []
    return [text]


def _learning_feedback_advisories(kind: Any) -> list[str]:
    return []


def _band_candidate(
    *,
    source: str,
    action: str,
    band: int,
    score: float,
    confidence: float,
    evidence_count: int,
    reason: str,
    rank: int,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "source": source,
        "action": action,
        "band": int(band),
        "bands": [int(band)] if int(band) > 0 else [],
        "score": round(float(score), 4),
        "confidence": round(max(0.0, min(1.0, float(confidence))), 3),
        "evidence_count": int(evidence_count),
        "reason": reason,
        "rank": int(rank),
    }
    if extra:
        payload.update(dict(extra))
    return stamp_candidate_execution_contract(payload)


def build_prediction_candidate_actions(
    *,
    prediction_vector: Mapping[str, Any],
    limit: int = 5,
) -> list[Dict[str, Any]]:
    """Build ranked Crystal Ball/history candidates from prediction payloads."""

    vector = _mapping(prediction_vector)
    advisory = _mapping(vector.get("crystal_ball_advisory"))
    candidates_raw = advisory.get("candidates")
    calibration_prediction = _calibration_prediction_support(vector)
    ranked: list[Dict[str, Any]] = []
    seen_bands: set[int] = set()

    rec = _mapping(advisory.get("recommendation"))
    if rec:
        band = _int(rec.get("band"))
        if band > 0:
            seen_bands.add(band)
            ranked.append(
                _band_candidate(
                    source="crystal_ball_recommendation",
                    action="prefer_band",
                    band=band,
                    score=_float(rec.get("score"), 0.0),
                    confidence=_prediction_confidence(
                        _float(rec.get("confidence"), 0.0) / 5.0,
                        calibration_prediction=calibration_prediction,
                    ),
                    evidence_count=_prediction_evidence_count(
                        _int(rec.get("confidence"), 0),
                        calibration_prediction=calibration_prediction,
                    ),
                    reason=_text(advisory.get("reason") or "crystal_ball_recommendation"),
                    rank=0,
                    extra={
                        "predicted_congestion": _float(rec.get("predicted_congestion"), 0.0),
                        "predicted_dl_mbps": _float(rec.get("predicted_dl_mbps"), 0.0),
                        "predicted_rtt_ms": _float(rec.get("predicted_rtt_ms"), 0.0),
                        "predicted_ca_rate": _float(rec.get("predicted_ca_rate"), 0.0),
                        "recommendation": "prefer",
                        "calibration_prediction_support": calibration_prediction,
                    },
                )
            )

    if isinstance(candidates_raw, list):
        for entry in candidates_raw:
            cand = _mapping(entry)
            band = _int(cand.get("band"))
            if band <= 0 or band in seen_bands:
                continue
            seen_bands.add(band)
            score = _float(cand.get("score"), 0.0)
            ranked.append(
                _band_candidate(
                    source="crystal_ball_ranked_candidate",
                    action="ranked_band",
                    band=band,
                    score=score,
                    confidence=_prediction_confidence(
                        _float(cand.get("confidence"), 0.0) / 5.0,
                        calibration_prediction=calibration_prediction,
                    ),
                    evidence_count=_prediction_evidence_count(
                        _int(cand.get("confidence"), 0),
                        calibration_prediction=calibration_prediction,
                    ),
                    reason=_text(advisory.get("reason") or "crystal_ball_ranked_history"),
                    rank=0,
                    extra={
                        "predicted_congestion": _float(cand.get("predicted_congestion"), 0.0),
                        "predicted_dl_mbps": _float(cand.get("predicted_dl_mbps"), 0.0),
                        "predicted_rtt_ms": _float(cand.get("predicted_rtt_ms"), 0.0),
                        "predicted_ca_rate": _float(cand.get("predicted_ca_rate"), 0.0),
                        "recommendation": "ranked",
                        "calibration_prediction_support": calibration_prediction,
                    },
                )
            )

    prior = _mapping(vector.get("predictive_history_prior"))
    if _bool(prior.get("brain_prior_active")):
        ranked.append(
            stamp_candidate_execution_contract({
                "source": "external_history_prior",
                "action": "seed_ltm_prediction",
                "band": 0,
                "bands": [],
                "score": 0.0,
                "confidence": 0.5 if _int(prior.get("observation_count"), 0) > 0 else 0.0,
                "evidence_count": _int(prior.get("observation_count"), 0),
                "reason": _text(prior.get("reason") or "external_history_available"),
                "rank": 0,
                "recommendation": "seed_history",
                "advisory_only": True,
                "direct_action_allowed": False,
                "direct_action_block_reason": "imported_history_advisory_only",
                "provenance": _text(prior.get("provenance") or prior.get("source") or "external_history_prior"),
                "identity_scope": _mapping(prior.get("identity_scope")),
                "freshness_s": _float(prior.get("freshness_s"), -1.0),
            })
        )

    if not ranked and _lower(advisory.get("reason")) == "current_band_not_predicted_congested":
        current = _mapping(advisory.get("current"))
        band = _int(current.get("band"))
        if band > 0:
            ranked.append(
                _band_candidate(
                    source="crystal_ball_current_lane",
                    action="stay_current",
                    band=band,
                    score=1.0,
                    confidence=_prediction_confidence(
                        _float(current.get("confidence"), 0.0) / 5.0,
                        calibration_prediction=calibration_prediction,
                    ),
                    evidence_count=_prediction_evidence_count(
                        _int(current.get("sample_count"), 0),
                        calibration_prediction=calibration_prediction,
                    ),
                    reason="current band is not predicted congested",
                    rank=0,
                    extra={
                        "recommendation": "stay",
                        "calibration_prediction_support": calibration_prediction,
                    },
                )
            )

    ranked.sort(
        key=lambda item: (
            item.get("action") == "prefer_band",
            item.get("action") == "stay_current",
            float(item.get("confidence", 0.0)),
            float(item.get("score", 0.0)),
            int(item.get("evidence_count", 0)),
        ),
        reverse=True,
    )
    out = ranked[: max(0, int(limit))]
    for idx, item in enumerate(out, start=1):
        item["rank"] = idx
        out[idx - 1] = stamp_candidate_execution_contract(item)
    return out


def _calibration_prediction_support(vector: Mapping[str, Any]) -> dict[str, Any]:
    package = _mapping(vector.get("calibration_fact_package"))
    valid = (
        package.get("schema") == "calibration_fact_package.v1"
        and package.get("owner") == "calibration_fact_confidence"
    )
    strength = _float(package.get("crystal_ball_input_strength"), 0.0) if valid else 0.0
    return {
        "schema": "calibration_prediction_support.v1",
        "source": "calibration_fact_package",
        "package_status": "active" if valid else "missing",
        "strength": max(0.0, min(1.0, strength)),
        "strong_enough_for_prediction": bool(package.get("strong_enough_for_prediction", False)) if valid else False,
        "strong_enough_for_preemption_facts": bool(
            package.get("strong_enough_for_preemption_facts", False)
        ) if valid else False,
        "missing_evidence": list(package.get("missing_evidence") or []) if valid else ["calibration_fact_package"],
    }


def _prediction_confidence(base: float, *, calibration_prediction: Mapping[str, Any]) -> float:
    current = max(0.0, min(1.0, float(base or 0.0)))
    strength = _float(calibration_prediction.get("strength"), 0.0)
    if bool(calibration_prediction.get("strong_enough_for_prediction", False)):
        return max(current, min(1.0, (0.70 * current) + (0.30 * strength), strength - 0.05))
    if calibration_prediction.get("package_status") == "active" and strength > 0.0:
        return min(current, max(0.0, (0.85 * current) + (0.15 * strength)))
    return current


def _prediction_evidence_count(base: int, *, calibration_prediction: Mapping[str, Any]) -> int:
    evidence = max(0, int(base or 0))
    if bool(calibration_prediction.get("strong_enough_for_preemption_facts", False)) and evidence > 0:
        return max(evidence, 2)
    return evidence


def build_constraint_action_budget(*, constraint_vector: Mapping[str, Any]) -> Dict[str, Any]:
    constraint = _mapping(constraint_vector)
    bouncer = _mapping(constraint.get("bouncer"))
    health = _mapping(constraint.get("health"))
    observe_only = _bool(constraint.get("observe_only"))
    planned_quiet = _bool(constraint.get("planned_radio_quiet_active"))
    supports = _bool(constraint.get("supports_active_control", True))
    health_observe = _lower(health.get("action_hint") or constraint.get("health_action_hint")) == "observe"
    bouncer_blocked = _lower(bouncer.get("state")) == "blocked"

    blocker = ""
    if observe_only:
        blocker = "observe_only_mode"
    elif planned_quiet:
        blocker = "planned_radio_quiet"
    elif not supports:
        blocker = "active_control_not_supported"
    elif bouncer_blocked:
        blocker = _text(bouncer.get("trip_reason") or "bouncer_blocked")
    elif health_observe:
        blocker = "health_observe_hold"

    if observe_only or planned_quiet or not supports or bouncer_blocked:
        mode = "blocked"
    elif blocker:
        mode = "reduced"
    else:
        mode = "available"

    return {
        "mode": mode,
        "safe_probe_allowed": mode == "available",
        "safe_recovery_allowed": mode in {"available", "reduced"} and not planned_quiet and not observe_only,
        "write_authority_required": True,
        "blocker": blocker,
        "reentry_condition": _text(bouncer.get("reentry_condition") or "not_applicable"),
        "planned_quiet_remaining_s": _float(constraint.get("planned_radio_quiet_remaining_s"), 0.0),
    }


def apply_common_vector_contracts(
    *,
    calibration_vector: Dict[str, Any],
    learning_vector: Dict[str, Any],
    prediction_vector: Dict[str, Any],
    constraint_vector: Dict[str, Any],
    smart_loop_candidate_projection: Mapping[str, Any],
    calibration_surface: Optional[Mapping[str, Any]] = None,
) -> None:
    """Mutate vector dicts to expose one comparable contract shape."""

    surface = _mapping(calibration_surface)
    trust_scores = _mapping(surface.get("trust_scores"))
    local_baselines = _mapping(surface.get("local_baselines"))
    action_family = _mapping(_mapping(surface.get("action_family_evidence")).get("rf_control"))
    fact_package = _mapping(calibration_vector.get("calibration_fact_package"))
    fact_package_valid = (
        fact_package.get("schema") == "calibration_fact_package.v1"
        and fact_package.get("owner") == "calibration_fact_confidence"
    )
    fact_confidence = _float(
        fact_package.get("confidence"),
        _float(calibration_vector.get("calibration_fact_confidence"), 0.0),
    )
    self_learning_input_strength = (
        _float(fact_package.get("self_learning_input_strength"), 0.0)
        if fact_package_valid
        else 0.0
    )
    crystal_ball_input_strength = (
        _float(fact_package.get("crystal_ball_input_strength"), 0.0)
        if fact_package_valid
        else 0.0
    )
    metric_trust = {
        "setup": _float(trust_scores.get("setup"), 0.0),
        "calibration_fact": _float(
            trust_scores.get("calibration_fact"),
            fact_confidence,
        ),
        "learning_input": _float(
            trust_scores.get("learning_input"),
            self_learning_input_strength,
        ),
        "prediction_input": _float(
            trust_scores.get("prediction_input"),
            crystal_ball_input_strength,
        ),
        "throughput_lane": _float(trust_scores.get("throughput_lane"), 0.0),
        "latency_lane": _float(trust_scores.get("latency_lane"), 0.0),
        "rf_lane": _float(trust_scores.get("rf_lane"), 0.0),
    }
    rf_trust = _float(metric_trust.get("rf_lane"), 0.0)
    latency_trust = _float(metric_trust.get("latency_lane"), 0.0)
    throughput_trust = _float(metric_trust.get("throughput_lane"), 0.0)
    ca_evidence_conf = _float(action_family.get("primary_metrics_confidence"), rf_trust)
    metric_trust.update(
        {
            "sinr": rf_trust,
            "rsrp": rf_trust,
            "rsrq": rf_trust,
            "rtt": latency_trust,
            "cqi_mcs": throughput_trust,
            "congestion": max(latency_trust, rf_trust),
            "ca_evidence": max(rf_trust, ca_evidence_conf),
        }
    )
    calibration_missing = _dedupe_text(action_family.get("primary_metrics_missing", []) or [])
    package_missing = (
        _dedupe_text(fact_package.get("missing_evidence", []) or [])
        if fact_package_valid
        else ["calibration_fact_package_missing"]
    )
    calibration_missing = _dedupe_text([*calibration_missing, *package_missing])
    calibration_blockers = calibration_missing if calibration_missing else []
    baseline_state = _text(calibration_vector.get("baseline_state")) or "NO_BASELINE"
    baseline_valid = _bool(calibration_vector.get("baseline_valid"))
    delta_valid = _bool(calibration_vector.get("delta_valid"))
    baseline_confidence = 1.0 if baseline_valid else 0.45 if baseline_state == "LEARNING_BASELINE" else 0.0
    if baseline_valid and not delta_valid:
        baseline_confidence = min(baseline_confidence, 0.75)
    calibration_vector.update(
        {
            "status": baseline_state,
            "confidence": baseline_confidence,
            "freshness_s": 0.0 if _lower(calibration_vector.get("freshness")) == "fresh" else -1.0,
            "evidence_count": _int(local_baselines.get("sample_count") or local_baselines.get("context_sample_count"), 0),
            "candidate_actions": [],
            "blockers": calibration_blockers,
            "missing_evidence": calibration_missing,
            "metric_trust": metric_trust,
            "fact_package_status": "active" if fact_package_valid else "missing",
            "fact_package": dict(fact_package) if fact_package_valid else {},
            "clean_for_learning": bool(fact_package.get("clean_for_learning", False)) if fact_package_valid else False,
            "strong_enough_for_prediction": bool(
                fact_package.get("strong_enough_for_prediction", False)
            ) if fact_package_valid else False,
            "strong_enough_for_preemption_facts": bool(
                fact_package.get("strong_enough_for_preemption_facts", False)
            ) if fact_package_valid else False,
            "reason": _text(calibration_vector.get("reason")),
        }
    )
    if fact_package_valid:
        prediction_vector["calibration_fact_package"] = dict(fact_package)
    else:
        prediction_vector.pop("calibration_fact_package", None)

    smart_loop_projection = _mapping(smart_loop_candidate_projection)
    smart_loop_projection_valid = (
        smart_loop_projection.get("schema") == "smart_loop.candidate_projection.v1"
        and smart_loop_projection.get("owner") == "arch4.smart_loop.closed_loop"
    )
    publication_status = _text(smart_loop_projection.get("status")) or "unavailable"
    publication_reason = _text(smart_loop_projection.get("reason"))
    if not smart_loop_projection_valid:
        publication_reason = publication_reason or "smart_loop_strategy_projection_unavailable"
    learning_quality = _mapping(smart_loop_projection.get("learning_quality"))
    learning_candidates = (
        list(smart_loop_projection.get("candidate_actions") or [])
        if smart_loop_projection_valid
        else []
    )
    learning_recommendation = _text(learning_candidates[0].get("action")) if learning_candidates else ""
    learning_feedback_kind = _text(learning_vector.get("referee_feedback_kind"))
    learning_blockers = _learning_feedback_blockers(learning_feedback_kind)
    learning_advisories = _learning_feedback_advisories(learning_feedback_kind)
    learning_reason = _text(learning_vector.get("referee_feedback_summary"))
    if learning_feedback_kind == "warning":
        learning_reason = ""
    learning_vector.update(
        {
            "status": "active" if _int(learning_vector.get("contexts_seen"), 0) > 0 else "empty",
            "confidence": _float(learning_quality.get("profile_confidence"), 0.0),
            "freshness_s": -1.0,
            "evidence_count": _int(
                learning_quality.get("sample_count"),
                _int(learning_vector.get("context_updates"), 0),
            ),
            "recommendation": learning_recommendation,
            "candidate_actions": learning_candidates,
            "learning_quality": dict(learning_quality),
            "blockers": learning_blockers,
            "advisories": learning_advisories,
            "referee_feedback_severity": "blocker" if learning_blockers else "advisory" if learning_advisories else "",
            "missing_evidence": []
            if learning_candidates
            else [publication_reason]
            if not smart_loop_projection_valid
            else ["learning_candidate_history"],
            "reason": learning_reason,
            "quality_trend": _text(learning_quality.get("quality_trend")),
            "recent_action_count": _int(learning_quality.get("recent_action_count"), 0),
            "profile_confidence": _float(learning_quality.get("profile_confidence"), 0.0),
            "candidate_register_status": "active" if smart_loop_projection_valid else publication_status,
            "candidate_register_reason": "" if smart_loop_projection_valid else publication_reason,
        }
    )

    prediction_candidates = build_prediction_candidate_actions(prediction_vector=prediction_vector)
    prediction_reason = _text(
        prediction_vector.get("prediction_gate_summary")
        or _mapping(prediction_vector.get("crystal_ball_advisory")).get("reason")
        or prediction_vector.get("feedback_summary")
    )
    prediction_vector.update(
        {
            "confidence": _float(prediction_vector.get("prediction_confidence_score"), 0.0),
            "freshness_s": -1.0,
            "evidence_count": _int(prediction_vector.get("slots"), 0)
            + _int(_mapping(prediction_vector.get("predictive_history_prior")).get("observation_count"), 0),
            "recommendation": _text(prediction_candidates[0].get("action")) if prediction_candidates else "",
            "candidate_actions": prediction_candidates,
            "blockers": _dedupe_text([prediction_vector.get("gate_reason"), prediction_vector.get("prediction_gate_summary")]),
            "missing_evidence": [] if prediction_candidates else _dedupe_text([prediction_reason or "prediction_candidate_history"]),
            "reason": prediction_reason,
        }
    )

    budget = build_constraint_action_budget(constraint_vector=constraint_vector)
    constraint_blockers = _dedupe_text([budget.get("blocker")])
    constraint_vector.update(
        {
            "confidence": 1.0,
            "freshness_s": 0.0,
            "evidence_count": 1,
            "recommendation": "allow_active_control" if budget["mode"] == "available" else "observe_monitor",
            "candidate_actions": [],
            "blockers": constraint_blockers,
            "missing_evidence": [],
            "reason": _text(budget.get("blocker")),
            "action_budget": budget,
        }
    )
