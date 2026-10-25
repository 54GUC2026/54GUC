from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from arch4.action_grading_policy import (
    delayed_action_feedback_horizons_s,
    should_delay_radio_action_grade,
    staged_roi_grade_delay_s,
)
from arch4.action_transaction import build_action_transaction_key
from arch4.brain.proof_feedback_owner import (
    build_layered_action_feedback,
    terminal_layered_action_feedback_is_valid,
)
from arch4.brain.proof_referee_context_owner import delayed_reward_proof_identity
from arch4.brain.proof_referee_historical_projection_owner import (
    commit_staged_roi_historical_projection,
)
from arch4.brain.proof_referee_secondary_learning import commit_staged_roi_secondary_learning
from arch4.interfaces import LeverAction, LeverResult, LeverType, ModemInput
from arch4.smart_loop.clock_lane import _proof_grade_from_row, _proof_row_matches_pending
from arch4.tests.controller_generation_factory import (
    materialize_controller_generation,
)


ROOT = Path(__file__).resolve().parents[2]


def _action(lever: LeverType, **params) -> LeverAction:
    return LeverAction(lever=lever, params=params, reason="delayed reward test")


def test_normal_radio_reward_lifecycle_is_60_360_600_and_legacy_delay_params_cannot_override_it():
    for action in (
        _action(LeverType.BAND_SELECTION, bands=[3], _roi_grade_delay_s=300),
        _action(LeverType.PBAND, pband=3),
        _action(LeverType.BAND_LOCK, bands=[3, 20]),
        _action(LeverType.CELL_LOCK, pci=100, earfcn=1300),
        _action(LeverType.RESTART),
    ):
        assert staged_roi_grade_delay_s(action) == 600.0
        assert delayed_action_feedback_horizons_s(action) == (60, 360)


def test_only_explicit_emergency_fast_restart_has_short_terminal_exception():
    emergency = _action(LeverType.RESTART, emergency_fast=True)
    assert staged_roi_grade_delay_s(emergency) == 90.0
    assert delayed_action_feedback_horizons_s(emergency) == (60,)

    not_emergency = _action(LeverType.RESTART, emergency_fast=False, _roi_grade_delay_s=90)
    assert staged_roi_grade_delay_s(not_emergency) == 600.0
    assert delayed_action_feedback_horizons_s(not_emergency) == (60, 360)
    string_false = _action(LeverType.RESTART, emergency_fast="false")
    assert staged_roi_grade_delay_s(string_false) == 600.0


def test_terminal_smart_loop_reward_consumes_exact_owner_stamped_grade_without_regrading():
    pending = SimpleNamespace(
        proof_id="proof-1",
        command_id="command-1",
        context_key="plmn|cell|B20",
        reason_ref="prefer-b20",
    )
    feedback = build_layered_action_feedback(
        label="good",
        reason="stable_after_horizon",
        cq_delta=0.2,
        congestion_delta=-12.0,
        evidence={"grade_evidence_valid": True, "grade_context_status": "same"},
        horizon_s=600,
    )
    row = {
        "terminal": True,
        "memory_admitted": True,
        "roi_horizon_s": 600,
        "label": "good",
        "reason": "stable_after_horizon",
        "referee_code": "staged_roi_positive",
        "grade_evidence_valid": True,
        "selected_action_proof_id": pending.proof_id,
        "selected_action_command_id": pending.command_id,
        "smart_loop_context_key": pending.context_key,
        "smart_loop_reason_ref": pending.reason_ref,
        "layered_feedback": feedback,
    }

    assert _proof_row_matches_pending(row, pending) is True
    grade = _proof_grade_from_row(row, pending)
    assert grade.terminal is True
    assert grade.memory_admitted is True
    assert grade.horizon_s == 600
    assert grade.layered_feedback == feedback
    assert -3 <= grade.layered_feedback["memory_grade_score"] <= 3

    mismatched = dict(row, smart_loop_context_key="different-context")
    assert _proof_row_matches_pending(mismatched, pending) is False


