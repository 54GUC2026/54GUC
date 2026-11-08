from __future__ import annotations

import pytest

from arch4.brain.learning_effect import (
    OWNER,
    BrainLearningEffectContractError,
    build_brain_learning_effect,
)
from arch4.brain.decision_quality_owner import build_decision_quality_summary


def test_learning_effect_summarizes_tsar_roi_and_referee_feedback():
    effect = build_brain_learning_effect(
        tsar_plan={
            "schema": "brain_tsar_candidate_plan.v1",
            "owner": "arch4.smart_loop.closed_loop",
            "arbitration_outcome": {
                "schema": "brain_tsar_arbitration_outcome.v1",
                "owner": "arch4.smart_loop.closed_loop",
                "verdict": "act",
                "reason": "eligible_candidate_selected",
                "reason_code": "eligible_candidate_selected",
                "context_ref": "0xctx123",
                "required_next_step": "command_authority",
            },
            "selected": {
                "action": "prefer_anchor_band",
                "band": 3,
                "reason_ref": "0xabc12345",
            },
            "considered": [
                {
                    "action": "prefer_band",
                    "band": 20,
                    "action_shape_roi_penalty": 0.35,
                },
                {
                    "action": "prefer_band",
                    "band": 3,
                    "action_shape_roi_benefit": 0.15,
                }
            ],
        },
        referee_feedback_quality={
            "quality": "outcome_signal",
            "learning_signal": True,
            "diagnostic_signal": False,
            "reason": "accepted_grade",
        },
    )

    assert effect["schema"] == "brain_learning_effect.v1"
    assert effect["owner"] == OWNER
    assert effect["roi_memory_applied"] is True
    assert effect["penalized_candidate_count"] == 1
    assert effect["boosted_candidate_count"] == 1
    assert effect["selected_avoided_penalized_candidate"] is True
    assert effect["selected_action"] == "prefer_anchor_band"
    assert effect["selected_band"] == 3
    assert effect["selected_reason_ref"] == "0xabc12345"
    assert effect["tsar_arbitration_verdict"] == "act"
    assert effect["tsar_arbitration_reason"] == "eligible_candidate_selected"
    assert effect["tsar_context_ref"] == "0xctx123"
    assert effect["tsar_arbitration_required_next_step"] == "command_authority"
    assert effect["tsar_no_action_reason"] == ""
    assert effect["referee_feedback_quality"] == "outcome_signal"
    assert effect["referee_feedback_outcome_signal_count"] == 1
    assert effect["learning_signal"] is True
    assert effect["diagnostic_signal"] is False


def test_learning_effect_rejects_old_unstamped_tsar_plan():
    with pytest.raises(BrainLearningEffectContractError, match="tsar_plan_owner_contract_missing"):
        build_brain_learning_effect(
            tsar_plan={
                "status": "idle",
                "reason": "no_eligible_candidate",
                "selected": {},
                "considered": [],
                "deferred": [],
            }
        )


