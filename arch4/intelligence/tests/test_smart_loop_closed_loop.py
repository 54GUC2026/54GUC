from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import time

import pytest

from arch4.core.action_source_state_authority import ActionSourceStateAuthority
from arch4.brain.feed_registers import (
    BrainFeedRegisterBank,
    FEED_ACTION_PROOF_CLOSURES,
    FEED_LEARNING_QUALITY,
    FEED_SELF_LEARNING_STRATEGY,
    OWNER_ACTION_PROOF_REGISTER,
)
from arch4.brain.proof_feedback_owner import build_layered_action_feedback
from arch4.brain.proof_referee_brain_event_owner import publish_staged_roi_brain_event
from arch4.brain.proof_referee_record_builder_owner import build_staged_roi_record
from arch4.interfaces import Arch4Config, LeverType, ModemInput
from arch4.contextual_bandit_state_store import ContextualBanditStateStore
from arch4.interfaces import ModemCapabilities
from arch4.runtime_registers import (
    REGISTER_CAPACITY,
    REGISTER_DECISION,
    REGISTER_IDENTITY,
    REGISTER_RADIO,
    RuntimeRegisterBank,
)
from arch4.runtime_decision_projection import (
    BOUNCER_PROJECTION_OWNER,
    BOUNCER_PROJECTION_SCHEMA,
    BOUNCER_SOURCE_OWNER,
    build_runtime_decision_projection,
)
from arch4.smartness_fact_register import publish_smartness_fact_register
from arch4.smart_loop.action_availability import build_action_availability
from arch4.smart_loop.clock_lane import (
    _candidate_opportunity_mapping,
    _strategy_input_signature,
    _runtime_settle_availability,
    run_smart_loop_strategy_clock_lane,
)
from arch4.smart_loop.closed_loop import (
    Candidate,
    LearningFacts,
    ModemContext,
    ProofGrade,
    RADIO_CONFIGURATION_ACTION,
    SmartLoopCore,
    SmartLoopObservation,
    SmartLoopState,
    TsarGate,
    default_smart_loop_matrix,
    run_smart_loop_matrix,
)
from arch4.smart_loop.capacity_probe import run_capacity_probe_clock_lane
from arch4.action_transaction_authority import ActionTransactionAuthority
from arch4.command_transactions import CommandAuthority
from arch4.smart_loop.state_store import SmartLoopStateStore
from arch4.tests.identity_relative_rf_proof_harness import (
    run_identity_relative_signal_simulation,
)
from arch4.tests.controller_generation_factory import (
    materialize_controller_generation,
)
from arch4.tests.smart_loop_test_helpers import (
    claim_selected_action_for_test,
    publish_smart_loop_radio_command_for_test,
    smartness_source_transaction_for_test,
    terminalize_selected_action_for_test,
)
from arch4.tests.runtime_signal_register_test_support import (
    publish_test_signal_generation,
)
from arch4.tests.runtime_identity_register_test_support import (
    install_test_identity_register,
)
from arch4.tests.config_generation_test_support import (
    ActiveConfigProjectionTestHost,
    install_active_config_generation_for_test,
)
from arch4.smart_loop.opportunity_matrix import build_opportunity_matrix
from arch4.smart_loop.register_adapter import smart_loop_inputs_from_registers
from arch4.smart_loop.strategy_register import read_smart_loop_strategy_register


ROOT = Path(__file__).resolve().parents[2]
_ACTION_READY_CONFIG = Arch4Config(
    startup_grace_s=0.0,
    autonomous_connect_settle_s=0.0,
    radio_change_settle_s=0.0,
)


def test_smart_loop_candidate_source_gate_preserves_blocked_evidence() -> None:
    blocked = _candidate_opportunity_mapping(
        Candidate(
            action=RADIO_CONFIGURATION_ACTION,
            target_band=3,
            target_bands=(1, 3, 20),
            context_key="ctx",
            score=0.82,
            confidence=0.9,
            evidence_count=1,
            reason_code="history_candidate",
            reason="history candidate below minimum evidence",
            reason_ref="0xblocked",
        ),
        gate=TsarGate("refused", "learning_evidence_low", "collect_more_samples"),
    )
    admitted = _candidate_opportunity_mapping(
        Candidate(
            action=RADIO_CONFIGURATION_ACTION,
            target_band=3,
            target_bands=(1, 3, 20),
            context_key="ctx",
            score=0.82,
            confidence=0.9,
            evidence_count=4,
            reason_code="history_candidate",
            reason="history candidate admitted",
            reason_ref="0xadmitted",
        ),
        gate=TsarGate("act", "tsar_approved", "materialize_selected_action"),
    )
    stay_current = _candidate_opportunity_mapping(
        Candidate(
            action="stay_current_anchor",
            target_band=3,
            target_bands=(3,),
            context_key="ctx",
            score=0.82,
            confidence=0.9,
            evidence_count=4,
            reason_code="current_anchor_is_best_known",
            reason="current anchor remains the best forecast",
            reason_ref="0xstay",
        ),
        gate=TsarGate(
            "no_action",
            "current_anchor_is_best_known",
            "keep_observing",
        ),
    )

    assert blocked["source_admitted"] is False
    assert (
        blocked["source_unavailability_reason"]
        == "smart_loop_candidate_gate:learning_evidence_low"
    )
    assert blocked["evidence"]["evidence_count"] == 1
    assert admitted["source_admitted"] is True
    assert admitted["source_unavailability_reason"] == ""
    assert stay_current["source_admitted"] is False
    assert (
        stay_current["source_unavailability_reason"]
        == "smart_loop_candidate_gate:current_anchor_is_best_known"
    )


def _terminal_proof_grade(
    state: SmartLoopState,
    *,
    label: str,
    reason: str,
    cq_delta: float | None,
    congestion_delta: float | None,
    admitted: bool,
) -> ProofGrade:
    proof = state.pending_proof
    assert proof is not None
    feedback = build_layered_action_feedback(
        label=label,
        reason=reason,
        cq_delta=cq_delta,
        congestion_delta=congestion_delta,
        evidence={
            "grade_evidence_valid": admitted,
            "grade_context_status": "same" if admitted else "invalid",
            "grade_validity_reason": reason,
        },
        horizon_s=600,
    )
    return ProofGrade(
        label=label,
        reason_code=reason,
        memory_admitted=admitted,
        cq_delta=cq_delta,
        congestion_delta=congestion_delta,
        proof_id=proof.proof_id,
        command_id=proof.command_id,
        context_key=proof.context_key,
        reason_ref=proof.reason_ref,
        layered_feedback=feedback,
        horizon_s=600,
        terminal=True,
        prediction_error_recorded=admitted,
        policy_update_applied=admitted,
        future_policy_effect_status="pending" if admitted else "",
    )


def _capacity_value(bank: RuntimeRegisterBank) -> dict:
    return bank.snapshot((REGISTER_CAPACITY,))[REGISTER_CAPACITY]["value"]


def _publish_smartness(bank: RuntimeRegisterBank, *, now: float) -> None:
    if not bank.read(REGISTER_DECISION).value:
        bank.write_decision(
            value=build_runtime_decision_projection({}),
            source="test-decision-projection",
            updated_at=now,
        )
    publish_smartness_fact_register(
        bank,
        transaction=smartness_source_transaction_for_test(bank),
        now=now,
    )


def _composed_engine(
    runtime_bank: RuntimeRegisterBank,
    feed_bank: BrainFeedRegisterBank | None = None,
    **attributes,
) -> SimpleNamespace:
    cfg = attributes.pop("cfg", _ACTION_READY_CONFIG)
    memory = attributes.pop("memory", None)
    engine = ActiveConfigProjectionTestHost(
        cfg=cfg,
        memory=memory,
        **attributes,
    )
    state_dir = TemporaryDirectory(prefix="smart-loop-state-test-")
    engine._smart_loop_test_state_dir = state_dir
    if feed_bank is not None:
        radio = runtime_bank.read(REGISTER_RADIO)
        if radio.value:
            radio_value = dict(radio.value)
            radio_value.pop("frequency_band_set", None)
            runtime_bank.write_radio(
                value={
                    **radio_value,
                    "sample_sequence": int(
                        dict(radio.value).get("sample_sequence") or 1
                    ),
                    "source_epoch": str(
                        dict(radio.value).get("source_epoch")
                        or "test-source-epoch"
                    ),
                    "sample_ts": float(
                        dict(radio.value).get("sample_ts") or 100.0
                    ),
                },
                source="test-accepted-generation",
                updated_at=float(radio.updated_at or 100.0),
                force=True,
            )
        identity = runtime_bank.read(REGISTER_IDENTITY)
        if not identity.value:
            install_test_identity_register(
                runtime_bank,
                value={
                    "identity_code": "0x0000000000000101",
                    "identity_status": "exact",
                    "identity_catalog_version": "test-identity-catalog-v1",
                    "identity_catalog_checksum": "a" * 64,
                    "session_scope_key": "test-session-scope",
                    "provider_plmn": "22005",
                    "model": "B818-263",
                },
                source="test-accepted-identity",
                updated_at=100.0,
                valid=True,
                stale=False,
                dirty=False,
                validity="valid",
                force=True,
            )
        # The strategy lane consumes the Smartness fact register, not direct
        # register replicas. Republish after completing the accepted-generation
        # fixture so the test cannot accidentally exercise an identity-less set.
        _publish_smartness(runtime_bank, now=100.0)
    action_transaction_authority = ActionTransactionAuthority()
    action_source_state_authority = ActionSourceStateAuthority()
    engine.register_bank = runtime_bank
    engine.action_transaction_authority = action_transaction_authority
    engine.action_source_state_authority = action_source_state_authority
    engine.controller = SimpleNamespace(
        runtime=SimpleNamespace(
            register_bank=runtime_bank,
            command_authority=CommandAuthority(),
        ),
        brain=SimpleNamespace(
            memory=memory,
            feed_registers=feed_bank or BrainFeedRegisterBank(),
            smart_loop_state_store=SmartLoopStateStore(state_dir.name),
            contextual_bandit_state_store=ContextualBanditStateStore(
                state_dir.name
            ),
        ),
        control=SimpleNamespace(
            decision=SimpleNamespace(
                action_source_state_authority=action_source_state_authority
            ),
            capabilities=ModemCapabilities(
                supports_pband=True,
                supports_band_selection=True,
                pband_control_mode="test_pband",
                ca_pool_control_mode="test_ca_pool",
            )
        ),
    )
    install_active_config_generation_for_test(engine)
    return engine


def _ack_active_selected_action_command(
    engine,
    *,
    now: float,
    success: bool = True,
    reason: str = "target_confirmed_chart_fresh",
):
    register = engine.action_transaction_authority
    claimed = claim_selected_action_for_test(
        register,
        consumer="arch4.brain.decision_pipeline",
        now=now,
    )
    assert claimed is not None
    return terminalize_selected_action_for_test(
        engine,
        success=success,
        reason=reason,
        now=now,
    )


class _Inbox:
    def __init__(self):
        self.published = []

    def publish(self, kind, *, payload, source, due_ts, priority, dedupe_key):
        self.published.append(
            {
                "kind": kind,
                "payload": payload,
                "source": source,
                "due_ts": due_ts,
                "priority": priority,
                "dedupe_key": dedupe_key,
            }
        )
        return SimpleNamespace(event_id="event-1", kind=kind, dedupe_key=dedupe_key)


def _context(**overrides) -> ModemContext:
    values = {
        "provider_plmn": "22005",
        "cell_id": 2050,
        "pci": 255,
        "earfcn": 1795,
        "hour": 6,
        "current_pband": 1,
        "allowed_bands": (1, 3, 20),
        "cq": 0.42,
        "congestion_pct": 74.0,
        "observed_ts": 100.0,
    }
    values.update(overrides)
    return ModemContext(**values)


def _decide(core: SmartLoopCore, **kwargs):
    """Exercise SmartLoop TSAR with an explicit test-oracle candidate.

    Production selection belongs to ActionOpportunitySet, Contextual Bandit and
    BrainDecisionAuthority in the clock lane. These core-only tests isolate the
    candidate/TSAR behavior by explicitly supplying the previously top-ranked
    forecast; this helper is not a runtime selection path.
    """

    decision = SmartLoopCore.decide(core, **kwargs)
    if decision.outcome != "candidate_set_ready":
        return decision
    candidate = decision.candidates[0] if decision.candidates else None
    return core.apply_authoritative_candidate(
        decision=decision,
        context=kwargs["context"],
        candidate=candidate,
    )


def _state_after_execution(
    core: SmartLoopCore,
    decision,
    *,
    context: ModemContext | None = None,
    opened_ts: float | None = None,
) -> SmartLoopState:
    """Model the controller's successful execution receipt in core-only tests."""

    assert decision.selected is not None
    execution_context = context or _context(observed_ts=opened_ts or 100.0)
    proof = core.open_proof(context=execution_context, plan=decision.selected)
    if opened_ts is not None:
        proof = replace(proof, opened_ts=float(opened_ts))
    return decision.state.with_proof(proof)


def _prediction_context_for_b3() -> dict:
    return {
        "schema": "crystal_ball_horizon_context.v1",
        "owner": "arch4.brain.crystal_ball",
        "advisory_only": True,
        "direct_modem_actuation": False,
        "current_band": 1,
        "deployed_bands": [1, 3, 20],
        "horizons": {
            "future_6m": {
                "horizon_s": 360,
                "current": {
                    "band": 1,
                    "predicted_congestion": 78.0,
                    "predicted_dl_mbps": 10.0,
                    "predicted_rtt_ms": 90.0,
                    "predicted_ca_rate": 0.20,
                    "score": 0.22,
                    "confidence": 4,
                    "sample_count": 42,
                },
                "best_alternate": {
                    "band": 3,
                    "confidence": 4,
                    "predicted_congestion": 24.0,
                    "predicted_dl_mbps": 65.0,
                    "predicted_rtt_ms": 34.0,
                    "predicted_ca_rate": 0.90,
                    "score": 0.84,
                    "sample_count": 55,
                },
                "expected_delta": {
                    "congestion_improvement_pct": 54.0,
                    "dl_gain_mbps": 55.0,
                    "rtt_reduction_ms": 56.0,
                    "ca_rate_gain": 0.70,
                    "score_delta": 0.62,
                },
                "confidence": 0.8,
                "evidence_strength": "high",
                "sample_count": 42,
            }
        },
    }


def test_layered_feedback_publishes_seven_point_memory_grade_score():
    cases = [
        (
            3,
            build_layered_action_feedback(
                label="good",
                reason="strong_positive",
                cq_delta=0.08,
                congestion_delta=-10.0,
                evidence={
                    "grade_evidence_valid": True,
                    "grade_context_status": "same",
                    "tp_before": 1.0,
                    "tp_now": 9.0,
                    "cast_ld_valid": True,
                    "cast_ld": 98.0,
                    "cart_ld_activity": 4.0,
                    "nothing_left_on_table_proven": True,
                },
            ),
        ),
        (
            2,
            build_layered_action_feedback(
                label="good",
                reason="medium_positive",
                cq_delta=0.06,
                evidence={"grade_evidence_valid": True, "grade_context_status": "same"},
            ),
        ),
        (
            1,
            build_layered_action_feedback(
                label="neutral",
                reason="weak_positive",
                evidence={
                    "grade_evidence_valid": True,
                    "grade_context_status": "same",
                    "nothing_left_on_table_proven": True,
                },
            ),
        ),
        (
            0,
            build_layered_action_feedback(
                label="neutral",
                reason="neutral",
                evidence={"grade_evidence_valid": True, "grade_context_status": "same"},
            ),
        ),
        (
            -1,
            build_layered_action_feedback(
                label="bad",
                reason="weak_negative",
                evidence={"grade_evidence_valid": True, "grade_context_status": "same"},
            ),
        ),
        (
            -2,
            build_layered_action_feedback(
                label="bad",
                reason="medium_negative",
                cq_delta=-0.06,
                evidence={"grade_evidence_valid": True, "grade_context_status": "same"},
            ),
        ),
        (
            -3,
            build_layered_action_feedback(
                label="bad",
                reason="strong_negative",
                cq_delta=-0.08,
                congestion_delta=12.0,
                evidence={
                    "grade_evidence_valid": True,
                    "grade_context_status": "same",
                    "incident_class": "link_regression",
                    "self_induced": True,
                    "cast_ld_valid": True,
                    "cast_ld": 40.0,
                    "cart_ld_activity": 80.0,
                },
            ),
        ),
    ]

    for expected_score, feedback in cases:
        assert feedback["memory_grade_score"] == expected_score
        assert feedback["dimensions"]["memory"]["memory_grade_score"] == expected_score
        assert "memory_weight" not in feedback


