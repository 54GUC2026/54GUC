from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from arch4.action_transaction import ActionTransactionKey
from arch4.action_transaction_authority import (
    ActionLifecycleStage,
    ActionTransactionAuthority,
)
from arch4.intelligence.checkpoint_store import select_checkpoint
from arch4.brain.crystal_ball import CrystalBall
from arch4.brain.event_inbox import BrainEventInbox
from arch4.brain.historical import HistoricalJournal, historical_evidence_scope
from arch4.brain.memory import Memory
from arch4.brain.memory_action_history_owner import _action_evidence_scope
from arch4.brain.memory_evidence_scope_owner import bind_memory_evidence_scope
from arch4.brain.memory_persistence import prepare_memory_checkpoint
from arch4.brain.action_proof_register import (
    action_proof_closure_from_mapping,
    action_proof_surface_from_memory,
)
from arch4.brain.proof_feedback_owner import build_layered_action_feedback
from arch4.brain.proof_memory_store import record_action_proof_feedback
from arch4.brain.proof_referee_secondary_learning import commit_staged_roi_secondary_learning
from arch4.brain.proof_referee_evidence import staged_roi_label
from arch4.brain.proof_referee_projection_outbox_owner import (
    _deliver_projection,
    project_reward_outbox_entry,
    resume_reward_projection_outbox,
)
from arch4.brain.reward_ledger import (
    PROJECTION_NAMES,
    admit_terminal_reward,
    projection_is_completed,
    reward_ledger_entry,
)
from arch4.interfaces import (
    ActionRecord,
    Arch4Config,
    ComputedSignals,
    LeverType,
    ModemInput,
)
from arch4.intelligence.evidence_recorder import IntelligenceEvidenceRecorder
from arch4.tests.controller_generation_factory import (
    materialize_controller_generation,
)
from arch4.tests.checkpoint_test_support import (
    commit_memory_checkpoint,
)
from arch4.terminal_reward_observation import TerminalRewardObservation


ROOT = Path(__file__).resolve().parents[2]


def _accepted_scope(*, sequence: int = 1, timestamp: float = 1000.0):
    raw = ModemInput.safe_defaults(
        sample_sequence=int(sequence),
        timestamp=float(timestamp),
        sample_age_s=0.0,
        provider_plmn="22005",
        access_type="lte",
        primary_band=20,
        earfcn=1795,
        pci=255,
        selected_band_set=[20],
    )
    return materialize_controller_generation(
        raw,
        owner_values={
            "identity_code": "0x0000000000000101",
            "identity_status": "exact",
            "identity_catalog_version": "test-catalog-v1",
            "identity_catalog_checksum": "a" * 64,
            "provider_plmn": "22005",
            "access_type": "lte",
            "access_type_id": "lte",
        },
        accepted_at=float(timestamp),
        source_sequence=int(sequence),
        source_epoch="epoch-1",
        session_scope_key="session-1",
    )


def _modem_scope():
    return _accepted_scope().intelligence


def _terminal_observation() -> TerminalRewardObservation:
    accepted = _accepted_scope()
    return TerminalRewardObservation.capture(
        accepted.intelligence,
        accepted.derived,
    )


def _action_transaction() -> dict:
    return ActionTransactionKey(
        root_id="action:test-root",
        command_id="command-1",
        proof_id="proof-1",
        session_scope_key="session-1",
        identity_key="modem-1",
        source_generation=1,
        source="smart_loop",
        origin_kind="smart_loop",
        correlation_id="command-1",
        correlation_source="command_id",
        lever="BAND_SELECTION",
        target_shape=(("bands", (20,)), ("lever", "BAND_SELECTION")),
    ).as_dict()


def _action_lifecycle_authority(
    persistence_dir=None,
) -> ActionTransactionAuthority:
    authority = ActionTransactionAuthority(persistence_dir=persistence_dir)
    if authority.receipt_for_root("action:test-root") is None:
        authority.transition(
            action_key=ActionTransactionKey.from_mapping(_action_transaction()),
            stage=ActionLifecycleStage.ADMITTED,
            now=999.0,
            evidence={"boundary": "reward_projection_test"},
        )
    return authority


