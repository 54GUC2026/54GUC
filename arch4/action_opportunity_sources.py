"""Pure boundary adapters for the complete action-opportunity source snapshot.

The adapters project already-owned facts into the typed input required by
``ActionOpportunitySet.v1``.  They do not rank, select, execute, grade, mutate
cooldowns, advance schedules, or read Modem Live.
"""

from __future__ import annotations

from dataclasses import asdict
import time
from typing import Any, Mapping, Sequence

from arch4.action_admission import ACTION_DESCRIPTORS
from arch4.action_opportunity_source_facts import (
    ActionOpportunitySourceFact,
    ActionOpportunitySourceFactSet,
    action_opportunity_source_fact_set_from_mapping,
)
from arch4.action_opportunity_set import ActionOpportunitySourceSnapshot
from arch4.brain.memory_band_learning_query_store import known_good_bandset
from arch4.brain.memory_cell_knowledge_owner import iter_cell_snapshots
from arch4.brain.memory_band_operational_history_owner import (
    band_ca_throughput_evidence,
)
from arch4.core.config_generation import require_active_config_snapshot
from arch4.brain.learning_memory_projection import screen_band_action_from_memory
from arch4.policy.a1_b818_windows import (
    a1_restricted_band_pool,
    is_a1_overnight_profile,
    is_a1_restricted_night_window,
)
from arch4.smart_loop.normal_behavior_opportunities import (
    build_normal_behavior_source_facts,
    systematic_probe_candidate_sets,
)
from arch4.smart_loop.normal_behavior_progress import NormalBehaviorProgress


OWNER = "arch4.action_opportunity_sources"


def source_snapshot_from_accepted_frame(
    *,
    engine: Any,
    accepted_frame: Any,
    requested_actions: Sequence[Mapping[str, Any]] = (),
) -> ActionOpportunitySourceSnapshot:
    """Compose the complete transaction-scoped source input from one frame."""

    if accepted_frame is None:
        raise TypeError("accepted_frame_required_for_opportunity_source_snapshot")
    cellular = accepted_frame.cellular
    identity = accepted_frame.identity
    action = accepted_frame.action
    authority = _bands(
        getattr(identity, "configured_band_pool", ())
        or _effective_deployed_bands(engine)
        or getattr(cellular, "selected_band_set", ())
        or getattr(cellular, "active_band_set", ())
    )
    selected = _bands(getattr(cellular, "selected_band_set", ()))
    active = _bands(getattr(cellular, "active_band_set", ()))
    current_pool = _bands(
        getattr(identity, "configured_band_pool", ()) or selected or active
    )
    current = {
        "primary_band": _int(getattr(cellular, "primary_band", 0), 0),
        "current_pband": _int(getattr(cellular, "primary_band", 0), 0),
        "authority_bands": authority,
        "known_good_bandset": _known_good_bandset(engine),
        "selected_bands": selected,
        "selected_band_set": selected,
        "active_bands": active,
        "ca_pool": current_pool,
        "radio_restricted": bool(
            authority and current_pool and set(current_pool) < set(authority)
        ),
        "radio_restoration_required": False,
        "cell_lock_active": bool(getattr(action, "cell_locked", False)),
        "cell_lock_pci": _int(getattr(action, "cell_lock_pci", 0), 0),
        "cell_lock_earfcn": _int(
            getattr(action, "cell_lock_earfcn", 0), 0
        ),
        "cell_lock_release_due": False,
        "serving_pci": _int(getattr(cellular, "pci", 0), 0),
        "serving_earfcn": _int(getattr(cellular, "earfcn_dl", 0), 0),
        "nr_lock_active": _optional_bool(
            getattr(action, "nr_lock_active", None)
        ),
        "nr_locked_bands": _bands(
            getattr(action, "nr_locked_bands", ())
        ),
        "nr_lock_release_due": False,
        "traffic_presence_active": bool(
            getattr(action, "traffic_presence_active", False)
        ),
        "traffic_presence_interval_s": _float(
            getattr(action, "traffic_presence_interval_s", 0.0), 0.0
        ),
    }
    return ActionOpportunitySourceSnapshot(
        capabilities=capability_projection(_engine_capabilities(engine)),
        current=current,
        source_facts=source_facts_from_accepted_frame(
            engine=engine,
            accepted_frame=accepted_frame,
        ).as_dict(),
        neighbor_cells=_neighbor_counterfactuals(
            getattr(cellular, "neighbors", ()),
            serving_pci=_int(getattr(cellular, "pci", 0), 0),
            serving_rsrp=_float(getattr(cellular, "rsrp_dbm", 0.0), 0.0),
        ),
        nr_candidates=(),
        requested_actions=tuple(
            dict(row) for row in requested_actions if isinstance(row, Mapping)
        ),
    )


