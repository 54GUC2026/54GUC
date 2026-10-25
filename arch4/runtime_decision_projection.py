"""Bounded public Decision read model.

The mutable Brain, SmartLoop, learning and history owners remain authoritative
for their domains.  This module is the one explicit boundary adapter that
projects their declared operator/policy fields into ``REGISTER_DECISION``.
It never retains their complete payloads.
"""

from __future__ import annotations

from typing import Any, Mapping

from arch4.payload_budget import retained_payload_bytes


SCHEMA = "runtime_decision_projection.v1"
OWNER = "decision_runtime_projection"
MAX_RUNTIME_DECISION_PROJECTION_BYTES = 64 * 1024
BOUNCER_PROJECTION_SCHEMA = "brain_bouncer_projection.v1"
BOUNCER_PROJECTION_OWNER = "arch4.engine_runtime_status"
BOUNCER_SOURCE_OWNER = "arch4.core.bouncer"
BOUNCER_SAFETY_SCHEMA = "runtime_bouncer_safety_projection.v1"
_BOUNCER_SAFETY_FIELDS = frozenset(
    {
        "schema",
        "owner",
        "source_owner",
        "state",
        "tripped",
        "trip_reason",
        "trip_source",
        "reentry_state",
        "reentry_condition",
        "hard_stop_active",
        "hard_stop_epoch",
        "hard_stop_source_generation",
        "safety_available",
    }
)

_SECTION_FIELDS: Mapping[str, frozenset[str]] = {
    "action_availability": frozenset(
        {
            "radio_stability_hold_active",
            "radio_stability_hold_remaining_s",
            "smart_loop_roi_observation_active",
            "smart_loop_roi_observation_remaining_s",
            "normal_policy_safety",
            "ca_recovery_trend",
            "rtt_warning_ms",
            "bouncer_safety",
        }
    ),
    "prediction": frozenset(
        {
            "prediction_posture",
            "prediction_modifier",
            "prediction_confidence_label",
            "evidence_count",
            "slots",
            "ready_source",
            "predictive_history_prior",
        }
    ),
    "smart_loop": frozenset(
        {
            "status",
            "confidence",
            "evidence",
            "intent",
            "selected_action",
            "candidate_count",
            "blockers",
            "slots_window",
            "prediction_posture",
            "prior",
            "brain_state",
            "mode",
            "budget",
            "bouncer",
            "coverage",
            "gate_review",
            "conclusion",
            "reason",
        }
    ),
    "learning": frozenset(
        {
            "status",
            "evidence_count",
            "contexts_seen",
            "recent_warning_rate",
            "recent_observe_monitor_rate",
            "recommendation",
            "confidence",
            "candidate_count",
            "blocked_context_updates",
            "recent_rejected_rate",
            "blockers",
            "reason",
        }
    ),
    "history": frozenset(
        {
            "import_status",
            "prior_source",
            "predictive_window",
            "soak_status",
            "history_window",
            "fact_count",
            "observation_count",
            "brain_influence",
            "matched_tier",
            "channel",
            "build_age",
            "merged_packs",
            "auto_import_status",
        }
    ),
    "dashboard": frozenset(
        {
            "mode",
            "operating_stance",
            "prediction_gate_summary",
            "prediction_confidence_label",
            "ticks_since_last_action",
            "last_outcome_code",
            "recent_action_count",
            "sample_count",
            "profile_confidence",
            "calibration_state",
            "calibration_freshness",
        }
    ),
}

_TOP_LEVEL_FIELDS = frozenset({"schema", "owner", *_SECTION_FIELDS})


class RuntimeDecisionProjectionError(ValueError):
    """Raised when an undeclared Decision read-model shape is submitted."""