def _identity() -> dict:
    return {
        "schema": "delayed_reward_proof_identity.v1",
        "owner": "arch4.brain.proof_referee_context_owner",
        "parent_trace_id": "trace-parent",
        "child_trace_id": "trace-parent:roi:600",
        "selected_action_command_id": "command-1",
        "selected_action_proof_id": "proof-1",
        "smart_loop_context_key": "22005|cell-1|B20",
        "smart_loop_reason_ref": "prefer-b20",
        "action_root_id": "action:test-root",
        "action_transaction": _action_transaction(),
        "command_id": "command-1",
        "proof_id": "proof-1",
        "session_scope_key": "session-1",
        "identity_key": "modem-1",
        "source_generation": 1,
        "opportunity_id": "",
        "decision_id": "",
        "decision_receipt_id": "",
        "bandit_selection_receipt_id": "",
        "brain_decision_receipt": {},
        "bandit_selection_receipt": {},
        "roi_horizon_s": 600,
        "target_post_action_bands": [20],
        "post_action_band": 20,
        "provider_plmn": "22005",
        "provider_name": "a1",
        "grade_context_status": "same",
        "exact_identity_complete": True,
    }


def _feedback() -> dict:
    return build_layered_action_feedback(
        label="good",
        reason="stable_after_horizon",
        cq_delta=0.1,
        congestion_delta=-10.0,
        evidence={"grade_evidence_valid": True, "grade_context_status": "same"},
        horizon_s=600,
    )


def _record(*, status: str = "classified") -> ActionRecord:
    record = ActionRecord(
        timestamp=1000.0,
        sinr_db=10.0,
        rsrq_db=-9.0,
        mcs_dl=12,
        ca_signature="B20",
        rtt_sliding_avg=30.0,
        rtt_ms=30.0,
        lever=LeverType.BAND_SELECTION,
        action_params={"bands": [20], "_roi_horizon_s": 600},
        success=True,
        throughput_before_mbps=20.0,
        throughput_after_mbps=30.0,
        sinr_after=11.0,
        ca_signature_after="B20",
        improvement=10.0,
        provider_plmn="22005",
        provider_name="A1",
        outcome_label="good",
        learning_ltm_applied=True,
        learning_commit_status=status,
        learning_gate={"learning_allowed": True, "gate_input_complete": True},
        decision_trace_id="trace-parent:roi:600",
        roi_horizon_s=600,
        roi_parent_trace_id="trace-parent",
        objective_metadata={
            "layered_action_feedback": _feedback(),
            "delayed_reward_proof_identity": _identity(),
            "selected_action_command_id": "command-1",
            "selected_action_proof_id": "proof-1",
            "smart_loop_context_key": "22005|cell-1|B20",
            "smart_loop_reason_ref": "prefer-b20",
            "action_root_id": "action:test-root",
            "action_transaction": _action_transaction(),
        },
    )
    scope = _action_evidence_scope(
        _modem_scope(),
        lever=LeverType.BAND_SELECTION,
        params=dict(record.action_params),
    )
    record.baseline_evidence_scope = dict(scope)
    record.evidence_scope = dict(scope)
    return record


def _evidence() -> dict:
    return {
        "grade_evidence_valid": True,
        "grade_context_status": "same",
        "grade_context_reason": "same_context",
        "cq_before": 0.5,
        "cq_now": 0.6,
        "congestion_before": 45.0,
        "congestion_now": 35.0,
        "congestion_before_valid": True,
        "congestion_now_valid": True,
        "context_provider_plmn_now": "22005",
        "context_provider_now": "a1",
    }