def test_learning_effect_summarizes_decision_quality_gaps():
    effect = build_brain_learning_effect(
        referee_feedback_qualities=[
            {"quality": "low_information", "learning_signal": False, "diagnostic_signal": False},
            {
                "quality": "counterfactual_diagnostic",
                "learning_signal": True,
                "diagnostic_signal": True,
                "reason": "referee_no_action_candidate_generation_gap",
            },
        ],
        decision_quality={
            "bucket": "brain_did_not_act_should_have",
            "action_count": 0,
            "should_have_acted": True,
            "should_have_acted_reason": "candidate_generation_failed",
            "decision_quality_reason": "rf_congestion_ca_state_needed_candidate_but_inventory_empty",
            "missed_candidate_audit_count": 1,
            "missed_candidate_reasons": [
                "rf_congestion_ca_state_needed_candidate_but_inventory_empty"
            ],
            "counterfactual_problem_reasons": ["congestion_pct_high"],
            "counterfactual_missing_evidence": [],
            "counterfactual_governor_blockers": [],
        },
        self_learning_candidate_surfaces=[
            {
                "roi_surface": {
                    "action_proof_recent_count": 2,
                    "action_proof_good_count": 1,
                    "action_proof_bad_count": 1,
                    "action_proof_memory_commit_count": 1,
                    "action_proof_historical_commit_count": 1,
                    "action_proof_crystal_ball_update_count": 1,
                    "action_proof_crystal_ball_regrade_count": 1,
                    "last_action_proof_label": "bad",
                    "last_action_proof_referee_code": "staged_roi_negative",
                    "last_action_proof_reason": "cast_ld_low",
                    "last_action_proof_secondary_learning_status": "committed",
                }
            }
        ],
    )

    assert effect["decision_quality_bucket"] == "brain_did_not_act_should_have"
    assert effect["brain_acted"] is False
    assert effect["brain_should_have_acted"] is True
    assert effect["candidate_generation_gap_detected"] is True
    assert effect["candidate_operationalization_gap_detected"] is False
    assert effect["decision_quality_reason"] == "rf_congestion_ca_state_needed_candidate_but_inventory_empty"
    assert effect["candidate_generation_gap_reasons"] == [
        "rf_congestion_ca_state_needed_candidate_but_inventory_empty"
    ]
    assert effect["counterfactual_problem_reasons"] == ["congestion_pct_high"]
    assert effect["counterfactual_missing_evidence"] == []
    assert effect["referee_feedback_quality"] == "counterfactual_diagnostic"
    assert effect["referee_feedback_quality_count"] == 2
    assert effect["referee_feedback_low_information_count"] == 1
    assert effect["referee_feedback_counterfactual_diagnostic_count"] == 1
    assert effect["referee_feedback_reason"] == "referee_no_action_candidate_generation_gap"
    assert effect["action_proof_signal"] is True
    assert effect["action_proof_recent_count"] == 2
    assert effect["action_proof_good_count"] == 1
    assert effect["action_proof_bad_count"] == 1
    assert effect["action_proof_memory_commit_count"] == 1
    assert effect["action_proof_historical_commit_count"] == 1
    assert effect["action_proof_crystal_ball_update_count"] == 1
    assert effect["action_proof_crystal_ball_regrade_count"] == 1
    assert effect["action_proof_secondary_learning_commit_count"] == 3
    assert effect["last_action_proof_label"] == "bad"
    assert effect["last_action_proof_referee_code"] == "staged_roi_negative"
    assert effect["last_action_proof_reason"] == "cast_ld_low"
    assert effect["last_action_proof_secondary_learning_status"] == "committed"
    assert effect["learning_signal"] is True
    assert effect["diagnostic_signal"] is True


def test_learning_effect_consumes_canonical_action_proof_surface_directly():
    effect = build_brain_learning_effect(
        action_proof_surface={
            "schema": "brain_action_proof_register.v1",
            "owner": "arch4.brain.action_proof_register",
            "recent_count": 1,
            "recent": [
                {
                    "schema": "brain_action_proof_closure.v1",
                    "owner": "arch4.brain.action_proof_register",
                    "label": "neutral",
                    "reason": "mixed_or_insufficient_horizon_evidence",
                    "referee_code": "staged_roi_inconclusive",
                    "referee_level": "neutral",
                    "memory_committed": False,
                    "historical_committed": True,
                    "crystal_ball_updated": True,
                    "crystal_ball_regraded": False,
                    "secondary_learning_status": "partial",
                    "selected_action_command_id": "command_test_123",
                    "selected_action_proof_id": "proof_test_456",
                    "smart_loop_reason_ref": "prefer_band_b3_high_roi",
                    "smart_loop_context_key": "ctx:22005:3:2050:1795",
                }
            ],
            "last": {
                "label": "neutral",
                "reason": "mixed_or_insufficient_horizon_evidence",
                "referee_code": "staged_roi_inconclusive",
                "secondary_learning_status": "partial",
                "selected_action_command_id": "command_test_123",
                "selected_action_proof_id": "proof_test_456",
                "smart_loop_reason_ref": "prefer_band_b3_high_roi",
                "smart_loop_context_key": "ctx:22005:3:2050:1795",
            },
            "good_count": 0,
            "bad_count": 0,
            "neutral_count": 1,
        }
    )

    assert effect["action_proof_signal"] is True
    assert effect["action_proof_recent_count"] == 1
    assert effect["last_action_proof_label"] == "neutral"
    assert effect["last_action_proof_referee_code"] == "staged_roi_inconclusive"
    assert effect["last_action_proof_reason"] == "mixed_or_insufficient_horizon_evidence"
    assert effect["action_proof_historical_commit_count"] == 1
    assert effect["action_proof_crystal_ball_update_count"] == 1
    assert effect["last_action_proof_secondary_learning_status"] == "partial"
    assert effect["last_action_proof_selected_action_command_id"] == "command_test_123"
    assert effect["last_action_proof_selected_action_proof_id"] == "proof_test_456"
    assert effect["last_action_proof_smart_loop_reason_ref"] == "prefer_band_b3_high_roi"
    assert effect["last_action_proof_smart_loop_context_key"] == "ctx:22005:3:2050:1795"
    assert effect["last_action_proof_smart_loop_identity_source"] == "action_proof_register"