def build_runtime_decision_projection(
    status: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Project the current owner publications into one bounded read model."""

    payload = _mapping(status)
    brain = _mapping(payload.get("brain"))
    vectors = _mapping(payload.get("brain_vectors"))
    strategy = _mapping(payload.get("self_learning_strategy"))
    authority = _mapping(payload.get("smart_loop_authority"))
    prediction = _mapping(vectors.get("prediction_vector"))
    learning_vector = _mapping(vectors.get("learning_vector"))
    brain_tsar = _mapping(vectors.get("brain_tsar"))
    constraint = _mapping(vectors.get("constraint_vector"))
    brain_bouncer = _mapping(payload.get("brain_bouncer"))
    bouncer_safety: dict[str, Any] = {}
    if brain_bouncer:
        if (
            str(brain_bouncer.get("schema") or "")
            != BOUNCER_PROJECTION_SCHEMA
            or str(brain_bouncer.get("owner") or "")
            != BOUNCER_PROJECTION_OWNER
            or str(brain_bouncer.get("source_owner") or "")
            != BOUNCER_SOURCE_OWNER
        ):
            raise RuntimeDecisionProjectionError(
                "canonical_brain_bouncer_projection_required"
            )
        latch = _mapping(brain_bouncer.get("hard_stop_latch"))
        bouncer_safety = {
            "schema": BOUNCER_SAFETY_SCHEMA,
            "owner": OWNER,
            "source_owner": BOUNCER_SOURCE_OWNER,
            "state": str(brain_bouncer.get("state") or ""),
            "tripped": bool(brain_bouncer.get("tripped", False)),
            "trip_reason": str(brain_bouncer.get("trip_reason") or ""),
            "trip_source": str(brain_bouncer.get("trip_source") or ""),
            "reentry_state": str(
                brain_bouncer.get("reentry_state") or ""
            ),
            "reentry_condition": str(
                brain_bouncer.get("reentry_condition") or ""
            ),
            "hard_stop_active": bool(latch.get("active", False)),
            "hard_stop_epoch": int(latch.get("epoch", 0) or 0),
            "hard_stop_source_generation": int(
                latch.get("source_generation", 0) or 0
            ),
            "safety_available": bool(
                latch.get("safety_available", True)
            ),
        }
        _validate_bouncer_safety_projection(bouncer_safety)
    action_budget = _mapping(constraint.get("action_budget"))
    bandit = _mapping(strategy.get("bandit_decision"))
    strategy_availability = _mapping(strategy.get("action_availability"))
    strategy_state = _mapping(strategy.get("state"))
    recent_windows = _mapping(strategy_state.get("recent_windows"))
    brain_posture = _mapping(brain.get("posture"))
    normal_safety = _mapping(brain.get("normal_policy_safety"))
    ca_recovery = _mapping(brain.get("ca_recovery_trend"))
    threshold_hard = _mapping(
        _mapping(brain.get("threshold_authority")).get("hard")
    )
    predictive_prior = _mapping(prediction.get("predictive_history_prior"))
    beta_soak = _mapping(payload.get("beta_soak"))
    candidate_projection = _mapping(
        payload.get("smart_loop_candidate_projection")
    )
    learning_quality = _mapping(candidate_projection.get("learning_quality"))
    calibration_vector = _mapping(vectors.get("calibration_vector"))

    action_availability = _present(
        radio_stability_hold_active=brain.get(
            "radio_stability_hold_active"
        ),
        radio_stability_hold_remaining_s=brain.get(
            "radio_stability_hold_remaining_s"
        ),
        smart_loop_roi_observation_active=brain.get(
            "smart_loop_roi_observation_active"
        ),
        smart_loop_roi_observation_remaining_s=brain.get(
            "smart_loop_roi_observation_remaining_s"
        ),
        normal_policy_safety=_named(
            normal_safety,
            ("throughput_hold", "incident_class", "known_outage_hold"),
        ),
        ca_recovery_trend=_named(ca_recovery, ("hold_restore_ca",)),
        rtt_warning_ms=threshold_hard.get("rtt_warning_ms"),
        bouncer_safety=bouncer_safety,
    )
    prediction_projection = _present(
        prediction_posture=prediction.get("prediction_posture"),
        prediction_modifier=prediction.get("prediction_modifier"),
        prediction_confidence_label=prediction.get(
            "prediction_confidence_label"
        ),
        evidence_count=prediction.get("evidence_count"),
        slots=prediction.get("slots"),
        ready_source=prediction.get("ready_source"),
        predictive_history_prior=_named(
            predictive_prior,
            (
                "status",
                "fact_count",
                "observation_count",
                "runtime_influence_kind",
                "scope_tiers",
                "auto_import_status",
            ),
        ),
    )
    smart_loop = _present(
        status=authority.get("status"),
        confidence=bandit.get("confidence"),
        evidence=bandit.get("evidence_count"),
        intent=bandit.get("decision_semantic"),
        selected_action=bandit.get("selected_action"),
        candidate_count=authority.get("candidate_count"),
        blockers=strategy_availability.get("reasons"),
        slots_window=recent_windows.get("observation_count"),
        prediction_posture=bandit.get("decision_kind"),
        prior=_mapping(strategy.get("inputs")).get("self_learning"),
        brain_state=brain.get("state"),
        mode=brain_tsar.get("mode"),
        budget=action_budget.get("mode"),
        bouncer=(bouncer_safety.get("state") if bouncer_safety else None),
        coverage=brain_tsar.get("operating_stance"),
        gate_review=brain_tsar.get("summary"),
        conclusion=brain_tsar.get("operating_stance_summary"),
        reason=authority.get("reason"),
    )
    # ``prior`` is a status field, never the complete SmartLoop input package.
    if isinstance(smart_loop.get("prior"), Mapping):
        smart_loop["prior"] = str(
            _mapping(smart_loop["prior"]).get("status") or ""
        )
    learning = _present(
        status=learning_vector.get("status"),
        evidence_count=learning_vector.get("evidence_count"),
        contexts_seen=learning_vector.get("contexts_seen"),
        recent_warning_rate=learning_vector.get("recent_warning_rate"),
        recent_observe_monitor_rate=learning_vector.get(
            "recent_observe_monitor_rate"
        ),
        recommendation=learning_vector.get("recommendation"),
        confidence=learning_vector.get("confidence"),
        candidate_count=authority.get("candidate_count"),
        blocked_context_updates=learning_vector.get("blocked_context_updates"),
        recent_rejected_rate=learning_vector.get("recent_rejected_rate"),
        blockers=learning_vector.get("blockers"),
        reason=authority.get("reason"),
    )
    continuity_source = str(
        beta_soak.get("continuity_source_version") or ""
    ).strip()
    prior_status = str(predictive_prior.get("status") or "").strip().lower()
    prior_present = prior_status not in {"", "missing", "unavailable"}
    history = _present(
        import_status=predictive_prior.get("status"),
        prior_source=continuity_source or ("Imported pack" if prior_present else "N/A"),
        predictive_window=prediction.get("slots"),
        soak_status=(beta_soak.get("status_label") or "N/A"),
        history_window=(
            f"{int(beta_soak.get('history_window_days') or 0)} / "
            f"{int(beta_soak.get('history_target_days') or 0)} days"
        ),
        fact_count=predictive_prior.get("fact_count"),
        observation_count=predictive_prior.get("observation_count"),
        brain_influence=predictive_prior.get("runtime_influence_kind"),
        matched_tier=predictive_prior.get("scope_tiers"),
        channel=(
            str(beta_soak.get("release_channel") or "mainline")
            .strip()
            .lower()
            or "mainline"
        ),
        build_age=f"{int(beta_soak.get('build_age_days') or 0)} days",
        merged_packs=int(beta_soak.get("continuity_source_stage_count") or 0),
        auto_import_status=predictive_prior.get("auto_import_status"),
    )
    dashboard = _present(
        mode=brain_tsar.get("mode"),
        operating_stance=brain_tsar.get("operating_stance"),
        prediction_gate_summary=brain_tsar.get("prediction_gate_summary"),
        prediction_confidence_label=brain_tsar.get(
            "prediction_confidence_label"
        ),
        ticks_since_last_action=brain_posture.get("ticks_since_last_action"),
        last_outcome_code=brain_posture.get("last_outcome_code"),
        recent_action_count=learning_quality.get("recent_action_count"),
        sample_count=learning_quality.get("sample_count"),
        profile_confidence=learning_quality.get("profile_confidence"),
        calibration_state=calibration_vector.get("state"),
        calibration_freshness=calibration_vector.get("freshness"),
    )
    return normalize_runtime_decision_projection(
        {
            "schema": SCHEMA,
            "owner": OWNER,
            "action_availability": action_availability,
            "prediction": prediction_projection,
            "smart_loop": smart_loop,
            "learning": learning,
            "history": history,
            "dashboard": dashboard,
        }
    )


def normalize_runtime_decision_projection(value: Any) -> dict[str, Any]:
    """Validate and freeze one exact Decision read-model publication."""

    if not isinstance(value, Mapping):
        raise RuntimeDecisionProjectionError(
            "runtime_decision_projection_mapping_required"
        )
    row = dict(value)
    if str(row.get("schema") or "") != SCHEMA:
        raise RuntimeDecisionProjectionError(
            "runtime_decision_projection_schema_mismatch"
        )
    if str(row.get("owner") or "") != OWNER:
        raise RuntimeDecisionProjectionError(
            "runtime_decision_projection_owner_mismatch"
        )
    unknown = sorted(set(row) - _TOP_LEVEL_FIELDS)
    if unknown:
        raise RuntimeDecisionProjectionError(
            "runtime_decision_projection_unknown_fields:" + ",".join(unknown)
        )
    out: dict[str, Any] = {"schema": SCHEMA, "owner": OWNER}
    for section, allowed in _SECTION_FIELDS.items():
        raw = row.get(section)
        if not isinstance(raw, Mapping):
            raise RuntimeDecisionProjectionError(
                f"runtime_decision_projection_section_invalid:{section}"
            )
        unknown_section = sorted(set(raw) - allowed)
        if unknown_section:
            raise RuntimeDecisionProjectionError(
                "runtime_decision_projection_unknown_section_fields:"
                f"{section}:" + ",".join(unknown_section)
            )
        normalized_section = _freeze(raw)
        if section == "action_availability" and "bouncer_safety" in raw:
            _validate_bouncer_safety_projection(raw.get("bouncer_safety"))
        out[section] = normalized_section
    retained_payload_bytes(
        out,
        max_bytes=MAX_RUNTIME_DECISION_PROJECTION_BYTES,
        label="runtime Decision projection",
    )
    return out


def _present(**values: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in values.items():
        if value is None:
            continue
        if isinstance(value, Mapping) and not value:
            continue
        out[name] = _freeze(value)
    return out


def _validate_bouncer_safety_projection(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise RuntimeDecisionProjectionError(
            "runtime_bouncer_safety_projection_mapping_required"
        )
    row = dict(value)
    if set(row) != _BOUNCER_SAFETY_FIELDS:
        raise RuntimeDecisionProjectionError(
            "runtime_bouncer_safety_projection_fields_invalid"
        )
    if (
        row.get("schema") != BOUNCER_SAFETY_SCHEMA
        or row.get("owner") != OWNER
        or row.get("source_owner") != BOUNCER_SOURCE_OWNER
    ):
        raise RuntimeDecisionProjectionError(
            "runtime_bouncer_safety_projection_contract_invalid"
        )
    if bool(row.get("hard_stop_active")) and (
        str(row.get("state") or "") != "blocked"
        or not bool(row.get("tripped"))
        or not str(row.get("trip_reason") or "")
    ):
        raise RuntimeDecisionProjectionError(
            "runtime_bouncer_safety_projection_latch_state_invalid"
        )


def _named(value: Any, names: tuple[str, ...]) -> dict[str, Any]:
    row = _mapping(value)
    return {name: _freeze(row[name]) for name in names if name in row}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _freeze(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_freeze(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_freeze(item) for item in sorted(value, key=repr)]
    return value


__all__ = [
    "BOUNCER_PROJECTION_OWNER",
    "BOUNCER_PROJECTION_SCHEMA",
    "BOUNCER_SAFETY_SCHEMA",
    "BOUNCER_SOURCE_OWNER",
    "MAX_RUNTIME_DECISION_PROJECTION_BYTES",
    "OWNER",
    "RuntimeDecisionProjectionError",
    "SCHEMA",
    "build_runtime_decision_projection",
    "normalize_runtime_decision_projection",
]