def test_terminal_reward_retry_is_idempotent_and_journals_once(tmp_path):
    memory = Memory(Arch4Config(calibration_db_enabled=False), memory_dir=str(tmp_path))
    record = _record()

    first = admit_terminal_reward(
        memory,
        record=record,
        proof_identity=_identity(),
        terminal_observation=_terminal_observation().as_dict(),
        layered_feedback=_feedback(),
        evidence=_evidence(),
        ts=1000.0,
    )
    count_after_first = memory._event_journal.count()
    second = admit_terminal_reward(
        memory,
        record=record,
        proof_identity=_identity(),
        terminal_observation=_terminal_observation().as_dict(),
        layered_feedback=_feedback(),
        evidence=_evidence(),
        ts=1001.0,
    )

    assert first["admitted"] is True
    assert second["admitted"] is False
    assert second["duplicate"] is True
    assert second["ledger_key"] == first["ledger_key"]
    assert memory._event_journal.count() == count_after_first
    assert len(memory._reward_ledger) == 1


def test_terminal_reward_keeps_action_and_horizon_generation_roles_distinct(tmp_path):
    memory = Memory(Arch4Config(calibration_db_enabled=False), memory_dir=str(tmp_path))
    record = _record()
    baseline = _accepted_scope(sequence=1, timestamp=1000.0)
    terminal = _accepted_scope(sequence=7, timestamp=1600.0)
    record.baseline_evidence_scope = _action_evidence_scope(
        baseline.intelligence,
        lever=LeverType.BAND_SELECTION,
        params=dict(record.action_params),
    )
    record.evidence_scope = _action_evidence_scope(
        terminal.intelligence,
        lever=LeverType.BAND_SELECTION,
        params=dict(record.action_params),
    )

    receipt = admit_terminal_reward(
        memory,
        record=record,
        proof_identity=_identity(),
        terminal_observation=TerminalRewardObservation.capture(
            terminal.intelligence,
            terminal.derived,
        ).as_dict(),
        layered_feedback=_feedback(),
        evidence=_evidence(),
        ts=1600.0,
    )

    assert receipt["admitted"] is True
    assert receipt["reason"] == "terminal_reward_admitted"


def test_rejected_reward_candidate_hash_is_not_a_canonical_ledger_key(
    tmp_path,
    monkeypatch,
):
    memory = Memory(Arch4Config(calibration_db_enabled=False), memory_dir=str(tmp_path))
    record = _record()
    original = SimpleNamespace(
        decision_trace_id="trace-parent",
        action_params=dict(record.action_params),
        objective_metadata={
            "action_root_id": "action:test-root",
            "action_transaction": _action_transaction(),
            "selected_action_command_id": "command-1",
            "selected_action_proof_id": "proof-1",
            "smart_loop_context_key": "22005|cell-1|B20",
            "smart_loop_reason_ref": "prefer-b20",
        },
        lever=LeverType.BAND_SELECTION,
    )
    monkeypatch.setattr(
        "arch4.brain.proof_referee_secondary_learning.admit_terminal_reward",
        lambda *_args, **_kwargs: {
            "admitted": False,
            "duplicate": False,
            "ledger_key": "candidate-only-hash",
            "reason": "terminal_reward_rejected_for_test",
            "error": "",
        },
    )
    accepted = _accepted_scope()

    receipt = commit_staged_roi_secondary_learning(
        proof_owner="test",
        logger=None,
        memory=memory,
        historical=None,
        crystal_ball=None,
        record=record,
        original=original,
        modem_now=accepted.intelligence,
        computed_now=accepted.derived,
        label="good",
        reason="stable_after_horizon",
        referee_code="staged_roi_positive",
        evidence=_evidence(),
        ts=1600.0,
        learning_gate={"learning_allowed": True},
    )

    assert receipt["reward_admitted"] is False
    assert receipt["reward_ledger_key"] == ""
    assert receipt["reason"] == "terminal_reward_rejected_for_test"
    assert len(memory._reward_ledger) == 0