def test_staged_roi_brain_event_preserves_selected_action_proof_identity():
    inbox = _Inbox()
    original = SimpleNamespace(
        decision_trace_id="dt-parent",
        learning_commit_status="pending",
        objective_metadata={
            "selected_action_command_id": "command_test_123",
            "selected_action_proof_id": "proof_test_456",
            "smart_loop_reason_ref": "prefer_band_b20_high_roi",
            "smart_loop_context_key": "22005|2070|153|1651|9",
        },
    )
    record = SimpleNamespace(
        decision_trace_id="dt-child", learning_commit_status="proof_closed"
    )

    result = publish_staged_roi_brain_event(
        proof_owner="arch4.brain.proof_referee_due_close_owner",
        brain_event_inbox=inbox,
        original=original,
        record=record,
        horizon=600,
        label="neutral",
        reason="mixed_or_insufficient_horizon_evidence",
        referee_code="staged_roi_inconclusive",
        secondary={"memory_committed": False},
        evidence={"grade_evidence_valid": True},
        ts=1300.0,
    )

    assert result["published"] is True
    payload = inbox.published[0]["payload"]
    assert payload["selected_action_command_id"] == "command_test_123"
    assert payload["selected_action_proof_id"] == "proof_test_456"
    assert payload["smart_loop_reason_ref"] == "prefer_band_b20_high_roi"
    assert payload["smart_loop_context_key"] == "22005|2070|153|1651|9"


