"""Clocked calibration and learning runtime contract.

This module is the boundary between slow calibration/learning work and the
rest of runtime. Modem/tick code feeds admissible samples into this contract;
consumers read the published surface instead of reshaping calibration,
learning, workflow, and gate dictionaries on their own.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from arch4.brain.calibration_contract import (
    build_calibration_observation,
    calibration_observation_error,
)
from arch4.runtime_boundary_contracts import LearningGateValue
from arch4.runtime_faults import record_suppressed_fault


CALIBRATION_LEARNING_SCHEMA = "calibration_learning_status_v1"
CALIBRATION_LEARNING_FEED_SCHEMA = "calibration_learning_feed_v1"
CALIBRATION_LEARNING_OWNER = "calibration_learning_authority"
CALIBRATION_LEARNING_FEEDER = "calibration_learning_feeder"
CALIBRATION_LEARNING_CONSUMER = "calibration_authority"
CALIBRATION_LEARNING_LANE = "slow_learning_calibration"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value or {}) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return bool(default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _compact_context(calibration: Mapping[str, Any], authority: Mapping[str, Any]) -> dict[str, Any]:
    band = _int(
        calibration.get("current_context_band")
        or calibration.get("serving_band")
        or authority.get("active_context_band")
        or authority.get("candidate_context_band")
    )
    cell = _int(
        calibration.get("current_context_cell")
        or calibration.get("pci")
        or authority.get("active_context_cell")
        or authority.get("candidate_context_cell")
    )
    key = _text(calibration.get("current_context_key") or authority.get("active_context_key"))
    if not key and band > 0 and cell > 0:
        key = f"{band}|{cell}"
    label = _text(calibration.get("current_context_label") or authority.get("active_context_label"))
    if not label and band > 0 and cell > 0:
        label = f"B{band} / PCI {cell}"
    return {
        "band": band,
        "cell": cell,
        "key": key,
        "label": label,
    }


def build_calibration_learning_feed(
    *,
    sample: Mapping[str, Any] | None,
    calibration_status: Mapping[str, Any] | None = None,
    learning_gate: Mapping[str, Any] | None = None,
    now: float | None = None,
    lane: str = CALIBRATION_LEARNING_LANE,
    source: str = "engine_tick_state",
    identity_code: str = "",
    identity_status: str = "",
    identity_catalog_version: str = "",
    identity_catalog_checksum: str = "",
    session_scope_key: str = "",
    source_epoch: str = "",
    provider_plmn: str = "",
    access_type_id: str = "",
    sim_scope_hash: str = "",
    site_scope_id: str = "",
    action_shape_key: str = "",
    generation_confirmed: bool = False,
) -> dict[str, Any]:
    """Return an explicit feeder envelope for the calibration consumer."""

    ts = float(time.time() if now is None else now)
    gate_value = LearningGateValue.from_mapping(learning_gate)
    gate = gate_value.as_dict()
    status = _mapping(calibration_status)
    payload = _mapping(sample)
    learning_allowed = gate_value.learning_allowed
    sample_stale = gate_value.sample_stale
    sample_fresh = _bool(payload.get("sample_fresh"), False)
    observation = build_calibration_observation(
        sample=payload,
        calibration_status=status,
        learning_gate=gate,
        identity_code=identity_code,
        identity_status=identity_status,
        identity_catalog_version=identity_catalog_version,
        identity_catalog_checksum=identity_catalog_checksum,
        session_scope_key=session_scope_key,
        source_epoch=source_epoch,
        provider_plmn=provider_plmn,
        access_type_id=access_type_id,
        sim_scope_hash=sim_scope_hash,
        site_scope_id=site_scope_id,
        action_shape_key=action_shape_key,
        generation_confirmed=generation_confirmed,
        now=ts,
    )
    block_reason = _text(observation.get("reason"))
    accepted = bool(payload) and bool(observation.get("accepted", False))
    if not payload:
        accepted = False
        block_reason = _text(gate_value.block_reason) or "empty_sample"
    return {
        "schema": CALIBRATION_LEARNING_FEED_SCHEMA,
        "owner": CALIBRATION_LEARNING_FEEDER,
        "consumer": CALIBRATION_LEARNING_CONSUMER,
        "lane": str(lane or CALIBRATION_LEARNING_LANE),
        "source": str(source or ""),
        "clocked": True,
        "ts": ts,
        "accepted": bool(accepted),
        "reason": "" if accepted else block_reason,
        "learning_allowed": bool(learning_allowed),
        "sample_stale": bool(sample_stale),
        "sample_fresh": bool(sample_fresh),
        "learning_gate": gate,
        "context": _compact_context(status, {}),
        "observation": observation if accepted else {},
        "observation_id": _text(observation.get("observation_id")),
        "evidence_scope": _mapping(observation.get("evidence_scope")),
        "sample": payload if accepted else {},
        "sample_keys": sorted(str(key) for key in payload.keys()) if payload else [],
    }


def compact_calibration_learning_feed_status(feed: Mapping[str, Any] | None) -> dict[str, Any]:
    data = _mapping(feed)
    sample = _mapping(data.get("sample"))
    return {
        "schema": _text(data.get("schema")) or CALIBRATION_LEARNING_FEED_SCHEMA,
        "owner": _text(data.get("owner")) or CALIBRATION_LEARNING_FEEDER,
        "consumer": _text(data.get("consumer")) or CALIBRATION_LEARNING_CONSUMER,
        "lane": _text(data.get("lane")) or CALIBRATION_LEARNING_LANE,
        "source": _text(data.get("source")),
        "clocked": _bool(data.get("clocked"), True),
        "ts": _float(data.get("ts")),
        "accepted": _bool(data.get("accepted"), False),
        "reason": _text(data.get("reason")),
        "learning_allowed": _bool(data.get("learning_allowed"), True),
        "sample_stale": _bool(data.get("sample_stale"), False),
        "sample_fresh": _bool(data.get("sample_fresh"), True),
        "context": _mapping(data.get("context")),
        "observation_id": _text(data.get("observation_id")),
        "evidence_scope_id": _text(
            _mapping(data.get("evidence_scope")).get("evidence_scope_id")
        ),
        "sample_key_count": len(sample) if sample else len(list(data.get("sample_keys", []) or [])),
    }


def consume_calibration_learning_feed(
    feed: Mapping[str, Any] | None,
    *,
    calibration_authority: Any = None,
    calibration_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Deliver an admitted feed envelope to the calibration consumer.

    The caller owns cadence and threading. This function owns the consumer
    boundary, so tick code does not directly decide how calibration authority
    receives samples.
    """

    data = _mapping(feed)
    status = compact_calibration_learning_feed_status(data)
    status["consumer_available"] = calibration_authority is not None
    status["background_noted"] = False
    status["sample_collected"] = False
    status["consumer_error"] = ""
    if calibration_authority is None:
        if status.get("accepted"):
            status["accepted"] = False
            status["reason"] = "consumer_unavailable"
        return status
    if not _bool(data.get("accepted"), False):
        return status
    try:
        observation = _mapping(data.get("observation"))
        observation_error = calibration_observation_error(observation)
        if observation_error:
            status["accepted"] = False
            status["reason"] = observation_error
            return status
        collect_fn = getattr(calibration_authority, "consume_calibration_observation", None)
        if not callable(collect_fn):
            status["accepted"] = False
            status["reason"] = "calibration_observation_consumer_missing"
            return status
        result = collect_fn(observation)
        result = _mapping(result)
        status["background_noted"] = _bool(result.get("background_noted"), False)
        status["sample_collected"] = _bool(result.get("accepted"), False)
        if not status["sample_collected"]:
            status["accepted"] = False
            status["reason"] = _text(result.get("reason")) or "calibration_observation_rejected"
    except Exception as exc:
        status["consumer_error"] = str(exc or "")
        status = record_suppressed_fault(
            status,
            category="learning",
            source="calibration_learning_consumer",
            error_type=type(exc).__name__,
            detail=str(exc),
            metadata={
                "boundary": "calibration_learning_runtime",
                "consumer": CALIBRATION_LEARNING_CONSUMER,
                "feed_schema": str(data.get("schema") or ""),
            },
        )
    return status


