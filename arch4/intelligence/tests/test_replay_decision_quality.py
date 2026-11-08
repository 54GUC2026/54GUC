from __future__ import annotations

from arch4.replay_event_adapter import (
    OWNER as REPLAY_ADAPTER_OWNER,
    counterfactual_audit_from_replay_event,
    referee_outcome_from_replay_event,
    smart_loop_candidate_projection_from_replay_event,
)
from arch4.replay_decision_quality import classify_decision_quality_window


def test_replay_legacy_flat_rows_are_explicit_boundary_adapters():
    audit = counterfactual_audit_from_replay_event(
        {
            "counterfactual_status": "needs_action_candidate",
            "counterfactual_should_have_candidate": True,
            "counterfactual_candidate_generation_failed": True,
        }
    )
    authority = smart_loop_candidate_projection_from_replay_event(
        {
            "smart_loop_candidate_projection": {
                "schema": "smart_loop.candidate_projection.v1",
                "owner": "arch4.smart_loop.closed_loop",
                "status": "action_rejected",
                "authority": {"candidate_operationalization_gap": True},
            },
        }
    )
    referee = referee_outcome_from_replay_event({"grade": "bad"})

    assert audit["owner"] == REPLAY_ADAPTER_OWNER
    assert audit["boundary"] == "historical_replay_import"
    assert audit["candidate_generation_failed"] is True
    assert authority["owner"] == "arch4.smart_loop.closed_loop"
    assert authority["authority"]["candidate_operationalization_gap"] is True
    assert referee["owner"] == REPLAY_ADAPTER_OWNER
    assert referee["boundary"] == "historical_replay_import"
    assert referee["level"] == "bad"


def test_replay_classifies_brain_acted_justified():
    result = classify_decision_quality_window(
        telemetry_rows=[{"ca_signature": "B3+CA"}, {"ca_signature": "B3+CA"}],
        event_rows=[
            {"event": "action", "lever": "PBAND"},
            {"event": "action_grade", "grade": "good"},
        ],
    )

    assert result["bucket"] == "brain_acted_justified"
    assert result["action_count"] == 1


def test_replay_classifies_brain_acted_unjustified():
    result = classify_decision_quality_window(
        telemetry_rows=[{"ca_signature": "B3+CA"}, {"ca_signature": "B3+CA"}],
        event_rows=[
            {"event": "action", "lever": "BAND_SELECTION"},
            {"event": "action_grade", "grade": "bad"},
        ],
    )

    assert result["bucket"] == "brain_acted_unjustified"
    assert result["bad_outcome_count"] == 1


def test_replay_uses_action_outcome_contract_for_referee_outcome_rows():
    result = classify_decision_quality_window(
        telemetry_rows=[],
        event_rows=[
            {"event": "action", "lever": "PBAND"},
            {
                "event": "action_learning_outcome",
                "referee_outcome": {
                    "level": "bad",
                    "code": "band_action_no_effect",
                    "message": "band action changed nothing",
                },
            },
        ],
    )

    assert result["bucket"] == "brain_acted_unjustified"
    assert result["bad_outcome_count"] == 1
    assert result["learning_effect"]["decision_quality_bucket"] == "brain_acted_unjustified"


def test_replay_requires_counterfactual_audit_for_should_have_acted():
    telemetry = [
        {"ca_signature": "B3+CA"},
        {"ca_signature": "B3"},
        {"ca_signature": "B3+CA"},
        {"ca_signature": "B3"},
        {"ca_signature": "B3+CA"},
        {"ca_signature": "B3"},
        {"ca_signature": "B3+CA"},
    ]

    result = classify_decision_quality_window(
        telemetry_rows=telemetry,
        event_rows=[],
        ca_flip_threshold=6,
    )

    assert result["bucket"] == "counterfactual_audit_missing"
    assert result["ca_signature_flips"] == 6
    assert result["should_have_acted"] is False


def test_replay_classifies_brain_did_not_act_from_counterfactual_audit():
    result = classify_decision_quality_window(
        telemetry_rows=[{"ca_signature": "B3+CA"}, {"ca_signature": "B3+CA"}],
        event_rows=[
            {
                "event": "decision_trace",
                "counterfactual_opportunity_audit": {
                    "status": "needs_action_candidate",
                    "should_have_candidate": True,
                    "candidate_generation_failed": True,
                    "reason": "rf_congestion_ca_state_needed_candidate_but_inventory_empty",
                },
            }
        ],
    )

    assert result["bucket"] == "brain_did_not_act_should_have"
    assert result["counterfactual_audit_count"] == 1
    assert result["missed_candidate_audit_count"] == 1
    assert result["should_have_acted_reason"] == "candidate_generation_failed"
    assert result["should_have_acted"] is True
    assert result["learning_effect"]["owner"] == "arch4.brain.learning_effect"
    assert result["learning_effect"]["decision_quality_bucket"] == "brain_did_not_act_should_have"
    assert result["learning_effect"]["candidate_generation_gap_detected"] is True


