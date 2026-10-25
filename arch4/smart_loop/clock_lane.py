"""Runtime-clock lane for the clean smart-loop strategy feed."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import time
from typing import Any, Mapping

from arch4.action_opportunity_set import build_action_opportunity_set
from arch4.action_session_scope import current_action_context_key_from_values
from arch4.action_opportunity_sources import (
    source_snapshot_from_smartness_register,
)
from arch4.action_selection_receipts import brain_decision_receipt_error
from arch4.brain_decision_authority import select_brain_decision
from arch4.brain.proof_feedback_owner import terminal_layered_action_feedback_is_valid
from arch4.brain.memory_band_operational_history_owner import (
    anchor_stability_projection_completed,
    record_anchor_stability,
)
from arch4.customer_contracts.optimization_action import (
    build_optimization_action_plan,
    optimization_proof_template,
    validate_optimization_proof_template,
)
from arch4.brain.feed_registers import (
    FEED_ACTION_PROOF_CLOSURES,
    FEED_LEARNING_QUALITY,
    publish_brain_feed_registers_status,
    require_brain_feed_register_bank,
)
from arch4.runtime_clock import ClockedJobOutcome
from arch4.contextual_bandit import select_contextual_bandit_candidate
from arch4.contextual_bandit_state_store import ContextualBanditStateStore
from arch4.evidence_scope import FormulaVector, evidence_scope_from_mapping
from arch4.runtime_registers import (
    REGISTER_SMARTNESS,
    require_core_runtime_register_bank,
)
from arch4.smartness_fact_register import (
    SMARTNESS_FACT_SECTIONS,
    smartness_fact_sections,
    smartness_fact_source_facts,
)
from arch4.smart_loop.action_availability import build_action_availability
from arch4.smart_loop.closed_loop import (
    Candidate,
    ObjectiveHold,
    ProofGrade,
    ProofWindow,
    RADIO_CONFIGURATION_ACTION,
    SmartLoopCore,
    SmartLoopState,
)
from arch4.core.selected_action_register import (
    SelectedActionStage,
)
from arch4.action_transaction_authority import require_action_transaction_authority
from arch4.command_transactions import CommandTerminalStatus
from arch4.customer_contracts.selected_action import (
    materialize_selected_action,
)
from arch4.smart_loop.state_store import require_smart_loop_state_store
from arch4.smart_loop.normal_behavior_progress import (
    NormalBehaviorProgress,
    NormalBehaviorResult,
    mark_anchor_learning_projected,
    observe_normal_behavior_progress,
    project_normal_behavior_terminal,
)
from arch4.smart_loop.proof_feedback_adapter import build_smart_loop_layered_feedback
from arch4.smart_loop.register_adapter import smart_loop_inputs_from_registers
from arch4.smart_loop.strategy_register import (
    DEFAULT_STALE_AFTER_S,
    publish_smart_loop_strategy_register,
    smart_loop_strategy_fault,
    touch_smart_loop_strategy_register,
)

UNCHANGED_INPUT_REFRESH_S = min(5.0, max(1.0, DEFAULT_STALE_AFTER_S * 0.5))
_CLOCK_LANE_CACHE_ATTR = "_smart_loop_clock_lane_cache"
_OPPORTUNITY_FORMULA_VECTOR = FormulaVector.build(
    {"action_opportunity": "smart_loop_candidate_forecast_v1"}
)


def run_smart_loop_strategy_clock_lane(engine: Any, *, now: float) -> ClockedJobOutcome:
    """Publish the clean smart-loop decision from owner-stamped registers."""

    try:
        require_action_transaction_authority(engine).expire_overdue(now=now)
        input_signature = _strategy_input_signature(engine, now=now)
        awaiting_receipt = _awaiting_selected_action_receipt_outcome(
            engine,
            input_signature=input_signature,
            now=now,
        )
        if awaiting_receipt is not None:
            return awaiting_receipt
        cached = _cached_strategy_outcome(
            engine, input_signature=input_signature, now=now
        )
        if cached is not None:
            return cached

        runtime_values = _runtime_values(engine)
        runtime_identity = _mapping(runtime_values.get("identity"))
        runtime_radio = _mapping(runtime_values.get("radio"))
        learning_quality = _learning_quality_publication(engine, now=now)
        learning = _clean_learning_inputs(
            engine=engine,
            runtime_radio=runtime_radio,
            runtime_learning=_mapping(runtime_values.get("learning")),
            runtime_capacity=_mapping(runtime_values.get("capacity")),
            learning_quality=learning_quality,
            now=now,
        )
        runtime_settle = _runtime_settle_availability(
            engine=engine,
            runtime_radio=runtime_radio,
            runtime_traffic=_mapping(runtime_values.get("traffic")),
            now=now,
        )
        availability = build_action_availability(
            decision=_mapping(runtime_values.get("decision")),
            mutation=_mapping(runtime_values.get("mutation")),
            health=_mapping(runtime_values.get("health")),
            runtime_settle=runtime_settle,
            now=now,
        )
        context, facts = smart_loop_inputs_from_registers(
            identity=runtime_identity,
            radio=runtime_radio,
            quality=_mapping(runtime_values.get("quality")),
            ca=_mapping(runtime_values.get("ca")),
            learning=learning,
            mutation=_mapping(runtime_values.get("mutation")),
            availability=availability,
            capacity=_mapping(runtime_values.get("capacity")),
        )
        state = getattr(engine, "_smart_loop_state", None)
        if not isinstance(state, SmartLoopState):
            state = SmartLoopState()
        runtime_quality = _mapping(runtime_values.get("quality"))
        observed_progress = observe_normal_behavior_progress(
            state.normal_behavior_progress,
            radio=runtime_radio,
            quality=runtime_quality,
            now=now,
        )
        observed_progress = _project_anchor_observation_learning(
            engine,
            progress=observed_progress,
            runtime_radio=runtime_radio,
            runtime_quality=runtime_quality,
            runtime_ca=_mapping(runtime_values.get("ca")),
            now=now,
        )
        state = state.with_normal_behavior_progress(observed_progress)
        state = _apply_optimization_selected_action_receipt(engine, state=state, now=now)
        core = SmartLoopCore()
        decision = core.decide(
            context=context,
            facts=facts,
            state=state,
            # Production proof grading is accepted only from the canonical
            # Brain action-proof register. Live CQ/congestion is input to the
            # Referee, never an alternate SmartLoop-side proof closer.
            proof_grade=_proof_grade_from_action_proof_register(
                engine, state=state, now=now
            ),
        )
        decision = apply_brain_candidate_selection(
            engine,
            core=core,
            decision=decision,
            context=context,
            facts=facts,
            runtime_identity=runtime_identity,
            runtime_radio=runtime_radio,
            runtime_cellular_slow=_mapping(
                runtime_values.get("modem_slow")
            ),
            runtime_latency=_mapping(runtime_values.get("latency")),
            runtime_quality=runtime_quality,
            runtime_ca=_mapping(runtime_values.get("ca")),
            runtime_traffic=_mapping(runtime_values.get("traffic")),
            runtime_decision=_mapping(runtime_values.get("decision")),
            runtime_health=_mapping(runtime_values.get("health")),
            runtime_frame_scope=_mapping(
                runtime_values.get("frame_scope")
            ),
            runtime_source_facts=_mapping(
                runtime_values.get("source_facts")
            ),
            normal_behavior_progress=decision.state.normal_behavior_progress,
            now=now,
        )
        _persist_smart_loop_state_and_ack_terminal(
            engine,
            state=decision.state,
            now=now,
        )
        publication = decision.as_register_payload()
        inputs = _mapping(publication.get("inputs"))
        inputs["action_availability"] = dict(availability)
        publication["inputs"] = inputs
        publication["action_availability"] = dict(availability)
        publication = _latch_brain_selected_action(
            engine,
            publication=publication,
            decision=decision,
            now=now,
        )
        publication = _with_clock_lane_status(
            publication,
            now=now,
            mode="recomputed",
            cache={},
        )
        result = publish_smart_loop_strategy_register(
            engine,
            publication=publication,
            now=now,
            stale_after_s=DEFAULT_STALE_AFTER_S,
        )
        _store_strategy_cache(
            engine,
            input_signature=_strategy_input_signature(engine, now=now),
            publication=publication,
            availability=availability,
            now=now,
        )
        _publish_status(engine, now=now)
        return ClockedJobOutcome(
            ran=bool(result.get("published", False)),
            last_error="" if result.get("published") else str(result.get("reason", "")),
            backpressure_reason=""
            if result.get("published")
            else "smart_loop_strategy_publish_failed",
        )
    except Exception as exc:
        result = publish_smart_loop_strategy_register(
            engine,
            publication=smart_loop_strategy_fault(
                reason=str(exc or "smart_loop_strategy_failed"),
                required_next_step="repair_smart_loop_strategy_inputs",
            ),
            now=now,
            stale_after_s=DEFAULT_STALE_AFTER_S,
        )
        _publish_status(engine, now=now)
        return ClockedJobOutcome(
            ran=bool(result.get("published", False)),
            last_error=str(exc or ""),
            backpressure_reason="smart_loop_strategy_failed",
        )


def _latch_brain_selected_action(
    engine: Any,
    *,
    publication: Mapping[str, Any],
    decision: Any,
    now: float,
) -> dict[str, Any]:
    """Move the post-Brain selected action into its sole durable owner."""

    row = dict(publication or {})
    selected_action = _mapping(
        getattr(decision, "brain_selected_action", {})
    )
    proof_template = _mapping(
        getattr(decision, "brain_selected_action_proof_template", {})
    )
    register = require_action_transaction_authority(engine)
    if selected_action:
        result = register.publish(
            selected_action=selected_action,
            proof_template=proof_template,
            now=now,
        )
        if not result.accepted:
            archive = result.archive_receipt
            if archive is None:
                raise RuntimeError(
                    "rejected_selected_action_archive_missing"
                )
            rejection_reason = str(
                result.reason or archive.terminal_reason
            )
            row["outcome"] = "action_rejected"
            row["reason"] = rejection_reason
            row["reason_code"] = rejection_reason
            row["required_next_step"] = "wait_for_changed_smart_loop_evidence"
            self_learning = _mapping(_mapping(row.get("inputs")).get("self_learning"))
            if self_learning:
                updated_inputs = dict(_mapping(row.get("inputs")))
                updated_self_learning = dict(self_learning)
                updated_self_learning.update(
                    {
                        "status": row["outcome"],
                        "reason": row["reason_code"],
                        "required_next_step": row["required_next_step"],
                    }
                )
                updated_inputs["self_learning"] = updated_self_learning
                row["inputs"] = updated_inputs
    # The strategy feed is a display/evidence projection. Executable ownership
    # exists only in ActionTransactionAuthority.
    row["action_transaction_authority"] = register.selection_status()
    return row


def _project_anchor_observation_learning(
    engine: Any,
    *,
    progress: NormalBehaviorProgress,
    runtime_radio: Mapping[str, Any],
    runtime_quality: Mapping[str, Any],
    runtime_ca: Mapping[str, Any],
    now: float,
) -> NormalBehaviorProgress:
    """Commit one observed priority-anchor result to both learning owners."""

    anchor = progress.anchor
    if (
        anchor is None
        or anchor.status not in {"held", "reverted"}
        or anchor.learning_projected
    ):
        return progress

    projection_root = f"anchor-observation:{anchor.command_id}"
    memory_key = f"{projection_root}:memory"
    historical_key = f"{projection_root}:historical"
    log_event = getattr(getattr(engine, "diagnostics", None), "log_event", None)
    try:
        memory = engine.memory
        historical = engine.historical
        if not anchor_stability_projection_completed(memory, memory_key):
            record_anchor_stability(
                memory,
                band=anchor.target_band,
                stuck=anchor.status == "held",
                projection_key=memory_key,
            )
        if not anchor_stability_projection_completed(memory, memory_key):
            raise RuntimeError("anchor_memory_projection_not_committed")

        historical_completed = getattr(
            historical, "band_drift_projection_completed", None
        )
        historical_scope = getattr(historical, "current_evidence_scope", None)
        if not callable(historical_completed) or not callable(historical_scope):
            raise RuntimeError("anchor_historical_projection_owner_unavailable")
        if not historical_completed(historical_key):
            historical.record_band_drift(
                hour=time.localtime(float(now)).tm_hour,
                target_band=anchor.target_band,
                actual_band=anchor.actual_band,
                stuck=anchor.status == "held",
                sinr=_float(runtime_quality.get("sinr_db"), 0.0),
                ca_sig=str(
                    runtime_ca.get("ca_signature")
                    or runtime_ca.get("ca_band_signature")
                    or runtime_radio.get("serving_band_signature")
                    or ""
                ),
                evidence_scope=historical_scope(),
                projection_key=historical_key,
            )
        if not historical_completed(historical_key):
            raise RuntimeError("anchor_historical_projection_not_committed")
    except Exception as exc:
        if callable(log_event):
            log_event(
                "anchor_observation_learning_projection_failed",
                command_id=anchor.command_id,
                target_band=anchor.target_band,
                actual_band=anchor.actual_band,
                observation_status=anchor.status,
                reason=str(exc or "anchor_learning_projection_failed"),
                retry="next_smart_loop_clock",
            )
        return progress

    if callable(log_event):
        log_event(
            "anchor_observation_learning_projected",
            command_id=anchor.command_id,
            target_band=anchor.target_band,
            actual_band=anchor.actual_band,
            observation_status=anchor.status,
        )
    return mark_anchor_learning_projected(
        progress,
        command_id=anchor.command_id,
    )


def _apply_optimization_selected_action_receipt(
    engine: Any,
    *,
    state: SmartLoopState,
    now: float,
) -> SmartLoopState:
    """Project one terminal receipt; clearing waits for durable state commit."""

    register = require_action_transaction_authority(engine)
    terminal = register.read_terminal()
    if terminal is None:
        return state
    selected_action = _mapping(terminal.selected_action)
    if str(selected_action.get("owner_class") or "") != "optimization":
        raise RuntimeError(
            "smart_loop_received_non_optimization_terminal_action"
        )
    validated_template = validate_optimization_proof_template(
        terminal.proof_template,
        selected_action=selected_action,
    )
    pending_ack = str(
        getattr(engine, "_smart_loop_terminal_receipt_pending_ack", "") or ""
    )
    if pending_ack and pending_ack != terminal.command_id:
        raise RuntimeError("smart_loop_terminal_receipt_ack_collision")
    engine._smart_loop_terminal_receipt_pending_ack = terminal.command_id
    receipt = terminal.as_dict(include_selected_action=True)
    setattr(engine, "_smart_loop_last_selected_action_receipt", receipt)
    log_event = getattr(getattr(engine, "diagnostics", None), "log_event", None)
    command_outcome = terminal.terminal_outcome()
    progress, behavior_result = project_normal_behavior_terminal(
        state.normal_behavior_progress,
        selected_action=selected_action,
        command_receipt=(
            command_outcome.as_dict() if command_outcome is not None else None
        ),
        terminal_stage=terminal.stage.value,
        terminal_reason=str(terminal.terminal_reason or ""),
        now=now,
    )
    state = state.with_normal_behavior_progress(progress)
    _project_controller_posture_terminal_progress(
        engine,
        behavior_result=behavior_result,
        now=now,
        log_event=log_event,
    )
    failed_command = bool(
        command_outcome is not None
        and command_outcome.terminal_status
        in {
            CommandTerminalStatus.FAILED,
            CommandTerminalStatus.REJECTED,
            CommandTerminalStatus.TIMED_OUT,
            CommandTerminalStatus.CANCELLED,
        }
    )
    if (
        terminal.stage is SelectedActionStage.REJECTED
        or failed_command
    ):
        state = _apply_rejected_optimization_action_hold(state=state, terminal=terminal, now=now)
        hold = state.objective_holds.get(
            str(dict(terminal.proof_template or {}).get("objective_key") or "")
        )
        if callable(log_event):
            log_event(
                "smart_loop_selected_action_rejected",
                command_id=terminal.command_id,
                proof_id=terminal.proof_id,
                reason=str(terminal.terminal_reason or "command_rejected"),
                receipt=(
                    command_outcome.as_dict()
                    if command_outcome is not None
                    else dict(terminal.handoff_receipt or {})
                ),
                objective_hold_until_ts=float(getattr(hold, "until_ts", 0.0) or 0.0),
            )
        return state
    if terminal.stage is not SelectedActionStage.TERMINAL:
        raise RuntimeError(
            f"smart_loop_selected_action_terminal_stage_invalid:{terminal.stage.value}"
        )
    if command_outcome is None:
        raise RuntimeError(
            "smart_loop_selected_action_command_terminal_missing"
        )
    if (
        command_outcome.terminal_status
        is not CommandTerminalStatus.SETTLED
        or not command_outcome.proof_ready
    ):
        state = _apply_rejected_optimization_action_hold(
            state=state,
            terminal=terminal,
            now=now,
        )
        if callable(log_event):
            log_event(
                "smart_loop_selected_action_not_proof_ready",
                command_id=terminal.command_id,
                proof_id=terminal.proof_id,
                terminal_status=command_outcome.terminal_status.value,
                reason=str(
                    command_outcome.reason
                    or "command_terminal_not_proof_ready"
                ),
                receipt=command_outcome.as_dict(),
            )
        return state
    template = validated_template
    target_shape = dict(template.get("target_shape") or {})
    target_band, target_bands = _proof_band_projection(
        lever=str(template.get("lever") or ""),
        target_shape=target_shape,
    )
    proof = ProofWindow(
        action=str(template.get("action") or ""),
        target_band=target_band,
        context_key=str(template.get("context_key") or ""),
        reason_ref=str(template.get("reason_ref") or ""),
        proof_id=str(template.get("proof_id") or ""),
        command_id=str(template.get("command_id") or ""),
        before_cq=_optional_float(template.get("before_cq")),
        before_congestion_pct=_optional_float(template.get("before_congestion_pct")),
        lever=str(template.get("lever") or ""),
        target_shape=target_shape,
        target_bands=target_bands,
        objective_key=str(template.get("objective_key") or ""),
        opened_ts=float(
            command_outcome.settled_ts
            or command_outcome.terminal_ts
            or terminal.terminal_ts
            or now
        ),
        target_confirmation_timeout_s=_float(
            template.get("target_confirmation_timeout_s"),
            180.0,
        ),
        failed_objective_hold_s=_float(template.get("failed_objective_hold_s"), 900.0),
    )
    if (
        not proof.action
        or not proof.lever
        or not dict(proof.target_shape or {})
        or not proof.command_id
        or not proof.proof_id
    ):
        raise RuntimeError("smart_loop_verified_receipt_proof_template_invalid")
    if (
        state.pending_proof is not None
        and state.pending_proof.command_id != proof.command_id
    ):
        raise RuntimeError("smart_loop_verified_receipt_collides_with_pending_proof")
    if callable(log_event):
        log_event(
            "smart_loop_selected_action_verified",
            command_id=proof.command_id,
            proof_id=proof.proof_id,
            lever=proof.lever,
            target_shape=dict(proof.target_shape or {}),
            target_band=proof.target_band,
            target_bands=list(proof.target_bands),
            proof_opened_ts=proof.opened_ts,
            receipt=command_outcome.as_dict(),
        )
    return state.with_proof(proof)


def _persist_smart_loop_state_and_ack_terminal(
    engine: Any,
    *,
    state: SmartLoopState,
    now: float,
) -> None:
    """Commit state first, then clear exactly one terminal command receipt."""

    terminal_command_id = str(
        getattr(engine, "_smart_loop_terminal_receipt_pending_ack", "") or ""
    )
    require_smart_loop_state_store(engine).save(
        state,
        now=now,
        force=bool(terminal_command_id),
    )
    engine._smart_loop_state = state
    if terminal_command_id:
        require_action_transaction_authority(engine).acknowledge_optimization_terminal(
            command_id=terminal_command_id,
        )
        engine._smart_loop_terminal_receipt_pending_ack = ""


def _project_controller_posture_terminal_progress(
    engine: Any,
    *,
    behavior_result: NormalBehaviorResult | None,
    now: float,
    log_event: Any,
) -> None:
    """Project posture counters only while the selected episode still owns them."""

    if behavior_result is None or behavior_result.behavior_id not in {
        "degraded_best_live_neighbor",
        "degraded_open_all_deployed_bands",
        "congested_neighbor_escape",
        "congested_restore_known_good_ca",
    }:
        return
    authority = getattr(engine, "posture_authority", None)
    projector = getattr(authority, "project_normal_optimization_progress", None)
    current = getattr(authority, "current_posture", None)
    if not callable(projector) or current is None:
        raise RuntimeError("controller_posture_progress_projector_unavailable")
    selected_scope = str(behavior_result.progress_scope_id or "")
    current_scope = str(getattr(current, "progress_scope_id", "") or "")
    if not selected_scope:
        raise RuntimeError("controller_posture_selected_progress_scope_missing")
    if selected_scope != current_scope:
        if callable(log_event):
            log_event(
                "controller_posture_terminal_progress_episode_ended",
                command_id=behavior_result.command_id,
                behavior_id=behavior_result.behavior_id,
                selected_progress_scope_id=selected_scope,
                current_progress_scope_id=current_scope,
            )
        return
    projected = projector(
        progress_scope_id=selected_scope,
        receipt_id=behavior_result.command_id,
        behavior_id=behavior_result.behavior_id,
        checked_at=float(behavior_result.result_ts or now),
        action_taken=bool(behavior_result.success),
    )
    if callable(log_event):
        log_event(
            "controller_posture_terminal_progress_projected",
            command_id=behavior_result.command_id,
            behavior_id=behavior_result.behavior_id,
            projected=bool(projected),
            action_taken=bool(behavior_result.success),
        )


def _apply_rejected_optimization_action_hold(
    *,
    state: SmartLoopState,
    terminal: Any,
    now: float,
) -> SmartLoopState:
    """Prevent a fresh sample from immediately retrying a rejected objective."""

    template = dict(getattr(terminal, "proof_template", {}) or {})
    objective_key = str(template.get("objective_key") or "").strip()
    lever = str(template.get("lever") or "").strip()
    target_shape = dict(template.get("target_shape") or {})
    if not objective_key or not lever or not target_shape:
        return state
    command_outcome = terminal.terminal_outcome()
    handoff_receipt = dict(getattr(terminal, "handoff_receipt", {}) or {})
    requested_hold_s = (
        float(command_outcome.retry_after_s)
        if command_outcome is not None and command_outcome.retry_after_s > 0.0
        else _float(
            handoff_receipt.get("retry_after_s")
            or handoff_receipt.get("remaining_s")
            or handoff_receipt.get("cooldown_s"),
            60.0,
        )
    )
    max_hold_s = max(1.0, _float(template.get("failed_objective_hold_s"), 900.0))
    hold_s = min(max_hold_s, max(1.0, requested_hold_s))
    terminal_ts = float(getattr(terminal, "terminal_ts", 0.0) or now)
    target_band, target_bands = _proof_band_projection(
        lever=lever,
        target_shape=target_shape,
    )
    return state.with_objective_hold(
        ObjectiveHold(
            key=objective_key,
            action=str(template.get("action") or ""),
            target_band=target_band,
            lever=lever,
            target_shape=target_shape,
            target_bands=target_bands,
            until_ts=terminal_ts + hold_s,
            reason_code=f"command_rejected:{str(getattr(terminal, 'terminal_reason', '') or 'unknown')}",
            command_id=str(getattr(terminal, "command_id", "") or ""),
            proof_id=str(getattr(terminal, "proof_id", "") or ""),
        )
    )


def _proof_band_projection(
    *, lever: str, target_shape: Mapping[str, Any]
) -> tuple[int, tuple[int, ...]]:
    """Return display-only LTE band fields from the generic target shape."""

    name = str(lever or "").strip().upper()
    row = dict(target_shape or {})
    if name == "PBAND":
        primary = _int(row.get("pband"), 0)
        bands = tuple(
            sorted(
                {
                    _int(value, 0)
                    for value in list(row.get("ca_pool") or [])
                    if _int(value, 0) > 0
                }
                | ({primary} if primary > 0 else set())
            )
        )
        return primary, bands
    if name in {"BAND_SELECTION", "BAND_LOCK"}:
        bands = tuple(
            sorted(
                {
                    _int(value, 0)
                    for value in list(row.get("bands") or [])
                    if _int(value, 0) > 0
                }
            )
        )
        return (bands[0] if bands else 0), bands
    band = _int(row.get("band"), 0)
    return band, ((band,) if band > 0 else ())


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _strategy_input_signature(engine: Any, *, now: float) -> tuple[Any, ...]:
    """Return the owner-local key for expensive SmartLoop recomputation.

    The 100 ms runtime lane must stay alive, but full candidate/proof planning
    should only run when owner-stamped inputs changed or a declared time gate
    is due. The signature uses register/feed sequences, not raw dictionaries.
    """

    parts: list[Any] = []
    runtime_bank = require_core_runtime_register_bank(engine)
    row = runtime_bank.read_metadata(REGISTER_SMARTNESS)
    parts.append(
        (
            REGISTER_SMARTNESS,
            _bool(row.get("valid"), False),
            _bool(row.get("stale"), False),
            _bool(row.get("dirty"), False),
            str(row.get("source") or ""),
            str(row.get("source_kind") or ""),
            str(row.get("validity") or ""),
            str(row.get("error") or ""),
            str(row.get("signature") or "missing_smartness_semantic_signature"),
        )
    )

    feed_bank = require_brain_feed_register_bank(engine)
    for feeder in (FEED_LEARNING_QUALITY, FEED_ACTION_PROOF_CLOSURES):
        row = feed_bank.read_feeder(feeder).as_dict(now=now, include_payload=False)
        semantic_key: Any
        if feeder == FEED_LEARNING_QUALITY:
            cache = _mapping(
                getattr(engine, "_learning_quality_publication_cache", None)
            )
            semantic_key = str(cache.get("fingerprint") or "")
            if not semantic_key:
                semantic_key = _int(row.get("sequence"), 0)
        else:
            semantic_key = _int(row.get("sequence"), 0)
        parts.append(
            (
                feeder,
                _bool(row.get("valid"), False),
                _bool(row.get("stale"), False),
                str(row.get("error") or ""),
                semantic_key,
            )
        )

    command_status = require_action_transaction_authority(engine).selection_status()
    active = _mapping(command_status.get("active"))
    parts.append(
        (
            "action_transaction_authority",
            _int(command_status.get("sequence"), 0),
            str(active.get("command_id") or ""),
            str(active.get("stage") or ""),
        )
    )

    return tuple(parts)


def _cached_strategy_outcome(
    engine: Any,
    *,
    input_signature: tuple[Any, ...],
    now: float,
) -> ClockedJobOutcome | None:
    cache = _mapping(getattr(engine, _CLOCK_LANE_CACHE_ATTR, None))
    if not cache or cache.get("input_signature") != input_signature:
        return None
    publication = _mapping(cache.get("publication"))
    if not publication:
        return None
    next_recompute_ts = _float(cache.get("next_recompute_ts"), 0.0)
    if next_recompute_ts > 0.0 and float(now) >= next_recompute_ts:
        return None

    skipped = _int(cache.get("skipped_unchanged_count"), 0) + 1
    cache["skipped_unchanged_count"] = skipped
    cache["last_heartbeat_ts"] = round(float(now), 3)
    last_refresh_ts = _float(cache.get("last_refresh_ts"), 0.0)

    if (
        last_refresh_ts <= 0.0
        or float(now) - last_refresh_ts >= UNCHANGED_INPUT_REFRESH_S
    ):
        result = touch_smart_loop_strategy_register(
            engine,
            now=now,
            stale_after_s=DEFAULT_STALE_AFTER_S,
        )
        if not bool(result.get("touched", False)):
            return ClockedJobOutcome(
                ran=False,
                last_error=str(result.get("reason", "") or ""),
                backpressure_reason="smart_loop_strategy_touch_failed",
            )
        cache["last_refresh_ts"] = float(now)

    setattr(engine, _CLOCK_LANE_CACHE_ATTR, dict(cache))
    _publish_status(engine, now=now, refresh_fused=False)
    return ClockedJobOutcome(
        ran=True, backpressure_reason="smart_loop_inputs_unchanged"
    )


def _awaiting_selected_action_receipt_outcome(
    engine: Any,
    *,
    input_signature: tuple[Any, ...],
    now: float,
) -> ClockedJobOutcome | None:
    """Keep a Brain-selected action durable while the controller executes it."""

    register = require_action_transaction_authority(engine)
    status = register.selection_status()
    active = _mapping(status.get("active"))
    if not active or str(active.get("stage") or "") in {
        SelectedActionStage.TERMINAL.value,
        SelectedActionStage.REJECTED.value,
    }:
        return None
    cache = _mapping(getattr(engine, _CLOCK_LANE_CACHE_ATTR, None))
    publication = _mapping(cache.get("publication"))
    if not publication:
        raise RuntimeError(
            "brain_selected_action_active_without_strategy_projection"
        )
    selected_command_id = str(
        _mapping(
            _mapping(publication.get("action_transaction_authority")).get(
                "active"
            )
        ).get("command_id")
        or ""
    )
    if str(active.get("command_id") or "") != selected_command_id:
        raise RuntimeError(
            "brain_selected_action_strategy_identity_mismatch"
        )
    cache["input_signature"] = input_signature
    cache["last_heartbeat_ts"] = float(now)
    cache["skipped_unchanged_count"] = _int(cache.get("skipped_unchanged_count"), 0) + 1
    last_refresh_ts = _float(cache.get("last_refresh_ts"), 0.0)
    if (
        last_refresh_ts <= 0.0
        or float(now) - last_refresh_ts >= UNCHANGED_INPUT_REFRESH_S
    ):
        result = touch_smart_loop_strategy_register(
            engine,
            now=now,
            stale_after_s=DEFAULT_STALE_AFTER_S,
        )
        if not bool(result.get("touched", False)):
            return ClockedJobOutcome(
                ran=False,
                last_error=str(result.get("reason", "") or ""),
                backpressure_reason="smart_loop_strategy_touch_failed",
            )
        cache["last_refresh_ts"] = float(now)
    setattr(engine, _CLOCK_LANE_CACHE_ATTR, dict(cache))
    _publish_status(engine, now=now, refresh_fused=False)
    return ClockedJobOutcome(
        ran=True,
        backpressure_reason=f"smart_loop_command_awaiting_{str(active.get('stage') or 'lifecycle')}",
    )


def _store_strategy_cache(
    engine: Any,
    *,
    input_signature: tuple[Any, ...],
    publication: Mapping[str, Any],
    availability: Mapping[str, Any],
    now: float,
) -> None:
    setattr(
        engine,
        _CLOCK_LANE_CACHE_ATTR,
        {
            "input_signature": input_signature,
            "publication": dict(publication),
            "last_recompute_ts": float(now),
            "last_refresh_ts": float(now),
            "last_heartbeat_ts": float(now),
            "next_recompute_ts": _next_recompute_ts(
                publication=publication,
                availability=availability,
                now=now,
            ),
            "skipped_unchanged_count": 0,
        },
    )


def _next_recompute_ts(
    *,
    publication: Mapping[str, Any],
    availability: Mapping[str, Any],
    now: float,
) -> float:
    candidates: list[float] = []
    rows = [availability, _mapping(availability.get("runtime_settle"))]
    for row in rows:
        for key in (
            "cooldown_until_ts",
            "startup_grace_until_ts",
            "connect_settle_until_ts",
            "radio_change_settle_until_ts",
        ):
            until_ts = _float(row.get(key), 0.0)
            if until_ts > float(now):
                candidates.append(until_ts)
    state = _mapping(publication.get("state"))
    objective_holds = _mapping(state.get("objective_holds"))
    for hold in objective_holds.values():
        hold_row = _mapping(hold)
        until_ts = _float(hold_row.get("until_ts"), 0.0)
        if until_ts > float(now):
            candidates.append(until_ts)
    pending = _mapping(state.get("pending_proof"))
    opened_ts = _float(pending.get("opened_ts"), 0.0)
    timeout_s = _float(pending.get("target_confirmation_timeout_s"), 0.0)
    if opened_ts > 0.0 and timeout_s > 0.0:
        until_ts = opened_ts + timeout_s
        if until_ts > float(now):
            candidates.append(until_ts)
    return min(candidates) if candidates else 0.0


def _with_clock_lane_status(
    publication: Mapping[str, Any],
    *,
    now: float,
    mode: str,
    cache: Mapping[str, Any],
) -> dict[str, Any]:
    row = dict(publication or {})
    row["clock_lane"] = {
        "schema": "smart_loop.clock_lane_status.v1",
        "owner": "arch4.smart_loop.clock_lane",
        "mode": str(mode or ""),
        "heartbeat_ts": round(float(now), 3),
        "last_recompute_ts": round(
            _float(cache.get("last_recompute_ts"), float(now)), 3
        ),
        "next_recompute_ts": round(_float(cache.get("next_recompute_ts"), 0.0), 3),
        "skipped_unchanged_count": _int(cache.get("skipped_unchanged_count"), 0),
    }
    return row


def _runtime_values(engine: Any) -> dict[str, Any]:
    bank = require_core_runtime_register_bank(engine)
    row = bank.read(REGISTER_SMARTNESS).as_dict()
    value = _mapping(row.get("value"))
    sections = smartness_fact_sections(value)
    meta = _mapping(value.get("section_meta"))
    out: dict[str, Any] = {}
    for name in SMARTNESS_FACT_SECTIONS:
        section = _mapping(sections.get(name))
        section_meta = _mapping(meta.get(name))
        section.setdefault("_register_updated_at", section_meta.get("updated_at"))
        section.setdefault("_register_valid", section_meta.get("valid"))
        section.setdefault("_register_stale", section_meta.get("stale"))
        section.setdefault("_register_source_kind", section_meta.get("source_kind"))
        out[name] = section
    out["source_facts"] = smartness_fact_source_facts(value)
    out["frame_scope"] = _mapping(value.get("frame_scope"))
    return out


def apply_brain_candidate_selection(
    engine: Any,
    *,
    core: SmartLoopCore,
    decision: Any,
    context: Any,
    facts: Any,
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
    now: float,
) -> Any:
    """Select one complete SmartLoop opportunity through canonical owners."""

    if str(getattr(decision, "outcome", "") or "") not in {
        "candidate_set_ready",
        "no_action_justified",
    }:
        return decision
    candidates = tuple(getattr(decision, "candidates", ()) or ())
    optimization_candidates = [
        _candidate_opportunity_mapping(
            candidate,
            gate=core.candidate_gate(context=context, candidate=candidate),
        )
        for candidate in candidates
        if isinstance(candidate, Candidate)
    ]
    scope = _accepted_opportunity_evidence_scope(
        identity=runtime_identity,
        radio=runtime_radio,
        frame_scope=runtime_frame_scope,
        now=now,
    )
    source_snapshot = source_snapshot_from_smartness_register(
        engine=engine,
        context=context,
        runtime_identity=runtime_identity,
        runtime_radio=runtime_radio,
        runtime_cellular_slow=runtime_cellular_slow,
        runtime_latency=runtime_latency,
        runtime_quality=runtime_quality,
        runtime_ca=runtime_ca,
        runtime_traffic=runtime_traffic,
        runtime_decision=runtime_decision,
        runtime_health=runtime_health,
        runtime_frame_scope=runtime_frame_scope,
        runtime_source_facts=runtime_source_facts,
        normal_behavior_progress=normal_behavior_progress,
        optimization_candidates=optimization_candidates,
        decision_ts=now,
    )
    opportunity_set = build_action_opportunity_set(
        learning_context_key=str(
            getattr(context, "learning_context_key", "") or ""
        ),
        action_context_key=_action_context_key_from_registers(
            identity=runtime_identity,
            radio=runtime_radio,
            evidence_scope=scope,
        ),
        evidence_scope=scope,
        source_snapshot=source_snapshot,
    )
    brain = getattr(getattr(engine, "controller", None), "brain", None)
    store = getattr(brain, "contextual_bandit_state_store", None)
    if not isinstance(store, ContextualBanditStateStore):
        raise RuntimeError("contextual_bandit_state_store_unavailable")
    decision_seed = _selection_space_decision_seed(opportunity_set)
    bandit_receipt = select_contextual_bandit_candidate(
        opportunity_set=opportunity_set,
        state=store.state(),
        decision_seed=decision_seed,
        future_context=_mapping(getattr(facts, "prediction_context", {})),
    )
    store.record_selection_policy_effects(
        bandit_receipt,
        now=now,
    )
    # A rejected Bandit receipt is still passed to Brain. Brain owns the safe
    # do-nothing fallback and records why no optimization arm was eligible.
    bandit_for_brain: Mapping[str, Any] | None = bandit_receipt
    decision_id = "brain:" + hashlib.sha256(
        (
            str(opportunity_set.get("opportunity_set_id") or "")
            + "|"
            + str(bandit_receipt.get("selection_receipt_id") or "")
        ).encode("utf-8")
    ).hexdigest()[:32]
    brain_receipt = select_brain_decision(
        opportunity_set=opportunity_set,
        bandit_receipt=bandit_for_brain,
        decision_id=decision_id,
        decision_ts=now,
    )
    brain_error = brain_decision_receipt_error(
        brain_receipt,
        expected_opportunity_set_id=str(
            opportunity_set.get("opportunity_set_id") or ""
        ),
        expected_opportunity_id=str(
            brain_receipt.get("selected_opportunity_id") or ""
        ),
    )
    if brain_error:
        raise RuntimeError(f"brain_decision_receipt_invalid:{brain_error}")
    selected_opportunity = _mapping(brain_receipt.get("selected_opportunity"))
    if brain_receipt.get("do_nothing") is True:
        _project_congested_normal_policy_evaluation(
            engine,
            brain_receipt=brain_receipt,
            runtime_health=runtime_health,
            now=now,
        )
        return core.apply_authoritative_candidate(
            decision=decision,
            context=context,
            candidate=None,
            action_opportunity_set=opportunity_set,
            contextual_bandit_selection_receipt=bandit_receipt,
            brain_decision_receipt=brain_receipt,
        )
    owner_class = str(selected_opportunity.get("owner_class") or "")
    if owner_class == "optimization":
        plan = build_optimization_action_plan(
            opportunity_set=opportunity_set,
            brain_decision_receipt=brain_receipt,
            contextual_bandit_selection_receipt=bandit_receipt,
            context=context,
            now=now,
        )
        plan_row = plan.as_dict()
        materialized = materialize_selected_action(
            opportunity_set=opportunity_set,
            brain_decision_receipt=brain_receipt,
            contextual_bandit_selection_receipt=bandit_receipt,
            now=now,
            optimization_plan=plan_row,
        )
        proof_template = optimization_proof_template(plan)
        return replace(
            decision,
            outcome="action_selected_execution_pending",
            reason_code="brain_selected_optimization_action",
            required_next_step="latch_selected_action_for_controller",
            selected=None,
            tsar_gate=None,
            action_opportunity_set=dict(opportunity_set),
            contextual_bandit_selection_receipt=dict(bandit_receipt),
            brain_decision_receipt=dict(brain_receipt),
            brain_selected_action=materialized.as_dict(),
            brain_selected_action_proof_template=proof_template,
        )

    source_context, parameter_metadata = _selected_action_materialization_inputs(
        selected_opportunity
    )
    materialized = materialize_selected_action(
        opportunity_set=opportunity_set,
        brain_decision_receipt=brain_receipt,
        contextual_bandit_selection_receipt=None,
        now=now,
        source_context=source_context,
        parameter_metadata=parameter_metadata,
    )
    return replace(
        decision,
        outcome="action_selected_execution_pending",
        reason_code="brain_selected_non_optimization_action",
        required_next_step="latch_selected_action_for_controller",
        selected=None,
        tsar_gate=None,
        action_opportunity_set=dict(opportunity_set),
        contextual_bandit_selection_receipt=dict(bandit_receipt),
        brain_decision_receipt=dict(brain_receipt),
        brain_selected_action=materialized.as_dict(),
        brain_selected_action_proof_template={},
    )


def _project_congested_normal_policy_evaluation(
    engine: Any,
    *,
    brain_receipt: Mapping[str, Any],
    runtime_health: Mapping[str, Any],
    now: float,
) -> None:
    posture = _mapping(runtime_health.get("controller_posture"))
    if str(posture.get("state") or "").upper() != "CONGESTED":
        return
    receipt_id = str(brain_receipt.get("decision_receipt_id") or "").strip()
    progress_scope_id = str(posture.get("progress_scope_id") or "").strip()
    if not receipt_id or not progress_scope_id:
        raise RuntimeError("congested_normal_policy_evaluation_identity_missing")
    authority = getattr(engine, "posture_authority", None)
    projector = getattr(authority, "project_normal_optimization_progress", None)
    current = getattr(authority, "current_posture", None)
    if not callable(projector) or current is None:
        raise RuntimeError("controller_posture_progress_projector_unavailable")
    if str(getattr(current, "progress_scope_id", "") or "") != progress_scope_id:
        raise RuntimeError("congested_normal_policy_progress_scope_stale")
    projector(
        progress_scope_id=progress_scope_id,
        receipt_id=receipt_id,
        behavior_id="congested_normal_policy_evaluation",
        checked_at=float(now),
        action_taken=False,
    )


def _selected_action_materialization_inputs(
    selected_opportunity: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read explicit boundary metadata without creating or changing an action."""

    evidence = _mapping(selected_opportunity.get("evidence"))
    materialization = _mapping(evidence.get("materialization"))
    source_context: dict[str, Any] = {}
    for key in (
        "source",
        "reason",
        "intent",
        "objective_id",
        "expires_at_ts",
        "expires_after_s",
    ):
        value = materialization.get(key)
        if value not in (None, ""):
            source_context[key] = value
    parameter_metadata = _mapping(materialization.get("parameter_metadata"))
    return source_context, parameter_metadata