def test_learning_effect_reports_matched_selected_selected_action_proof_identity():
    effect = build_brain_learning_effect(
        tsar_plan={
            "schema": "brain_tsar_candidate_plan.v1",
            "owner": "arch4.smart_loop.closed_loop",
            "arbitration_outcome": {
                "schema": "brain_tsar_arbitration_outcome.v1",
                "owner": "arch4.smart_loop.closed_loop",
                "verdict": "act",
                "reason": "eligible_candidate_selected",
                "reason_code": "eligible_candidate_selected",
                "context_ref": "ctx:22005:3:2050:1795",
                "required_next_step": "command_authority",
            },
            "selected": {
                "action": "prefer_anchor_band",
                "band": 3,
                "reason_ref": "prefer_band_b3_high_roi",
                "context_key": "ctx:22005:3:2050:1795",
                "command_id": "command_test_123",
                "proof_id": "proof_test_456",
            },
        },
        action_proof_surface={
            "schema": "brain_action_proof_register.v1",
            "owner": "arch4.brain.action_proof_register",
            "recent_count": 1,
            "recent": [
                {
                    "schema": "brain_action_proof_closure.v1",
                    "owner": "arch4.brain.action_proof_register",
                    "label": "good",
                    "reason": "post_action_improved",
                    "referee_code": "staged_roi_positive",
                    "memory_committed": True,
                    "selected_action_command_id": "command_test_123",
                    "selected_action_proof_id": "proof_test_456",
                    "smart_loop_reason_ref": "prefer_band_b3_high_roi",
                    "smart_loop_context_key": "ctx:22005:3:2050:1795",
                }
            ],
            "last": {
                "label": "good",
                "reason": "post_action_improved",
                "referee_code": "staged_roi_positive",
                "selected_action_command_id": "command_test_123",
                "selected_action_proof_id": "proof_test_456",
                "smart_loop_reason_ref": "prefer_band_b3_high_roi",
                "smart_loop_context_key": "ctx:22005:3:2050:1795",
            },
            "good_count": 1,
            "bad_count": 0,
            "neutral_count": 0,
        },
    )

    assert effect["expected_smart_loop_proof_source"] == "selected_tsar_plan"
    assert effect["expected_selected_action_command_id"] == "command_test_123"
    assert effect["expected_selected_action_proof_id"] == "proof_test_456"
    assert effect["selected_action_proof_identity_status"] == "matched"
    assert effect["selected_action_proof_identity_matched"] is True


def test_learning_effect_reports_closed_smart_loop_proof_without_public_action_proof():
    effect = build_brain_learning_effect(
        tsar_plan={
            "schema": "brain_tsar_candidate_plan.v1",
            "owner": "arch4.smart_loop.closed_loop",
            "arbitration_outcome": {
                "schema": "brain_tsar_arbitration_outcome.v1",
                "owner": "arch4.smart_loop.closed_loop",
                "verdict": "no_action",
                "reason": "post_action_improved",
                "reason_code": "post_action_improved",
                "context_ref": "ctx:22005:3:2050:1795",
                "required_next_step": "commit_learning_memory",
            },
            "selected": {},
        },
        self_learning_candidate_surfaces=[
            {
                "schema": "smart_loop.closed_loop.v1",
                "owner": "arch4.smart_loop.closed_loop",
                "outcome": "proof_closed_good",
                "closed_proof": {
                    "label": "good",
                    "reason_code": "post_action_improved",
                    "memory_admitted": True,
                    "command_id": "command_test_123",
                    "proof_id": "proof_test_456",
                    "reason_ref": "prefer_band_b3_high_roi",
                    "context_key": "ctx:22005:3:2050:1795",
                },
            }
        ],
    )

    assert effect["expected_smart_loop_proof_source"] == "closed_proof"
    assert effect["expected_selected_action_proof_id"] == "proof_test_456"
    assert effect["selected_action_proof_identity_status"] == "closed_proof_missing_action_proof"
    assert effect["selected_action_proof_identity_matched"] is False