def source_snapshot_from_smartness_register(
    *,
    engine: Any,
    context: Any,
    runtime_identity: Mapping[str, Any],
    runtime_radio: Mapping[str, Any],
    runtime_cellular_slow: Mapping[str, Any],
    runtime_latency: Mapping[str, Any],
    runtime_quality: Mapping[str, Any],
    runtime_ca: Mapping[str, Any],
    runtime_traffic: Mapping[str, Any],
    runtime_decision: Mapping[str, Any],
    runtime_health: Mapping[str, Any],
    runtime_frame_scope: Mapping[str, Any],
    runtime_source_facts: Mapping[str, Any],
    normal_behavior_progress: NormalBehaviorProgress,
    optimization_candidates: Sequence[Mapping[str, Any]],
    requested_actions: Sequence[Mapping[str, Any]] = (),
    decision_ts: float | None = None,
) -> ActionOpportunitySourceSnapshot:
    """Compose one asynchronous snapshot from owner-stamped register facts."""

    radio = dict(runtime_radio or {})
    traffic = dict(runtime_traffic or {})
    cellular_slow = dict(runtime_cellular_slow or {})
    latency = dict(runtime_latency or {})
    quality = dict(runtime_quality or {})
    ca = dict(runtime_ca or {})
    health = dict(runtime_health or {})
    now = _float(decision_ts, 0.0)
    if now <= 0.0:
        now = _float(
            radio.get("sample_ts", getattr(context, "observed_ts", 0.0)),
            0.0,
        )
    if now <= 0.0:
        raise ValueError("smartness_opportunity_decision_ts_required")
    authority = _bands(getattr(context, "authority_bands", ()) or ())
    selected = _bands(
        radio.get("selected_band_set")
        or radio.get("configured_band_pool")
        or radio.get("selected_bands")
        or ()
    )
    active = _bands(
        radio.get("active_band_set")
        or radio.get("active_bands")
        or radio.get("configured_band_pool")
        or getattr(context, "ca_pool", ())
        or ()
    )
    current_pool = _bands(
        radio.get("configured_band_pool") or getattr(context, "ca_pool", ()) or ()
    )
    current = {
        "current_pband": _int(getattr(context, "current_pband", 0), 0),
        "primary_band": _int(getattr(context, "current_pband", 0), 0),
        "authority_bands": authority,
        "known_good_bandset": _known_good_bandset(engine),
        "selected_bands": selected,
        "active_bands": active,
        "ca_pool": current_pool,
        "radio_restricted": bool(
            authority and current_pool and set(current_pool) < set(authority)
        ),
        "radio_restoration_required": False,
        "cell_lock_active": _optional_bool(
            radio.get("cell_lock_active", radio.get("cell_locked"))
        ),
        "cell_lock_pci": _int(radio.get("cell_lock_pci"), 0),
        "cell_lock_earfcn": _int(radio.get("cell_lock_earfcn"), 0),
        "cell_lock_release_due": False,
        "serving_pci": _int(radio.get("pci"), 0),
        "serving_earfcn": _int(
            radio.get("earfcn_dl", radio.get("earfcn")), 0
        ),
        "traffic_presence_active": _optional_bool(
            traffic.get("traffic_presence_active")
        ),
        "traffic_presence_interval_s": traffic.get(
            "traffic_presence_interval_s"
        ),
        "nr_lock_active": _optional_bool(radio.get("nr_lock_active")),
        "nr_locked_bands": _bands(radio.get("nr_locked_bands") or ()),
        "nr_lock_release_due": False,
    }
    capabilities = capability_projection(_engine_capabilities(engine))
    active_config = require_active_config_snapshot(engine)
    systematic_probe_after_s = _float(
        active_config.bad_rf_systematic_probe_after_s,
        0.0,
    )
    systematic_candidates = systematic_probe_candidate_sets(
        authority=authority,
        current_band=_int(getattr(context, "current_pband", 0), 0),
    )
    systematic_candidate_admission = {
        bands: {
            "allowed": decision.allowed,
            "reason": decision.reason,
            "source": decision.source,
        }
        for bands in systematic_candidates
        for decision in (
            screen_band_action_from_memory(
                engine.memory,
                hour=int(time.localtime(now).tm_hour),
                bands=bands,
                now=now,
                current_sinr=_float(radio.get("sinr_db"), 0.0),
            ),
        )
    }
    normal_behavior_facts = build_normal_behavior_source_facts(
        context=context,
        identity=dict(runtime_identity or {}),
        radio=radio,
        cellular_slow=cellular_slow,
        latency=latency,
        quality=quality,
        ca=ca,
        traffic=traffic,
        decision=dict(runtime_decision or {}),
        health=health,
        capabilities=capabilities,
        known_good_bands=_known_good_bandset(engine),
        cell_memory=_cell_memory_evidence(engine),
        band_memory=_band_operational_evidence(engine),
        systematic_probe_after_s=systematic_probe_after_s,
        systematic_candidate_admission=systematic_candidate_admission,
        progress=normal_behavior_progress,
        now=now,
    )
    return ActionOpportunitySourceSnapshot(
        capabilities=capabilities,
        current=current,
        source_facts=_validated_frame_source_facts(
            engine=engine,
            context=context,
            runtime_radio=radio,
            decision_ts=decision_ts,
            frame_scope=runtime_frame_scope,
            frozen_source_facts=runtime_source_facts,
            extra_facts=normal_behavior_facts,
        ).as_dict(),
        optimization_candidates=tuple(
            dict(row)
            for row in optimization_candidates
            if isinstance(row, Mapping)
        ),
        neighbor_cells=_neighbor_counterfactuals(
            cellular_slow.get("neighbors") or (),
            serving_pci=_int(radio.get("pci"), 0),
            serving_rsrp=_float(radio.get("rsrp_dbm"), 0.0),
        ),
        nr_candidates=(),
        requested_actions=tuple(
            dict(row) for row in requested_actions if isinstance(row, Mapping)
        ),
    )


