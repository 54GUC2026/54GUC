"""Typed producers for retained vB5.3 normal optimization behavior.

The old decision mixins combined observation, candidate construction and
selection.  This owner preserves their useful triggers and candidate shapes as
source facts only.  It cannot rank, select, acquire a lease, execute, grade,
write Memory or communicate with Modem Live.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Sequence

from arch4.runtime_decision_projection import (
    OWNER as DECISION_PROJECTION_OWNER,
    SCHEMA as DECISION_PROJECTION_SCHEMA,
)

from arch4.action_opportunity_source_facts import ActionOpportunitySourceFact
from arch4.smart_loop.normal_behavior_progress import NormalBehaviorProgress


OWNER = "arch4.smart_loop.normal_behavior_opportunities"
NORMAL_NEIGHBOR_MARGIN_DB = 6.0
RECOVERED_LANE_NEIGHBOR_MARGIN_DB = 12.0
NR_MIN_OBSERVATIONS = 150

_BEHAVIOR_IDS: tuple[str, ...] = (
    "working_restore_known_good_ca",
    "anchor_enforcement",
    "degraded_best_live_neighbor",
    "degraded_open_all_deployed_bands",
    "congested_neighbor_escape",
    "congested_restore_known_good_ca",
    "systematic_bad_rf_band_probe",
    "cell_exploration_and_optimization",
    "band_priority_rotation",
    "nr_capacity_coverage_preference",
)


def build_normal_behavior_source_facts(
    *,
    context: Any,
    identity: Mapping[str, Any],
    radio: Mapping[str, Any],
    cellular_slow: Mapping[str, Any],
    latency: Mapping[str, Any],
    quality: Mapping[str, Any],
    ca: Mapping[str, Any],
    traffic: Mapping[str, Any],
    decision: Mapping[str, Any],
    health: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    known_good_bands: Sequence[int],
    cell_memory: Sequence[Mapping[str, Any]],
    band_memory: Mapping[int, Mapping[str, Any]],
    systematic_probe_after_s: float,
    systematic_candidate_admission: Mapping[
        tuple[int, ...], Mapping[str, Any]
    ],
    progress: NormalBehaviorProgress,
    now: float,
) -> tuple[ActionOpportunitySourceFact, ...]:
    """Return one complete, current source-fact population for normal policy."""

    current_band = _positive_int(
        radio.get("serving_band", radio.get("primary_band"))
    ) or _positive_int(getattr(context, "current_pband", 0))
    authority = _bands(getattr(context, "authority_bands", ()))
    current_pool = _bands(
        radio.get("configured_band_pool")
        or getattr(context, "ca_pool", ())
        or radio.get("active_band_set")
    )
    active_bands = _bands(
        radio.get("active_band_set")
        or _carrier_bands(radio.get("carrier_details"))
        or current_pool
    )
    known_good = tuple(
        band for band in _bands(known_good_bands) if not authority or band in authority
    )
    state, posture = _controller_posture(health)
    if decision and (
        str(decision.get("schema") or "") != DECISION_PROJECTION_SCHEMA
        or str(decision.get("owner") or "") != DECISION_PROJECTION_OWNER
    ):
        raise ValueError("runtime_decision_projection_required")
    decision_availability = _mapping(decision.get("action_availability"))
    normal_safety = _mapping(
        decision_availability.get("normal_policy_safety")
    )
    prediction_vector = _mapping(decision.get("prediction"))
    prediction_posture = str(
        prediction_vector.get("prediction_posture") or "normal"
    ).strip().lower()
    prediction_modifier = _float(
        prediction_vector.get("prediction_modifier"),
        1.0,
    )
    recovery_disruption_allowed = bool(
        prediction_modifier >= 0.70
        and prediction_posture not in {"cautious", "conservative"}
    )
    recovery_disruption_reason = (
        "prediction_posture_cautious"
        if prediction_posture == "cautious"
        else "prediction_posture_conservative"
        if not recovery_disruption_allowed
        else ""
    )
    incident_class = str(normal_safety.get("incident_class") or "")
    known_outage_hold = bool(normal_safety.get("known_outage_hold") is True)
    radio_ready = bool(
        radio.get("_register_valid", True)
        and not radio.get("_register_stale", False)
        and radio.get("radio_connected") is True
    )
    ca_known = bool(
        ca.get("_register_valid", True)
        and not ca.get("_register_stale", False)
        and isinstance(ca.get("ca_active"), bool)
    )
    ca_missing = bool(ca_known and ca.get("ca_active") is False)
    ca_recovery_trend = _mapping(
        decision_availability.get("ca_recovery_trend")
    )
    rtt_warning_ms = _float(
        decision_availability.get("rtt_warning_ms"),
        150.0,
    )
    rtt_ms = _float(
        latency.get(
            "rtt_sliding_avg",
            latency.get("rtt_ema", latency.get("rtt_baseline")),
        ),
        0.0,
    )
    traffic_active = bool(
        traffic.get("has_traffic") is True
        or _float(traffic.get("dl_mbps"), 0.0) >= 0.4
        or _float(traffic.get("ul_mbps"), 0.0) >= 0.05
    )
    ca_recovery_trend_hold = bool(
        state == "WORKING"
        and ca_recovery_trend.get("hold_restore_ca") is True
        and (traffic_active or rtt_ms > rtt_warning_ms)
    )
    supports_pband = bool(capabilities.get("supports_pband"))
    supports_band_selection = bool(capabilities.get("supports_band_selection"))
    supports_cell_lock = bool(capabilities.get("supports_cell_lock"))
    supports_nr_lock = bool(capabilities.get("supports_nr_band_lock"))
    rows: list[ActionOpportunitySourceFact] = []

    rows.append(
        _radio_candidate_or_observation(
            behavior_id="working_restore_known_good_ca",
            active=bool(
                (state == "WORKING" or (
                    state == "DEGRADED" and incident_class == "radio_ca_loss"
                ))
                and radio_ready
                and ca_missing
                and known_good
                and (supports_pband or supports_band_selection)
                and not ca_recovery_trend_hold
            ),
            current_band=current_band,
            bands=known_good,
            supports_pband=supports_pband,
            reason="working_load_qualified_ca_missing_restore_known_good",
            inactive_reason=(
                "restore_ca_deferred_ca_metric_improving"
                if ca_recovery_trend_hold
                else _inactive_reason(
                    state == "WORKING" or (
                        state == "DEGRADED" and incident_class == "radio_ca_loss"
                    ),
                    radio_ready,
                    ca_known,
                    ca_missing,
                    bool(known_good),
                    bool(supports_pband or supports_band_selection),
                )
            ),
            uncertainty=0.20,
            evidence={
                "controller_state": state,
                "ca_active": ca.get("ca_active"),
                "known_good_bands": list(known_good),
                "incident_class": incident_class,
                "ca_recovery_trend": ca_recovery_trend,
                "ca_recovery_trend_hold": ca_recovery_trend_hold,
                "traffic_active": traffic_active,
                "rtt_ms": rtt_ms,
                "rtt_warning_ms": rtt_warning_ms,
            },
        )
    )

    if not isinstance(progress, NormalBehaviorProgress):
        raise TypeError("normal_behavior_progress_required")
    anchor_fact = _anchor_enforcement_fact(
        radio=radio,
        latency=latency,
        traffic=traffic,
        capabilities=capabilities,
        progress=progress,
        current_band=current_band,
        state=state,
        now=now,
    )
    rows.append(anchor_fact)

    cellular_slow_ready = bool(
        cellular_slow.get("_register_valid", True)
        and not cellular_slow.get("_register_stale", False)
    )
    neighbors = _neighbor_rows(cellular_slow.get("neighbors")) if cellular_slow_ready else ()
    best_neighbor = _best_live_neighbor(
        neighbors,
        serving_pci=_positive_int(radio.get("pci")),
    )
    if best_neighbor:
        neighbor_evidence = {
            "pci": int(best_neighbor["pci"]),
            "earfcn": int(best_neighbor["earfcn"]),
            "band": int(best_neighbor.get("band") or 0),
            "rsrp_dbm": float(best_neighbor["rsrp_dbm"]),
            "serving_rsrp_dbm": _float(radio.get("rsrp_dbm"), -140.0),
        }
    else:
        neighbor_evidence = {}
    learned_cell = _best_memory_cell_candidate(
        cell_memory,
        serving_pci=_positive_int(radio.get("pci")),
        now=now,
        minimum_score_gap=0.30 if known_outage_hold else 0.15,
    )
    degraded_cell = best_neighbor or learned_cell

    degraded_neighbor_active = bool(
        state == "DEGRADED"
        and radio_ready
        and supports_cell_lock
        and degraded_cell
        and _int(posture.get("degraded_actions_tried"), 0) <= 3
        and recovery_disruption_allowed
    )
    rows.append(
        _cell_candidate_or_observation(
            behavior_id="degraded_best_live_neighbor",
            active=degraded_neighbor_active,
            neighbor=degraded_cell,
            reason="degraded_best_live_neighbor_candidate",
            inactive_reason=(
                recovery_disruption_reason
                if not recovery_disruption_allowed and state == "DEGRADED"
                else _inactive_reason(
                    state == "DEGRADED",
                    radio_ready,
                    cellular_slow_ready,
                    supports_cell_lock,
                    bool(degraded_cell),
                )
            ),
            uncertainty=0.45,
            evidence={
                **(neighbor_evidence if best_neighbor else learned_cell),
                "controller_state": state,
                "degraded_actions_tried": _int(
                    posture.get("degraded_actions_tried"), 0
                ),
                "prediction_posture": prediction_posture,
                "prediction_modifier": prediction_modifier,
                "recovery_disruption_allowed": recovery_disruption_allowed,
            },
        )
    )

    open_all_active = bool(
        state == "DEGRADED"
        and radio_ready
        and authority
        and set(authority) != set(current_pool)
        and (supports_pband or supports_band_selection)
        and _int(posture.get("degraded_actions_tried"), 0) <= 4
        and recovery_disruption_allowed
    )
    rows.append(
        _radio_candidate_or_observation(
            behavior_id="degraded_open_all_deployed_bands",
            active=open_all_active,
            current_band=current_band,
            bands=authority,
            supports_pband=supports_pband,
            reason="degraded_open_all_deployed_bands_candidate",
            inactive_reason=(
                recovery_disruption_reason
                if not recovery_disruption_allowed and state == "DEGRADED"
                else _inactive_reason(
                    state == "DEGRADED",
                    radio_ready,
                    bool(authority),
                    set(authority) != set(current_pool),
                    bool(supports_pband or supports_band_selection),
                )
            ),
            uncertainty=0.55,
            evidence={
                "controller_state": state,
                "authority_bands": list(authority),
                "current_pool": list(current_pool),
                "prediction_posture": prediction_posture,
                "prediction_modifier": prediction_modifier,
                "recovery_disruption_allowed": recovery_disruption_allowed,
            },
        )
    )

    serving_rsrp = _float(radio.get("rsrp_dbm"), -140.0)
    recovered_until = _float(posture.get("recovered_lane_until"), 0.0)
    recovered_bands = set(_bands(posture.get("recovered_lane_bands")))
    on_recovered_lane = bool(
        recovered_until > float(now)
        and recovered_bands
        and recovered_bands == set(active_bands)
    )
    neighbor_margin = (
        RECOVERED_LANE_NEIGHBOR_MARGIN_DB
        if on_recovered_lane
        else NORMAL_NEIGHBOR_MARGIN_DB
    )
    if known_outage_hold:
        neighbor_margin += 4.0
    stronger_neighbor = bool(
        best_neighbor
        and _float(best_neighbor.get("rsrp_dbm"), -140.0)
        > serving_rsrp + neighbor_margin
    )
    congestion_kind = str(posture.get("congestion_kind") or "attached_cell")
    congestion_due = bool(
        float(now) - _float(posture.get("congested_last_check"), 0.0)
        >= 150.0
    )
    congestion_base = bool(
        state == "CONGESTED"
        and congestion_kind != "local_traffic"
        and posture.get("congested_action_taken") is not True
        and congestion_due
    )
    rows.append(
        _cell_candidate_or_observation(
            behavior_id="congested_neighbor_escape",
            active=bool(
                congestion_base
                and radio_ready
                and supports_cell_lock
                and stronger_neighbor
                and recovery_disruption_allowed
            ),
            neighbor=best_neighbor,
            reason="congested_stronger_live_neighbor_candidate",
            inactive_reason=(
                recovery_disruption_reason
                if not recovery_disruption_allowed and congestion_base
                else _inactive_reason(
                    congestion_base,
                    radio_ready,
                    cellular_slow_ready,
                    supports_cell_lock,
                    stronger_neighbor,
                )
            ),
            uncertainty=0.30 if stronger_neighbor else 0.70,
            evidence={
                **neighbor_evidence,
                "controller_state": state,
                "congestion_kind": congestion_kind,
                "required_rsrp_margin_db": neighbor_margin,
                "on_recovered_lane": on_recovered_lane,
                "prediction_posture": prediction_posture,
                "prediction_modifier": prediction_modifier,
                "recovery_disruption_allowed": recovery_disruption_allowed,
            },
        )
    )
    rows.append(
        _radio_candidate_or_observation(
            behavior_id="congested_restore_known_good_ca",
            active=bool(
                congestion_base
                and radio_ready
                and ca_missing
                and known_good
                and (supports_pband or supports_band_selection)
            ),
            current_band=current_band,
            bands=known_good,
            supports_pband=supports_pband,
            reason="congested_missing_good_ca_restore_candidate",
            inactive_reason=_inactive_reason(
                congestion_base,
                radio_ready,
                ca_known,
                ca_missing,
                bool(known_good),
                bool(supports_pband or supports_band_selection),
            ),
            uncertainty=0.25,
            evidence={
                "controller_state": state,
                "ca_active": ca.get("ca_active"),
                "known_good_bands": list(known_good),
            },
        )
    )

    probe_after_s = float(systematic_probe_after_s)
    if probe_after_s <= 0.0:
        raise ValueError("systematic_probe_after_s_must_be_positive")
    systematic_active = bool(
        state == "BAD_RF"
        and radio_ready
        and _float(posture.get("time_in_state_s"), 0.0)
        >= probe_after_s
        and _int(posture.get("bad_rf_recovery_step"), 0) >= 2
        and authority
        and (supports_pband or supports_band_selection)
        and not known_outage_hold
    )
    systematic_sets = systematic_probe_candidate_sets(
        authority=authority,
        current_band=current_band,
    )
    admitted_systematic_sets: list[tuple[int, ...]] = []
    blocked_systematic_sets: list[dict[str, Any]] = []
    if systematic_active:
        for bands in systematic_sets:
            admission = systematic_candidate_admission.get(bands)
            if not isinstance(admission, Mapping):
                raise RuntimeError(
                    "systematic_candidate_admission_missing:"
                    + ",".join(str(band) for band in bands)
                )
            if admission.get("allowed") is True:
                admitted_systematic_sets.append(bands)
            else:
                blocked_systematic_sets.append(
                    {
                        "bands": list(bands),
                        "reason": str(
                            admission.get("reason")
                            or "learned_band_action_blocked"
                        ),
                    }
                )
    if systematic_active and admitted_systematic_sets:
        for index, bands in enumerate(admitted_systematic_sets):
            rows.append(
                _radio_candidate_or_observation(
                    behavior_id="systematic_bad_rf_band_probe",
                    active=True,
                    current_band=current_band,
                    bands=bands,
                    supports_pband=supports_pband,
                    reason="sustained_bad_rf_systematic_candidate",
                    inactive_reason="",
                    uncertainty=0.70,
                    evidence={
                        "controller_state": state,
                        "time_in_state_s": _float(
                            posture.get("time_in_state_s"), 0.0
                        ),
                        "candidate_index": index,
                        "candidate_count": len(admitted_systematic_sets),
                        "blocked_candidate_count": len(blocked_systematic_sets),
                        "systematic_probe_after_s": probe_after_s,
                        "minimum_observation_window_s": 600,
                        "old_probe_dwell_s": 900,
                        "known_outage_hold": known_outage_hold,
                    },
                )
            )
    elif systematic_active:
        rows.append(
            _inactive(
                "systematic_bad_rf_band_probe",
                "systematic_candidates_blocked_by_learning",
                evidence={
                    "controller_state": state,
                    "time_in_state_s": _float(posture.get("time_in_state_s"), 0.0),
                    "bad_rf_recovery_step": _int(
                        posture.get("bad_rf_recovery_step"), 0
                    ),
                    "systematic_probe_after_s": probe_after_s,
                    "blocked_candidates": blocked_systematic_sets,
                },
            )
        )
    else:
        rows.append(
            _inactive(
                "systematic_bad_rf_band_probe",
                _inactive_reason(
                    state == "BAD_RF",
                    radio_ready,
                    _float(posture.get("time_in_state_s"), 0.0)
                    >= probe_after_s,
                    _int(posture.get("bad_rf_recovery_step"), 0) >= 2,
                    bool(authority),
                    bool(supports_pband or supports_band_selection),
                    not known_outage_hold,
                ),
                evidence={
                    "controller_state": state,
                    "time_in_state_s": _float(posture.get("time_in_state_s"), 0.0),
                    "bad_rf_recovery_step": _int(
                        posture.get("bad_rf_recovery_step"), 0
                    ),
                    "systematic_probe_after_s": probe_after_s,
                },
            )
        )

    learned_cell_admitted = bool(learned_cell)
    cell_explore_target = best_neighbor if stronger_neighbor else learned_cell
    repeated_cell_target = bool(
        cell_explore_target
        and _positive_int(cell_explore_target.get("pci"))
        == progress.cell_last_target_pci
    )
    best_effort_family = str(
        identity.get("backend_family") or identity.get("access_type") or ""
    ).lower()
    stable_best_effort = bool(
        any(token in best_effort_family for token in ("huawei", "hilink", "b818"))
        and 0.0
        < _float(
            latency.get("rtt_baseline", latency.get("rtt_sliding_avg")),
            0.0,
        )
        <= 100.0
        and _float(quality.get("cq_cell"), 0.0) >= 0.65
        and _float(radio.get("sinr_db"), -99.0) >= 8.0
    )
    cell_explore_active = bool(
        state == "WORKING"
        and radio_ready
        and now >= progress.cell_next_allowed_ts
        and not repeated_cell_target
        and (
            (supports_cell_lock and (stronger_neighbor or learned_cell_admitted))
            or (
                not supports_cell_lock
                and learned_cell_admitted
                and (supports_pband or supports_band_selection)
                and not stable_best_effort
            )
        )
    )
    if supports_cell_lock or not learned_cell_admitted:
        cell_fact = _cell_candidate_or_observation(
            behavior_id="cell_exploration_and_optimization",
            active=cell_explore_active,
            neighbor=cell_explore_target,
            reason=(
                "working_stronger_neighbor_exploration_candidate"
                if stronger_neighbor
                else "learned_cell_score_gap_candidate"
            ),
            inactive_reason=_inactive_reason(
                state == "WORKING",
                radio_ready,
                cellular_slow_ready,
                bool(supports_cell_lock or supports_pband or supports_band_selection),
                bool(stronger_neighbor or learned_cell_admitted),
                not repeated_cell_target,
                now >= progress.cell_next_allowed_ts,
            ),
            uncertainty=0.55,
            evidence={
                **(
                    neighbor_evidence
                    if stronger_neighbor
                    else learned_cell
                ),
                "controller_state": state,
                "required_rsrp_margin_db": NORMAL_NEIGHBOR_MARGIN_DB,
                "memory_cell_count": len(cell_memory),
                "known_outage_hold": known_outage_hold,
                "repeated_target": repeated_cell_target,
                "next_allowed_ts": progress.cell_next_allowed_ts,
            },
        )
    else:
        steering_bands, steering_kind = _best_effort_cell_steering(
            cell_memory,
            current_pci=_positive_int(radio.get("pci")),
            target_pci=_positive_int(learned_cell.get("pci")),
            current_band=current_band,
        )
        cell_fact = _radio_candidate_or_observation(
            behavior_id="cell_exploration_and_optimization",
            active=bool(cell_explore_active and steering_bands),
            current_band=(steering_bands[0] if steering_bands else 0),
            bands=steering_bands,
            supports_pband=supports_pband,
            reason=f"learned_cell_{steering_kind}_steering_candidate",
            inactive_reason=_inactive_reason(
                cell_explore_active,
                bool(steering_bands),
                not stable_best_effort,
            ),
            uncertainty=0.75,
            evidence={
                **learned_cell,
                "controller_state": state,
                "cell_opt_best_effort": True,
                "cell_opt_steering_kind": steering_kind,
                "cell_opt_target_pci": _positive_int(learned_cell.get("pci")),
                "stable_best_effort_hold": stable_best_effort,
                "known_outage_hold": known_outage_hold,
            },
        )
    rows.append(cell_fact)

    is_routeros = any(
        token in best_effort_family for token in ("routeros", "mikrotik")
    )
    is_hilink = any(
        token in best_effort_family for token in ("huawei", "hilink", "b818")
    )
    priority_target = (
        3
        if is_routeros and current_band != 3 and 3 in active_bands
        else next((band for band in active_bands if band != current_band), 0)
    )
    traffic_active = bool(
        traffic.get("has_traffic") is True
        or _float(traffic.get("dl_mbps"), 0.0) >= 0.5
    )
    priority_results_last_hour = sum(
        1
        for result in progress.recent_results
        if result.behavior_id == "band_priority_rotation"
        and result.success
        and now - result.result_ts <= 3600.0
    )
    target_band_evidence = _mapping(band_memory.get(priority_target))
    hilink_confident = bool(
        not is_hilink
        or (
            _int(target_band_evidence.get("times_appeared_in_ca"), 0) > 0
            and _float(target_band_evidence.get("avg_throughput_with"), 0.0)
            >= 0.5
        )
    )
    priority_active = bool(
        state == "WORKING"
        and radio_ready
        and (supports_pband or supports_band_selection)
        and len(active_bands) >= 2
        and priority_target > 0
        and traffic_active
        and _float(traffic.get("dl_mbps"), 0.0) >= (5.0 if is_routeros else 0.5)
        and _float(radio.get("sinr_db"), -99.0) >= (12.0 if is_routeros else 15.0)
        and (is_routeros or _float(radio.get("rsrp_dbm"), -140.0) >= -90.0)
        and (not is_routeros or priority_results_last_hour < 2)
        and hilink_confident
    )
    rows.append(
        _radio_candidate_or_observation(
            behavior_id="band_priority_rotation",
            active=priority_active,
            current_band=priority_target,
            bands=active_bands,
            supports_pband=supports_pband,
            sequential_anchor=True,
            reason="strong_rf_loaded_multi_band_anchor_rotation_candidate",
            inactive_reason=_inactive_reason(
                state == "WORKING",
                radio_ready,
                bool(supports_pband or supports_band_selection),
                len(active_bands) >= 2,
                priority_target > 0,
                traffic_active,
                _float(radio.get("sinr_db"), -99.0)
                >= (12.0 if is_routeros else 15.0),
                is_routeros or _float(radio.get("rsrp_dbm"), -140.0) >= -90.0,
                priority_results_last_hour < 2 if is_routeros else True,
                hilink_confident,
            ),
            uncertainty=0.60,
            evidence={
                "controller_state": state,
                "current_pband": current_band,
                "target_pband": priority_target,
                "active_bands": list(active_bands),
                "has_traffic": traffic_active,
                "sinr_db": _float(radio.get("sinr_db"), 0.0),
                "rsrp_dbm": _float(radio.get("rsrp_dbm"), 0.0),
                "backend_family": best_effort_family,
                "priority_results_last_hour": priority_results_last_hour,
                "target_band_evidence": target_band_evidence,
            },
        )
    )

    rows.append(
        _nr_preference_fact(
            state=state,
            radio=radio,
            supports_nr_lock=supports_nr_lock,
            cell_memory=cell_memory,
            quality=quality,
            progress=progress,
            now=now,
        )
    )

    produced = {row.behavior_id for row in rows}
    missing = set(_BEHAVIOR_IDS) - produced
    if missing:
        raise RuntimeError(
            "normal_behavior_source_fact_population_incomplete:"
            + ",".join(sorted(missing))
        )
    posture_scope = str(posture.get("progress_scope_id") or "")
    backend_family = str(
        identity.get("backend_family")
        or identity.get("access_type")
        or radio.get("backend")
        or ""
    )
    paced_rows: list[ActionOpportunitySourceFact] = []
    for row in rows:
        paced = row
        if row.active and not progress.behavior_available(row.behavior_id, now=now):
            paced = replace(
                row,
                active=False,
                fact_kind="observation",
                source_admitted=False,
                source_unavailability_reason="normal_behavior_pacing_active",
                evidence={
                    **dict(row.evidence or {}),
                    "behavior_next_allowed_ts": float(
                        progress.behavior_next_allowed_ts.get(row.behavior_id, 0.0)
                    ),
                },
            )
        paced_rows.append(paced)
    return tuple(
        replace(
            row,
            evidence={
                **dict(row.evidence or {}),
                "progress_scope_id": posture_scope,
                "backend_family": backend_family,
            },
        )
        for row in paced_rows
    )


def _anchor_enforcement_fact(
    *,
    radio: Mapping[str, Any],
    latency: Mapping[str, Any],
    traffic: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    progress: NormalBehaviorProgress,
    current_band: int,
    state: str,
    now: float,
) -> ActionOpportunitySourceFact:
    anchor = progress.anchor
    if anchor is None:
        return _inactive("anchor_enforcement", "priority_rotation_receipt_missing")
    target_band = int(anchor.target_band)
    family_escalation_allowed = not any(
        token in str(anchor.backend_family or "").lower()
        for token in ("huawei", "hilink", "b818", "routeros", "mikrotik")
    )
    age_s = max(0.0, float(now) - float(anchor.observed_at or 0.0))
    target_earfcn = _carrier_earfcn_for_band(
        radio.get("carrier_details"), target_band
    )
    current_pci = _positive_int(radio.get("pci"))
    quiet_rtt = _float(
        latency.get("rtt_baseline", latency.get("rtt_sliding_avg")),
        9999.0,
    )
    traffic_active = bool(
        traffic.get("has_traffic") is True
        or _float(traffic.get("dl_mbps"), 0.0) >= 0.4
    )
    active = bool(
        state == "WORKING"
        and capabilities.get("supports_cell_lock") is True
        and family_escalation_allowed
        and target_band > 0
        and target_band != current_band
        and target_earfcn > 0
        and current_pci > 0
        and anchor.status == "reverted"
        and quiet_rtt <= 100.0
        and not traffic_active
        and _float(radio.get("sinr_db"), -99.0) >= 10.0
        and _float(radio.get("rsrp_dbm"), -140.0) >= -100.0
    )
    return _cell_candidate_or_observation(
        behavior_id="anchor_enforcement",
        active=active,
        neighbor={
            "pci": current_pci,
            "earfcn": target_earfcn,
            "band": target_band,
            "rsrp_dbm": _float(radio.get("rsrp_dbm"), -140.0),
        },
        reason="confirmed_priority_anchor_reverted_cell_lock_candidate",
        inactive_reason=_inactive_reason(
            state == "WORKING",
            capabilities.get("supports_cell_lock") is True,
            family_escalation_allowed,
            target_band > 0,
            target_band != current_band,
            target_earfcn > 0,
            current_pci > 0,
            anchor.status == "reverted",
            quiet_rtt <= 100.0,
            not traffic_active,
        ),
        uncertainty=0.25,
        evidence={
            "priority_command_id": anchor.command_id,
            "target_pband": target_band,
            "actual_pband": current_band,
            "confirmation_age_s": round(age_s, 3),
            "verify_at": anchor.verify_at,
            "anchor_status": anchor.status,
            "family_escalation_allowed": family_escalation_allowed,
            "quiet_rtt_ms": quiet_rtt,
        },
    )


def _nr_preference_fact(
    *,
    state: str,
    radio: Mapping[str, Any],
    supports_nr_lock: bool,
    cell_memory: Sequence[Mapping[str, Any]],
    quality: Mapping[str, Any],
    progress: NormalBehaviorProgress,
    now: float,
) -> ActionOpportunitySourceFact:
    del state, radio, supports_nr_lock, cell_memory, quality, progress, now
    return _inactive(
        "nr_capacity_coverage_preference",
        "nr_observation_only_action_unsupported",
        evidence={"product_scope": "lte_anchored_nr_nsa_observation_only"},
    )


def _radio_candidate_or_observation(
    *,
    behavior_id: str,
    active: bool,
    current_band: int,
    bands: Sequence[int],
    supports_pband: bool,
    reason: str,
    inactive_reason: str,
    uncertainty: float,
    evidence: Mapping[str, Any],
    sequential_anchor: bool = False,
) -> ActionOpportunitySourceFact:
    normalized = _bands(bands)
    if not active or not normalized:
        return _inactive(behavior_id, inactive_reason, evidence=evidence)
    pband = int(current_band or 0)
    if pband <= 0 or pband not in normalized:
        pband = normalized[0]
    if supports_pband:
        lever = "PBAND"
        params = {
            "pband": pband,
            "ca_pool": list(normalized),
        }
    else:
        lever = "BAND_SELECTION"
        ordered = (
            (pband, *tuple(band for band in normalized if band != pband))
            if sequential_anchor
            else normalized
        )
        params = {"bands": list(ordered)}
        if sequential_anchor:
            params["sequential"] = True
    return ActionOpportunitySourceFact(
        behavior_id=behavior_id,
        fact_kind="candidate",
        source_owner=OWNER,
        state_id=f"{behavior_id}:{pband}:{'+'.join(str(b) for b in normalized)}",
        active=True,
        lever=lever,
        action="apply_radio_configuration",
        params=params,
        owner_class="optimization",
        reason=reason,
        source_admitted=True,
        evidence={
            **dict(evidence),
            "disruption_cost": 0.65,
            "uncertainty": float(uncertainty),
        },
    )


def _cell_candidate_or_observation(
    *,
    behavior_id: str,
    active: bool,
    neighbor: Mapping[str, Any] | None,
    reason: str,
    inactive_reason: str,
    uncertainty: float,
    evidence: Mapping[str, Any],
) -> ActionOpportunitySourceFact:
    row = _mapping(neighbor)
    pci = _positive_int(row.get("pci"))
    earfcn = _positive_int(row.get("earfcn"))
    if not active or pci <= 0 or earfcn <= 0:
        return _inactive(behavior_id, inactive_reason, evidence=evidence)
    params: dict[str, Any] = {"pci": pci, "earfcn": earfcn}
    band = _positive_int(row.get("band"))
    if band > 0:
        params["band"] = band
    return ActionOpportunitySourceFact(
        behavior_id=behavior_id,
        fact_kind="candidate",
        source_owner=OWNER,
        state_id=f"{behavior_id}:pci{pci}:earfcn{earfcn}",
        active=True,
        lever="CELL_LOCK",
        action="apply_cell_lock",
        params=params,
        owner_class="optimization",
        reason=reason,
        source_admitted=True,
        evidence={
            **dict(evidence),
            "disruption_cost": 0.80,
            "uncertainty": float(uncertainty),
        },
    )


def _inactive(
    behavior_id: str,
    reason: str,
    *,
    evidence: Mapping[str, Any] | None = None,
) -> ActionOpportunitySourceFact:
    return ActionOpportunitySourceFact(
        behavior_id=behavior_id,
        fact_kind="observation",
        source_owner=OWNER,
        state_id=f"{behavior_id}:inactive:{str(reason or 'not_triggered')}",
        active=False,
        reason=str(reason or "normal_behavior_not_triggered"),
        owner_class="optimization",
        source_admitted=False,
        source_unavailability_reason=str(
            reason or "normal_behavior_not_triggered"
        ),
        evidence=dict(evidence or {}),
    )


def _inactive_reason(*conditions: bool) -> str:
    for index, condition in enumerate(conditions, start=1):
        if not condition:
            return f"normal_behavior_condition_{index}_not_satisfied"
    return "normal_behavior_not_triggered"


def _controller_posture(health: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    if health.get("_register_valid", True) is not True or health.get(
        "_register_stale", False
    ) is True:
        return "", {}
    posture = _mapping(health.get("controller_posture"))
    state = str(posture.get("state") or health.get("state") or "").upper()
    return state, posture


def systematic_probe_candidate_sets(
    *, authority: Sequence[int], current_band: int
) -> tuple[tuple[int, ...], ...]:
    deployed = _bands(authority)
    rows: list[tuple[int, ...]] = []
    for band in (20, int(current_band or 0), *deployed):
        if band > 0 and band in deployed and (band,) not in rows:
            rows.append((band,))
    for raw in ((3, 20), (1, 3), (1, 3, 20)):
        current = tuple(band for band in raw if band in deployed)
        if current and current not in rows:
            rows.append(current)
    if deployed and tuple(deployed) not in rows:
        rows.append(tuple(deployed))
    return tuple(rows)


def _neighbor_rows(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[dict[str, Any]] = []
    for item in value:
        row = _mapping(item)
        pci = _positive_int(row.get("pci"))
        earfcn = _positive_int(row.get("earfcn"))
        rsrp = _float(row.get("rsrp_dbm", row.get("rsrp")), 0.0)
        if pci <= 0 or earfcn <= 0 or not -140.0 <= rsrp <= -44.0:
            continue
        out.append(
            {
                "pci": pci,
                "earfcn": earfcn,
                "band": _positive_int(row.get("band")),
                "rsrp_dbm": rsrp,
            }
        )
    return tuple(out)


def _best_live_neighbor(
    rows: Sequence[Mapping[str, Any]], *, serving_pci: int
) -> dict[str, Any]:
    candidates = [
        dict(row)
        for row in rows
        if _positive_int(row.get("pci")) != int(serving_pci or 0)
        and _float(row.get("rsrp_dbm"), -140.0) > -110.0
    ]
    return max(candidates, key=lambda row: _float(row.get("rsrp_dbm"), -140.0)) if candidates else {}


def _best_memory_cell_candidate(
    rows: Sequence[Mapping[str, Any]],
    *,
    serving_pci: int,
    now: float,
    minimum_score_gap: float,
) -> dict[str, Any]:
    eligible = [
        dict(row)
        for row in rows
        if _positive_int(row.get("pci")) > 0
        and _positive_int(row.get("earfcn")) > 0
        and _int(row.get("times_served"), 0) >= 120
        and (
            _positive_int(row.get("pci")) == int(serving_pci or 0)
            or (
                _float(row.get("last_seen"), 0.0) > 0.0
                and float(now) - _float(row.get("last_seen"), 0.0) <= 900.0
            )
        )
    ]
    current = next(
        (
            row
            for row in eligible
            if _positive_int(row.get("pci")) == int(serving_pci or 0)
        ),
        {},
    )
    current_score = _cell_score(current)
    if current_score <= 0.0:
        return {}
    alternatives = [
        row
        for row in eligible
        if _positive_int(row.get("pci")) != int(serving_pci or 0)
    ]
    if not alternatives:
        return {}
    target = max(alternatives, key=_cell_score)
    target_score = _cell_score(target)
    gap = (target_score - current_score) / max(current_score, 0.001)
    if gap < float(minimum_score_gap):
        return {}
    return {
        "pci": _positive_int(target.get("pci")),
        "earfcn": _positive_int(target.get("earfcn")),
        "band": _positive_int(target.get("band")),
        "rsrp_dbm": -140.0,
        "memory_candidate": True,
        "current_cell_score": round(current_score, 6),
        "target_cell_score": round(target_score, 6),
        "score_gap": round(gap, 6),
        "target_last_seen_age_s": round(
            max(0.0, float(now) - _float(target.get("last_seen"), 0.0)), 3
        ),
        "target_times_served": _int(target.get("times_served"), 0),
        "minimum_score_gap": float(minimum_score_gap),
    }


def _best_effort_cell_steering(
    rows: Sequence[Mapping[str, Any]],
    *,
    current_pci: int,
    target_pci: int,
    current_band: int,
) -> tuple[tuple[int, ...], str]:
    current = next(
        (
            row
            for row in rows
            if _positive_int(row.get("pci")) == int(current_pci or 0)
        ),
        {},
    )
    target = next(
        (
            row
            for row in rows
            if _positive_int(row.get("pci")) == int(target_pci or 0)
        ),
        {},
    )
    if not current or not target:
        return (), "unavailable"
    current_bands = _cell_band_set(current)
    target_bands = _cell_band_set(target)
    unique = target_bands - current_bands
    order = (1, 3, 7, 20, 28, 8, 32, 38, 40, 41, 42, 43)
    hook = next((band for band in order if band in unique), 0)
    if hook <= 0 and unique:
        hook = min(unique)
    if hook > 0:
        return (hook,), "hook_band"
    target_band = _positive_int(target.get("band"))
    if target_band > 0 and target_band != int(current_band or 0):
        return (target_band,), "target_band_bounce"
    return (), "unavailable"


def _cell_band_set(row: Mapping[str, Any]) -> set[int]:
    values = {
        _positive_int(row.get("band")),
        *_parse_band_signature(row.get("best_ca_signature")),
    }
    return {value for value in values if value > 0}


def _parse_band_signature(value: Any) -> set[int]:
    return {
        _positive_int(token)
        for token in str(value or "").upper().replace("B", "").split("+")
        if _positive_int(token) > 0
    }


def _nr_target_mode(target: Sequence[int]) -> str:
    values = set(_bands(target))
    if values == {28}:
        return "coverage"
    if 28 in values and values & {7, 78}:
        return "all"
    if values & {7, 78}:
        return "capacity"
    return ""


def _cell_score(row: Mapping[str, Any]) -> float:
    base = (
        _float(row.get("avg_bq_cell"), 0.0)
        or _float(row.get("avg_cq_cell"), 0.0)
        or _float(row.get("avg_throughput"), 0.0)
    )
    bandwidth = _float(row.get("observed_bw_mhz"), 0.0)
    bandwidth_bonus = 0.10 * min(1.0, bandwidth / 20.0) if bandwidth > 0.0 else 0.0
    total = _int(row.get("nr_total_count"), 0)
    if total >= 5:
        attach_rate = _int(row.get("nr_attach_count"), 0) / float(total)
        sinr_bonus = _float(row.get("nr_sinr_ema"), 0.0) / 30.0
        nr_bonus = 0.10 * attach_rate + 0.05 * min(1.0, sinr_bonus)
    else:
        nr_bonus = 0.0
    return base * (1.0 + bandwidth_bonus + nr_bonus)


def _carrier_bands(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return _bands(
        _mapping(item).get("band")
        for item in value
        if isinstance(item, Mapping)
    )


def _carrier_earfcn_for_band(value: Any, band: int) -> int:
    if not isinstance(value, (list, tuple)):
        return 0
    for item in value:
        row = _mapping(item)
        if _positive_int(row.get("band")) == int(band or 0):
            return _positive_int(row.get("earfcn"))
    return 0


def _bands(value: Any) -> tuple[int, ...]:
    if isinstance(value, str):
        value = value.replace("+", ",").split(",")
    if not isinstance(value, (list, tuple, set, frozenset)) and not hasattr(
        value, "__iter__"
    ):
        return ()
    out: list[int] = []
    for item in value or ():
        band = _positive_int(str(item).lstrip("Bb"))
        if band > 0 and band not in out:
            out.append(band)
    return tuple(sorted(out))


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _positive_int(value: Any) -> int:
    return max(0, _int(value, 0))


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


__all__ = [
    "NR_MIN_OBSERVATIONS",
    "NORMAL_NEIGHBOR_MARGIN_DB",
    "OWNER",
    "RECOVERED_LANE_NEIGHBOR_MARGIN_DB",
    "build_normal_behavior_source_facts",
    "systematic_probe_candidate_sets",
]