def test_learning_effect_projects_owner_stamped_smart_loop_closure_audit():
    effect = build_brain_learning_effect(
        self_learning_candidate_surfaces=[
            {
                "schema": "smart_loop.closed_loop.v1",
                "owner": "arch4.smart_loop.closed_loop",
                "outcome": "proof_closed_good",
                "closed_proof": {
                    "label": "good",
                    "reason_code": "post_action_improved",
                    "memory_admitted": True,
                    "command_id": "command_test_123",
                    "proof_id": "proof_test_456",
                    "reason_ref": "prefer_band_b3_high_roi",
                    "context_key": "ctx:22005:3:2050:1795",
                },
                "closure_audit": {
                    "schema": "smart_loop.closure_audit.v1",
                    "owner": "arch4.smart_loop.closed_loop",
                    "status": "closed_policy_update_applied_effect_pending",
                    "reason_code": "post_action_improved",
                    "required_next_step": "commit_learning_memory",
                    "links": {
                        "candidate_generated": True,
                        "tsar_arbitrated": True,
                        "action_selected": False,
                        "selected_action_materialized": False,
                        "proof_opened": False,
                        "proof_closed": True,
                        "memory_admitted": True,
                        "prediction_error_recorded": True,
                        "policy_update_applied": True,
                    },
                    "missing_links": [],
                    "memory_admitted": True,
                    "prediction_error_recorded": True,
                    "policy_update_applied": True,
                    "future_policy_effect_status": "pending",
                },
            }
        ],
    )

    assert effect["smart_loop_closure_status"] == (
        "closed_policy_update_applied_effect_pending"
    )
    assert effect["smart_loop_closure_reason_code"] == "post_action_improved"
    assert effect["smart_loop_closure_required_next_step"] == "commit_learning_memory"
    assert effect["smart_loop_closure_missing_links"] == []
    assert effect["smart_loop_closure_candidate_generated"] is True
    assert effect["smart_loop_closure_tsar_arbitrated"] is True
    assert effect["smart_loop_closure_proof_closed"] is True
    assert effect["smart_loop_closure_memory_admitted"] is True
    assert effect["smart_loop_closure_prediction_error_recorded"] is True
    assert effect["smart_loop_closure_policy_update_applied"] is True
    assert effect["smart_loop_closure_future_policy_effect_status"] == "pending"
    assert effect["learning_signal"] is True
    assert effect["diagnostic_signal"] is False


def test_learning_effect_marks_owner_stamped_no_action_decision_candidate_penalty():
    effect = build_brain_learning_effect(
        self_learning_candidate_surfaces=[
            {
                "schema": "smart_loop.closed_loop.v1",
                "owner": "arch4.smart_loop.closed_loop",
                "outcome": "no_action_justified",
                "reason": "current_anchor_penalized_no_better_candidate",
                "candidates": [
                    {
                        "schema": "smart_loop.candidate.v1",
                        "owner": "arch4.smart_loop.closed_loop",
                        "action": "stay_current_anchor",
                        "band": 3,
                        "target_band": 3,
                        "candidate_class": "operational",
                        "reason_ref": "0xpenalty",
                        "reason_code": "current_anchor_penalized_by_roi",
                        "action_shape_roi_penalty": 0.55,
                    },
                    {
                        "schema": "smart_loop.candidate.v1",
                        "owner": "arch4.smart_loop.closed_loop",
                        "action": "avoid_band",
                        "band": 1,
                        "target_band": 1,
                        "candidate_class": "advisory",
                        "reason_ref": "0xavoid",
                        "reason_code": "learned_anchor_underperforms",
                        "action_shape_roi_penalty": 0.25,
                    },
                ],
                "bandit_decision": {
                    "schema": "smart_loop.bandit_decision.v1",
                    "owner": "arch4.smart_loop.closed_loop",
                    "selected_action": "stay_current_anchor",
                    "target_band": 3,
                    "reason_ref": "0xpenalty",
                    "reason_code": "current_anchor_penalized_no_better_candidate",
                },
                "closure_audit": {
                    "schema": "smart_loop.closure_audit.v1",
                    "owner": "arch4.smart_loop.closed_loop",
                    "status": "no_action_justified",
                    "reason_code": "current_anchor_penalized_no_better_candidate",
                    "required_next_step": "generate_better_executable_candidate",
                    "links": {
                        "candidate_generated": True,
                        "tsar_arbitrated": True,
                        "action_selected": False,
                        "selected_action_materialized": False,
                        "proof_opened": False,
                        "proof_closed": False,
                        "memory_admitted": False,
                        "prediction_error_recorded": False,
                        "policy_update_applied": False,
                    },
                    "missing_links": [],
                    "memory_admitted": False,
                    "prediction_error_recorded": False,
                    "policy_update_applied": False,
                    "future_policy_effect_status": "",
                },
            }
        ],
    )

    assert effect["penalized_candidate_count"] == 2
    assert effect["selected_candidate_penalized"] is True
    assert effect["selected_action"] == "stay_current_anchor"
    assert effect["selected_band"] == 3
    assert effect["selected_reason_ref"] == "0xpenalty"
    assert effect["smart_loop_closure_reason_code"] == "current_anchor_penalized_no_better_candidate"