def _domain_rows(
    *,
    surface: Mapping[str, Any],
    learning_quality: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    quality = _mapping(learning_quality)
    local_baselines = _mapping(surface.get("local_baselines"))
    candidate = _mapping(surface.get("candidate"))
    policy = _mapping(surface.get("runtime_policy_publication"))
    return {
        "rf": {
            "owner": "calibration_authority",
            "state": "active" if _int(local_baselines.get("active_profile_version")) > 0 else "collecting",
            "published": bool(surface),
            "sample_count": _int(candidate.get("session_sample_count") or local_baselines.get("sample_count")),
            "confidence": _float(_mapping(surface.get("trust_scores")).get("calibration_fact")),
        },
        "congestion": {
            "owner": "arch4.smart_loop.closed_loop",
            "state": "observing" if quality else "unknown",
            "published": bool(quality),
            "source": "smart_loop.candidate_projection.learning_quality" if quality else "",
        },
        "provider_time_behavior": {
            "owner": _text(policy.get("owner")) or "arch4.brain.calibration_publication",
            "state": _text(policy.get("status")) or "inactive",
            "published": _bool(policy.get("published"), False),
            "source": _text(policy.get("source")),
        },
        "modem_protocol": {
            "owner": "modem_live",
            "state": "contract_reserved",
            "published": False,
            "source": "",
        },
        "device_static_profile": {
            "owner": "modem_profile",
            "state": "contract_reserved",
            "published": False,
            "source": "",
        },
    }


def _surface_context(surface: Mapping[str, Any]) -> dict[str, Any]:
    current = _mapping(surface.get("current_context"))
    baseline = _mapping(surface.get("baseline_context"))
    local = _mapping(surface.get("local_baselines"))
    band = _int(current.get("band") or baseline.get("band") or local.get("context_band"))
    cell = _int(current.get("cell") or baseline.get("cell") or local.get("context_cell"))
    key = _text(current.get("key") or baseline.get("key") or local.get("context_key"))
    if not key and band > 0 and cell > 0:
        key = f"{band}|{cell}"
    label = _text(current.get("label") or baseline.get("label") or local.get("context_label"))
    if not label and band > 0 and cell > 0:
        label = f"B{band} / PCI {cell}"
    return {"band": band, "cell": cell, "key": key, "label": label}


def build_calibration_learning_surface(
    *,
    calibration_surface: Mapping[str, Any],
    learning_authority: Mapping[str, Any] | None = None,
    smart_loop_candidate_projection: Mapping[str, Any] | None = None,
    learning_gate: Mapping[str, Any] | None = None,
    radio_mutation_window: Mapping[str, Any] | None = None,
    runtime_clock: Mapping[str, Any] | None = None,
    last_feed: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Publish one first-class calibration/learning runtime surface."""

    ts = float(time.time() if now is None else now)
    surface = _mapping(calibration_surface)
    if surface.get("schema") != "calibration_surface.v1" or surface.get("owner") != "calibration_authority":
        raise RuntimeError("calibration_surface_required")
    learning = _mapping(learning_authority)
    smart_loop_projection = _mapping(smart_loop_candidate_projection)
    learning_quality = _mapping(smart_loop_projection.get("learning_quality"))
    gate_value = LearningGateValue.from_mapping(learning_gate)
    gate = gate_value.as_dict()
    mutation = _mapping(radio_mutation_window)
    clock = _mapping(runtime_clock)
    feed_status = compact_calibration_learning_feed_status(last_feed)
    learning_allowed = gate_value.learning_allowed
    sample_stale = gate_value.sample_stale
    block_reason = _text(gate_value.block_reason)
    if not learning_allowed and not block_reason:
        block_reason = "learning_gate_blocked"
    context = _surface_context(surface)
    candidate = _mapping(surface.get("candidate"))
    local_baselines = _mapping(surface.get("local_baselines"))
    workflow = _mapping(surface.get("workflow"))
    setup = _mapping(surface.get("setup"))
    trust_scores = _mapping(surface.get("trust_scores"))
    candidate_samples = _int(candidate.get("session_sample_count") or local_baselines.get("sample_count"))
    min_samples = _int(candidate.get("session_min_samples"))
    progress = _int(candidate.get("session_progress_pct"))
    if progress <= 0 and min_samples > 0:
        progress = max(0, min(100, int((candidate_samples / float(min_samples)) * 100.0)))
    handoff = _mapping(surface.get("handoff"))
    baseline_contract = _mapping(surface.get("baseline_contract"))
    current_delta = _mapping(surface.get("current_delta"))
    baseline_state = _text(
        handoff.get("baseline_state")
        or baseline_contract.get("state")
        or ("VERIFIED_BASELINE" if _bool(local_baselines.get("baseline_ready"), False) else "NO_BASELINE")
    )
    baseline_valid = _bool(
        handoff.get("baseline_valid"),
        _bool(baseline_contract.get("valid"), baseline_state == "VERIFIED_BASELINE"),
    )
    baseline_learning = _bool(
        handoff.get("baseline_learning"),
        _bool(baseline_contract.get("learning"), baseline_state == "LEARNING_BASELINE"),
    )
    session_state = _text(
        handoff.get("session_state")
        or (
            "READY"
            if baseline_valid
            else "RUNNING"
            if baseline_learning or candidate_samples > 0
            else "ABSENT"
        )
    )
    delta_state = _text(handoff.get("delta_state") or current_delta.get("state") or "UNKNOWN")
    delta_valid = _bool(handoff.get("delta_valid"), _bool(current_delta.get("valid"), False))
    fact_package = _mapping(handoff.get("calibration_fact_package"))
    confidence = _float(
        fact_package.get("confidence")
        or trust_scores.get("calibration_fact")
    )
    status = "blocked" if not learning_allowed else (_text(surface.get("state")) or "unknown")
    return {
        "schema": CALIBRATION_LEARNING_SCHEMA,
        "owner": CALIBRATION_LEARNING_OWNER,
        "lane": CALIBRATION_LEARNING_LANE,
        "clocked": True,
        "ts": ts,
        "state": status,
        "learning_allowed": bool(learning_allowed),
        "blocked": not bool(learning_allowed),
        "block_reason": block_reason,
        "sample_trust": "stale" if sample_stale else ("transition" if _bool(mutation.get("active"), False) else "derived"),
        "sample_stale": bool(sample_stale),
        "context": context,
        "calibration": {
            "enabled": _text(surface.get("state")) != "disabled",
            "phase": _text(workflow.get("phase") or candidate.get("workflow_mode")),
            "phase_reason": _text(workflow.get("phase_reason")),
            "active_profile_id": _text(local_baselines.get("active_profile_id")),
            "active_profile_version": _int(local_baselines.get("active_profile_version")),
            "candidate_profile_id": _text(candidate.get("profile_id")),
            "candidate_workflow_mode": _text(candidate.get("workflow_mode")),
            "sample_count": candidate_samples,
            "min_samples": min_samples,
            "progress_pct": progress,
            "confidence": confidence,
            "fact_package": fact_package,
            "self_learning_input_strength": _float(fact_package.get("self_learning_input_strength")),
            "crystal_ball_input_strength": _float(fact_package.get("crystal_ball_input_strength")),
            "baseline_ready": _bool(local_baselines.get("baseline_ready"), False),
            "needs_calibration": _bool(setup.get("recommended_action")),
            "last_collection_reason": _text(candidate.get("session_note")),
        },
        "baseline_contract": {
            "state": baseline_state,
            "valid": bool(baseline_valid),
            "learning": bool(baseline_learning),
            "session_state": session_state,
            "sample_count": candidate_samples,
            "context": context,
        },
        "current_delta": {
            "state": delta_state,
            "valid": bool(delta_valid),
            "values": _mapping(current_delta.get("values") or handoff.get("delta_values")),
        },
        "learning": {
            "contexts_seen": _int(learning.get("contexts_seen")),
            "context_updates": _int(learning.get("context_updates")),
            "blocked_context_updates": _int(learning.get("blocked_context_updates")),
            "last_context": _mapping(learning.get("last_context")),
            "learning_quality": learning_quality,
        },
        "evidence": {
            "enough_samples": bool(min_samples > 0 and candidate_samples >= min_samples),
            "candidate_sample_count": candidate_samples,
            "active_context_sample_count": _int(local_baselines.get("context_sample_count")),
            "confidence": confidence,
            "fact_package_status": "active"
            if fact_package.get("schema") == "calibration_fact_package.v1"
            else "missing",
            "sample_status": "enough_samples" if min_samples > 0 and candidate_samples >= min_samples else "insufficient_samples",
            "trust_level": _text(surface.get("trust_level")),
        },
        "domains": _domain_rows(
            surface=surface,
            learning_quality=learning_quality,
        ),
        "feed": feed_status,
        "clock": {
            "clock_ms": _int(clock.get("clock_ms")),
            "sequence": _int(clock.get("sequence") or clock.get("tick_index") or clock.get("tick")),
            "clock_authority": _text(clock.get("clock_authority") or "wall_clock"),
            "lane": CALIBRATION_LEARNING_LANE,
            "last_due_ts": _calibration_clock_job(clock).get("last_due_ts"),
            "last_finish_ts": _calibration_clock_job(clock).get("last_finish_ts"),
            "wall_age_s": _calibration_clock_job(clock).get("wall_age_s"),
            "fresh_by_clock": _calibration_clock_job(clock).get("fresh_by_clock"),
            "run_count": _int(_calibration_clock_job(clock).get("run_count")),
        },
        "operator_summary": _operator_summary(
            status=status,
            learning_allowed=learning_allowed,
            block_reason=block_reason,
            candidate_samples=candidate_samples,
            min_samples=min_samples,
            active_version=_int(local_baselines.get("active_profile_version")),
        ),
    }


def _calibration_clock_job(clock: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(_mapping(clock.get("jobs")).get("brain:calibration_learning_lane"))


def _operator_summary(
    *,
    status: str,
    learning_allowed: bool,
    block_reason: str,
    candidate_samples: int,
    min_samples: int,
    active_version: int,
) -> str:
    if not learning_allowed:
        return f"Calibration learning paused: {block_reason or 'learning gate blocked'}."
    if active_version > 0:
        return f"Calibration profile active, version {active_version}."
    if candidate_samples > 0:
        if min_samples > 0:
            return f"Calibration collecting evidence: {candidate_samples}/{min_samples} samples."
        return f"Calibration collecting evidence: {candidate_samples} samples."
    if status == "disabled":
        return "Calibration is disabled."
    return "Calibration has no trusted evidence yet."