def _selection_space_decision_seed(
    opportunity_set: Mapping[str, Any],
) -> str:
    selection_space_id = str(
        opportunity_set.get("selection_space_id") or ""
    )
    if not selection_space_id:
        raise RuntimeError("smart_loop_selection_space_identity_missing")
    return "smart_loop_opportunity_set:" + selection_space_id


def _candidate_opportunity_mapping(
    candidate: Candidate,
    *,
    gate: Any,
) -> dict[str, Any]:
    row = candidate.as_dict()
    score = max(0.0, min(1.0, _float(row.get("score"), 0.0)))
    confidence = max(0.0, min(1.0, _float(row.get("confidence"), 0.0)))
    candidate_class = str(row.get("candidate_class") or "")
    direct_action = str(row.get("action") or "") == RADIO_CONFIGURATION_ACTION
    gate_verdict = str(getattr(gate, "verdict", "") or "")
    gate_reason = str(getattr(gate, "reason_code", "") or "")
    source_admitted = (
        candidate_class == "operational"
        and direct_action
        and gate_verdict == "act"
    )
    source_unavailability_reason = ""
    if not source_admitted:
        if gate_verdict and gate_verdict != "act":
            source_unavailability_reason = "smart_loop_candidate_gate:" + (
                gate_reason or gate_verdict
            )
        elif not direct_action:
            source_unavailability_reason = (
                "smart_loop_candidate_not_direct_action:"
                + str(row.get("action") or "unknown")
            )
        elif confidence < float(SmartLoopCore.min_confidence):
            source_unavailability_reason = "smart_loop_confidence_below_minimum"
        elif _int(row.get("evidence_count"), 0) < int(SmartLoopCore.min_evidence):
            source_unavailability_reason = "smart_loop_evidence_below_minimum"
        else:
            source_unavailability_reason = (
                "smart_loop_candidate_not_operational:"
                + (candidate_class or "unknown")
            )
    evidence = {
        "confidence": confidence,
        "evidence_count": max(0, _int(row.get("evidence_count"), 0)),
        "expected_referee_grade": max(-3.0, min(3.0, (score * 6.0) - 3.0)),
        "smart_loop_score": score,
        "smart_loop_roi_bias": _float(row.get("roi_bias"), 0.0),
        "smart_loop_reason_ref": str(row.get("reason_ref") or ""),
        "smart_loop_reason_code": str(row.get("reason_code") or ""),
        "smart_loop_context_key": str(row.get("context_key") or ""),
        "smart_loop_context": _mapping(row.get("context")),
    }
    return {
        "source": "arch4.smart_loop.closed_loop",
        "reason": str(row.get("reason") or row.get("reason_code") or ""),
        "target_band": _int(row.get("target_band"), 0),
        "target_bands": _int_list(row.get("target_bands") or ()),
        "confidence": confidence,
        "evidence_count": evidence["evidence_count"],
        "uncertainty": 1.0 - confidence,
        "disruption_cost": 0.65,
        "source_admitted": source_admitted,
        "source_unavailability_reason": source_unavailability_reason,
        "evidence": evidence,
    }