def test_replay_classifies_brain_did_not_act_justified_from_counterfactual_audit():
    result = classify_decision_quality_window(
        telemetry_rows=[{"ca_signature": "B3+CA"}, {"ca_signature": "B3+CA"}],
        event_rows=[
            {
                "event": "decision_trace",
                "counterfactual_opportunity_audit": {
                    "status": "ok",
                    "should_have_candidate": False,
                    "candidate_generation_failed": False,
                    "reason": "no_counterfactual_action_need_detected",
                },
            }
        ],
    )

    assert result["bucket"] == "brain_did_not_act_justified"
    assert result["counterfactual_audit_count"] == 1
    assert result["justified_no_action_audit_count"] == 1
    assert result["should_have_acted"] is False
    assert result["learning_effect"]["decision_quality_bucket"] == "brain_did_not_act_justified"


def test_replay_classifies_brain_did_not_act_from_self_learning_operationalization_gap():
    result = classify_decision_quality_window(
        telemetry_rows=[{"ca_signature": "B1+CA"}, {"ca_signature": "B3+CA"}],
        event_rows=[
            {
                "event": "decision_trace",
                "counterfactual_opportunity_audit": {
                    "status": "candidate_present",
                    "should_have_candidate": True,
                    "candidate_generation_failed": False,
                },
                "smart_loop_authority": {
                    "status": "advisory_only",
                    "reason": "learning_candidate_advisory_waiting_for_tsar",
                    "required_next_step": "brain_tsar_arbitration",
                    "candidate_operationalization_gap": True,
                },
            }
        ],
    )

    assert result["bucket"] == "brain_did_not_act_should_have"
    assert result["missed_candidate_audit_count"] == 0
    assert result["self_learning_candidate_surface_count"] == 1
    assert result["candidate_operationalization_gap_count"] == 1
    assert result["should_have_acted_reason"] == "candidate_not_operationalized"
    assert result["should_have_acted"] is True


def test_replay_counts_referee_feedback_quality_from_authority_surface():
    result = classify_decision_quality_window(
        telemetry_rows=[],
        event_rows=[
            {
                "event": "decision_trace",
                "smart_loop_authority": {
                    "status": "idle",
                    "referee_feedback_quality": {
                        "quality": "low_information",
                        "learning_signal": False,
                    },
                },
            },
            {
                "event": "decision_trace",
                "smart_loop_authority": {
                    "status": "idle",
                    "referee_feedback_quality": {
                        "quality": "outcome_signal",
                        "learning_signal": True,
                    },
                },
            },
        ],
    )

    assert result["referee_feedback_quality_count"] == 2
    assert result["referee_feedback_low_information_count"] == 1
    assert result["referee_feedback_outcome_signal_count"] == 1


def test_replay_promotes_action_proof_closed_rows_into_learning_effect():
    result = classify_decision_quality_window(
        telemetry_rows=[],
        event_rows=[
            {
                "event": "action_proof_closed",
                "event_id": "proof-1",
                "parent_trace_id": "parent-1",
                "child_trace_id": "child-1",
                "roi_horizon_s": 300,
                "label": "bad",
                "reason": "cast_ld_low",
                "referee_code": "staged_roi_negative",
                "learning_commit_status": "committed",
                "secondary_learning": {
                    "memory_committed": True,
                    "historical_committed": True,
                    "crystal_ball_updated": True,
                },
            }
        ],
    )

    effect = result["learning_effect"]
    assert effect["action_proof_signal"] is True
    assert effect["action_proof_recent_count"] == 1
    assert effect["action_proof_bad_count"] == 1
    assert effect["action_proof_memory_commit_count"] == 1
    assert effect["action_proof_historical_commit_count"] == 1
    assert effect["action_proof_crystal_ball_update_count"] == 1
    assert effect["action_proof_secondary_learning_commit_count"] == 3
    assert effect["last_action_proof_label"] == "bad"
    assert effect["last_action_proof_referee_code"] == "staged_roi_negative"
    assert effect["last_action_proof_reason"] == "cast_ld_low"
    assert effect["diagnostic_signal"] is True


def test_replay_counts_referee_feedback_quality_from_public_referee_rows():
    result = classify_decision_quality_window(
        telemetry_rows=[],
        event_rows=[
            {
                "event": "brain_referee",
                "brain_referee": {
                    "state": "idle",
                    "grade": "none",
                },
            },
            {
                "event": "brain_referee",
                "brain_referee": {
                    "state": "executed",
                    "grade": "rejected",
                },
            },
        ],
    )

    assert result["referee_feedback_quality_count"] == 2
    assert result["referee_feedback_low_information_count"] == 1
    assert result["referee_feedback_outcome_signal_count"] == 1


def test_replay_classifies_no_action_feedback_with_counterfactual_audit():
    result = classify_decision_quality_window(
        telemetry_rows=[],
        event_rows=[
            {
                "event": "decision_trace",
                "brain_referee": {"grade": "none"},
                "counterfactual_opportunity_audit": {
                    "status": "ok",
                    "reason": "no_counterfactual_action_need_detected",
                    "should_have_candidate": False,
                    "candidate_generation_failed": False,
                },
            },
            {
                "event": "decision_trace",
                "brain_referee": {"grade": "none"},
                "counterfactual_opportunity_audit": {
                    "status": "needs_action_candidate",
                    "reason": "rf_congestion_ca_state_needed_candidate_but_inventory_empty",
                    "should_have_candidate": True,
                    "candidate_generation_failed": True,
                },
            },
        ],
    )

    assert result["referee_feedback_quality_count"] == 2
    assert result["referee_feedback_valid_no_action_count"] == 1
    assert result["referee_feedback_counterfactual_diagnostic_count"] == 1
    assert result["referee_feedback_low_information_count"] == 0