def source_facts_from_accepted_frame(
    *,
    engine: Any,
    accepted_frame: Any,
) -> ActionOpportunitySourceFactSet:
    generation = accepted_frame.generation
    identity = accepted_frame.identity
    now = _float(
        getattr(generation, "accepted_ts", getattr(generation, "sample_ts", 0.0)),
        0.0,
    )
    facts = list(
        _provider_constraint_facts(
            engine=engine,
            modem=accepted_frame,
            now=now,
        )
    )
    return ActionOpportunitySourceFactSet(
        identity_code=str(getattr(identity, "identity_code", "") or ""),
        session_scope_key=str(
            getattr(generation, "session_scope_key", "") or ""
        ),
        source_epoch=str(getattr(generation, "source_epoch", "") or ""),
        source_sequence=_int(getattr(generation, "generation", 0), 0),
        facts=tuple(facts),
    )


def _validated_frame_source_facts(
    *,
    engine: Any,
    context: Any,
    runtime_radio: Mapping[str, Any],
    decision_ts: float | None,
    frame_scope: Mapping[str, Any],
    frozen_source_facts: Mapping[str, Any],
    extra_facts: Sequence[ActionOpportunitySourceFact] = (),
) -> ActionOpportunitySourceFactSet:
    """Validate frozen owner facts and add only named frame-derived facts.

    This boundary never reads ``ActionSourceStateAuthority``.  The mutable
    authority was crossed once by the final frame publisher; this function
    can only validate that retained fact set and add pure candidate facts from
    the immutable frame plus explicitly named domain-owner adapters.
    """

    now = _float(decision_ts, 0.0)
    if now <= 0.0:
        now = _float(getattr(context, "observed_ts", 0.0), 0.0)
    frozen = action_opportunity_source_fact_set_from_mapping(
        frozen_source_facts
    )
    radio = dict(runtime_radio or {})
    all_extra_facts = list(
        _provider_constraint_facts(
            engine=engine,
            modem=None,
            now=now,
        )
    )
    all_extra_facts.extend(
        _global_autonomous_constraint_facts(context=context, now=now)
    )
    all_extra_facts.extend(
        fact
        for fact in extra_facts
        if isinstance(fact, ActionOpportunitySourceFact)
    )
    scope = dict(frame_scope or {})
    expected_scope = {
        "identity_code": str(scope.get("identity_code") or ""),
        "session_scope_key": str(scope.get("session_scope_key") or ""),
        "source_epoch": str(scope.get("source_epoch") or ""),
        "source_sequence": _int(scope.get("source_sequence"), 0),
    }
    retained_scope = {
        "identity_code": frozen.identity_code,
        "session_scope_key": frozen.session_scope_key,
        "source_epoch": frozen.source_epoch,
        "source_sequence": int(frozen.source_sequence),
    }
    if retained_scope != expected_scope:
        raise RuntimeError("smartness_source_fact_frame_scope_mismatch")
    replacements = tuple(all_extra_facts)
    replaced_behaviors = {
        fact.behavior_id for fact in replacements if fact.behavior_id
    }
    return ActionOpportunitySourceFactSet(
        **retained_scope,
        facts=tuple(
            fact
            for fact in frozen.facts
            if fact.behavior_id not in replaced_behaviors
        )
        + replacements,
    )