def _accepted_opportunity_evidence_scope(
    *,
    identity: Mapping[str, Any],
    radio: Mapping[str, Any],
    frame_scope: Mapping[str, Any],
    now: float,
) -> dict[str, Any]:
    observed_at = _float(radio.get("sample_ts"), 0.0)
    accepted_scope = _mapping(frame_scope)
    raw = {
        "identity_code": str(accepted_scope.get("identity_code") or ""),
        "identity_status": str(identity.get("identity_status") or ""),
        "identity_catalog_version": str(
            identity.get("identity_catalog_version")
            or identity.get("catalog_version")
            or ""
        ),
        "identity_catalog_checksum": str(
            identity.get("identity_catalog_checksum")
            or identity.get("catalog_checksum")
            or ""
        ),
        "session_scope_key": str(
            accepted_scope.get("session_scope_key") or ""
        ),
        "source_epoch": str(accepted_scope.get("source_epoch") or ""),
        "source_sequence": _int(
            accepted_scope.get("source_sequence"), 0
        ),
        "sample_ts": observed_at,
        "sample_age_s": max(0.0, float(now) - observed_at)
        if observed_at > 0.0
        else 0.0,
        "provider_plmn": str(identity.get("provider_plmn") or ""),
        "access_type_id": str(
            identity.get("access_type_id")
            or identity.get("access_type")
            or ""
        ),
        "model_family_id": str(identity.get("model_family_id") or ""),
        "serving_band": _int(
            radio.get("serving_band", radio.get("primary_band")), 0
        ),
        "earfcn": _int(radio.get("earfcn_dl", radio.get("earfcn")), 0),
        "pci": _int(radio.get("pci"), 0),
    }
    try:
        return evidence_scope_from_mapping(
            raw,
            formula_vector=_OPPORTUNITY_FORMULA_VECTOR,
            observed_at=observed_at if observed_at > 0.0 else None,
        ).as_dict()
    except (TypeError, ValueError):
        # The opportunity owner records the exact scope rejection and keeps only
        # its mandatory do-nothing arm executable. Never synthesize identity.
        return {}