@pytest.mark.parametrize("grade_score", (-3, -2, -1, 0, 1, 2, 3))
def test_memory_projection_preserves_every_layered_grade_strength(
    tmp_path,
    grade_score,
):
    memory = Memory(
        Arch4Config(calibration_db_enabled=False),
        memory_dir=str(tmp_path / str(grade_score)),
    )
    record = _record()
    record.outcome_label = "good" if grade_score > 0 else "bad" if grade_score < 0 else "neutral"
    receipt = _deliver_projection(
        projection="memory",
        ledger_key=f"ledger-{grade_score}",
        memory=memory,
        historical=None,
        crystal_ball=None,
        brain_event_inbox=None,
        logger=None,
        action_transaction_authority=ActionTransactionAuthority(),
        record=record,
        original=record,
        terminal_observation=_terminal_observation(),
        proof_identity=_identity(),
        evidence={},
        label={
            -3: "strong_bad",
            -2: "bad",
            -1: "weak_bad",
            0: "neutral",
            1: "weak_good",
            2: "good",
            3: "strong_good",
        }[grade_score],
        reason="grade_matrix",
        referee_code="staged_roi_positive" if grade_score > 0 else "staged_roi_negative",
        referee_level="good" if grade_score > 0 else "bad",
        grade_score=grade_score,
        ts=1000.0,
        secondary={},
    )
    assert receipt["completed"] is True
    assert receipt["grade_score"] == grade_score
    if grade_score == 0:
        assert receipt["committed"] is False
        assert memory.band_learning_stm == {}
    else:
        assert receipt["committed"] is True
        assert receipt["grade_weight"] == abs(grade_score)
        bucket = next(iter(memory.band_learning_stm.values()))
        assert bucket["good" if grade_score > 0 else "bad"] == abs(grade_score)


def test_terminal_reward_replays_after_restart_and_retry_does_not_increment(tmp_path):
    cfg = Arch4Config(calibration_db_enabled=False)
    memory = Memory(cfg, memory_dir=str(tmp_path))
    admitted = admit_terminal_reward(
        memory,
        record=_record(),
        proof_identity=_identity(),
        terminal_observation=_terminal_observation().as_dict(),
        layered_feedback=_feedback(),
        evidence=_evidence(),
        ts=1000.0,
    )
    assert admitted["admitted"] is True

    restarted = Memory(cfg, memory_dir=str(tmp_path))
    assert list(restarted._reward_ledger) == [admitted["ledger_key"]]
    before_retry = restarted._event_journal.count()
    duplicate = admit_terminal_reward(
        restarted,
        record=_record(),
        proof_identity=_identity(),
        terminal_observation=_terminal_observation().as_dict(),
        layered_feedback=_feedback(),
        evidence=_evidence(),
        ts=1002.0,
    )
    assert duplicate["duplicate"] is True
    assert restarted._event_journal.count() == before_retry
    assert len(restarted._reward_ledger) == 1


def test_unscoped_checkpoint_fails_before_advancing_reward_journal_watermark(
    tmp_path,
):
    cfg = Arch4Config(calibration_db_enabled=False)
    memory = Memory(cfg, memory_dir=str(tmp_path))
    admitted = admit_terminal_reward(
        memory,
        record=_record(),
        proof_identity=_identity(),
        terminal_observation=_terminal_observation().as_dict(),
        layered_feedback=_feedback(),
        evidence=_evidence(),
        ts=1000.0,
    )
    previous_applied = int(memory._journal_applied_count)

    prepared = prepare_memory_checkpoint(
        memory,
        checkpoint_reason="unscoped_reward_test",
    )
    receipt = prepared.receipt

    assert receipt["saved"] is False
    assert receipt["status"] == "deferred"
    assert receipt["defer_reason"] == "identity_scope_not_bound"
    assert memory._journal_applied_count == previous_applied
    assert not (tmp_path / "memory.json").exists()
    assert not (tmp_path / "memory_meta.json").exists()
    restarted = Memory(cfg, memory_dir=str(tmp_path))
    assert list(restarted._reward_ledger) == [admitted["ledger_key"]]