def test_learning_effect_rejects_unowned_smart_loop_closure_audit():
    with pytest.raises(BrainLearningEffectContractError, match="smart_loop_closure_audit_owner_mismatch"):
        build_brain_learning_effect(
            self_learning_candidate_surfaces=[
                {
                    "schema": "smart_loop.closed_loop.v1",
                    "owner": "arch4.smart_loop.closed_loop",
                    "closure_audit": {
                        "schema": "smart_loop.closure_audit.v1",
                        "owner": "wrong.owner",
                    },
                }
            ],
        )


def test_decision_quality_summary_owns_brain_action_buckets():
    summary = build_decision_quality_summary(
        action_count=0,
        counterfactual_audits=[
            {
                "status": "needs_action_candidate",
                "should_have_candidate": True,
                "candidate_generation_failed": True,
                "reason": "rf_congestion_ca_state_needed_candidate_but_inventory_empty",
                "problem_reasons": ["congestion_pct_high", "cq_low"],
                "missing_evidence": [],
            }
        ],
        self_learning_candidate_surfaces=[],
    )

    assert summary["bucket"] == "brain_did_not_act_should_have"
    assert summary["missed_candidate_audit_count"] == 1
    assert summary["should_have_acted_reason"] == "candidate_generation_failed"
    assert summary["decision_quality_reason"] == "rf_congestion_ca_state_needed_candidate_but_inventory_empty"
    assert summary["missed_candidate_reasons"] == [
        "rf_congestion_ca_state_needed_candidate_but_inventory_empty"
    ]
    assert summary["counterfactual_problem_reasons"] == ["congestion_pct_high", "cq_low"]
    assert summary["counterfactual_missing_evidence"] == []
    assert summary["should_have_acted"] is True

    pending = build_decision_quality_summary(action_count=1)
    assert pending["bucket"] == "brain_acted_outcome_pending"


def test_decision_quality_treats_safety_gated_advisory_as_justified_no_action():
    summary = build_decision_quality_summary(
        action_count=0,
        counterfactual_audits=[],
        self_learning_candidate_surfaces=[
            {
                "schema": "smart_loop_authority.v1",
                "owner": "arch4.brain.smart_loop_authority",
                "status": "advisory_only",
                "reason": "learning_candidate_advisory_waiting_for_governor:connect_settle",
                "required_next_step": "wait_for_safety_or_capability_release",
                "candidate_operationalization_gap": False,
                "candidate_generation_gap": False,
            }
        ],
    )

    assert summary["bucket"] == "brain_did_not_act_justified"
    assert summary["should_have_acted"] is False
    assert summary["candidate_operationalization_gap_count"] == 0
    assert summary["decision_quality_reason"] == (
        "learning_candidate_advisory_waiting_for_governor:connect_settle"
    )
    assert summary["counterfactual_governor_blockers"] == ["connect_settle"]


def test_decision_quality_treats_provider_time_held_advisory_as_justified_no_action():
    summary = build_decision_quality_summary(
        action_count=0,
        counterfactual_audits=[],
        self_learning_candidate_surfaces=[
            {
                "schema": "smart_loop_authority.v1",
                "owner": "arch4.brain.smart_loop_authority",
                "status": "advisory_only",
                "reason": "learning_candidate_advisory_recorded",
                "required_next_step": "continue_learning_observation",
                "candidate_operationalization_gap": False,
                "candidate_generation_gap": False,
            }
        ],
    )

    assert summary["bucket"] == "brain_did_not_act_justified"
    assert summary["should_have_acted"] is False
    assert summary["candidate_operationalization_gap_count"] == 0
    assert summary["decision_quality_reason"] == "learning_candidate_advisory_recorded"