def _action_context_key_from_registers(
    *,
    identity: Mapping[str, Any],
    radio: Mapping[str, Any],
    evidence_scope: Mapping[str, Any],
) -> str:
    """Cross the register boundary through the sole action-key formula."""

    scope = _mapping(evidence_scope)
    if not scope:
        return "unadmitted_action_context"
    return current_action_context_key_from_values(
        identity_code=str(scope.get("identity_code") or ""),
        provider_plmn=str(identity.get("provider_plmn") or ""),
        cell_id=_int(radio.get("cell_id"), 0),
        pci=_int(radio.get("pci"), 0),
        earfcn=_int(radio.get("earfcn_dl", radio.get("earfcn")), 0),
        session_scope_key=str(scope.get("session_scope_key") or ""),
    )


def _optional_bool_value(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _runtime_settle_availability(
    *,
    engine: Any,
    runtime_radio: Mapping[str, Any],
    runtime_traffic: Mapping[str, Any],
    now: float,
) -> dict[str, Any]:
    """Publish SmartLoop-owned runtime-settle facts before action selection.

    SmartLoop must always be alive, including while disconnected. Lack of live
    context is represented as an availability blocker, not as a dormant loop.
    """

    cfg = getattr(engine, "cfg", None)
    state = _mapping(getattr(engine, "_smart_loop_runtime_settle_state", None))
    connected = _bool(runtime_radio.get("radio_connected"), False)
    computed_at_ts = round(float(now), 3)
    if not connected:
        state = {
            "connected_since": 0.0,
            "last_serving_key": (),
            "radio_change_since": 0.0,
        }
        engine._smart_loop_runtime_settle_state = state
        return {
            "schema": "smart_loop.runtime_settle_availability.v1",
            "owner": "arch4.smart_loop.action_availability",
            "source": "smart_loop_clock_lane",
            "computed_at_ts": computed_at_ts,
            "connected": False,
            "context_ready": False,
            "context_unready_active": True,
            "context_unready_reason": "radio_disconnected",
            "context_unready_remaining_s": 0.0,
        }

    connected_since = _float(state.get("connected_since"), 0.0)
    if connected_since <= 0.0:
        connected_since = float(now)
    observed_connected_s = max(0.0, float(now) - connected_since)

    raw_serving_key = _radio_serving_key(runtime_radio)
    radio_observed_at = _float(runtime_radio.get("_register_updated_at"), float(now))
    if radio_observed_at <= 0.0:
        radio_observed_at = float(now)
    (
        serving_key,
        radio_change_since,
        serving_state,
    ) = _confirmed_radio_serving_key_state(
        state=state,
        raw_serving_key=raw_serving_key,
        observed_at=radio_observed_at,
        now=float(now),
    )

    state = {
        "connected_since": connected_since,
        **serving_state,
        "radio_change_since": radio_change_since,
    }
    engine._smart_loop_runtime_settle_state = state

    startup_grace_s = max(0.0, _float(getattr(cfg, "startup_grace_s", 0.0), 0.0))
    startup_elapsed_s = max(
        0.0, float(now) - _float(getattr(engine, "_startup_time", 0.0), 0.0)
    )
    startup_remaining_s = max(0.0, startup_grace_s - startup_elapsed_s)

    connect_settle_s = max(
        0.0, _float(getattr(cfg, "autonomous_connect_settle_s", 0.0), 0.0)
    )
    session_uptime_s = _float(runtime_radio.get("session_uptime_s"), 0.0)
    if session_uptime_s <= 0.0:
        session_uptime_s = _float(runtime_traffic.get("session_uptime_s"), 0.0)
    settle_elapsed_s = (
        min(session_uptime_s, observed_connected_s)
        if session_uptime_s > 0.0
        else observed_connected_s
    )
    connect_remaining_s = max(0.0, connect_settle_s - settle_elapsed_s)

    radio_change_settle_s = max(
        0.0,
        _float(
            getattr(cfg, "radio_change_settle_s", connect_settle_s), connect_settle_s
        ),
    )
    radio_change_remaining_s = 0.0
    if radio_change_settle_s > 0.0 and radio_change_since > 0.0:
        radio_change_remaining_s = max(
            0.0, radio_change_settle_s - max(0.0, float(now) - radio_change_since)
        )

    return {
        "schema": "smart_loop.runtime_settle_availability.v1",
        "owner": "arch4.smart_loop.action_availability",
        "source": "smart_loop_clock_lane",
        "computed_at_ts": computed_at_ts,
        "connected": True,
        "context_ready": True,
        "context_unready_active": bool(
            serving_state.get("identity_pending_confirmation_active", False)
        ),
        "context_unready_reason": (
            "radio_identity_pending_confirmation"
            if bool(serving_state.get("identity_pending_confirmation_active", False))
            else ""
        ),
        "identity_pending_confirmation_active": bool(
            serving_state.get("identity_pending_confirmation_active", False)
        ),
        "identity_pending_confirmation_count": _int(
            serving_state.get("pending_serving_count"),
            0,
        ),
        "raw_serving_key": list(raw_serving_key),
        "startup_grace_active": startup_remaining_s > 0.0,
        "startup_grace_remaining_s": round(startup_remaining_s, 3),
        "startup_grace_until_ts": _until_ts(now, startup_remaining_s),
        "connect_settle_active": connect_remaining_s > 0.0,
        "connect_settle_remaining_s": round(connect_remaining_s, 3),
        "connect_settle_until_ts": _until_ts(now, connect_remaining_s),
        "radio_change_settle_active": radio_change_remaining_s > 0.0,
        "radio_change_settle_remaining_s": round(radio_change_remaining_s, 3),
        "radio_change_settle_until_ts": _until_ts(now, radio_change_remaining_s),
        "session_uptime_s": round(max(0.0, session_uptime_s), 3),
        "observed_connected_s": round(observed_connected_s, 3),
        "serving_key": list(serving_key),
    }


def _until_ts(now: float, remaining_s: float) -> float:
    remaining = max(0.0, _float(remaining_s, 0.0))
    if remaining <= 0.0:
        return 0.0
    return round(float(now) + remaining, 3)


def _radio_serving_key(
    runtime_radio: Mapping[str, Any],
) -> tuple[int, int, int, int, int]:
    return (
        _int(
            runtime_radio.get("serving_band")
            if "serving_band" in runtime_radio
            else runtime_radio.get("primary_band"),
            0,
        ),
        _int(runtime_radio.get("pci"), 0),
        _int(
            runtime_radio.get("earfcn_dl")
            if "earfcn_dl" in runtime_radio
            else runtime_radio.get("earfcn"),
            0,
        ),
        _int(runtime_radio.get("cell_id"), 0),
        _int(runtime_radio.get("enb_id"), 0),
    )


def _radio_serving_key_valid(serving_key: tuple[int, int, int, int, int]) -> bool:
    band, pci, earfcn, cell_id, _enb_id = serving_key
    return int(band or 0) > 0 and any(
        int(value or 0) > 0 for value in (pci, earfcn, cell_id)
    )


def _confirmed_radio_serving_key_state(
    *,
    state: Mapping[str, Any],
    raw_serving_key: tuple[int, int, int, int, int],
    observed_at: float,
    now: float,
) -> tuple[tuple[int, int, int, int, int], float, dict[str, Any]]:
    """Debounce radio identity before SmartLoop starts autonomous settle.

    Raw radio identity is allowed to change immediately for logs and GUI, but
    autonomous action availability is a safety owner. It needs a second fresh
    modem sample before treating a different identity as a real transition.
    This prevents one-sample parser/backend wobble from restarting the 30 s
    radio-change settle clock.
    """

    previous_key = tuple(state.get("last_serving_key") or ())
    radio_change_since = _float(state.get("radio_change_since"), 0.0)
    pending_key = tuple(state.get("pending_serving_key") or ())
    pending_count = max(0, _int(state.get("pending_serving_count"), 0))
    pending_since = _float(state.get("pending_serving_since"), 0.0)
    pending_last_observed_at = _float(
        state.get("pending_serving_last_observed_at"), 0.0
    )

    if not _radio_serving_key_valid(raw_serving_key):
        return (
            previous_key,
            radio_change_since,
            {
                "last_serving_key": previous_key,
                "pending_serving_key": (),
                "pending_serving_count": 0,
                "pending_serving_since": 0.0,
                "pending_serving_last_observed_at": 0.0,
                "identity_pending_confirmation_active": False,
                "serving_key_status": "held_invalid" if previous_key else "invalid",
            },
        )

    if not previous_key:
        return (
            raw_serving_key,
            radio_change_since,
            {
                "last_serving_key": tuple(raw_serving_key),
                "pending_serving_key": (),
                "pending_serving_count": 0,
                "pending_serving_since": 0.0,
                "pending_serving_last_observed_at": 0.0,
                "identity_pending_confirmation_active": False,
                "serving_key_status": "initial",
            },
        )

    if raw_serving_key == previous_key:
        return (
            previous_key,
            radio_change_since,
            {
                "last_serving_key": tuple(previous_key),
                "pending_serving_key": (),
                "pending_serving_count": 0,
                "pending_serving_since": 0.0,
                "pending_serving_last_observed_at": 0.0,
                "identity_pending_confirmation_active": False,
                "serving_key_status": "stable",
            },
        )

    if raw_serving_key != pending_key:
        pending_key = raw_serving_key
        pending_count = 1
        pending_since = float(now)
        pending_last_observed_at = float(observed_at)
    elif float(observed_at) > pending_last_observed_at + 1e-6:
        pending_count += 1
        pending_last_observed_at = float(observed_at)

    if pending_count >= 2:
        return (
            pending_key,
            float(now),
            {
                "last_serving_key": tuple(pending_key),
                "pending_serving_key": (),
                "pending_serving_count": 0,
                "pending_serving_since": 0.0,
                "pending_serving_last_observed_at": 0.0,
                "identity_pending_confirmation_active": False,
                "serving_key_status": "confirmed_change",
            },
        )

    return (
        previous_key,
        radio_change_since,
        {
            "last_serving_key": tuple(previous_key),
            "pending_serving_key": tuple(pending_key),
            "pending_serving_count": int(pending_count),
            "pending_serving_since": round(float(pending_since), 3)
            if pending_since > 0.0
            else 0.0,
            "pending_serving_last_observed_at": round(
                float(pending_last_observed_at), 3
            )
            if pending_last_observed_at > 0.0
            else 0.0,
            "identity_pending_confirmation_active": True,
            "serving_key_status": "pending_confirmation",
        },
    )


def _learning_quality_publication(engine: Any, *, now: float) -> dict[str, Any]:
    bank = require_brain_feed_register_bank(engine)
    row = bank.read_feeder(FEED_LEARNING_QUALITY).as_dict(now=now)
    if str(row.get("error") or "") or not bool(row.get("valid", False)):
        return {}
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    return dict(payload)


def _clean_learning_inputs(
    *,
    engine: Any,
    runtime_radio: Mapping[str, Any],
    runtime_learning: Mapping[str, Any],
    runtime_capacity: Mapping[str, Any],
    learning_quality: Mapping[str, Any],
    now: float,
) -> dict[str, Any]:
    quality = _mapping(learning_quality.get("learning_quality"))
    if not quality:
        quality = _mapping(learning_quality)
    current_hour = _int(
        learning_quality.get("current_hour")
        if "current_hour" in learning_quality
        else runtime_learning.get("current_hour"),
        int(time.localtime(float(now)).tm_hour),
    )
    band_scores: dict[int, float] = {}
    evidence_counts: dict[int, int] = {}
    for key, raw_score in _mapping(quality.get("band_hour_quality")).items():
        band, hour = _split_band_hour(key)
        if band > 0 and hour == current_hour:
            band_scores[band] = _clamp_score(raw_score)
    for key, raw_count in _mapping(quality.get("band_hour_counts")).items():
        band, hour = _split_band_hour(key)
        if band > 0 and hour == current_hour:
            evidence_counts[band] = max(0, _int(raw_count, 0))
    for key, raw_score in _mapping(quality.get("band_quality")).items():
        band = _int(key, 0)
        if band > 0 and band not in band_scores:
            band_scores[band] = _clamp_score(raw_score)
    calibration_fact_package = _calibration_fact_package(runtime_learning)
    control_authority_bands = _int_list(quality.get("control_authority_bands"))
    prediction_context = _crystal_ball_horizon_context(
        engine=engine,
        runtime_radio=runtime_radio,
        runtime_capacity=runtime_capacity,
        deployed_bands=control_authority_bands,
        now=now,
    )
    return {
        "current_hour": current_hour,
        "band_scores": band_scores,
        "evidence_counts": evidence_counts,
        "profile_confidence": _float(quality.get("profile_confidence"), 0.0),
        "sample_count": _int(quality.get("sample_count"), 0),
        "ca_active": runtime_learning.get("ca_active"),
        "proof_pending": runtime_learning.get("proof_pending"),
        "reason": str(quality.get("quality_trend") or quality.get("reason") or ""),
        "action_roi": _mapping(
            quality.get("pband_action_roi") or quality.get("action_roi")
        ),
        "control_authority_bands": control_authority_bands,
        "control_authority_owner": str(quality.get("control_authority_owner") or ""),
        "calibration_fact_package": calibration_fact_package,
        "prediction_context": prediction_context,
        "measured_capacity_fact": _measured_capacity_fact(runtime_capacity),
        "passive_dl_peak_fact": _passive_dl_peak_fact(runtime_capacity),
    }


def _crystal_ball_horizon_context(
    *,
    engine: Any,
    runtime_radio: Mapping[str, Any],
    runtime_capacity: Mapping[str, Any],
    deployed_bands: list[int],
    now: float,
) -> dict[str, Any]:
    crystal_ball = getattr(engine, "crystal_ball", None)
    builder = getattr(crystal_ball, "build_horizon_context", None)
    if not callable(builder):
        return {}
    current_band = _int(
        runtime_radio.get("serving_band")
        if "serving_band" in runtime_radio
        else runtime_radio.get("primary_band"),
        0,
    )
    if current_band <= 0 or not deployed_bands:
        return {}
    row = builder(
        current_band=current_band,
        deployed_bands=deployed_bands,
        current_cell_id=_int(runtime_radio.get("cell_id"), 0),
        measured_capacity_fact=_measured_capacity_fact(runtime_capacity),
        passive_dl_peak_fact=_passive_dl_peak_fact(runtime_capacity),
        now_ts=float(now),
    )
    if not isinstance(row, Mapping):
        raise ValueError("crystal_ball_horizon_context_not_mapping")
    if row.get("schema") != "crystal_ball_horizon_context.v1":
        raise ValueError("crystal_ball_horizon_context_bad_schema")
    if row.get("owner") != "arch4.brain.crystal_ball":
        raise ValueError("crystal_ball_horizon_context_bad_owner")
    if not bool(row.get("advisory_only", False)) or bool(
        row.get("direct_modem_actuation", True)
    ):
        raise ValueError("crystal_ball_horizon_context_not_advisory")
    return dict(row)


def _proof_grade_from_action_proof_register(
    engine: Any,
    *,
    state: SmartLoopState,
    now: float,
) -> ProofGrade | None:
    pending = getattr(state, "pending_proof", None)
    if pending is None:
        return None
    bank = require_brain_feed_register_bank(engine)
    row = bank.read_feeder(FEED_ACTION_PROOF_CLOSURES).as_dict(now=now)
    if str(row.get("error") or "") or not bool(row.get("valid", False)):
        return None
    payload = _mapping(row.get("payload"))
    if str(payload.get("schema") or "") != "brain_action_proof_register.v1":
        return None
    if str(payload.get("owner") or "") != "arch4.brain.action_proof_register":
        return None
    recent = payload.get("recent") if isinstance(payload.get("recent"), list) else []
    matching = [
        _mapping(raw)
        for raw in recent
        if _proof_row_matches_pending(_mapping(raw), pending)
    ]
    done = {
        _int(value, 0)
        for value in getattr(pending, "feedback_horizons_done", ())
        if _int(value, 0) > 0
    }
    for proof_row in sorted(
        (row for row in matching if not _bool(row.get("terminal"), True)),
        key=lambda row: _int(row.get("roi_horizon_s"), 0),
    ):
        if _int(proof_row.get("roi_horizon_s"), 0) in done:
            continue
        return _proof_grade_from_row(proof_row, pending)
    for proof_row in reversed(matching):
        if _bool(proof_row.get("terminal"), True):
            return _proof_grade_from_row(proof_row, pending)
    return None


def _proof_row_matches_pending(row: Mapping[str, Any], pending: Any) -> bool:
    actual_proof_id = str(row.get("selected_action_proof_id") or "").strip()
    actual_command_id = str(row.get("selected_action_command_id") or "").strip()
    actual_context_key = str(row.get("smart_loop_context_key") or "").strip()
    expected_proof_id = str(getattr(pending, "proof_id", "") or "").strip()
    expected_command_id = str(getattr(pending, "command_id", "") or "").strip()
    expected_context_key = str(getattr(pending, "context_key", "") or "").strip()
    actual_reason_ref = str(row.get("smart_loop_reason_ref") or "").strip()
    expected_reason_ref = str(getattr(pending, "reason_ref", "") or "").strip()
    return bool(
        expected_proof_id
        and actual_proof_id == expected_proof_id
        and expected_command_id
        and actual_command_id == expected_command_id
        and expected_context_key
        and actual_context_key == expected_context_key
        and (not expected_reason_ref or actual_reason_ref == expected_reason_ref)
    )


def _proof_grade_from_row(row: Mapping[str, Any], pending: Any) -> ProofGrade:
    terminal = _bool(row.get("terminal"), True)
    evidence_valid = _bool(row.get("grade_evidence_valid"), False)
    label = str(row.get("label") or "").strip().lower()
    owner_stamped_feedback = _mapping(row.get("layered_feedback"))
    terminal_feedback_valid = terminal_layered_action_feedback_is_valid(
        owner_stamped_feedback,
        horizon_s=_int(row.get("roi_horizon_s"), 0),
    )
    if terminal and not terminal_feedback_valid:
        label = "invalid"
        memory_admitted = False
        reason_code = "terminal_layered_reward_package_invalid"
    elif terminal and not bool(owner_stamped_feedback.get("context_clean", False)):
        label = "invalid"
        memory_admitted = False
        reason_code = "terminal_layered_reward_context_quarantined"
    elif label not in {"good", "bad", "neutral"} or not evidence_valid:
        label = "invalid"
        memory_admitted = False
        reason_code = str(
            row.get("referee_code") or row.get("reason") or "proof_evidence_invalid"
        )
    else:
        memory_admitted = bool(terminal and row.get("memory_admitted", False))
        reason_code = str(
            row.get("referee_code") or row.get("reason") or f"proof_closed_{label}"
        )
    feedback = (
        dict(owner_stamped_feedback)
        if terminal and terminal_feedback_valid
        else build_smart_loop_layered_feedback(
            label=label,
            reason=reason_code,
            cq_delta=_optional_delta(
                _first_present(
                    row, "cq_after", "cq_now", "cq_cell_after", "cq_cell_now"
                ),
                _first_present(row, "cq_before", "cq_cell_before"),
            ),
            congestion_delta=_optional_delta(
                _first_present(
                    row,
                    "congestion_after",
                    "congestion_now",
                    "congestion_pct_after",
                    "congestion_pct_now",
                ),
                _first_present(row, "congestion_before", "congestion_pct_before"),
            ),
            evidence=row,
            horizon_s=_int(row.get("roi_horizon_s"), 0),
        )
    )
    return ProofGrade(
        label=label,
        reason_code=reason_code,
        memory_admitted=memory_admitted,
        proof_id=str(getattr(pending, "proof_id", "") or ""),
        command_id=str(getattr(pending, "command_id", "") or ""),
        context_key=str(getattr(pending, "context_key", "") or ""),
        reason_ref=str(getattr(pending, "reason_ref", "") or ""),
        layered_feedback=feedback,
        horizon_s=_int(row.get("roi_horizon_s"), 0),
        terminal=terminal,
        prediction_error_recorded=_bool(
            row.get("prediction_error_recorded"), False
        ),
        policy_update_applied=_bool(
            row.get("policy_update_applied"), False
        ),
        future_policy_effect_status=str(
            row.get("future_policy_effect_status") or ""
        ),
    )


def _calibration_fact_package(runtime_learning: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(runtime_learning.get("calibration_fact_package"))


def _measured_capacity_fact(runtime_capacity: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(runtime_capacity)
    if row.get("schema") != "capacity_evidence_register.v1":
        return {}
    if row.get("owner") != "arch4.smart_loop.capacity_probe":
        return {}
    row = _mapping(row.get("active_capacity_fact"))
    if row.get("schema") != "measured_capacity_fact.v1":
        return {}
    if row.get("owner") != "arch4.smart_loop.capacity_probe":
        return {}
    if not bool(row.get("measured", False)):
        return {}
    return row


def _passive_dl_peak_fact(runtime_capacity: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(runtime_capacity)
    if row.get("schema") != "capacity_evidence_register.v1":
        return {}
    if row.get("owner") != "arch4.smart_loop.capacity_probe":
        return {}
    row = _mapping(row.get("passive_dl_peak_fact"))
    if row.get("schema") != "passive_dl_peak_fact.v1":
        return {}
    if row.get("owner") != "arch4.smart_loop.capacity_probe":
        return {}
    if not bool(row.get("observed", False)):
        return {}
    return row


def _publish_status(engine: Any, *, now: float, refresh_fused: bool = True) -> None:
    try:
        publish_brain_feed_registers_status(
            engine, now=now, refresh_fused=refresh_fused
        )
    except Exception:
        pass


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _first_present(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _optional_delta(after: Any, before: Any) -> float | None:
    if after in (None, "") or before in (None, ""):
        return None
    try:
        return float(after) - float(before)
    except (TypeError, ValueError):
        return None


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "ok", "valid"}:
        return True
    if text in {"0", "false", "no", "off", "invalid"}:
        return False
    return bool(default)


def _clamp_score(value: Any) -> float:
    return max(0.0, min(1.0, _float(value, 0.0)))


def _int_list(value: Any) -> list[int]:
    if isinstance(value, str):
        value = value.replace("+", ",").split(",")
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[int] = []
    for item in value:
        parsed = _int(item, 0)
        if parsed > 0 and parsed not in out:
            out.append(parsed)
    return out


def _split_band_hour(value: Any) -> tuple[int, int]:
    parts = str(value or "").split(":", 1)
    if len(parts) != 2:
        return 0, -1
    return _int(parts[0], 0), _int(parts[1], -1)


__all__ = [
    "apply_brain_candidate_selection",
    "run_smart_loop_strategy_clock_lane",
]