@pytest.mark.parametrize("crash_projection", PROJECTION_NAMES)
def test_each_projection_boundary_resumes_after_crash_without_duplicate_durable_effect(
    tmp_path,
    crash_projection,
):
    """Crash after destination effect but before its RewardLedger receipt."""

    class _InjectedCrash(RuntimeError):
        pass

    cfg = Arch4Config(calibration_db_enabled=False)
    memory_dir = tmp_path / "memory"
    historical_dir = tmp_path / "historical"
    log_dir = tmp_path / "logs"
    memory = Memory(cfg, memory_dir=str(memory_dir))
    historical = HistoricalJournal(cfg, root_dir=historical_dir)
    assert bind_memory_evidence_scope(memory, _modem_scope()) is True
    historical._current_evidence_scope = historical_evidence_scope(_modem_scope())
    crystal = CrystalBall()
    inbox = BrainEventInbox()
    logger = IntelligenceEvidenceRecorder(output_dir=str(log_dir), enable_rollup=False)
    action_authority_dir = tmp_path / "action_lifecycle"
    action_authority = _action_lifecycle_authority(action_authority_dir)
    admitted = admit_terminal_reward(
        memory,
        record=_record(),
        proof_identity=_identity(),
        terminal_observation=_terminal_observation().as_dict(),
        layered_feedback=_feedback(),
        evidence=_evidence(),
        ts=1000.0,
    )
    ledger_key = admitted["ledger_key"]
    entry = reward_ledger_entry(memory, ledger_key)

    def _crash_after_effect(key: str, projection: str, phase: str) -> None:
        if projection != crash_projection or phase != "after_effect_before_receipt":
            return
        assert key == ledger_key
        # Model the hardest crash gap: the destination effect and prior
        # receipts reached disk, but this destination receipt did not.
        commit_memory_checkpoint(
            memory,
            checkpoint_reason=f"test_crash_after_{projection}",
            intelligence_artifacts={
                "historical.json": historical.get_state_for_save(),
                "crystal_ball.json": crystal.to_dict(),
                "calibration_authority.json": {},
                "intelligence_runtime.json": {
                    "schema": "IntelligenceRuntimeCheckpoint.v1",
                    "owner": "54guc.intelligence.persistence",
                },
            },
        )
        (tmp_path / "crystal_ball.json").write_text(
            json.dumps(crystal.to_dict(), sort_keys=True),
            encoding="utf-8",
        )
        raise _InjectedCrash(projection)

    try:
        with pytest.raises(_InjectedCrash, match=crash_projection):
            project_reward_outbox_entry(
                memory=memory,
                historical=historical,
                crystal_ball=crystal,
                brain_event_inbox=inbox,
                logger=logger,
                action_transaction_authority=action_authority,
                ledger_entry=entry,
                ts=1000.0,
                fault_injector=_crash_after_effect,
            )
    finally:
        logger.close()

    # A process restart creates fresh volatile owners and reloads every durable
    # destination registry.  The prior Brain inbox is deliberately discarded.
    restarted_memory = Memory(cfg, memory_dir=str(memory_dir))
    restarted_historical = HistoricalJournal(cfg, root_dir=historical_dir)
    selection = select_checkpoint(memory_dir)
    assert selection is not None
    restarted_historical.load_state(selection.artifact("historical.json"))
    # Runtime re-establishes the accepted current scope before durable reward
    # outbox delivery resumes. Historical may not have had an effect to save
    # yet when the injected crash occurred at an earlier projection boundary.
    restarted_historical._current_evidence_scope = historical_evidence_scope(
        _modem_scope()
    )
    restarted_crystal = CrystalBall()
    restarted_crystal.from_dict(
        json.loads((tmp_path / "crystal_ball.json").read_text(encoding="utf-8"))
    )
    restarted_inbox = BrainEventInbox()
    restarted_logger = IntelligenceEvidenceRecorder(output_dir=str(log_dir), enable_rollup=False)
    restarted_action_authority = _action_lifecycle_authority(
        action_authority_dir
    )
    try:
        resumed = resume_reward_projection_outbox(
            memory=restarted_memory,
            historical=restarted_historical,
            crystal_ball=restarted_crystal,
            brain_event_inbox=restarted_inbox,
            logger=restarted_logger,
            action_transaction_authority=restarted_action_authority,
            now=1001.0,
        )
        assert resumed
        assert resumed[-1]["complete"] is True
        assert resume_reward_projection_outbox(
            memory=restarted_memory,
            historical=restarted_historical,
            crystal_ball=restarted_crystal,
            brain_event_inbox=restarted_inbox,
            logger=restarted_logger,
            action_transaction_authority=restarted_action_authority,
            now=1002.0,
        ) == []
    finally:
        restarted_logger.close()

    assert all(
        projection_is_completed(restarted_memory, ledger_key, projection)
        for projection in PROJECTION_NAMES
    )
    assert f"memory:{ledger_key}" in restarted_memory._reward_projection_effect_keys
    assert sum(
        int(bucket.get("n", 0) or 0)
        for bucket in restarted_memory.band_learning_ltm.values()
    ) == 1
    assert ledger_key in restarted_historical._delayed_reward_projection_keys
    historical_effects = [
        row
        for rows in restarted_historical.action_outcomes.values()
        for row in rows
        if str(dict(row.get("meta") or {}).get("reward_ledger_key") or "") == ledger_key
    ]
    assert len(historical_effects) == 1
    assert ledger_key in restarted_crystal._delayed_reward_projection_keys
    assert sum(slot.sample_count for slot in restarted_crystal._table.values()) == 1

    # Brain's durable projection is the journaled RewardLedger receipt; the
    # inbox is a volatile notification transport.  It contains at most one
    # live notification after restart, never two copies of the same key.
    assert restarted_inbox.pending_count() in {0, 1}
    assert len({event.dedupe_key for event in restarted_inbox._pending}) == restarted_inbox.pending_count()

    evidence_rows = []
    for path in sorted(log_dir.glob("events_*.jsonl")):
        evidence_rows.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    evidence_keys = [
        str(row.get("idempotency_key") or "")
        for row in evidence_rows
        if str(row.get("idempotency_key") or "").startswith("delayed_reward:")
    ]
    assert evidence_keys.count(f"delayed_reward:evidence:{ledger_key}") == 1
    assert evidence_keys.count(f"delayed_reward:secondary:{ledger_key}") == 1