def test_clean_loop_selects_useful_candidate_without_opening_proof_before_execution():
    decision = _decide(SmartLoopCore(),
        context=_context(
            identity_code="0x1ABC1234", identity_status="exact", model="B818-263"
        ),
        facts=LearningFacts(
            band_scores={3: 0.82, 1: 0.38},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
    )

    assert decision.schema == "smart_loop.closed_loop.v1"
    assert decision.owner == "arch4.smart_loop.closed_loop"
    assert decision.outcome == "action_selected_execution_pending"
    assert decision.selected is not None
    assert decision.selected.action == RADIO_CONFIGURATION_ACTION
    assert decision.selected.target_band == 3
    assert not hasattr(decision, "command")
    assert decision.opened_proof is None
    assert decision.state.pending_proof is None
    payload = decision.as_register_payload()
    assert payload["schema"] == "smart_loop.closed_loop.v1"
    assert payload["owner"] == "arch4.smart_loop.closed_loop"
    assert payload["selected"]["target_band"] == 3
    assert (
        payload["selected"]["context_key"]
        == _context(
            identity_code="0x1ABC1234",
            identity_status="exact",
            model="B818-263",
        ).learning_context_key
    )
    assert payload["selected"]["command_id"]
    assert payload["selected"]["proof_id"]
    assert "command" not in payload
    assert payload["opened_proof"] is None
    assert payload["candidate_counts"]["operational"] >= 1
    assert payload["candidate_counts"]["actionable"] == 1
    assert payload["bandit_decision"]["schema"] == "smart_loop.bandit_decision.v1"
    assert payload["bandit_decision"]["decision_made"] is True
    assert payload["bandit_decision"]["decision_kind"] == "execute"
    assert payload["bandit_decision"]["reason_ref"] == decision.selected.reason_ref
    assert payload["bandit_decision"]["proof_required"] is True
    bandit = payload["bandit_reasoning_flow"]
    assert bandit["schema"] == "smart_loop.bandit_reasoning_flow.v1"
    assert bandit["owner"] == "arch4.smart_loop.closed_loop"
    assert bandit["decision"] == payload["bandit_decision"]
    assert bandit["requirements"]["schema"] == "smart_loop.bandit_requirements.v1"
    assert bandit["requirements"]["ready"] is True
    assert bandit["requirements"]["context_vector"] is True
    assert bandit["requirements"]["discrete_action_space"] is True
    assert bandit["requirements"]["candidate_scores"] is True
    assert bandit["requirements"]["safety_shield"] is True
    assert bandit["requirements"]["bandit_decision"] is True
    assert bandit["requirements"]["decision_kind"] == "execute"
    assert bandit["requirements"]["proof_reward_path"] is True
    assert (
        bandit["requirements"]["reward_path_state"]
        == "required_for_executable_decision"
    )
    assert bandit["requirements"]["policy_logging"] is True
    assert bandit["requirements"]["future_policy_path"] is True
    assert bandit["context_vector"]["identity_code"] == "0x1ABC1234"
    assert bandit["context_vector"]["pci"] == 255
    assert bandit["action_space"]["candidate_counts"] == payload["candidate_counts"]
    assert bandit["action_space"]["actions"][0]["candidate_class"] == "operational"
    assert bandit["safety_shield"]["tsar_verdict"] == "act"
    assert bandit["safety_shield"]["command_allowed"] is True
    assert bandit["reward_path"]["opened_proof"] is False
    assert bandit["reward_path"]["grade_scale"] == [-3, -2, -1, 0, 1, 2, 3]
    assert "context" in bandit["policy_logging"]["candidate_evidence_fields"]
    assert (
        bandit["policy_logging"]["selected_reason_ref"] == decision.selected.reason_ref
    )
    closure = payload["closure_audit"]
    assert closure["schema"] == "smart_loop.closure_audit.v1"
    assert closure["owner"] == "arch4.smart_loop.closed_loop"
    assert closure["status"] == "selected_action_pending_execution"
    assert closure["links"]["candidate_generated"] is True
    assert closure["links"]["tsar_arbitrated"] is True
    assert closure["links"]["selected_action_materialized"] is False
    assert closure["links"]["proof_opened"] is False
    assert closure["missing_links"] == [
        "selected_action_materialized",
        "proof_opened",
        "proof_closed",
        "memory_admitted",
    ]
    candidate = payload["candidates"][0]
    assert candidate["schema"] == "smart_loop.candidate.v1"
    assert candidate["owner"] == "arch4.smart_loop.closed_loop"
    assert (
        candidate["context_key"]
        == _context(
            identity_code="0x1ABC1234",
            identity_status="exact",
            model="B818-263",
        ).learning_context_key
    )
    assert candidate["context"]["identity_code"] == "0x1ABC1234"
    assert candidate["context"]["model"] == "B818-263"
    assert "backend_family" not in candidate["context"]
    assert candidate["context"]["provider_plmn"] == "22005"
    assert candidate["context"]["pci"] == 255


def test_clean_loop_refuses_radio_action_without_observed_timestamp():
    decision = _decide(SmartLoopCore(),
        context=_context(
            identity_code="0x1ABC1234",
            identity_status="exact",
            model="B818-263",
            observed_ts=0.0,
        ),
        facts=LearningFacts(
            band_scores={3: 0.82, 1: 0.38},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
    )

    assert decision.outcome == "action_rejected"
    assert decision.reason_code == "context_timestamp_missing"
    assert decision.required_next_step == "wait_for_fresh_context"
    assert decision.selected is None
    assert not hasattr(decision, "command")
    assert decision.opened_proof is None
    assert decision.state.pending_proof is None
    payload = decision.as_register_payload()
    assert payload["candidate_counts"]["operational"] >= 1
    assert (
        payload["bandit_reasoning_flow"]["safety_shield"]["reason_code"]
        == "context_timestamp_missing"
    )
    assert payload["bandit_reasoning_flow"]["reward_path"]["opened_proof"] is False


def test_zero_timestamp_pending_proof_is_invalidated_not_left_immortal():
    core = SmartLoopCore()
    first = _decide(core,
        context=_context(
            identity_code="0x1ABC1234", identity_status="exact", model="B818-263"
        ),
        facts=LearningFacts(
            band_scores={3: 0.82, 1: 0.38},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
    )
    assert first.state.pending_proof is None
    bad_state = _state_after_execution(core, first, opened_ts=0.0)

    decision = _decide(core,
        context=_context(
            identity_code="0x1ABC1234",
            identity_status="exact",
            model="B818-263",
            current_pband=1,
            observed_ts=400.0,
        ),
        facts=LearningFacts(
            band_scores={3: 0.82, 1: 0.38},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
        state=bad_state,
    )

    assert decision.outcome == "proof_invalid"
    assert decision.reason_code == "proof_timestamp_missing"
    assert decision.closed_proof is not None
    assert decision.closed_proof.label == "invalid"
    assert decision.closed_proof.memory_admitted is False
    assert decision.opened_proof is None
    assert not hasattr(decision, "command")
    assert decision.state.pending_proof is None
    assert "roi" not in decision.state.as_dict()


def test_clean_loop_never_invents_target_not_confirmed_reward():
    core = SmartLoopCore()
    facts = LearningFacts(
        band_scores={3: 0.82, 1: 0.38},
        evidence_counts={3: 4},
        profile_confidence=0.9,
    )
    first = _decide(core,
        context=_context(hour=6, current_pband=1, observed_ts=100.0), facts=facts
    )

    assert first.outcome == "action_selected_execution_pending"
    executed_state = _state_after_execution(core, first, opened_ts=100.0)
    assert executed_state.pending_proof is not None

    during_confirmation = _decide(core,
        context=_context(
            hour=7, current_pband=1, cooldown_s=0.0, link_valid=True, observed_ts=120.0
        ),
        facts=facts,
        state=executed_state,
    )

    assert during_confirmation.outcome == "proof_pending"
    assert not hasattr(during_confirmation, "command")
    assert during_confirmation.state.pending_proof is not None

    later = _decide(core,
        context=_context(
            hour=7, current_pband=1, cooldown_s=0.0, link_valid=True, observed_ts=400.0
        ),
        facts=facts,
        state=during_confirmation.state,
    )

    assert later.outcome == "proof_pending"
    assert later.closed_proof is None
    assert later.state.pending_proof is not None
    assert "roi" not in later.state.as_dict()
    assert later.state.objective_holds == {}


def test_register_adapter_maps_runtime_snapshots_without_old_runtime_dependency():
    context, facts = smart_loop_inputs_from_registers(
        identity={
            "identity_code": "0xFFFF000000000201",
            "identity_status": "exact",
            "identity_catalog_version": "test-identity-catalog-v1",
            "identity_catalog_checksum": "a" * 64,
            "provider_plmn": "22005",
            "model": "B818-263",
            "profile_id": "huawei_b818_263_h220sp1c11",
            "firmware": "11.0.1.1(H220SP1C11)",
        },
        radio={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
            "sample_ts": 1234.5,
        },
        quality={"cq_cell_display": 0.42, "congestion_pct": 74.0},
        ca={"ca_active": False},
        learning={
            "current_hour": 6,
            "band_scores": {"3": 0.82},
            "evidence_counts": {"3": 4},
            "profile_confidence": 0.9,
            "sample_count": 20,
            "control_authority_bands": [1, 3, 20],
            "calibration_fact_package": {
                "schema": "calibration_fact_package.v1",
                "owner": "calibration_fact_confidence",
                "confidence": 0.8,
                "clean_for_learning": True,
            },
            "prediction_context": _prediction_context_for_b3(),
        },
        mutation={"cooldown_s": 0.0},
    )

    assert context.learning_context_key == "0xFFFF000000000201|22005|2050|255|1795|6||0|0|"
    assert context.identity_code == "0xFFFF000000000201"
    assert context.model == "B818-263"
    assert context.profile_id == "huawei_b818_263_h220sp1c11"
    assert context.firmware == "11.0.1.1(H220SP1C11)"
    assert context.current_pband == 1
    assert context.allowed_bands == (1, 3, 20)
    assert context.cq == 0.42
    assert context.observed_ts == 1234.5
    assert facts.band_scores == {3: 0.82}
    assert facts.evidence_counts == {3: 4}
    assert facts.calibration_fact_package["schema"] == "calibration_fact_package.v1"
    assert facts.prediction_clean_for_learning is True
    assert facts.prediction_context["schema"] == "crystal_ball_horizon_context.v1"


def test_identity_relative_signal_simulation_is_nr_ready_and_capacity_aware():
    result = run_identity_relative_signal_simulation()

    assert result["schema"] == "identity_relative_rf_simulation.v1"
    assert result["owner"] == "arch4.tests.identity_relative_rf_proof_harness"
    assert result["all_nr_ready"] is True
    assert result["capacity_wins_when_pipe_is_good"] is True
    cases = {row["name"]: row for row in result["cases"]}
    assert cases["b818_low_raw_sinr_good_pipe"]["raw_sinr_db"] == 0.0
    assert cases["mikrotik_high_raw_sinr_same_pipe"]["raw_sinr_db"] == 10.0
    assert (
        cases["b818_low_raw_sinr_good_pipe"]["normalized_rf_score"]
        == cases["mikrotik_high_raw_sinr_same_pipe"]["normalized_rf_score"]
    )
    assert (
        cases["b818_low_raw_sinr_good_pipe"]["identity_code"]
        != cases["mikrotik_high_raw_sinr_same_pipe"]["identity_code"]
    )


def test_register_adapter_rejects_non_canonical_prediction_context():
    _context, facts = smart_loop_inputs_from_registers(
        radio={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "radio_connected": True,
        },
        quality={},
        ca={},
        learning={
            "current_hour": 6,
            "control_authority_bands": [1, 3, 20],
            "prediction_context": {
                "schema": "old_prediction_context.v0",
                "owner": "other",
                "advisory_only": False,
            },
        },
        mutation={},
    )

    assert facts.prediction_context == {}
    assert facts.prediction_clean_for_learning is False


def test_capacity_probe_publishes_owner_stamped_capacity_register_from_quiet_window():
    runtime_bank = RuntimeRegisterBank()
    install_test_identity_register(
        runtime_bank,
        value={
            "identity_code": "0x100CAFFE",
            "identity_status": "exact",
            "model": "B818-263",
        },
        source="test",
        updated_at=100.0,
    )
    runtime_bank.write_radio(
        value={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
            "sample_ts": 100.0,
        },
        source="test",
        updated_at=100.0,
    )
    runtime_bank.write_traffic(
        value={"dl_mbps": 0.0, "ul_mbps": 0.0, "traffic_valid": True},
        source="test",
        updated_at=100.0,
    )
    runtime_bank.write_mutation(
        value={"cooldown_s": 0.0, "active": False}, source="test", updated_at=100.0
    )
    runtime_bank.write_decision(
        value=build_runtime_decision_projection({}),
        source="test",
        updated_at=100.0,
    )
    runtime_bank.write_learning(
        value={"proof_pending": False}, source="test", updated_at=100.0
    )
    engine = _composed_engine(
        runtime_bank,
        _capacity_probe_quiet_since=60.0,
        _capacity_probe_runner=lambda _cfg, _context, _now: {
            "dl_mbps": 37.5,
            "latency_ms": 28.0,
            "bytes": 250000,
            "duration_s": 0.053,
        },
    )

    outcome = run_capacity_probe_clock_lane(engine, now=100.0)
    value = _capacity_value(runtime_bank)

    assert outcome.ran is True
    assert value["schema"] == "capacity_evidence_register.v1"
    assert value["owner"] == "arch4.smart_loop.capacity_probe"
    assert value["active_probe_status"] == "measured"
    active = value["active_capacity_fact"]
    assert active["schema"] == "measured_capacity_fact.v1"
    assert active["owner"] == "arch4.smart_loop.capacity_probe"
    assert active["measured"] is True
    assert active["serving_band"] == 1
    assert active["target_bands"] == [1, 3, 20]
    assert active["dl_mbps"] == 37.5
    assert active["latency_ms"] == 28.0
    assert active["confidence"] == 1.0


def test_capacity_owner_publishes_passive_dl_peak_without_active_probe():
    runtime_bank = RuntimeRegisterBank()
    install_test_identity_register(
        runtime_bank,
        value={
            "identity_code": "0x100CAFFE",
            "identity_status": "exact",
            "model": "B818-263",
        },
        source="test",
        updated_at=100.0,
    )
    runtime_bank.write_radio(
        value={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
            "sample_ts": 401.0,
            "nr_band": "n78",
            "nr_pci": 11,
            "nr_arfcn": 641000,
            "nr_cell_id": "NR-1",
        },
        source="test",
        updated_at=100.0,
    )
    runtime_bank.write_traffic(
        value={
            "dl_mbps": 28.0,
            "ul_mbps": 0.0,
            "traffic_valid": True,
            "traffic_rate_source": "router_counter",
        },
        source="test",
        updated_at=100.0,
    )
    runtime_bank.write_mutation(
        value={"cooldown_s": 0.0, "active": False}, source="test", updated_at=100.0
    )
    runtime_bank.write_decision(
        value=build_runtime_decision_projection({}),
        source="test",
        updated_at=100.0,
    )
    runtime_bank.write_learning(
        value={"proof_pending": False}, source="test", updated_at=100.0
    )
    engine = _composed_engine(
        runtime_bank,
        _capacity_probe_quiet_since=60.0,
        _capacity_probe_runner=lambda _cfg, _context, _now: pytest.fail(
            "active probe must not run while user traffic is active"
        ),
    )

    outcome = run_capacity_probe_clock_lane(engine, now=100.0)
    value = _capacity_value(runtime_bank)
    passive = value["passive_dl_peak_fact"]

    assert outcome.ran is True
    assert outcome.backpressure_reason == "user_traffic_active"
    assert value["schema"] == "capacity_evidence_register.v1"
    assert passive["schema"] == "passive_dl_peak_fact.v1"
    assert passive["owner"] == "arch4.smart_loop.capacity_probe"
    assert passive["peak_dl_mbps_lower_bound"] == 28.0
    assert passive["identity_code"] == "0x100CAFFE"
    assert "n78" in passive["context_key"]

    runtime_bank.write_traffic(
        value={
            "dl_mbps": 12.0,
            "ul_mbps": 0.0,
            "traffic_valid": True,
            "traffic_rate_source": "router_counter",
        },
        source="test",
        updated_at=101.0,
    )
    run_capacity_probe_clock_lane(engine, now=101.0)
    assert (
        _capacity_value(runtime_bank)["passive_dl_peak_fact"][
            "peak_dl_mbps_lower_bound"
        ]
        == 28.0
    )

    runtime_bank.write_traffic(
        value={
            "dl_mbps": 36.0,
            "ul_mbps": 0.0,
            "traffic_valid": True,
            "traffic_rate_source": "router_counter",
        },
        source="test",
        updated_at=102.0,
    )
    run_capacity_probe_clock_lane(engine, now=102.0)
    assert (
        _capacity_value(runtime_bank)["passive_dl_peak_fact"][
            "peak_dl_mbps_lower_bound"
        ]
        == 36.0
    )

    runtime_bank.write_traffic(
        value={
            "dl_mbps": 80.0,
            "ul_mbps": 0.0,
            "traffic_valid": True,
            "traffic_rate_source": "router_counter",
        },
        source="test",
        updated_at=103.0,
        stale=True,
    )
    run_capacity_probe_clock_lane(engine, now=103.0)
    assert (
        _capacity_value(runtime_bank)["passive_dl_peak_fact"][
            "peak_dl_mbps_lower_bound"
        ]
        == 36.0
    )

    runtime_bank.write_traffic(
        value={
            "dl_mbps": 90.0,
            "ul_mbps": 0.0,
            "traffic_valid": True,
            "traffic_rate_source": "router_counter",
        },
        source="test",
        updated_at=104.0,
        valid=False,
    )
    run_capacity_probe_clock_lane(engine, now=104.0)
    assert (
        _capacity_value(runtime_bank)["passive_dl_peak_fact"][
            "peak_dl_mbps_lower_bound"
        ]
        == 36.0
    )


@pytest.mark.parametrize(
    ("traffic", "mutation", "learning", "expected_reason"),
    [
        (
            {"dl_mbps": 8.0, "ul_mbps": 0.0, "traffic_valid": True},
            {"cooldown_s": 0.0, "active": False},
            {"proof_pending": False},
            "user_traffic_active",
        ),
        (
            {"dl_mbps": 0.0, "ul_mbps": 0.0},
            {"remaining_s": 20.0, "active": False},
            {"proof_pending": False},
            "mutation_cooldown_active",
        ),
        (
            {"dl_mbps": 0.0, "ul_mbps": 0.0},
            {"cooldown_s": 0.0, "active": False},
            {"proof_pending": True},
            "proof_pending",
        ),
    ],
)
def test_capacity_probe_refuses_non_quiet_mutation_and_proof_windows(
    traffic, mutation, learning, expected_reason
):
    runtime_bank = RuntimeRegisterBank()
    runtime_bank.write_radio(
        value={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
            "sample_ts": 400.0,
        },
        source="test",
        updated_at=100.0,
    )
    runtime_bank.write_traffic(value=traffic, source="test", updated_at=100.0)
    runtime_bank.write_mutation(value=mutation, source="test", updated_at=100.0)
    runtime_bank.write_decision(
        value=build_runtime_decision_projection({}),
        source="test",
        updated_at=100.0,
    )
    runtime_bank.write_learning(value=learning, source="test", updated_at=100.0)
    engine = _composed_engine(
        runtime_bank,
        _capacity_probe_quiet_since=60.0,
        _capacity_probe_runner=lambda _cfg, _context, _now: pytest.fail(
            "capacity probe ran while gate was closed"
        ),
    )

    outcome = run_capacity_probe_clock_lane(engine, now=100.0)
    value = _capacity_value(runtime_bank)

    assert outcome.ran is False
    assert outcome.backpressure_reason == expected_reason
    assert value["schema"] == "capacity_evidence_register.v1"
    assert value["owner"] == "arch4.smart_loop.capacity_probe"
    assert value["active_capacity_fact"] == {}
    assert value["active_probe_status"] == "skipped"
    assert value["active_probe_reason"] == expected_reason


def test_register_adapter_accepts_only_capacity_evidence_register_fact():
    _context, facts = smart_loop_inputs_from_registers(
        radio={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "radio_connected": True,
        },
        quality={},
        ca={},
        learning={"current_hour": 6, "control_authority_bands": [1, 3, 20]},
        mutation={},
        capacity={
            "schema": "measured_capacity_fact.v1",
            "owner": "arch4.smart_loop.capacity_probe",
            "measured": True,
            "confidence": 0.8,
            "serving_band": 1,
            "dl_mbps": 3.0,
        },
    )

    assert facts.measured_capacity_fact == {}
    assert facts.measured_capacity_clean_for_learning is False

    _context, rejected = smart_loop_inputs_from_registers(
        radio={"serving_band": 1, "radio_connected": True},
        quality={},
        ca={},
        learning={"current_hour": 6},
        mutation={},
        capacity={
            "schema": "measured_capacity_fact.v1",
            "owner": "other",
            "measured": True,
            "confidence": 0.8,
        },
    )

    assert rejected.measured_capacity_fact == {}
    assert rejected.measured_capacity_clean_for_learning is False

    _context, composite = smart_loop_inputs_from_registers(
        identity={"identity_code": "0x100CAFFE", "identity_status": "exact"},
        radio={"serving_band": 1, "radio_connected": True},
        quality={},
        ca={},
        learning={"current_hour": 6},
        mutation={},
        capacity={
            "schema": "capacity_evidence_register.v1",
            "owner": "arch4.smart_loop.capacity_probe",
            "active_capacity_fact": {
                "schema": "measured_capacity_fact.v1",
                "owner": "arch4.smart_loop.capacity_probe",
                "measured": True,
                "confidence": 0.8,
                "serving_band": 1,
                "dl_mbps": 37.0,
            },
            "passive_dl_peak_fact": {
                "schema": "passive_dl_peak_fact.v1",
                "owner": "arch4.smart_loop.capacity_probe",
                "observed": True,
                "serving_band": 1,
                "peak_dl_mbps_lower_bound": 42.0,
            },
        },
    )
    assert composite.measured_capacity_fact["dl_mbps"] == 37.0
    assert composite.passive_dl_peak_fact["peak_dl_mbps_lower_bound"] == 42.0


def test_clean_loop_uses_measured_capacity_fact_as_current_anchor_evidence():
    decision = _decide(SmartLoopCore(),
        context=_context(current_pband=1, allowed_bands=(1, 3, 20)),
        facts=LearningFacts(
            band_scores={1: 0.55, 3: 0.58},
            evidence_counts={3: 4},
            profile_confidence=0.9,
            measured_capacity_fact={
                "schema": "measured_capacity_fact.v1",
                "owner": "arch4.smart_loop.capacity_probe",
                "measured": True,
                "confidence": 0.85,
                "serving_band": 1,
                "target_bands": [1, 3, 20],
                "dl_mbps": 3.0,
                "latency_ms": 88.0,
                "context_key": _context().learning_context_key,
                "sample_ts": 100.0,
            },
        ),
    )

    assert decision.outcome == "action_selected_execution_pending"
    assert decision.selected is not None
    assert decision.selected.action == RADIO_CONFIGURATION_ACTION
    assert decision.selected.target_band == 3
    payload = decision.as_register_payload()
    selected = payload["selected"]
    assert selected["measured_capacity_fact"]["schema"] == "measured_capacity_fact.v1"
    assert selected["measured_capacity_fact"]["dl_mbps"] == 3.0


def test_passive_dl_peak_supports_current_anchor_but_low_passive_traffic_is_not_bad_proof():
    context = _context(current_pband=1, allowed_bands=(1, 3, 20))
    passive_context_key = context.learning_context_key

    stay = _decide(SmartLoopCore(),
        context=context,
        facts=LearningFacts(
            band_scores={1: 0.57, 3: 0.58},
            evidence_counts={1: 2, 3: 2},
            profile_confidence=0.5,
            passive_dl_peak_fact={
                "schema": "passive_dl_peak_fact.v1",
                "owner": "arch4.smart_loop.capacity_probe",
                "observed": True,
                "serving_band": 1,
                "target_bands": [1, 3, 20],
                "peak_dl_mbps_lower_bound": 38.0,
                "context_key": passive_context_key,
                "sample_ts": 100.0,
            },
        ),
    )
    assert stay.outcome == "no_action_justified"
    assert stay.reason_code == "current_anchor_already_best"
    assert stay.candidates
    assert stay.candidates[0].action == "stay_current_anchor"
    assert (
        stay.candidates[0].metadata["passive_dl_peak_fact"]["peak_dl_mbps_lower_bound"]
        == 38.0
    )
    stay_payload = stay.as_register_payload()
    assert stay_payload["bandit_decision"]["decision_made"] is True
    assert stay_payload["bandit_decision"]["decision_kind"] == "stay"
    assert stay_payload["bandit_decision"]["decision_semantic"] == "stay_current_anchor"
    assert (
        stay_payload["bandit_decision"]["reason_code"] == "current_anchor_already_best"
    )
    assert stay_payload["bandit_decision"]["reason_ref"]
    assert stay_payload["bandit_decision"]["proof_required"] is False
    assert (
        stay_payload["bandit_reasoning_flow"]["requirements"]["proof_reward_path"]
        is True
    )
    assert (
        stay_payload["bandit_reasoning_flow"]["requirements"]["reward_path_state"]
        == "not_required_for_non_executable_decision"
    )

    low = _decide(SmartLoopCore(),
        context=context,
        facts=LearningFacts(
            band_scores={1: 0.57, 3: 0.58},
            evidence_counts={1: 2, 3: 2},
            profile_confidence=0.5,
            passive_dl_peak_fact={
                "schema": "passive_dl_peak_fact.v1",
                "owner": "arch4.smart_loop.capacity_probe",
                "observed": True,
                "serving_band": 1,
                "peak_dl_mbps_lower_bound": 1.0,
                "context_key": passive_context_key,
                "sample_ts": 101.0,
            },
        ),
    )
    payload = low.as_register_payload()
    assert "passive_dl_peak_fact" not in (payload.get("selected") or {})


def test_action_availability_owner_turns_autonomous_embargo_into_pre_decision_cooldown():
    availability = build_action_availability(
        decision=build_runtime_decision_projection({
            "brain": {
                "autonomous_band_embargo_active": True,
                "autonomous_band_embargo_remaining_s": 900,
            }
        }),
        mutation={},
        health={},
        now=100.0,
    )
    context, _ = smart_loop_inputs_from_registers(
        radio={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
            "sample_ts": 401.0,
        },
        quality={"cq_cell_display": 0.42, "congestion_pct": 74.0},
        ca={},
        learning={"current_hour": 6, "control_authority_bands": [1, 3, 20]},
        mutation={},
        availability=availability,
    )

    decision = _decide(SmartLoopCore(),
        context=context,
        facts=LearningFacts(
            band_scores={3: 0.82, 1: 0.38},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
    )

    assert availability["owner"] == "arch4.smart_loop.action_availability"
    assert context.cooldown_s == 900.0
    assert decision.outcome == "action_rejected"
    assert decision.reason_code == "cooldown_active"
    assert not hasattr(decision, "command")


def test_durable_bouncer_latch_blocks_register_to_smart_loop_decision_end_to_end():
    runtime_decision = build_runtime_decision_projection(
        {
            "brain_bouncer": {
                "schema": BOUNCER_PROJECTION_SCHEMA,
                "owner": BOUNCER_PROJECTION_OWNER,
                "source_owner": BOUNCER_SOURCE_OWNER,
                "state": "blocked",
                "tripped": True,
                "trip_reason": "unknown_intent_blocked",
                "trip_source": "bouncer_hard_stop_latch",
                "reentry_state": "waiting",
                "reentry_condition": "explicit recovery required",
                "hard_stop_latch": {
                    "active": True,
                    "epoch": 7,
                    "source_generation": 324,
                    "safety_available": True,
                },
            }
        }
    )
    availability = build_action_availability(
        decision=runtime_decision,
        mutation={},
        health={},
        now=100.0,
    )
    context, _ = smart_loop_inputs_from_registers(
        radio={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
            "sample_ts": 401.0,
        },
        quality={"cq_cell_display": 0.42, "congestion_pct": 74.0},
        ca={},
        learning={"current_hour": 6, "control_authority_bands": [1, 3, 20]},
        mutation={},
        availability=availability,
    )
    decision = _decide(
        SmartLoopCore(),
        context=context,
        facts=LearningFacts(
            band_scores={3: 0.82, 1: 0.38},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
    )

    assert context.safety_block == (
        "bouncer_hard_stop:unknown_intent_blocked"
    )
    assert decision.outcome == "action_rejected"
    assert decision.reason_code == (
        "safety_block:bouncer_hard_stop:unknown_intent_blocked"
    )
    assert not hasattr(decision, "command")
    assert decision.opened_proof is None


def test_action_availability_owner_turns_runtime_settle_into_pre_decision_cooldown():
    availability = build_action_availability(
        decision=build_runtime_decision_projection({}),
        mutation={},
        health={},
        runtime_settle={
            "schema": "smart_loop.runtime_settle_availability.v1",
            "owner": "arch4.smart_loop.action_availability",
            "connect_settle_active": True,
            "connect_settle_remaining_s": 29.8,
        },
        now=100.0,
    )
    context, _ = smart_loop_inputs_from_registers(
        radio={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
            "sample_ts": 401.0,
        },
        quality={"cq_cell_display": 0.42, "congestion_pct": 74.0},
        ca={},
        learning={"current_hour": 6, "control_authority_bands": [1, 3, 20]},
        mutation={},
        availability=availability,
    )

    decision = _decide(SmartLoopCore(),
        context=context,
        facts=LearningFacts(
            band_scores={3: 0.82, 1: 0.38},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
    )

    assert availability["owner"] == "arch4.smart_loop.action_availability"
    assert availability["connect_settle_active"] is True
    assert context.cooldown_s == 29.8
    assert decision.outcome == "action_rejected"
    assert decision.reason_code == "safety_block:runtime_settle:connect_settle"
    assert not hasattr(decision, "command")
    assert decision.opened_proof is None


def test_action_availability_owner_ages_runtime_settle_from_absolute_expiry():
    frozen_projection = {
        "schema": "smart_loop.runtime_settle_availability.v1",
        "owner": "arch4.smart_loop.action_availability",
        "connect_settle_active": True,
        "connect_settle_remaining_s": 999.0,
        "connect_settle_until_ts": 130.0,
    }

    early = build_action_availability(
        decision=build_runtime_decision_projection({}),
        mutation={},
        health={},
        runtime_settle=frozen_projection,
        now=100.0,
    )
    late = build_action_availability(
        decision=build_runtime_decision_projection({}),
        mutation={},
        health={},
        runtime_settle=frozen_projection,
        now=125.0,
    )

    assert early["connect_settle_remaining_s"] == 30.0
    assert early["cooldown_until_ts"] == 130.0
    assert late["connect_settle_remaining_s"] == 5.0
    assert late["cooldown_s"] == 5.0
    assert late["runtime_settle"]["connect_settle_remaining_s"] == 5.0


def test_runtime_settle_debounces_single_sample_radio_identity_wobble():
    engine = SimpleNamespace(
        cfg=SimpleNamespace(
            startup_grace_s=0.0,
            autonomous_connect_settle_s=0.0,
            radio_change_settle_s=30.0,
        )
    )
    base_radio = {
        "radio_connected": True,
        "serving_band": 3,
        "pci": 255,
        "earfcn_dl": 1795,
        "cell_id": 2050,
        "enb_id": 8,
        "session_uptime_s": 1000.0,
        "_register_updated_at": 100.0,
    }

    stable = _runtime_settle_availability(
        engine=engine,
        runtime_radio=base_radio,
        runtime_traffic={},
        now=100.0,
    )
    assert stable["serving_key"] == [3, 255, 1795, 2050, 8]
    assert stable["radio_change_settle_active"] is False
    assert stable["identity_pending_confirmation_active"] is False

    one_sample_wobble = {
        **base_radio,
        "serving_band": 7,
        "_register_updated_at": 102.0,
    }
    pending = _runtime_settle_availability(
        engine=engine,
        runtime_radio=one_sample_wobble,
        runtime_traffic={},
        now=102.0,
    )
    assert pending["serving_key"] == [3, 255, 1795, 2050, 8]
    assert pending["raw_serving_key"] == [7, 255, 1795, 2050, 8]
    assert pending["identity_pending_confirmation_active"] is True
    assert pending["context_unready_reason"] == "radio_identity_pending_confirmation"
    assert pending["radio_change_settle_active"] is False

    same_sample_replayed = _runtime_settle_availability(
        engine=engine,
        runtime_radio=one_sample_wobble,
        runtime_traffic={},
        now=102.5,
    )
    assert same_sample_replayed["serving_key"] == [3, 255, 1795, 2050, 8]
    assert same_sample_replayed["identity_pending_confirmation_count"] == 1
    assert same_sample_replayed["radio_change_settle_active"] is False

    confirmed_change = {
        **one_sample_wobble,
        "_register_updated_at": 104.0,
    }
    changed = _runtime_settle_availability(
        engine=engine,
        runtime_radio=confirmed_change,
        runtime_traffic={},
        now=104.0,
    )
    assert changed["serving_key"] == [7, 255, 1795, 2050, 8]
    assert changed["identity_pending_confirmation_active"] is False
    assert changed["radio_change_settle_active"] is True
    assert changed["radio_change_settle_remaining_s"] == 30.0

    aged = _runtime_settle_availability(
        engine=engine,
        runtime_radio={**confirmed_change, "_register_updated_at": 106.0},
        runtime_traffic={},
        now=110.0,
    )
    assert aged["serving_key"] == [7, 255, 1795, 2050, 8]
    assert aged["radio_change_settle_remaining_s"] == 24.0


def test_action_availability_blocks_pending_radio_identity_without_long_cooldown():
    availability = build_action_availability(
        decision=build_runtime_decision_projection({}),
        mutation={},
        health={},
        runtime_settle={
            "schema": "smart_loop.runtime_settle_availability.v1",
            "owner": "arch4.smart_loop.action_availability",
            "context_unready_active": True,
            "context_unready_reason": "radio_identity_pending_confirmation",
            "identity_pending_confirmation_active": True,
            "radio_change_settle_active": False,
            "radio_change_settle_remaining_s": 0.0,
        },
        now=100.0,
    )

    assert availability["decision_allowed"] is False
    assert availability["cooldown_s"] == 0.0
    assert (
        availability["safety_block"]
        == "runtime_settle:radio_identity_pending_confirmation"
    )
    assert availability["radio_change_settle_active"] is False


def test_smart_loop_stays_alive_but_disconnected_context_is_not_actionable():
    context, _ = smart_loop_inputs_from_registers(
        radio={},
        quality={},
        ca={},
        learning={"current_hour": 6, "control_authority_bands": [1, 3, 20]},
        mutation={},
    )

    decision = _decide(SmartLoopCore(),
        context=context,
        facts=LearningFacts(
            band_scores={3: 0.82, 1: 0.38},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
    )

    assert context.link_valid is False
    assert decision.outcome == "no_action_justified"
    assert decision.reason_code == "live_context_unavailable"
    assert not hasattr(decision, "command")
    assert decision.opened_proof is None


def test_register_adapter_uses_learning_control_authority_not_current_pool():
    context, _ = smart_loop_inputs_from_registers(
        radio={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1],
            "selected_band_set": [1],
            "radio_connected": True,
            "sample_ts": 100.0,
        },
        quality={},
        ca={},
        learning={"current_hour": 6, "control_authority_bands": [1, 3]},
        mutation={},
    )

    assert context.allowed_bands == (1, 3)
    decision = _decide(SmartLoopCore(),
        context=context,
        facts=LearningFacts(
            band_scores={3: 0.82, 1: 0.38},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
    )
    assert decision.reason_code != "target_band_outside_authority"
    assert decision.outcome == "action_selected_execution_pending"


def test_current_single_band_pool_does_not_block_history_backed_recovery_candidate():
    context, facts = smart_loop_inputs_from_registers(
        radio={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1],
            "radio_connected": True,
            "sample_ts": 100.0,
        },
        quality={"cq_cell_display": 0.42, "congestion_pct": 74.0},
        ca={},
        learning={
            "current_hour": 6,
            "band_scores": {"3": 0.8836},
            "evidence_counts": {"3": 200},
            "profile_confidence": 0.85,
            "sample_count": 37806,
            "control_authority_bands": [1, 3, 20],
        },
        mutation={"cooldown_s": 0.0},
    )

    decision = _decide(SmartLoopCore(), context=context, facts=facts)

    assert context.ca_pool == (1,)
    assert context.allowed_bands == (1, 3, 20)
    assert decision.outcome == "action_selected_execution_pending"
    assert decision.selected is not None
    assert decision.selected.target_band == 3


def test_clean_smart_loop_core_does_not_rederive_profile_control_policy():
    decision = _decide(SmartLoopCore(),
        context=_context(
            identity_code="0x1C0FFEE0",
            identity_status="exact",
            model="Chateau LTE12",
            profile_id="mikrotik_chateau_lte12_eg120k_routeros_v7",
        ),
        facts=LearningFacts(
            band_scores={3: 0.82, 1: 0.38},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
    )

    assert decision.candidates
    assert decision.candidates[0].action == RADIO_CONFIGURATION_ACTION
    assert decision.outcome == "action_selected_execution_pending"
    assert decision.reason_code != "routeros_rf_control_best_effort_manual_only"
    assert decision.selected is not None
    assert not hasattr(decision, "command")
    assert decision.opened_proof is None
    payload = decision.as_register_payload()
    assert payload["candidate_counts"]["actionable"] == 1


def test_register_adapter_refuses_when_declared_control_authority_is_absent():
    context, _ = smart_loop_inputs_from_registers(
        radio={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "selected_band_set": [1, 3, 20],
            "radio_connected": True,
        },
        quality={},
        ca={},
        learning={"current_hour": 6},
        mutation={},
    )

    assert context.allowed_bands == ()


def test_tsar_refusal_is_explicit_and_learnable():
    decision = _decide(SmartLoopCore(),
        context=_context(),
        facts=LearningFacts(
            band_scores={3: 0.91},
            evidence_counts={3: 1},
            profile_confidence=0.9,
        ),
    )

    assert decision.outcome == "action_rejected"
    assert decision.reason_code == "learning_evidence_low"
    assert decision.required_next_step == "collect_more_samples"
    assert decision.candidates[0].reason_ref.startswith("0x")


def test_calibration_fact_lifts_confidence_but_not_history_evidence():
    package = {
        "schema": "calibration_fact_package.v1",
        "owner": "calibration_fact_confidence",
        "confidence": 0.82,
        "clean_for_learning": True,
        "strong_enough_for_preemption_facts": True,
        "self_learning_input_strength": 0.82,
        "evidence_count": 24,
    }

    decision = _decide(SmartLoopCore(),
        context=_context(),
        facts=LearningFacts(
            band_scores={3: 0.82, 1: 0.38},
            evidence_counts={3: 1},
            profile_confidence=0.45,
            calibration_fact_package=package,
        ),
    )

    assert decision.outcome == "action_rejected"
    assert decision.reason_code == "learning_evidence_low"
    first = decision.candidates[0].as_dict()
    assert first["evidence_count"] == 1
    assert first["confidence"] > 0.5
    assert first["calibration_fact_package"]["schema"] == "calibration_fact_package.v1"
    assert "calibration_evidence_lift" not in first
    assert first["calibration_confidence_lift"] > 0


def test_calibration_fact_cannot_materialize_candidate_without_learning_hypothesis():
    package = {
        "schema": "calibration_fact_package.v1",
        "owner": "calibration_fact_confidence",
        "confidence": 1.0,
        "clean_for_learning": True,
        "strong_enough_for_preemption_facts": True,
        "self_learning_input_strength": 1.0,
        "evidence_count": 30,
    }

    decision = _decide(SmartLoopCore(),
        context=_context(
            current_pband=3, allowed_bands=(1, 3, 20), cq=0.33, congestion_pct=82.0
        ),
        facts=LearningFacts(
            band_scores={3: 0.3333},
            evidence_counts={3: 3},
            profile_confidence=0.3,
            calibration_fact_package=package,
        ),
    )

    assert decision.outcome == "no_action_justified"
    assert decision.reason_code == "current_anchor_underperforms_no_better_candidate"
    assert decision.required_next_step == "generate_better_executable_candidate"
    assert decision.selected is None
    assert not hasattr(decision, "command")
    first = decision.candidates[0].as_dict()
    assert first["reason_code"] == "learned_anchor_underperforms"
    assert first["action"] == "avoid_band"
    assert first["direct_action_allowed"] is False
    assert first["calibration_fact_package"]["schema"] == "calibration_fact_package.v1"
    assert decision.tsar_gate is not None
    assert decision.tsar_gate.verdict == "no_action"
    assert decision.opened_proof is None


def test_weak_current_anchor_without_authority_alternate_is_justified_no_action():
    package = {
        "schema": "calibration_fact_package.v1",
        "owner": "calibration_fact_confidence",
        "confidence": 1.0,
        "clean_for_learning": True,
        "strong_enough_for_preemption_facts": True,
        "self_learning_input_strength": 1.0,
        "evidence_count": 30,
    }

    decision = _decide(SmartLoopCore(),
        context=_context(
            current_pband=3,
            allowed_bands=(3,),
            ca_pool=(3,),
            cq=0.33,
            congestion_pct=82.0,
        ),
        facts=LearningFacts(
            band_scores={3: 0.3333},
            evidence_counts={3: 3},
            profile_confidence=0.3,
            calibration_fact_package=package,
        ),
    )

    assert decision.outcome == "no_action_justified"
    assert decision.reason_code == "current_anchor_underperforms_no_better_candidate"
    assert decision.required_next_step == "generate_better_executable_candidate"
    assert not hasattr(decision, "command")


def test_advisory_avoid_band_for_non_current_anchor_is_recorded_not_rejected():
    decision = _decide(SmartLoopCore(),
        context=_context(
            current_pband=3, allowed_bands=(1, 3, 20), cq=0.64, congestion_pct=21.0
        ),
        facts=LearningFacts(
            band_scores={20: 0.0},
            evidence_counts={20: 60},
            profile_confidence=1.0,
            action_roi={
                "by_pband": {
                    "20": {
                        "penalty": 0.55,
                        "reason": "recent_bad_pband_proof",
                        "last_bad_provider_plmn": "22005",
                        "last_bad_cell_id": 2050,
                        "last_bad_pci": 255,
                        "last_bad_earfcn": 1795,
                        "last_bad_hour": 6,
                    }
                }
            },
        ),
    )

    assert decision.candidates
    assert decision.candidates[0].action == "avoid_band"
    assert decision.candidates[0].target_band == 20
    assert decision.outcome == "no_action_justified"
    assert decision.reason_code == "advisory_avoid_band_recorded"
    assert not hasattr(decision, "command")
    payload = decision.as_register_payload()
    assert payload["bandit_decision"]["decision_made"] is True
    assert payload["bandit_decision"]["decision_kind"] == "advisory"
    assert payload["bandit_decision"]["decision_semantic"] == "advisory_only"
    assert payload["bandit_decision"]["selected_action"] == "avoid_band"
    assert payload["bandit_decision"]["reason_code"] == "advisory_avoid_band_recorded"
    assert payload["bandit_decision"]["reason_ref"]
    assert payload["bandit_decision"]["proof_required"] is False
    assert payload["bandit_reasoning_flow"]["requirements"]["proof_reward_path"] is True
    assert (
        payload["bandit_reasoning_flow"]["requirements"]["reward_path_state"]
        == "not_required_for_non_executable_decision"
    )


def test_penalized_current_anchor_does_not_claim_current_anchor_is_best():
    decision = _decide(SmartLoopCore(),
        context=_context(
            current_pband=3, allowed_bands=(1, 3, 20), cq=0.64, congestion_pct=21.0
        ),
        facts=LearningFacts(
            band_scores={3: 0.93, 1: 0.0, 20: 0.0},
            evidence_counts={3: 200, 1: 60, 20: 60},
            profile_confidence=1.0,
            action_roi={
                "by_pband": {
                    "3": {
                        "penalty": 0.55,
                        "reason": "recent_bad_pband_proof",
                        "last_bad_provider_plmn": "22005",
                        "last_bad_cell_id": 2050,
                        "last_bad_pci": 255,
                        "last_bad_earfcn": 1795,
                        "last_bad_hour": 6,
                    }
                }
            },
        ),
    )

    assert decision.candidates
    assert decision.candidates[0].action == "stay_current_anchor"
    assert decision.candidates[0].reason_code == "current_anchor_penalized_by_roi"
    assert decision.outcome == "no_action_justified"
    assert decision.reason_code == "current_anchor_penalized_no_better_candidate"
    assert decision.required_next_step == "generate_better_executable_candidate"
    payload = decision.as_register_payload()
    assert payload["bandit_decision"]["decision_kind"] == "stay"
    assert (
        payload["bandit_decision"]["reason_code"]
        == "current_anchor_penalized_no_better_candidate"
    )
    assert (
        payload["bandit_decision"]["reason"]
        == "self-learning has negative ROI evidence for the current anchor"
    )
    assert (
        payload["closure_audit"]["reason_code"]
        == "current_anchor_penalized_no_better_candidate"
    )


def test_avoid_current_anchor_without_recovery_is_justified_no_action_not_rejected():
    decision = _decide(SmartLoopCore(),
        context=_context(
            current_pband=3, allowed_bands=(1, 3, 20), cq=0.20, congestion_pct=90.0
        ),
        facts=LearningFacts(
            band_scores={3: 0.20, 1: 0.0, 20: 0.0},
            evidence_counts={3: 200, 1: 580, 20: 21134},
            profile_confidence=1.0,
        ),
    )

    assert decision.candidates
    assert decision.candidates[0].action == "avoid_band"
    assert decision.candidates[0].target_band == 3
    assert decision.outcome == "no_action_justified"
    assert decision.reason_code == "current_anchor_underperforms_no_better_candidate"
    assert decision.required_next_step == "generate_better_executable_candidate"
    payload = decision.as_register_payload()
    assert payload["candidate_counts"]["operational"] == 0
    assert payload["candidate_counts"]["advisory"] == 3
    assert payload["bandit_decision"]["decision_kind"] == "advisory"
    assert (
        payload["bandit_decision"]["reason_code"]
        == "current_anchor_underperforms_no_better_candidate"
    )
    assert payload["closure_audit"]["status"] == "no_action_justified"


def test_negative_current_anchor_evidence_generates_executable_recovery_candidate():
    decision = _decide(SmartLoopCore(),
        context=_context(
            current_pband=20, allowed_bands=(1, 3, 20), cq=0.33, congestion_pct=82.0
        ),
        facts=LearningFacts(
            band_scores={20: 0.0, 3: 0.20},
            evidence_counts={20: 60, 3: 8},
            profile_confidence=1.0,
            action_roi={
                "by_pband": {
                    "20": {
                        "penalty": 0.55,
                        "reason": "recent_bad_pband_proof",
                        "last_bad_previous_pband": 3,
                        "last_bad_provider_plmn": "22005",
                        "last_bad_cell_id": 2050,
                        "last_bad_pci": 255,
                        "last_bad_earfcn": 1795,
                        "last_bad_hour": 6,
                    }
                }
            },
        ),
    )

    assert decision.outcome == "action_selected_execution_pending"
    assert decision.reason_code == "tsar_approved"
    assert decision.selected is not None
    assert decision.selected.action == RADIO_CONFIGURATION_ACTION
    assert decision.selected.target_band == 3
    assert not hasattr(decision, "command")
    assert decision.candidates[0].reason_code == "negative_current_anchor_recovery"
    assert decision.candidates[0].as_dict()["direct_action_allowed"] is True


def test_current_single_band_pool_generates_radio_configuration_probe():
    decision = _decide(SmartLoopCore(),
        context=_context(
            current_pband=1,
            allowed_bands=(1, 3, 20),
            ca_pool=(1,),
            cq=0.31,
            congestion_pct=84.0,
            ca_active=False,
        ),
        facts=LearningFacts(
            band_scores={1: 0.42},
            evidence_counts={1: 24},
            profile_confidence=0.9,
            action_roi={
                "by_pband": {
                    "1": {
                        "penalty": 0.22,
                        "reason": "recent_bad_current_configuration",
                        "last_bad_provider_plmn": "22005",
                        "last_bad_cell_id": 2050,
                        "last_bad_pci": 255,
                        "last_bad_earfcn": 1795,
                        "last_bad_hour": 6,
                    }
                }
            },
        ),
    )

    assert decision.outcome == "action_selected_execution_pending"
    assert decision.selected is not None
    assert decision.selected.action == RADIO_CONFIGURATION_ACTION
    assert decision.selected.target_band == 1
    assert decision.selected.target_bands == (1, 3, 20)
    assert decision.selected.reason_code == "radio_configuration_space_probe"
    assert not hasattr(decision, "command")
    candidate = decision.candidates[0].as_dict()
    assert candidate["target_bands"] == [1, 3, 20]
    assert candidate["radio_configuration_hypothesis"] is True
    assert candidate["configuration_previous_bands"] == [1]


def test_tsar_does_not_use_current_ca_pool_as_action_authority():
    decision = _decide(SmartLoopCore(),
        context=_context(allowed_bands=(), ca_pool=(1, 3)),
        facts=LearningFacts(
            band_scores={3: 0.82, 1: 0.38},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
    )

    assert decision.outcome == "action_rejected"
    assert decision.reason_code == "band_authority_unavailable"
    assert decision.required_next_step == "wait_for_radio_authority"


def test_tsar_reports_missing_authority_distinct_from_outside_authority():
    decision = _decide(SmartLoopCore(),
        context=_context(allowed_bands=(), ca_pool=()),
        facts=LearningFacts(
            band_scores={3: 0.82, 1: 0.38},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
    )

    assert decision.outcome == "action_rejected"
    assert decision.reason_code == "band_authority_unavailable"
    assert decision.required_next_step == "wait_for_radio_authority"


def test_crystal_ball_horizon_context_seeds_useful_candidate_without_direct_action_bypass():
    decision = _decide(SmartLoopCore(),
        context=_context(),
        facts=LearningFacts(
            band_scores={1: 0.46},
            evidence_counts={1: 3},
            profile_confidence=0.55,
            prediction_context=_prediction_context_for_b3(),
        ),
    )

    assert decision.outcome == "action_selected_execution_pending"
    assert decision.reason_code == "tsar_approved"
    assert decision.selected is not None
    assert decision.selected.action == RADIO_CONFIGURATION_ACTION
    assert decision.selected.target_band == 3
    selected = decision.candidates[0].as_dict()
    assert selected["reason_code"] == "crystal_ball_horizon_predicts_better_anchor"
    assert selected["prediction_context"]["schema"] == "crystal_ball_horizon_context.v1"
    assert (
        selected["prediction_context"]["reason"]
        == "crystal_ball_future_horizon_predicts_better_anchor"
    )
    assert decision.selected.command_id
    assert decision.selected.proof_id
    assert not hasattr(decision, "command")


def test_recent_window_context_seeds_useful_candidate_before_roi_is_graded():
    state = SmartLoopState(
        observations=(
            SmartLoopObservation(
                ts=650.0,
                pband=3,
                cq=0.78,
                congestion_pct=22.0,
                ca_active=True,
                context_key=_context().learning_context_key,
            ),
            SmartLoopObservation(
                ts=760.0,
                pband=3,
                cq=0.76,
                congestion_pct=24.0,
                ca_active=True,
                context_key=_context().learning_context_key,
            ),
            SmartLoopObservation(
                ts=880.0,
                pband=3,
                cq=0.80,
                congestion_pct=26.0,
                ca_active=True,
                context_key=_context().learning_context_key,
            ),
            SmartLoopObservation(
                ts=930.0,
                pband=1,
                cq=0.32,
                congestion_pct=82.0,
                ca_active=False,
                context_key=_context().learning_context_key,
            ),
        )
    )

    decision = _decide(SmartLoopCore(),
        context=_context(
            current_pband=1,
            cq=0.31,
            congestion_pct=84.0,
            ca_active=False,
            observed_ts=1000.0,
        ),
        facts=LearningFacts(
            band_scores={1: 0.46},
            evidence_counts={1: 3},
            profile_confidence=0.4,
        ),
        state=state,
    )

    assert decision.outcome == "action_selected_execution_pending"
    assert decision.reason_code == "tsar_approved"
    assert decision.selected is not None
    assert decision.selected.target_band == 3
    selected = decision.candidates[0].as_dict()
    assert selected["reason_code"] == "recent_window_context_predicts_better_anchor"
    assert (
        selected["recent_window_context"]["schema"]
        == "smart_loop.recent_window_context.v1"
    )
    assert selected["recent_window_context"]["owner"] == "arch4.smart_loop.closed_loop"
    assert selected["recent_window_context"]["sample_count"] >= 2
    assert selected["evidence_count"] >= 2
    payload = decision.as_register_payload()
    assert payload["state"]["recent_windows"]["owner"] == "arch4.smart_loop.closed_loop"
    assert (
        payload["state"]["recent_windows"]["bands"]["3"]["past_6m"]["sample_count"] >= 2
    )


def test_crystal_ball_future_plus_smart_loop_past_windows_close_to_bandit_update():
    core = SmartLoopCore()
    past_state = SmartLoopState(
        observations=(
            SmartLoopObservation(
                ts=940.0,
                pband=3,
                cq=0.78,
                congestion_pct=23.0,
                ca_active=True,
                context_key=_context().learning_context_key,
            ),
            SmartLoopObservation(
                ts=970.0,
                pband=3,
                cq=0.80,
                congestion_pct=21.0,
                ca_active=True,
                context_key=_context().learning_context_key,
            ),
            SmartLoopObservation(
                ts=990.0,
                pband=1,
                cq=0.32,
                congestion_pct=84.0,
                ca_active=False,
                context_key=_context().learning_context_key,
            ),
        )
    )

    first = _decide(core,
        context=_context(
            current_pband=1,
            cq=0.31,
            congestion_pct=86.0,
            ca_active=False,
            observed_ts=1000.0,
        ),
        facts=LearningFacts(
            band_scores={1: 0.46},
            evidence_counts={1: 3},
            profile_confidence=0.4,
            prediction_context=_prediction_context_for_b3(),
        ),
        state=past_state,
    )

    assert first.outcome == "action_selected_execution_pending"
    assert first.selected is not None
    assert first.selected.target_band == 3
    first_payload = first.as_register_payload()
    selected = first_payload["selected"]
    assert selected["reason_code"] == "crystal_ball_horizon_predicts_better_anchor"
    assert selected["prediction_context"]["schema"] == "crystal_ball_horizon_context.v1"
    assert (
        selected["recent_window_context"]["schema"]
        == "smart_loop.recent_window_context.v1"
    )
    assert (
        first_payload["state"]["recent_windows"]["bands"]["3"]["past_1m"][
            "sample_count"
        ]
        >= 2
    )
    assert first_payload["closure_audit"]["status"] == "selected_action_pending_execution"

    executed_state = _state_after_execution(
        core,
        first,
        context=_context(
            current_pband=1,
            cq=0.31,
            congestion_pct=86.0,
            ca_active=False,
            observed_ts=1000.0,
        ),
        opened_ts=1000.0,
    )

    closed = _decide(core,
        context=_context(
            current_pband=1,
            cq=0.31,
            congestion_pct=86.0,
            ca_active=False,
            observed_ts=1000.0,
        ),
        facts=LearningFacts(
            band_scores={1: 0.46},
            evidence_counts={1: 3},
            profile_confidence=0.4,
        ),
        state=executed_state,
        proof_grade=_terminal_proof_grade(
            executed_state,
            label="good",
            reason="post_action_improved",
            cq_delta=0.41,
            congestion_delta=-44.0,
            admitted=True,
        ),
    )

    assert closed.outcome == "proof_closed_good"
    assert closed.closed_proof is not None
    assert closed.closed_proof.memory_admitted is True
    assert closed.policy_update_applied is True
    assert closed.future_policy_effect_status == "pending"
    closed_payload = closed.as_register_payload()
    assert closed_payload["closure_audit"]["status"] == (
        "closed_policy_update_applied_effect_pending"
    )
    assert closed_payload["closure_audit"]["missing_links"] == []
    assert "roi" not in closed.state.as_dict()
    assert (
        closed_payload["closed_proof"]["layered_feedback"]["schema"]
        == "layered_action_feedback.v1"
    )
    assert "roi" not in closed_payload["state"]

    without_roi = _decide(core,
        context=_context(
            current_pband=1,
            cq=0.33,
            congestion_pct=82.0,
            ca_active=False,
            observed_ts=1120.0,
        ),
        facts=LearningFacts(
            band_scores={1: 0.46, 3: 0.54},
            evidence_counts={1: 3, 3: 2},
            profile_confidence=0.5,
        ),
        state=SmartLoopState(),
    )
    with_memory_roi = _decide(core,
        context=_context(
            current_pband=1,
            cq=0.33,
            congestion_pct=82.0,
            ca_active=False,
            observed_ts=1120.0,
        ),
        facts=LearningFacts(
            band_scores={1: 0.46, 3: 0.54},
            evidence_counts={1: 3, 3: 2},
            profile_confidence=0.5,
            action_roi={
                "by_pband": {
                    "3": {
                        "benefit": 0.25,
                        "reason": "terminal_reward_memory_projection",
                        "last_good_provider_plmn": "22005",
                        "last_good_cell_id": 2050,
                        "last_good_pci": 255,
                        "last_good_earfcn": 1795,
                        "last_good_hour": 6,
                    }
                }
            },
        ),
        state=SmartLoopState(),
    )

    assert without_roi.outcome != "action_selected_execution_pending"
    assert with_memory_roi.outcome == "action_selected_execution_pending"
    assert with_memory_roi.selected is not None
    assert with_memory_roi.selected.target_band == 3
    selected_with_roi = with_memory_roi.candidates[0].as_dict()
    assert selected_with_roi["roi_bias"] > 0.0
    assert "recent_window_context" not in selected_with_roi


def test_proof_grade_admits_good_reward_and_reports_bandit_update():
    core = SmartLoopCore()
    before = _decide(core,
        context=_context(),
        facts=LearningFacts(
            band_scores={3: 0.82},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
    )

    executed_state = _state_after_execution(
        core, before, context=_context(), opened_ts=100.0
    )
    after = _decide(core,
        context=_context(),
        facts=LearningFacts(
            band_scores={3: 0.82},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
        state=executed_state,
        proof_grade=_terminal_proof_grade(
            executed_state,
            label="good",
            reason="post_action_improved",
            cq_delta=0.14,
            congestion_delta=-19.0,
            admitted=True,
        ),
    )

    assert after.closed_proof is not None
    assert after.closed_proof.label == "good"
    assert after.closed_proof.memory_admitted is True
    opened_proof = executed_state.pending_proof
    assert opened_proof is not None
    assert after.closed_proof.proof_id == opened_proof.proof_id
    assert after.closed_proof.command_id == opened_proof.command_id
    assert after.closed_proof.context_key == opened_proof.context_key
    assert after.closed_proof.reason_ref == opened_proof.reason_ref
    payload = after.as_register_payload()
    assert payload["closed_proof"]["proof_id"] == opened_proof.proof_id
    assert payload["closed_proof"]["command_id"] == opened_proof.command_id
    assert after.policy_update_applied is True
    assert after.future_policy_effect_status == "pending"
    assert payload["closure_audit"]["status"] == (
        "closed_policy_update_applied_effect_pending"
    )
    assert payload["closure_audit"]["memory_admitted"] is True
    assert payload["closure_audit"]["prediction_error_recorded"] is True
    assert payload["closure_audit"]["policy_update_applied"] is True
    assert (
        payload["closure_audit"]["future_policy_effect_status"]
        == "pending"
    )
    assert payload["closure_audit"]["missing_links"] == []
    assert "roi" not in after.state.as_dict()


def test_invalid_proof_does_not_mutate_roi():
    core = SmartLoopCore()
    first = _decide(core,
        context=_context(),
        facts=LearningFacts(
            band_scores={3: 0.82},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
    )
    executed_state = _state_after_execution(
        core, first, context=_context(), opened_ts=100.0
    )
    closed = _decide(core,
        context=_context(),
        facts=LearningFacts(
            band_scores={3: 0.82},
            evidence_counts={3: 4},
            profile_confidence=0.9,
        ),
        state=executed_state,
        proof_grade=_terminal_proof_grade(
            executed_state,
            label="invalid",
            reason="proof_missing_quality",
            cq_delta=None,
            congestion_delta=None,
            admitted=False,
        ),
    )

    assert closed.closed_proof is not None
    assert closed.closed_proof.label == "invalid"
    assert closed.closed_proof.memory_admitted is False
    assert closed.policy_update_applied is False
    audit = closed.as_register_payload()["closure_audit"]
    assert audit["status"] == "proof_invalid"
    assert audit["memory_admitted"] is False
    assert audit["policy_update_applied"] is False
    assert audit["missing_links"] == ["memory_admitted"]
    assert "roi" not in closed.state.as_dict()


def test_bad_roi_bias_avoids_repeating_same_weak_band_when_alternative_exists():
    core = SmartLoopCore()
    first = _decide(core,
        context=_context(),
        facts=LearningFacts(
            band_scores={3: 0.82}, evidence_counts={3: 4}, profile_confidence=0.9
        ),
    )
    executed_state = _state_after_execution(
        core, first, context=_context(), opened_ts=100.0
    )
    bad = _decide(core,
        context=_context(),
        facts=LearningFacts(
            band_scores={3: 0.82, 20: 0.70},
            evidence_counts={3: 4, 20: 4},
            profile_confidence=0.9,
        ),
        state=executed_state,
        proof_grade=_terminal_proof_grade(
            executed_state,
            label="bad",
            reason="post_action_regressed",
            cq_delta=-0.12,
            congestion_delta=14.0,
            admitted=True,
        ),
    )

    assert bad.closed_proof is not None
    assert bad.closed_proof.label == "bad"
    assert bad.closed_proof.layered_feedback["schema"] == "layered_action_feedback.v1"
    assert (
        bad.closed_proof.layered_feedback["owner"] == "arch4.brain.proof_feedback_owner"
    )
    assert bad.closed_proof.layered_feedback["learning_label"] in {"bad", "strong_bad"}
    assert bad.closed_proof.layered_feedback["memory_grade_score"] in {-1, -2, -3}
    assert "roi" not in bad.state.as_dict()
    assert bad.candidates[0].target_band == 20
    assert all(
        candidate.target_band != 3 or candidate.roi_bias < 0
        for candidate in bad.candidates
    )


def test_opportunity_matrix_enumerates_useful_sources_without_action_authority():
    state = SmartLoopState(
        observations=(
            SmartLoopObservation(
                ts=940.0,
                pband=3,
                cq=0.78,
                congestion_pct=23.0,
                ca_active=True,
                context_key=_context().learning_context_key,
            ),
            SmartLoopObservation(
                ts=970.0,
                pband=3,
                cq=0.80,
                congestion_pct=21.0,
                ca_active=True,
                context_key=_context().learning_context_key,
            ),
        )
    )
    facts = LearningFacts(
        band_scores={1: 0.34, 3: 0.61},
        evidence_counts={1: 4, 3: 4},
        profile_confidence=0.8,
        prediction_context=_prediction_context_for_b3(),
        measured_capacity_fact={
            "schema": "measured_capacity_fact.v1",
            "owner": "arch4.smart_loop.capacity_probe",
            "measured": True,
            "confidence": 0.9,
            "serving_band": 1,
            "dl_mbps": 3.0,
            "expected_dl_mbps": 35.0,
            "alternate_bands": [3, 20],
            "sample_count": 8,
        },
    )
    matrix = build_opportunity_matrix(
        context=_context(
            current_pband=1, ca_pool=(1,), allowed_bands=(1, 3, 20), observed_ts=1000.0
        ),
        facts=facts,
        state=state,
    )

    assert matrix["schema"] == "smart_loop.opportunity_matrix.v1"
    assert matrix["owner"] == "arch4.smart_loop.opportunity_matrix"
    sources = {row["source"] for row in matrix["rows"]}
    assert {
        "current_anchor",
        "learned_band_score",
        "crystal_ball_future",
        "recent_window",
        "measured_capacity_gap",
        "configuration_space",
    }.issubset(sources)
    assert matrix["target_bands"]
    assert "command" not in matrix


def test_staged_roi_record_uses_single_layered_feedback_owner():
    original = SimpleNamespace(
        timestamp=100.0,
        sinr_db=2.0,
        rsrq_db=-8.0,
        mcs_dl=0,
        ca_signature="B1",
        rtt_sliding_avg=40.0,
        rtt_ms=40.0,
        lever=LeverType.PBAND,
        action_params={"pband": 3},
        throughput_before_mbps=8.0,
        provider_plmn="22005",
        provider_name="A1",
        calibration_marker="C1",
        calibration_signature="sig",
        decision_trace_id="dt-parent",
        objective_metadata={},
        proposal_intent="smart_loop_candidate",
        proposal_objective_id="obj",
        proposal_reason="test",
        proposal_score=0.8,
        proposal_action_origin="smart_loop",
        proposal_stage="candidate",
        proposal_authority="arch4.smart_loop.closed_loop",
        proposal_triggered_by="arch4.smart_loop.closed_loop",
        learning_gate={},
        cq_cell=0.35,
    )
    modem_now = materialize_controller_generation(
        ModemInput.safe_defaults(
            primary_band=3,
            ca_active=True,
            ca_band_signature="B3",
            selected_band_set=[3],
        )
    ).intelligence
    record, _ = build_staged_roi_record(
        proof_owner="arch4.brain.proof_referee_due_close_owner",
        original=original,
        modem_now=modem_now,
        horizon=300,
        label="good",
        reason="stable_after_horizon",
        evidence={
            "grade_evidence_valid": True,
            "grade_context_status": "same",
            "cq_before": 0.35,
            "cq_now": 0.62,
            "cast_ld_valid": True,
            "cast_ld": 97.0,
            "cart_ld_activity": 4.0,
        },
        ts=400.0,
    )

    feedback = record.objective_metadata["layered_action_feedback"]
    assert feedback["schema"] == "layered_action_feedback.v1"
    assert feedback["owner"] == "arch4.brain.proof_feedback_owner"
    assert feedback["learning_label"] in {"good", "strong_good"}
    assert feedback["memory_grade_score"] in {1, 2, 3}
    assert (
        record.action_params["_layered_feedback_owner"]
        == "arch4.brain.proof_feedback_owner"
    )
    assert record.action_params["_layered_feedback_grade_score"] in {1, 2, 3}
    assert "_layered_feedback_memory_weight" not in record.action_params


def test_recent_bad_action_shape_penalizes_repeat_even_when_hour_drifted():
    core = SmartLoopCore()
    decision = _decide(core,
        context=_context(hour=7),
        facts=LearningFacts(
            band_scores={3: 0.82, 20: 0.70},
            evidence_counts={3: 4, 20: 4},
            profile_confidence=0.9,
            action_roi={
                "by_pband": {
                    "3": {
                        "penalty": 0.35,
                        "reason": "recent_bad_pband_proof",
                        "last_bad_provider_plmn": "22005",
                        "last_bad_cell_id": 2050,
                        "last_bad_pci": 255,
                        "last_bad_earfcn": 1795,
                        "last_bad_hour": 6,
                    }
                }
            },
        ),
    )

    assert decision.candidates
    assert decision.candidates[0].target_band == 20
    repeated = next(
        candidate for candidate in decision.candidates if candidate.target_band == 3
    )
    assert repeated.roi_bias < 0.0
    assert repeated.metadata["action_shape_roi_recent_bad_match"] is True


def test_matrix_covers_closed_loop_release_contract():
    cases = default_smart_loop_matrix()
    assert {case.name for case in cases} == {
        "useful_candidate_becomes_action",
        "calibration_strengthens_existing_learning_candidate",
        "recent_window_context_seeds_useful_candidate",
        "low_evidence_candidate_is_refused_with_learnable_reason",
        "safety_gate_blocks_action",
        "missing_candidate_is_counterfactual_failure",
        "good_referee_proof_closes_without_local_bias",
        "bad_proof_hold_blocks_repeated_objective",
        "invalid_proof_does_not_mutate_roi",
    }
    for case, (_, decision) in zip(cases, run_smart_loop_matrix(cases), strict=True):
        assert decision.outcome == case.expected_outcome
        assert decision.reason_code == case.expected_reason
        audit = decision.as_register_payload()["closure_audit"]
        assert audit["schema"] == "smart_loop.closure_audit.v1"
        assert audit["owner"] == "arch4.smart_loop.closed_loop"
        if case.name == "missing_candidate_is_counterfactual_failure":
            assert audit["status"] == "candidate_generation_gap"
            assert audit["missing_links"] == [
                "candidate_generated",
                "action_selected",
                "selected_action_materialized",
                "proof_opened",
            ]
        if case.name == "good_referee_proof_closes_without_local_bias":
            assert audit["status"] == (
                "closed_policy_update_applied_effect_pending"
            )
            assert audit["policy_update_applied"] is True


def test_clean_loop_has_no_dependency_on_old_smartness_modules():
    source = "\n".join(
        (
            (ROOT / "arch4" / "smart_loop" / "closed_loop.py").read_text(
                encoding="utf-8"
            ),
            (ROOT / "arch4" / "smart_loop" / "register_adapter.py").read_text(
                encoding="utf-8"
            ),
        )
    )
    banned = (
        "arch4.brain",
        "arch4.smart_core",
        "arch4.engine",
        "arch4.runtime",
        "Memory",
        "smart_loop_authority",
        "tsar_candidate_planner",
        "proof_referee",
        "action_roi_memory",
        "learning_loop_simulator",
        "fallback",
        "compatibility",
    )
    offenders = [token for token in banned if token in source]
    assert offenders == []


def test_old_runtime_smartness_remains_outside_clean_loop_state():
    state = SmartLoopState()
    assert not hasattr(state, "memory")
    assert not hasattr(state, "brain_vectors")
    assert not hasattr(state, "tsar_plan")


def test_clean_strategy_clock_lane_publishes_live_owner_from_registers(monkeypatch):
    # This test proves the selected-action path.  Pin only the Bandit's
    # reproducible propensity draw so an unrelated evidence-hash change cannot
    # turn the test into a do-nothing-path proof.
    monkeypatch.setattr(
        "arch4.contextual_bandit._deterministic_unit_interval",
        lambda _seed: 0.999999,
    )
    runtime_bank = RuntimeRegisterBank()
    install_test_identity_register(
        runtime_bank,
        value={
            "identity_code": "0x0000000000000101",
            "identity_status": "exact",
            "identity_catalog_version": "test-identity-catalog-v1",
            "identity_catalog_checksum": "a" * 64,
            "session_scope_key": "test-signal-session",
            "source_epoch": "test-signal-epoch",
            "source_sequence": 1,
            "provider_plmn": "22005",
        },
        source="test",
        updated_at=100.0,
        valid=True,
        stale=False,
        dirty=False,
        validity="valid",
        force=True,
    )
    runtime_bank.write_radio(
        value={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
        },
        source="test",
        updated_at=100.0,
    )
    publish_test_signal_generation(
        runtime_bank,
        quality={"cq_cell_display": 0.42, "congestion_pct": 74.0},
        ca={"ca_active": True},
        updated_at=100.0,
    )
    runtime_bank.write_learning(
        value={
            "learning_allowed": True,
            "calibration_fact_package": {
                "schema": "calibration_fact_package.v1",
                "owner": "calibration_fact_confidence",
                "confidence": 0.82,
                "clean_for_learning": True,
                "strong_enough_for_preemption_facts": True,
                "self_learning_input_strength": 0.82,
                "evidence_count": 24,
            },
        },
        source="test",
        updated_at=100.0,
    )
    runtime_bank.write_mutation(
        value={"cooldown_s": 0.0}, source="test", updated_at=100.0
    )
    feed_bank = BrainFeedRegisterBank()
    feed_bank.write_feeder(
        FEED_LEARNING_QUALITY,
        owner="arch4.brain.learning_quality",
        payload={
            "schema": "brain_learning_quality_publication.v1",
            "owner": "arch4.brain.learning_quality",
            "current_hour": 6,
            "learning_quality": {
                "profile_confidence": 0.9,
                "sample_count": 20,
                "band_hour_quality": {"3:6": 0.82},
                "band_hour_counts": {"3:6": 4},
                "control_authority_bands": [1, 3, 20],
            },
        },
        source="test",
        updated_at=100.0,
        stale_after_s=5.0,
    )
    _publish_smartness(runtime_bank, now=100.0)
    engine = _composed_engine(runtime_bank, feed_bank)

    outcome = run_smart_loop_strategy_clock_lane(engine, now=100.0)
    payload = read_smart_loop_strategy_register(engine, now=100.1)
    row = feed_bank.read_feeder(FEED_SELF_LEARNING_STRATEGY)

    assert outcome.ran is True
    assert row.owner == "arch4.smart_loop.closed_loop"
    assert row.source == "smart_loop_closed_loop"
    assert payload["schema"] == "smart_loop.closed_loop.v1"
    assert payload["owner"] == "arch4.smart_loop.closed_loop"
    assert payload["outcome"] == "action_selected_execution_pending"
    assert payload["reason_code"] == "brain_selected_optimization_action"
    assert "command" not in payload
    assert payload["opened_proof"] is None
    assert (
        engine.action_transaction_authority.selection_status()[
            "active"
        ]
        is not None
    )
    assert payload["selected"]["target_band"] == 3
    assert payload["action_opportunity_set"]["scope_admitted"] is True
    assert payload["contextual_bandit_selection_receipt"]["selected"] is True
    assert payload["brain_decision_receipt"]["do_nothing"] is False
    assert (
        payload["candidates"][0]["calibration_fact_package"]["schema"]
        == "calibration_fact_package.v1"
    )
    assert payload["candidates"][0]["context"]["cq"] == 0.42
    assert payload["candidates"][0]["context"]["congestion_pct"] == 74.0
    assert payload["candidates"][0]["context"]["ca_active"] is True
    context_vector = payload["bandit_reasoning_flow"]["context_vector"]
    assert context_vector["cq"] == 0.42
    assert context_vector["congestion_pct"] == 74.0
    assert context_vector["ca_active"] is True

def test_clean_strategy_clock_lane_keeps_proof_pending_after_live_target_band_confirmation():
    runtime_bank = RuntimeRegisterBank()
    runtime_bank.write_radio(
        value={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
        },
        source="test",
        updated_at=100.0,
    )
    publish_test_signal_generation(
        runtime_bank,
        quality={"cq_cell_display": 0.42, "congestion_pct": 74.0},
        updated_at=100.0,
    )
    runtime_bank.write_learning(
        value={"learning_allowed": True}, source="test", updated_at=100.0
    )
    runtime_bank.write_mutation(
        value={"cooldown_s": 0.0}, source="test", updated_at=100.0
    )
    feed_bank = BrainFeedRegisterBank()
    feed_bank.write_feeder(
        FEED_LEARNING_QUALITY,
        owner="arch4.brain.learning_quality",
        payload={
            "schema": "brain_learning_quality_publication.v1",
            "owner": "arch4.brain.learning_quality",
            "current_hour": 6,
            "learning_quality": {
                "profile_confidence": 0.9,
                "sample_count": 20,
                "band_hour_quality": {"3:6": 0.82, "1:6": 0.38},
                "band_hour_counts": {"3:6": 4, "1:6": 4},
                "control_authority_bands": [1, 3, 20],
            },
        },
        source="test",
        updated_at=100.0,
        stale_after_s=30.0,
    )
    engine = _composed_engine(runtime_bank, feed_bank)

    _publish_smartness(runtime_bank, now=100.0)
    first = run_smart_loop_strategy_clock_lane(engine, now=100.0)
    first_payload = read_smart_loop_strategy_register(engine, now=100.1)

    assert first.ran is True
    assert first_payload["outcome"] == "action_selected_execution_pending"
    assert first_payload["closure_audit"]["status"] == "selected_action_pending_execution"
    assert engine._smart_loop_state.pending_proof is None
    first_proof_id = first_payload["selected"]["proof_id"]
    _ack_active_selected_action_command(engine, now=100.2)

    unchanged = run_smart_loop_strategy_clock_lane(engine, now=100.5)
    unchanged_payload = read_smart_loop_strategy_register(engine, now=100.6)

    assert unchanged.ran is True
    assert unchanged_payload["outcome"] == "proof_pending"
    assert "command" not in unchanged_payload
    assert unchanged_payload["opened_proof"]["proof_id"] == first_proof_id
    assert unchanged_payload["candidate_counts"]["operational"] >= 1
    assert unchanged_payload["candidate_counts"]["actionable"] == 0
    assert unchanged_payload["bandit_reasoning_flow"]["requirements"]["ready"] is True
    assert (
        unchanged_payload["bandit_reasoning_flow"]["safety_shield"]["reason_code"]
        == "proof_pending"
    )
    assert (
        unchanged_payload["bandit_reasoning_flow"]["safety_shield"]["command_allowed"]
        is False
    )
    assert (
        unchanged_payload["bandit_reasoning_flow"]["reward_path"]["opened_proof"]
        is True
    )
    assert unchanged_payload["closure_audit"]["status"] == "proof_pending"
    assert engine._smart_loop_state.pending_proof is not None

    cached = run_smart_loop_strategy_clock_lane(engine, now=100.7)
    cached_payload = read_smart_loop_strategy_register(engine, now=100.8)

    assert cached.ran is True
    assert cached.backpressure_reason == "smart_loop_inputs_unchanged"
    assert cached_payload["outcome"] == "proof_pending"
    assert "command" not in cached_payload
    assert cached_payload["opened_proof"]["proof_id"] == first_proof_id

    seq_before_touch = feed_bank.read_feeder(FEED_SELF_LEARNING_STRATEGY).sequence
    updated_before_touch = feed_bank.read_feeder(FEED_SELF_LEARNING_STRATEGY).updated_at
    touched = run_smart_loop_strategy_clock_lane(engine, now=106.0)
    touched_row = feed_bank.read_feeder(FEED_SELF_LEARNING_STRATEGY)

    assert touched.ran is True
    assert touched.backpressure_reason == "smart_loop_inputs_unchanged"
    assert touched_row.sequence == seq_before_touch
    assert touched_row.updated_at > updated_before_touch

    runtime_bank.write_radio(
        value={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 3,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
        },
        source="test",
        updated_at=101.0,
    )
    publish_test_signal_generation(
        runtime_bank,
        quality={"cq_cell_display": 0.70, "congestion_pct": 55.0},
        updated_at=101.0,
    )
    _publish_smartness(runtime_bank, now=101.0)

    second = run_smart_loop_strategy_clock_lane(engine, now=101.0)
    second_payload = read_smart_loop_strategy_register(engine, now=101.1)

    assert second.ran is True
    assert second_payload["outcome"] == "proof_pending"
    assert second_payload["closed_proof"] is None
    assert second_payload["closure_audit"]["status"] == "proof_pending"
    assert second_payload["closure_audit"]["links"]["proof_closed"] is False
    assert second_payload["closure_audit"]["links"]["memory_admitted"] is False
    assert (
        second_payload["closure_audit"]["links"][
            "policy_update_applied"
        ]
        is False
    )
    assert engine._smart_loop_state.pending_proof is not None
    assert "roi" not in second_payload["state"]


def test_clean_strategy_clock_lane_ignores_smartness_publication_metadata_churn():
    runtime_bank = RuntimeRegisterBank()
    runtime_bank.write_radio(
        value={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 3,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
        },
        source="test",
        updated_at=100.0,
    )
    publish_test_signal_generation(
        runtime_bank,
        quality={"cq_cell_display": 0.75, "congestion_pct": 12.0},
        ca={"ca_active": True},
        updated_at=100.0,
    )
    runtime_bank.write_learning(
        value={"learning_allowed": True}, source="test", updated_at=100.0
    )
    runtime_bank.write_mutation(
        value={"cooldown_s": 0.0}, source="test", updated_at=100.0
    )
    feed_bank = BrainFeedRegisterBank()
    feed_bank.write_feeder(
        FEED_LEARNING_QUALITY,
        owner="arch4.brain.learning_quality",
        payload={
            "schema": "brain_learning_quality_publication.v1",
            "owner": "arch4.brain.learning_quality",
            "current_hour": 6,
            "learning_quality": {
                "profile_confidence": 0.9,
                "sample_count": 20,
                "band_hour_quality": {"3:6": 0.82},
                "band_hour_counts": {"3:6": 4},
                "control_authority_bands": [1, 3, 20],
            },
            "published_at": 100.0,
        },
        source="test",
        updated_at=100.0,
        stale_after_s=30.0,
    )
    engine = _composed_engine(runtime_bank, feed_bank)

    _publish_smartness(runtime_bank, now=100.0)
    first = run_smart_loop_strategy_clock_lane(engine, now=100.0)
    first_payload = read_smart_loop_strategy_register(engine, now=100.1)

    _publish_smartness(runtime_bank, now=100.4)
    second = run_smart_loop_strategy_clock_lane(engine, now=100.4)
    second_payload = read_smart_loop_strategy_register(engine, now=100.5)

    assert first.ran is True
    assert second.ran is True
    assert second.backpressure_reason == "smart_loop_inputs_unchanged"
    assert first_payload["outcome"] == second_payload["outcome"]
    assert second_payload["clock_lane"]["mode"] == "recomputed"


def test_clean_strategy_clock_lane_holds_objective_after_unconfirmed_target_timeout():
    runtime_bank = RuntimeRegisterBank()
    runtime_bank.write_radio(
        value={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
            "sample_ts": 100.0,
        },
        source="test",
        updated_at=100.0,
    )
    publish_test_signal_generation(
        runtime_bank,
        quality={"cq_cell_display": 0.42, "congestion_pct": 74.0},
        updated_at=100.0,
    )
    runtime_bank.write_learning(
        value={"learning_allowed": True}, source="test", updated_at=100.0
    )
    runtime_bank.write_mutation(
        value={"cooldown_s": 0.0}, source="test", updated_at=100.0
    )
    feed_bank = BrainFeedRegisterBank()
    feed_bank.write_feeder(
        FEED_LEARNING_QUALITY,
        owner="arch4.brain.learning_quality",
        payload={
            "schema": "brain_learning_quality_publication.v1",
            "owner": "arch4.brain.learning_quality",
            "current_hour": 6,
            "learning_quality": {
                "profile_confidence": 0.9,
                "sample_count": 20,
                "band_hour_quality": {"3:6": 0.82, "1:6": 0.38},
                "band_hour_counts": {"3:6": 4, "1:6": 4},
                "control_authority_bands": [1, 3, 20],
            },
        },
        source="test",
        updated_at=100.0,
        stale_after_s=900.0,
    )
    engine = _composed_engine(runtime_bank, feed_bank)

    _publish_smartness(runtime_bank, now=100.0)
    first = run_smart_loop_strategy_clock_lane(engine, now=100.0)
    first_payload = read_smart_loop_strategy_register(engine, now=100.1)

    assert first.ran is True
    assert first_payload["outcome"] == "action_selected_execution_pending"
    assert "command" not in first_payload
    assert first_payload["selected"]["target_band"] == 3
    first_command_id = first_payload["selected"]["command_id"]
    _ack_active_selected_action_command(engine, now=100.2)
    proof_opened = run_smart_loop_strategy_clock_lane(engine, now=100.2)
    proof_payload = read_smart_loop_strategy_register(engine, now=100.3)
    assert proof_opened.ran is True
    assert proof_payload["outcome"] == "proof_pending"

    runtime_bank.write_radio(
        value={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
            "sample_ts": 400.0,
        },
        source="test",
        updated_at=400.0,
    )
    publish_test_signal_generation(
        runtime_bank,
        quality={"cq_cell_display": 0.41, "congestion_pct": 76.0},
        updated_at=400.0,
    )
    _publish_smartness(runtime_bank, now=400.0)

    pending_later = run_smart_loop_strategy_clock_lane(engine, now=400.0)
    pending_payload = read_smart_loop_strategy_register(engine, now=400.1)

    assert pending_later.ran is True
    assert pending_payload["outcome"] == "proof_pending"
    assert pending_payload["state"]["pending_proof"]["command_id"] == first_command_id
    assert pending_payload["closed_proof"] is None
    assert "command" not in pending_payload
    assert pending_payload["state"]["objective_holds"] == {}

    runtime_bank.write_radio(
        value={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
            "sample_ts": 401.0,
        },
        source="test",
        updated_at=401.0,
    )
    publish_test_signal_generation(
        runtime_bank,
        quality={"cq_cell_display": 0.41, "congestion_pct": 76.0},
        updated_at=401.0,
    )
    _publish_smartness(runtime_bank, now=401.0)
    repeated = run_smart_loop_strategy_clock_lane(engine, now=401.0)
    repeated_payload = read_smart_loop_strategy_register(engine, now=401.1)

    assert repeated.ran is True
    assert "command" not in repeated_payload
    assert repeated_payload["state"]["pending_proof"]["command_id"] == first_command_id
    assert repeated_payload["state"]["objective_holds"] == {}


def test_clean_strategy_clock_lane_does_not_close_proof_during_roi_observation_window():
    runtime_bank = RuntimeRegisterBank()
    runtime_bank.write_radio(
        value={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
        },
        source="test",
        updated_at=100.0,
    )
    publish_test_signal_generation(
        runtime_bank,
        quality={"cq_cell_display": 0.42, "congestion_pct": 74.0},
        updated_at=100.0,
    )
    runtime_bank.write_learning(
        value={"learning_allowed": True}, source="test", updated_at=100.0
    )
    runtime_bank.write_decision(
        value=build_runtime_decision_projection({}),
        source="test",
        updated_at=100.0,
    )
    runtime_bank.write_mutation(
        value={"cooldown_s": 0.0}, source="test", updated_at=100.0
    )
    feed_bank = BrainFeedRegisterBank()
    feed_bank.write_feeder(
        FEED_LEARNING_QUALITY,
        owner="arch4.brain.learning_quality",
        payload={
            "schema": "brain_learning_quality_publication.v1",
            "owner": "arch4.brain.learning_quality",
            "current_hour": 6,
            "learning_quality": {
                "profile_confidence": 0.9,
                "sample_count": 20,
                "band_hour_quality": {"3:6": 0.82, "1:6": 0.38},
                "band_hour_counts": {"3:6": 4, "1:6": 4},
                "control_authority_bands": [1, 3, 20],
            },
        },
        source="test",
        updated_at=100.0,
        stale_after_s=30.0,
    )
    engine = _composed_engine(runtime_bank, feed_bank)

    _publish_smartness(runtime_bank, now=100.0)
    first = run_smart_loop_strategy_clock_lane(engine, now=100.0)
    first_payload = read_smart_loop_strategy_register(engine, now=100.1)

    assert first.ran is True
    assert first_payload["outcome"] == "action_selected_execution_pending"
    first_proof_id = first_payload["selected"]["proof_id"]
    _ack_active_selected_action_command(engine, now=100.2)
    proof_opened = run_smart_loop_strategy_clock_lane(engine, now=100.2)
    assert proof_opened.ran is True

    runtime_bank.write_radio(
        value={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 3,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
        },
        source="test",
        updated_at=101.0,
    )
    publish_test_signal_generation(
        runtime_bank,
        quality={"cq_cell_display": 0.70, "congestion_pct": 55.0},
        updated_at=101.0,
    )
    runtime_bank.write_decision(
        value=build_runtime_decision_projection(
            {
                "brain": {
                    "smart_loop_roi_observation_active": True,
                    "smart_loop_roi_observation_remaining_s": 1799,
                },
            }
        ),
        source="test",
        updated_at=101.0,
    )
    _publish_smartness(runtime_bank, now=101.0)

    during = run_smart_loop_strategy_clock_lane(engine, now=101.0)
    during_payload = read_smart_loop_strategy_register(engine, now=101.1)

    assert during.ran is True
    assert during_payload["outcome"] == "proof_pending"
    assert "command" not in during_payload
    assert during_payload["opened_proof"]["proof_id"] == first_proof_id
    assert during_payload["closed_proof"] is None
    assert engine._smart_loop_state.pending_proof is not None


def test_clean_strategy_clock_lane_closes_pending_proof_from_action_proof_register():
    test_now = 100.0
    test_hour = time.localtime(test_now).tm_hour
    runtime_bank = RuntimeRegisterBank()
    runtime_bank.write_radio(
        value={
            "provider_plmn": "22005",
            "cell_id": 2070,
            "pci": 153,
            "earfcn_dl": 1651,
            "serving_band": 1,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
        },
        source="test",
        updated_at=100.0,
    )
    publish_test_signal_generation(
        runtime_bank,
        quality={"cq_cell_display": 0.70, "congestion_pct": 45.0},
        updated_at=100.0,
    )
    runtime_bank.write_learning(
        value={"learning_allowed": True}, source="test", updated_at=100.0
    )
    runtime_bank.write_mutation(
        value={"cooldown_s": 0.0}, source="test", updated_at=100.0
    )
    feed_bank = BrainFeedRegisterBank()
    feed_bank.write_feeder(
        FEED_LEARNING_QUALITY,
        owner="arch4.brain.learning_quality",
        payload={
            "schema": "brain_learning_quality_publication.v1",
            "owner": "arch4.brain.learning_quality",
            "current_hour": test_hour,
            "learning_quality": {
                "profile_confidence": 0.9,
                "sample_count": 20,
                "band_hour_quality": {
                    f"20:{test_hour}": 0.82,
                    f"1:{test_hour}": 0.38,
                },
                "band_hour_counts": {
                    f"20:{test_hour}": 4,
                    f"1:{test_hour}": 4,
                },
                "control_authority_bands": [1, 3, 20],
            },
        },
        source="test",
        updated_at=100.0,
        stale_after_s=30.0,
    )
    engine = _composed_engine(runtime_bank, feed_bank)
    engine._smart_loop_strategy_feed_port = feed_bank

    first = publish_smart_loop_radio_command_for_test(
        engine,
        target_band=20,
        target_bands=(1, 3, 20),
        lever="PBAND",
        reason_code="proof_closure_fixture",
        now=test_now,
    )
    first_payload = read_smart_loop_strategy_register(engine, now=test_now + 0.1)

    assert first["published"] is True
    assert first_payload["outcome"] == "action_selected_execution_pending"
    assert first_payload["selected"]["target_band"] == 20
    _ack_active_selected_action_command(engine, now=100.2)
    proof_opened = run_smart_loop_strategy_clock_lane(engine, now=100.2)
    proof_opened_payload = read_smart_loop_strategy_register(engine, now=100.3)
    assert proof_opened.ran is True
    pending = proof_opened_payload["opened_proof"]
    assert pending["target_band"] == 20

    proof_row = {
        "schema": "brain_action_proof_closure.v1",
        "owner": OWNER_ACTION_PROOF_REGISTER,
        "roi_horizon_s": 60,
        "label": "neutral",
        "reason": "mixed_or_insufficient_horizon_evidence",
        "referee_code": "staged_roi_inconclusive",
        "referee_level": "neutral",
        "grade_evidence_valid": True,
        "terminal": False,
        "memory_admitted": False,
        "grade_kind": "smart_loop_interim",
        "selected_action_command_id": pending["command_id"],
        "selected_action_proof_id": pending["proof_id"],
        "smart_loop_reason_ref": pending["reason_ref"],
        "smart_loop_context_key": pending["context_key"],
    }
    feed_bank.write_feeder(
        FEED_ACTION_PROOF_CLOSURES,
        owner=OWNER_ACTION_PROOF_REGISTER,
        payload={
            "schema": "brain_action_proof_register.v1",
            "owner": OWNER_ACTION_PROOF_REGISTER,
            "recent_count": 1,
            "recent": [proof_row],
            "last": proof_row,
            "good_count": 0,
            "bad_count": 0,
            "neutral_count": 1,
        },
        source="test",
        updated_at=105.0,
        stale_after_s=0.0,
    )
    _publish_smartness(runtime_bank, now=105.0)

    second = run_smart_loop_strategy_clock_lane(engine, now=105.0)
    second_payload = read_smart_loop_strategy_register(engine, now=105.1)

    assert second.ran is True
    assert second_payload["outcome"] == "proof_feedback_recorded"
    assert second_payload["reason_code"] == "proof_feedback_60s"
    assert second_payload["opened_proof"]["proof_id"] == pending["proof_id"]
    assert second_payload["proof_feedback"]["horizon_s"] == 60
    assert second_payload["proof_feedback"]["terminal"] is False
    assert second_payload["proof_feedback"]["memory_admitted"] is False
    assert second_payload["state"]["pending_proof"]["feedback_horizons_done"] == [60]
    assert engine._smart_loop_state.pending_proof is not None
    assert second_payload["closure_audit"]["status"] == "proof_pending"
    assert second_payload["closure_audit"]["links"]["proof_feedback_received"] is True
    assert second_payload["closure_audit"]["links"]["proof_closed"] is False
    assert (
        second_payload["bandit_reasoning_flow"]["safety_shield"]["proof_pending"]
        is True
    )
    assert (
        second_payload["bandit_reasoning_flow"]["reward_path"]["feedback_horizon_s"]
        == 60
    )

    final_row = {
        **proof_row,
        "roi_horizon_s": 600,
        "terminal": True,
        "memory_admitted": True,
        "prediction_error_recorded": True,
        "policy_update_applied": True,
        "future_policy_effect_status": "pending",
        "grade_kind": "terminal_layered_reward",
        "layered_feedback": build_layered_action_feedback(
            label="neutral",
            reason="mixed_or_insufficient_horizon_evidence",
            evidence={"grade_evidence_valid": True, "grade_context_status": "same"},
            horizon_s=600,
        ),
    }
    feed_bank.write_feeder(
        FEED_ACTION_PROOF_CLOSURES,
        owner=OWNER_ACTION_PROOF_REGISTER,
        payload={
            "schema": "brain_action_proof_register.v1",
            "owner": OWNER_ACTION_PROOF_REGISTER,
            "recent_count": 2,
            "recent": [proof_row, final_row],
            "last": final_row,
            "good_count": 0,
            "bad_count": 0,
            "neutral_count": 1,
            "feedback_count": 1,
        },
        source="test",
        updated_at=600.0,
        stale_after_s=0.0,
    )
    _publish_smartness(runtime_bank, now=600.0)

    final = run_smart_loop_strategy_clock_lane(engine, now=600.0)
    final_payload = read_smart_loop_strategy_register(engine, now=600.1)

    assert final.ran is True
    assert final_payload["outcome"] == "proof_closed_neutral"
    assert final_payload["reason_code"] == "staged_roi_inconclusive"
    assert final_payload["opened_proof"] is None
    assert final_payload["closed_proof"]["proof_id"] == pending["proof_id"]
    assert final_payload["closed_proof"]["horizon_s"] == 600
    assert final_payload["closed_proof"]["memory_admitted"] is True
    assert final_payload["state"]["pending_proof"] is None
    assert engine._smart_loop_state.pending_proof is None
    assert final_payload["closure_audit"]["links"]["proof_closed"] is True
    assert (
        final_payload["closure_audit"]["status"]
        == "closed_policy_update_applied_effect_pending"
    )


def test_clock_lane_autonomous_embargo_is_consumed_before_selected_action_creation():
    runtime_bank = RuntimeRegisterBank()
    runtime_bank.write_radio(
        value={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 1,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
        },
        source="test",
        updated_at=100.0,
    )
    publish_test_signal_generation(
        runtime_bank,
        quality={"cq_cell_display": 0.42, "congestion_pct": 74.0},
        updated_at=100.0,
    )
    runtime_bank.write_learning(
        value={"learning_allowed": True}, source="test", updated_at=100.0
    )
    runtime_bank.write_decision(
        value=build_runtime_decision_projection(
            {
                "brain": {
                    "autonomous_band_embargo_active": True,
                    "autonomous_band_embargo_remaining_s": 900,
                },
            }
        ),
        source="test",
        updated_at=100.0,
    )
    runtime_bank.write_mutation(
        value={"cooldown_s": 0.0}, source="test", updated_at=100.0
    )
    feed_bank = BrainFeedRegisterBank()
    feed_bank.write_feeder(
        FEED_LEARNING_QUALITY,
        owner="arch4.brain.learning_quality",
        payload={
            "schema": "brain_learning_quality_publication.v1",
            "owner": "arch4.brain.learning_quality",
            "current_hour": 6,
            "learning_quality": {
                "profile_confidence": 0.9,
                "sample_count": 20,
                "band_hour_quality": {"3:6": 0.82},
                "band_hour_counts": {"3:6": 4},
                "control_authority_bands": [1, 3, 20],
            },
        },
        source="test",
        updated_at=100.0,
        stale_after_s=5.0,
    )
    engine = _composed_engine(runtime_bank, feed_bank)

    _publish_smartness(runtime_bank, now=100.0)
    outcome = run_smart_loop_strategy_clock_lane(engine, now=100.0)
    payload = read_smart_loop_strategy_register(engine, now=100.1)

    assert outcome.ran is True
    assert payload["outcome"] == "no_action_justified"
    assert payload["reason_code"] == "brain_selected_do_nothing"
    assert "command" not in payload
    assert payload["candidate_counts"]["actionable"] == 0


def test_clock_lane_uses_calibration_only_to_strengthen_existing_learning_candidate(
    monkeypatch,
):
    # The assertions below are about calibration evidence on an executed
    # candidate, not about which deterministic exploration draw happened to be
    # derived from the surrounding frame.
    monkeypatch.setattr(
        "arch4.contextual_bandit._deterministic_unit_interval",
        lambda _seed: 0.999999,
    )
    runtime_bank = RuntimeRegisterBank()
    runtime_bank.write_radio(
        value={
            "provider_plmn": "22005",
            "cell_id": 2050,
            "pci": 255,
            "earfcn_dl": 1795,
            "serving_band": 3,
            "configured_band_pool": [1, 3, 20],
            "radio_connected": True,
        },
        source="test",
        updated_at=100.0,
    )
    publish_test_signal_generation(
        runtime_bank,
        quality={"cq_cell_display": 0.33, "congestion_pct": 82.0},
        updated_at=100.0,
    )
    runtime_bank.write_learning(
        value={
            "learning_allowed": True,
            "calibration_fact_package": {
                "schema": "calibration_fact_package.v1",
                "owner": "calibration_fact_confidence",
                "confidence": 1.0,
                "clean_for_learning": True,
                "strong_enough_for_preemption_facts": True,
                "self_learning_input_strength": 1.0,
                "evidence_count": 30,
            },
        },
        source="test",
        updated_at=100.0,
    )
    runtime_bank.write_mutation(
        value={"cooldown_s": 0.0}, source="test", updated_at=100.0
    )
    exact_radio = dict(runtime_bank.read(REGISTER_RADIO).value)
    exact_radio.pop("frequency_band_set", None)
    exact_radio["session_scope_key"] = "test-session-scope"
    runtime_bank.write_radio(
        value=exact_radio,
        source="test-exact-session",
        updated_at=100.0,
        force=True,
    )
    feed_bank = BrainFeedRegisterBank()
    feed_bank.write_feeder(
        FEED_LEARNING_QUALITY,
        owner="arch4.brain.learning_quality",
        payload={
            "schema": "brain_learning_quality_publication.v1",
            "owner": "arch4.brain.learning_quality",
            "current_hour": 6,
            "learning_quality": {
                "profile_confidence": 0.3,
                "sample_count": 3,
                "band_hour_quality": {"3:6": 0.3333, "1:6": 0.66},
                "band_hour_counts": {"3:6": 3, "1:6": 2},
                "control_authority_bands": [1, 3, 20],
            },
        },
        source="test",
        updated_at=100.0,
        stale_after_s=5.0,
    )
    engine = _composed_engine(runtime_bank, feed_bank)

    _publish_smartness(runtime_bank, now=100.0)
    outcome = run_smart_loop_strategy_clock_lane(engine, now=100.0)
    payload = read_smart_loop_strategy_register(engine, now=100.1)

    assert outcome.ran is True
    assert payload["outcome"] == "action_selected_execution_pending"
    assert payload["reason_code"] == "brain_selected_optimization_action"
    assert "command" not in payload
    assert payload["selected"]["target_band"] == 1
    assert (
        payload["candidates"][0]["reason_code"] == "learned_anchor_outperforms_current"
    )
    assert payload["candidates"][0]["evidence_count"] == SmartLoopCore.min_evidence
    assert "calibration_evidence_lift" not in payload["candidates"][0]
    assert payload["candidates"][0]["calibration_confidence_lift"] > 0
    assert payload["opened_proof"] is None
    assert (
        engine.action_transaction_authority.selection_status()[
            "active"
        ]
        is not None
    )
    assert payload["brain_decision_receipt"]["do_nothing"] is False


def test_live_smartness_cutover_disallows_old_strategy_runtime_path():
    runtime_clock = (ROOT / "arch4" / "core" / "runtime_clock_dispatcher.py").read_text(
        encoding="utf-8"
    )
    decision_pipeline = (ROOT / "arch4" / "brain" / "decision_pipeline.py").read_text(
        encoding="utf-8"
    )
    feed_registers = (ROOT / "arch4" / "brain" / "feed_registers.py").read_text(
        encoding="utf-8"
    )

    assert "run_smart_loop_strategy_clock_lane" not in runtime_clock
    assert "arch4.smart_loop" not in runtime_clock
    assert "run_smart_loop_strategy_clock_lane" in (
        ROOT / "arch4" / "smart_loop" / "clock_lane.py"
    ).read_text(encoding="utf-8")
    assert "run_self_learning_strategy_clock_lane" not in runtime_clock
    assert (
        'OWNER_SELF_LEARNING_STRATEGY = "arch4.smart_loop.closed_loop"'
        in feed_registers
    )
    assert "self._read_smart_loop_strategy(" in decision_pipeline
    assert "read_smart_loop_strategy_from_bank(feed" in decision_pipeline
    assert "SelectedActionContract.from_mapping" in decision_pipeline
    assert "smart_loop.command_envelope.v1" not in decision_pipeline
    assert "OUTCOME_PROOF_PENDING" not in decision_pipeline
    assert "OUTCOME_PROOF_INVALID" not in decision_pipeline


def test_smart_loop_clock_lane_hard_fails_full_recompute_on_unchanged_inputs():
    lane_src = (ROOT / "arch4" / "smart_loop" / "clock_lane.py").read_text(
        encoding="utf-8"
    )
    cache_check = lane_src.index("cached = _cached_strategy_outcome")
    closed_loop_call = lane_src.index("decision = core.decide")
    signature_body = lane_src[
        lane_src.index("def _strategy_input_signature(") : lane_src.index(
            "def _cached_strategy_outcome("
        )
    ]
    smartness_signature_body = signature_body[: signature_body.index("feed_bank =")]

    assert cache_check < closed_loop_call
    assert "def _strategy_input_signature(" in lane_src
    assert "def _cached_strategy_outcome(" in lane_src
    assert "def _stable_signature(" not in lane_src
    assert "_SIGNATURE_VOLATILE_KEYS" not in lane_src
    assert "read_metadata(REGISTER_SMARTNESS)" in smartness_signature_body
    assert "read(REGISTER_SMARTNESS).as_dict()" not in smartness_signature_body
    assert "smartness_fact_sections(" not in smartness_signature_body
    assert 'row.get("sequence")' not in smartness_signature_body
    assert 'value.get("register_sequence")' not in signature_body
    assert 'section_meta.get("sequence")' not in signature_body
    assert "smart_loop_inputs_unchanged" in lane_src
    assert "touch_smart_loop_strategy_register(" in lane_src


def test_smart_loop_signature_uses_smartness_metadata_not_full_payload_copy():
    bank = RuntimeRegisterBank()
    install_test_identity_register(
        bank,
        value={
            "identity_code": "0x0000000000000101",
            "identity_status": "exact",
            "identity_catalog_version": "test-identity-catalog-v1",
            "identity_catalog_checksum": "a" * 64,
            "session_scope_key": "test-signal-session",
            "source_epoch": "test-signal-epoch",
            "source_sequence": 1,
            "profile_id": "huawei_b818_263_h220sp1c11",
            "model": "B818-263",
            "firmware": "11.0.1.1(H220SP1C11)",
        },
        source="test",
        updated_at=100.0,
        valid=True,
        stale=False,
        dirty=False,
        trust="derived",
        source_kind="derived",
        validity="valid",
    )
    bank.write_radio(
        value={
            "provider_plmn": "22005",
            "provider_name": "A1",
            "serving_band": 3,
            "configured_band_pool": [1, 3, 20],
        },
        source="test",
        updated_at=100.0,
        valid=True,
        stale=False,
        dirty=False,
        trust="live",
        source_kind="live",
        validity="valid",
    )
    publish_test_signal_generation(bank, updated_at=100.0)
    _publish_smartness(bank, now=100.0)

    metadata_reader = bank.read_metadata

    def forbidden_full_read(
        name: str,
    ):  # pragma: no cover - failure path proves the contract.
        raise AssertionError(f"full register read is illegal in signature path: {name}")

    bank.read = forbidden_full_read  # type: ignore[method-assign]
    bank.read_metadata = metadata_reader  # type: ignore[method-assign]
    engine = _composed_engine(bank)

    signature = _strategy_input_signature(engine, now=101.0)

    assert any(part[0] == "smartness" for part in signature)
    assert "missing_smartness_semantic_signature" not in repr(signature)