def _provider_constraint_facts(
    *,
    engine: Any,
    modem: Any,
    now: float,
) -> tuple[ActionOpportunitySourceFact, ...]:
    profile_active = False
    window_active = False
    allowed: list[int] = []
    try:
        profile_active = bool(is_a1_overnight_profile(engine))
        window_active = bool(
            is_a1_restricted_night_window(engine, now=now, modem=modem)
        )
        if window_active and modem is not None:
            allowed = a1_restricted_band_pool(engine, modem, now=now)
        elif window_active:
            available = _effective_deployed_bands(engine)
            allowed = [band for band in available if band in {3, 20}]
    except (AttributeError, TypeError, ValueError):
        profile_active = False
        window_active = False
        allowed = []
    return (
        ActionOpportunitySourceFact(
            behavior_id="a1_restricted_night_band_pool",
            fact_kind="constraint" if window_active else "observation",
            source_owner="arch4.policy.a1_b818_windows",
            state_id=(
                "a1_restricted_night:"
                + str(int(now // 3600))
                + ":"
                + ",".join(str(value) for value in allowed)
            ),
            active=window_active,
            reason=(
                "provider_restricted_night_band_pool"
                if window_active
                else "provider_restricted_window_inactive"
            ),
            owner_class="policy",
            source_admitted=window_active,
            source_unavailability_reason=(
                "" if window_active else "provider_restricted_window_inactive"
            ),
            evidence={
                "profile_active": profile_active,
                "window_active": window_active,
                "allowed_bands": allowed,
                "window_hour": time.localtime(now).tm_hour if now > 0.0 else -1,
            },
            applies_to={
                "levers": ["BAND_SELECTION", "PBAND"],
                "owner_classes": [
                    "optimization",
                    "policy",
                    "maintenance",
                    "service",
                ],
                "allowed_bands": allowed,
                "blocked_reason": "provider_restricted_night_band_pool",
            },
        ),
    )


def _global_autonomous_constraint_facts(
    *, context: Any, now: float
) -> tuple[ActionOpportunitySourceFact, ...]:
    safety_block = str(getattr(context, "safety_block", "") or "").strip()
    cooldown_s = max(0.0, _float(getattr(context, "cooldown_s", 0.0), 0.0))
    proof_pending = bool(getattr(context, "proof_pending", False))
    active = bool(safety_block or cooldown_s > 0.0 or proof_pending)
    reason = (
        "smart_loop_proof_pending"
        if proof_pending
        else safety_block
        or ("smart_loop_cooldown_active" if cooldown_s > 0.0 else "available")
    )
    return (
        ActionOpportunitySourceFact(
            behavior_id="smart_loop_global_action_availability",
            fact_kind="constraint" if active else "observation",
            source_owner="arch4.smart_loop.action_availability",
            state_id=(
                "smart_loop_availability:"
                + str(reason)
                + ":until:"
                + str(int(now + cooldown_s) if cooldown_s > 0.0 else 0)
            ),
            active=active,
            reason=str(reason),
            owner_class="policy",
            source_admitted=active,
            source_unavailability_reason="" if active else "smart_loop_available",
            evidence={
                "safety_block": safety_block,
                "cooldown_s": cooldown_s,
                "proof_pending": proof_pending,
            },
            applies_to={
                "levers": [
                    "BAND_SELECTION",
                    "BAND_LOCK",
                    "PBAND",
                    "CELL_LOCK",
                    "NR_BAND_LOCK",
                ],
                "owner_classes": [
                    "optimization",
                    "policy",
                    "maintenance",
                    "service",
                ],
                "blocked_reason": str(reason),
            },
        ),
    )


def capability_projection(value: Any) -> dict[str, Any]:
    """Project every canonical action capability plus catalog control modes."""

    if isinstance(value, Mapping):
        raw = dict(value)
    elif value is None:
        raw = {}
    else:
        raw = {
            name: getattr(value, name)
            for name in dir(value)
            if name.startswith("supports_")
            or name
            in {
                "pband_control_mode",
                "ca_pool_control_mode",
                "cell_lock_control_mode",
                "traffic_presence_owner",
            }
        }
    projected: dict[str, Any] = {}
    for descriptor in ACTION_DESCRIPTORS.values():
        key = str(descriptor.required_capability or "")
        if key:
            projected[f"supports_{key}"] = bool(
                raw.get(
                    f"supports_{key}",
                    raw.get(key, False),
                )
            )
    projected.update(
        {
            "supports_neighbors": bool(raw.get("supports_neighbors", False)),
            "pband_control_mode": str(
                raw.get("pband_control_mode") or "none"
            ),
            "ca_pool_control_mode": str(
                raw.get("ca_pool_control_mode") or "none"
            ),
            "cell_lock_control_mode": str(
                raw.get("cell_lock_control_mode") or "none"
            ),
            "traffic_presence_owner": str(
                raw.get("traffic_presence_owner") or ""
            ),
        }
    )
    projected["supports_nr_band_lock"] = False
    return projected


def _neighbor_counterfactuals(
    values: Any,
    *,
    serving_pci: int,
    serving_rsrp: float,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    rows: list[dict[str, Any]] = []
    for value in values:
        getter = value.get if isinstance(value, Mapping) else lambda key, default=None: getattr(value, key, default)
        pci = _int(getter("pci", 0), 0)
        earfcn = _int(getter("earfcn", 0), 0)
        if pci <= 0 or earfcn <= 0 or pci == serving_pci:
            continue
        band = _int(getter("band", 0), 0)
        rsrp = _float(getter("rsrp_dbm", 0.0), 0.0)
        row = {
            "pci": pci,
            "earfcn": earfcn,
            "source": OWNER,
            "reason": "observed_neighbor_counterfactual",
            "source_admitted": False,
            "source_unavailability_reason": (
                "typed_neighbor_policy_evidence_pending_s4b"
            ),
            "uncertainty": 0.65,
            "evidence": {
                "neighbor_rsrp_dbm": rsrp,
                "serving_rsrp_dbm": serving_rsrp,
                "rsrp_delta_db": (
                    round(rsrp - serving_rsrp, 3)
                    if rsrp and serving_rsrp
                    else None
                ),
            },
        }
        if band > 0:
            row["band"] = band
        rows.append(row)
    return tuple(rows)


def _engine_capabilities(engine: Any) -> Any:
    return getattr(
        getattr(getattr(engine, "controller", None), "control", None),
        "capabilities",
        None,
    )


def _effective_deployed_bands(engine: Any) -> list[int]:
    reader = getattr(engine, "_effective_deployed_bands", None)
    return _bands(reader()) if callable(reader) else []


def _known_good_bandset(engine: Any) -> list[int]:
    memory = getattr(
        getattr(getattr(engine, "controller", None), "brain", None),
        "memory",
        None,
    )
    if memory is None:
        memory = getattr(
            getattr(getattr(engine, "controller", None), "control", None),
            "decision",
            None,
        )
        memory = getattr(memory, "memory", None)
    return _bands(known_good_bandset(memory)) if memory is not None else []


def _engine_memory(engine: Any) -> Any:
    memory = getattr(
        getattr(getattr(engine, "controller", None), "brain", None),
        "memory",
        None,
    )
    if memory is not None:
        return memory
    decision = getattr(
        getattr(getattr(engine, "controller", None), "control", None),
        "decision",
        None,
    )
    return getattr(decision, "memory", None)


def _cell_memory_evidence(engine: Any) -> list[dict[str, Any]]:
    memory = _engine_memory(engine)
    if memory is None:
        return []
    return [
        asdict(snapshot)
        for _key, snapshot in iter_cell_snapshots(memory)
    ]


def _band_operational_evidence(engine: Any) -> dict[int, dict[str, float | int]]:
    memory = _engine_memory(engine)
    return band_ca_throughput_evidence(memory) if memory is not None else {}


def _bands(value: Any) -> list[int]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return sorted({_int(item, 0) for item in value if _int(item, 0) > 0})


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


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
    "OWNER",
    "capability_projection",
    "source_snapshot_from_accepted_frame",
    "source_snapshot_from_smartness_register",
    "source_facts_from_accepted_frame",
]