def test_terminal_reward_append_fault_fails_before_in_memory_admission():
    class _BrokenMemory:
        def __init__(self):
            self._reward_ledger = {}
            self._reward_trajectory_feedback = {}

        def _append_journal_event_strict(self, _event):
            raise OSError("disk full")

        def _mark_dirty(self, *, events=0):
            raise AssertionError(f"dirty mutation after failed append: {events}")

    memory = _BrokenMemory()
    receipt = admit_terminal_reward(
        memory,
        record=_record(),
        proof_identity=_identity(),
        terminal_observation=_terminal_observation().as_dict(),
        layered_feedback=_feedback(),
        evidence=_evidence(),
        ts=1000.0,
    )
    assert receipt["admitted"] is False
    assert receipt["reason"] == "terminal_reward_journal_append_failed"
    assert receipt["error"] == "OSError"
    assert memory._reward_ledger == {}


def test_secondary_learning_only_admits_and_duplicate_delivery_reuses_outbox(tmp_path):
    memory = Memory(Arch4Config(calibration_db_enabled=False), memory_dir=str(tmp_path))
    original = SimpleNamespace(
        decision_trace_id="trace-parent",
        action_params={"bands": [20]},
        objective_metadata={
            "selected_action_command_id": "command-1",
            "selected_action_proof_id": "proof-1",
            "smart_loop_context_key": "22005|cell-1|B20",
            "smart_loop_reason_ref": "prefer-b20",
            "action_root_id": "action:test-root",
            "action_transaction": _action_transaction(),
        },
        proposal_objective_id="objective-1",
    )
    accepted = _accepted_scope()

    first = commit_staged_roi_secondary_learning(
        proof_owner="test",
        logger=None,
        memory=memory,
        historical=SimpleNamespace(),
        crystal_ball=SimpleNamespace(),
        record=_record(),
        original=original,
        modem_now=accepted.intelligence,
        computed_now=accepted.derived,
        label="good",
        reason="stable_after_horizon",
        referee_code="staged_roi_positive",
        evidence=_evidence(),
        ts=1000.0,
        learning_gate={"learning_allowed": True},
    )
    second_record = _record()
    second = commit_staged_roi_secondary_learning(
        proof_owner="test",
        logger=None,
        memory=memory,
        historical=SimpleNamespace(),
        crystal_ball=SimpleNamespace(),
        record=second_record,
        original=original,
        modem_now=accepted.intelligence,
        computed_now=accepted.derived,
        label="good",
        reason="stable_after_horizon",
        referee_code="staged_roi_positive",
        evidence=_evidence(),
        ts=1001.0,
        learning_gate={"learning_allowed": True},
    )

    assert first["reward_admitted"] is True
    assert second["reward_admitted"] is True
    assert second["reward_duplicate"] is True
    assert second_record.learning_commit_status == "classified"
    assert len(memory._reward_ledger) == 1
    assert len(memory._reward_projection_outbox) == 1