def test_terminal_reward_without_owner_stamped_layered_package_fails_closed():
    pending = SimpleNamespace(
        proof_id="proof-2",
        command_id="command-2",
        context_key="plmn|cell|B3",
        reason_ref="prefer-b3",
    )
    row = {
        "terminal": True,
        "memory_admitted": True,
        "roi_horizon_s": 600,
        "label": "good",
        "reason": "stable_after_horizon",
        "grade_evidence_valid": True,
        "selected_action_proof_id": pending.proof_id,
        "selected_action_command_id": pending.command_id,
        "smart_loop_context_key": pending.context_key,
        "smart_loop_reason_ref": pending.reason_ref,
    }
    grade = _proof_grade_from_row(row, pending)
    assert grade.label == "invalid"
    assert grade.memory_admitted is False
    assert grade.reason_code == "terminal_layered_reward_package_invalid"


def test_terminal_reward_package_validator_rejects_coercion_and_horizon_mismatch():
    feedback = build_layered_action_feedback(
        label="good",
        reason="stable_after_horizon",
        evidence={"grade_evidence_valid": True, "grade_context_status": "same"},
        horizon_s=600,
    )
    assert terminal_layered_action_feedback_is_valid(feedback, horizon_s=600) is True
    assert terminal_layered_action_feedback_is_valid(
        dict(feedback, memory_grade_score="2"), horizon_s=600
    ) is False
    assert terminal_layered_action_feedback_is_valid(
        dict(feedback, horizon_s="600"), horizon_s=600
    ) is False
    assert terminal_layered_action_feedback_is_valid(feedback, horizon_s=360) is False


def test_secondary_learning_rejects_missing_terminal_owner_package_before_any_store_commit():
    class _ForbiddenStore:
        def __getattr__(self, name):
            raise AssertionError(f"secondary store was touched: {name}")

    original = SimpleNamespace(
        decision_trace_id="trace-parent",
        action_params={"bands": [20]},
        objective_metadata={},
    )
    record = SimpleNamespace(
        objective_metadata={},
        roi_horizon_s=600,
        decision_trace_id="trace-parent:roi:600",
    )
    receipt = commit_staged_roi_secondary_learning(
        proof_owner="test",
        logger=None,
        memory=_ForbiddenStore(),
        historical=_ForbiddenStore(),
        crystal_ball=_ForbiddenStore(),
        record=record,
        original=original,
        modem_now=SimpleNamespace(primary_band=20),
        computed_now=None,
        label="good",
        reason="stable_after_horizon",
        referee_code="staged_roi_positive",
        evidence={"grade_evidence_valid": True, "grade_context_status": "same"},
        ts=1000.0,
        learning_gate={"learning_allowed": True},
    )
    assert receipt["committed"] is False
    assert receipt["reason"] == "terminal_layered_reward_package_invalid"


def test_terminal_historical_projection_uses_target_and_post_band_with_exact_identity():
    captured: dict = {}

    class _Historical:
        def record_terminal_reward_outcome(self, **kwargs):
            captured.update(kwargs)
            return {"recorded": True, "reason": "staged_roi"}

    action = LeverAction(
        lever=LeverType.PBAND,
        params={"pband": 20, "_selected_action_proof_id": "proof-20"},
        reason="operator proof",
        intent="manual",
    )
    action.command_id = "command-20"
    action_key = build_action_transaction_key(
        action,
        session_scope_key="session-20",
        identity_key="identity-20",
        source_generation=20,
        source="ngui_manual_nmi",
        origin_kind="operator_ngui",
    )
    original = SimpleNamespace(
        decision_trace_id="trace-parent",
        action_params={"pband": 20},
        objective_metadata={
            "selected_action_command_id": action_key.command_id,
            "selected_action_proof_id": action_key.proof_id,
            "smart_loop_context_key": "",
            "smart_loop_reason_ref": "",
            "action_transaction": action_key.as_dict(),
        },
        proposal_objective_id="objective-20",
    )
    record = SimpleNamespace(
        action_params={"bands": [20]},
        decision_trace_id="trace-parent:roi:600",
        roi_parent_trace_id="trace-parent",
        roi_horizon_s=600,
        provider_plmn="22005",
        provider_name="A1",
        emergency_class="",
        objective_metadata={
            "layered_action_feedback": {"memory_grade_score": 2},
        },
    )
    modem_now = materialize_controller_generation(
        ModemInput.safe_defaults(
            primary_band=20,
            selected_band_set=[20],
        )
    ).intelligence
    evidence = {
        "tp_now": 100.0,
        "grade_context_status": "same",
        "grade_evidence_valid": True,
        "context_provider_plmn_now": "22005",
        "context_provider_now": "a1",
    }

    identity = delayed_reward_proof_identity(
        original=original,
        record=record,
        modem_now=modem_now,
        evidence=evidence,
    )
    assert identity["exact_identity_complete"] is True
    assert identity["optimization_selected"] is False
    assert identity["smart_loop_context_key"] == ""
    assert identity["target_post_action_bands"] == [20]

    result = commit_staged_roi_historical_projection(
        historical=_Historical(),
        record=record,
        original=original,
        evidence=evidence,
        ts=1000.0,
        contract_label="good",
        referee_code="staged_roi_positive",
        referee_level="good",
        reason="stable_after_horizon",
        proof_identity=identity,
        reward_entry={"schema": "TerminalRewardRecord.v2"},
        reward_ledger_key="reward-ledger-20",
    )
    assert result["historical_committed"] is True
    assert captured["action"].params["bands"] == [20]
    assert captured["reward_entry"]["schema"] == "TerminalRewardRecord.v2"
    metadata = captured["metadata_extra"]
    assert metadata["terminal_grade_score"] == 2
    assert metadata["post_action_band"] == 20
    assert metadata["proof_identity"]["selected_action_proof_id"] == "proof-20"
    assert "modem_before" not in metadata