def test_nonterminal_feedback_replays_as_trajectory_without_reward(tmp_path):
    cfg = Arch4Config(calibration_db_enabled=False)
    memory = Memory(cfg, memory_dir=str(tmp_path))
    record = _record(status="pending")
    record.decision_trace_id = "trace-parent"
    record.roi_horizon_s = 0
    record.objective_metadata["smart_loop_feedback"] = [
        {
            "horizon_s": 60,
            "label": "good",
            "reason": "trajectory_only",
            "terminal": False,
            "memory_admitted": False,
            "evidence": {"congestion_before": 45.0, "congestion_now": 40.0},
        }
    ]
    memory._append_action_history_record(record)
    receipt = record_action_proof_feedback(memory, record, ts=1060.0, horizon=60)
    assert receipt["recorded"] is True
    assert len(memory._reward_trajectory_feedback) == 1
    assert memory._reward_ledger == {}

    restarted = Memory(cfg, memory_dir=str(tmp_path))
    assert len(restarted._reward_trajectory_feedback) == 1
    assert restarted._reward_ledger == {}
    trajectory = next(iter(restarted._reward_trajectory_feedback.values()))
    assert trajectory["terminal"] is False
    assert trajectory["memory_admitted"] is False
    assert trajectory["horizon_s"] == 60


def test_terminal_evidence_carries_canonical_congestion_before_and_now():
    original = _record(status="pending")
    original.decision_trace_id = "trace-parent"
    original.action_params = {"bands": [20]}
    original.objective_metadata["delayed_reward_baseline"] = {
        "schema": "delayed_reward_baseline.v1",
        "owner": "arch4.brain.memory_action_history_owner",
        "cq_before": 0.5,
        "congestion_before": 45.0,
        "congestion_before_valid": True,
    }
    modem = ModemInput.safe_defaults(
        sinr_db=10.0,
        rsrq_db=-9.0,
        rx_bytes_per_sec=3_750_000.0,
        tx_bytes_per_sec=250_000.0,
        provider_plmn="22005",
        provider_name="A1",
        primary_band=20,
        selected_band_set=[20],
    )
    computed = ComputedSignals(
        cq_cell=0.6,
        congestion_pct=35.0,
        congestion_pct_valid=True,
        rtt_sliding_avg=30.0,
        cart_activity_per_hour=0.0,
        cast=95.0,
    )
    accepted = materialize_controller_generation(modem, computed)
    _label, _reason, evidence = staged_roi_label(
        original=original,
        modem_now=accepted.intelligence,
        computed_now=accepted.derived,
        extra={"health_stage": "healthy"},
        incident_assessment={},
    )
    assert evidence["congestion_before"] == 45.0
    assert evidence["congestion_now"] == 35.0
    assert evidence["congestion_before_valid"] is True
    assert evidence["congestion_now_valid"] is True


def test_static_terminal_projection_gate_cannot_bypass_reward_ledger():
    secondary = (ROOT / "arch4/brain/proof_referee_secondary_learning.py").read_text(
        encoding="utf-8"
    )
    assert "reward_receipt = admit_terminal_reward(" in secondary
    assert "commit_action_learning_to_memory_store(" not in secondary
    assert "commit_staged_roi_historical_projection(" not in secondary
    assert "commit_staged_roi_crystal_ball_projection(" not in secondary

    outbox = (
        ROOT / "arch4/brain/proof_referee_projection_outbox_owner.py"
    ).read_text(encoding="utf-8")
    delivery = outbox.index("receipt = _deliver_projection(")
    durable_receipt = outbox.index("            complete_reward_projection(")
    assert delivery < durable_receipt
    assert "commit_terminal_reward_learning_to_memory_store(" in outbox
    assert "commit_staged_roi_historical_projection(" in outbox
    assert "commit_staged_roi_crystal_ball_projection(" in outbox
    assert "_modem_from_projection" not in outbox
    assert "_computed_from_projection" not in outbox
    assert "SimpleNamespace" not in outbox

    production_calls = []
    for path in (ROOT / "arch4/brain").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "admit_terminal_reward(" in source and path.name != "reward_ledger.py":
            production_calls.append(path.name)
    assert production_calls == ["proof_referee_secondary_learning.py"]

    due_owner = (ROOT / "arch4/brain/proof_referee_due_owner.py").read_text(
        encoding="utf-8"
    )
    assert due_owner.index("resume_reward_projection_outbox(") < due_owner.index(
        "staged_roi_runtime_ready("
    )


def test_live_and_durable_terminal_proof_envelopes_have_identical_learning_truth(tmp_path):
    memory = Memory(Arch4Config(calibration_db_enabled=False), memory_dir=str(tmp_path))
    record = _record(status="committed")
    record.referee_outcome_level = "good"
    record.referee_outcome_code = "staged_roi_positive"
    record.learning_policy_reason = "stable_after_horizon"
    record.objective_metadata.update(
        {
            "staged_roi_evidence": _evidence(),
            "reward_ledger_receipt": {
                "schema": "delayed_reward_ledger_receipt.v1",
                "owner": "arch4.brain.reward_ledger",
                "admitted": True,
                "duplicate": False,
                "ledger_key": "ledger-1",
            },
            "secondary_learning": {
                "reward_admitted": True,
                "reward_duplicate": False,
                "reward_ledger_key": "ledger-1",
                "memory_committed": True,
                "historical_committed": True,
                "crystal_ball_updated": True,
            },
        }
    )
    memory._current_action_evidence_scope = dict(record.evidence_scope)
    memory._append_action_history_record(record)
    payload = {
        "parent_trace_id": "trace-parent",
        "child_trace_id": "trace-parent:roi:600",
        "roi_horizon_s": 600,
        "label": "good",
        "reason": "stable_after_horizon",
        "referee_code": "staged_roi_positive",
        "referee_level": "good",
        "learning_commit_status": "committed",
        "parent_status": "",
        "terminal": True,
        "memory_admitted": True,
        "grade_kind": "terminal_layered_reward",
        "secondary_learning": record.objective_metadata["secondary_learning"],
        "evidence": _evidence(),
        "layered_feedback": _feedback(),
        "proof_identity": _identity(),
        "selected_action_command_id": "command-1",
        "selected_action_proof_id": "proof-1",
        "smart_loop_context_key": "22005|cell-1|B20",
        "smart_loop_reason_ref": "prefer-b20",
    }
    live = action_proof_closure_from_mapping(
        {"event_id": "event-1", "source": "staged_roi", "payload": payload, "ts": 1000.0}
    )
    durable = action_proof_surface_from_memory(memory)["last_terminal"]
    truth_fields = (
        "parent_trace_id",
        "child_trace_id",
        "roi_horizon_s",
        "label",
        "reason",
        "referee_code",
        "referee_level",
        "terminal",
        "memory_admitted",
        "grade_kind",
        "reward_admitted",
        "reward_duplicate",
        "reward_ledger_key",
        "memory_committed",
        "historical_committed",
        "crystal_ball_updated",
        "selected_action_command_id",
        "selected_action_proof_id",
        "smart_loop_context_key",
        "smart_loop_reason_ref",
        "layered_feedback",
        "proof_identity",
        "grade_context_status",
        "grade_evidence_valid",
        "cq_before",
        "cq_now",
        "congestion_before",
        "congestion_now",
        "congestion_before_valid",
        "congestion_now_valid",
    )
    assert {key: live.get(key) for key in truth_fields} == {
        key: durable.get(key) for key in truth_fields
    }