def test_loose_selected_ids_cannot_replace_an_action_transaction_key():
    original = SimpleNamespace(
        decision_trace_id="trace-parent",
        action_params={"bands": [3, 20]},
        objective_metadata={
            "action_root_id": "loose-root",
            "selected_action_command_id": "loose-command",
            "selected_action_proof_id": "loose-proof",
            "action_transaction": {"root_id": "loose-root"},
        },
    )
    identity = delayed_reward_proof_identity(
        original=original,
        record=SimpleNamespace(
            decision_trace_id="trace-parent:roi:600",
            roi_horizon_s=600,
        ),
        modem_now=materialize_controller_generation(
            ModemInput.safe_defaults(
                primary_band=3,
                selected_band_set=[3, 20],
            )
        ).intelligence,
        evidence={"grade_context_status": "same"},
    )

    assert identity["exact_identity_complete"] is False
    assert identity["action_transaction"] == {}
    assert identity["selected_action_command_id"] == ""
    assert identity["selected_action_proof_id"] == ""


def test_successful_delayed_execution_result_contract_is_not_itself_a_grade():
    result = LeverResult(success=True, lever=LeverType.BAND_SELECTION)
    assert result.success is True
    # The strategic contract is exercised above: no score exists until the
    # terminal owner-stamped package at 600 seconds.
    assert not hasattr(result, "memory_grade_score")


def test_failed_execution_is_immediately_terminal_and_never_marked_settling():
    action = _action(LeverType.PBAND, pband=3)
    failed_record = SimpleNamespace(success=False, timestamp=1000.0)
    successful_record = SimpleNamespace(success=True, timestamp=1000.0)
    assert should_delay_radio_action_grade(action, failed_record, now=1001.0) is False
    assert should_delay_radio_action_grade(action, successful_record, now=1001.0) is True


def test_static_guards_block_immediate_binary_reward_and_legacy_300s_final_reintroduction():
    policy = (ROOT / "arch4/action_grading_policy.py").read_text(encoding="utf-8")
    authority = (ROOT / "arch4/brain/learning_authority.py").read_text(encoding="utf-8")
    legacy_owner = (
        ROOT / "arch4/brain/learning_authority_quality_journal_owner.py"
    ).read_text(encoding="utf-8")
    historical = (
        ROOT / "arch4/brain/proof_referee_historical_projection_owner.py"
    ).read_text(encoding="utf-8")
    clock_lane = (ROOT / "arch4/smart_loop/clock_lane.py").read_text(encoding="utf-8")

    assert "300.0" not in policy
    assert "_ca_pool_grade_delay_s" not in policy
    assert 'params.get("_roi_grade_delay_s")' not in policy
    assert "LearningAuthorityQualityJournalOwner" not in authority
    assert "append_referee_outcome" not in authority
    assert "RefereeJournal(" not in legacy_owner
    assert "RefereeJournalEntry(" not in legacy_owner
    assert "return None" in legacy_owner
    assert not (ROOT / "arch4/engine_outcome_learning.py").exists()
    assert "modem_before" not in historical
    assert "terminal_layered_reward_package_invalid" in clock_lane
    assert "actual_context_key == expected_context_key" in clock_lane
