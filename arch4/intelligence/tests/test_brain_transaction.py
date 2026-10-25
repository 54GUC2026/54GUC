import json
from pathlib import Path
from types import SimpleNamespace

from arch4.interfaces import ModemInput
import pytest

from arch4.action_transaction import (
    action_transaction_key_from_action,
    bind_action_transaction_key,
    build_action_transaction_key,
)
from arch4.action_origin import ActionOriginKind
from arch4.action_admission import (
    bind_selected_action_context,
    selected_action_context_from_action,
)
from arch4.brain.tick_contract import DecisionTrace, ExecutionOutcome
from arch4.brain.transaction import BrainTransaction, BrainTransactionStage
from arch4.brain.transaction_register import (
    BrainTransactionRegister,
    require_brain_transaction_register,
)
from arch4.brain.action_proof_register import (
    action_proof_surface_from_engine,
    record_action_proof_event,
    record_action_proof_feedback_event,
)
from arch4.brain.event_inbox import BrainEvent
from arch4.brain.feed_registers import BrainFeedRegisterBank
from arch4.event_journal import ForensicEventJournal
from arch4.interfaces import ComputedSignals, LeverAction, LeverType
from arch4.controller_frames import CellularIntelligenceGeneration
from arch4.tests.controller_generation_factory import (
    materialize_cellular_intelligence,
)


def _facts() -> CellularIntelligenceGeneration:
    intelligence, _derived = materialize_cellular_intelligence(
        ModemInput.safe_defaults(sample_sequence=17, primary_band=3),
        ComputedSignals(rtt_ms=35.0, cq_cell=0.8),
    )
    return intelligence


def test_brain_transaction_requires_explicit_end_to_end_stage_progression():
    action = LeverAction(
        lever=LeverType.BAND_SELECTION,
        params={"bands": [1, 3, 20]},
        reason="candidate",
    )
    bind_selected_action_context(
        action,
        origin_kind=ActionOriginKind.SMART_LOOP,
        selection_evidence={},
    )
    trace = DecisionTrace(
        decision_trace_id="trace-17",
        source="state:WORKING",
        state="WORKING",
        proposed="BAND_SELECTION",
        final="BAND_SELECTION",
        passed=4,
        blocked=0,
    )
    transaction = BrainTransaction.from_facts(_facts())
    transaction = transaction.with_decision(
        candidate_action=action,
        selected_action=action,
        trace=trace,
    )
    transaction = transaction.with_execution(
        execution=ExecutionOutcome.from_runtime(
            decision_trace_id="trace-17",
            action=action,
            action_suppressed=False,
        ),
        readback={"effective_band_match": True},
    )
    transaction = transaction.with_proof(
        proof={"proof_level": "proven"},
        grade="positive",
        learning_admission="admitted",
    ).close()

    payload = transaction.to_event_payload()
    assert transaction.stage is BrainTransactionStage.CLOSED
    assert payload["generation"] == 17
    assert payload["selection"]["lever"] == "BAND_SELECTION"
    assert payload["readback"]["effective_band_match"] is True
    assert payload["learning_admission"] == "admitted"


def test_brain_transaction_rejects_stage_skips():
    transaction = BrainTransaction.from_facts(_facts())
    with pytest.raises(RuntimeError, match="brain_transaction_execution_from_facts"):
        transaction.with_execution(
            execution=ExecutionOutcome.from_runtime(
                decision_trace_id="",
                action=None,
                action_suppressed=False,
            )
        )


def _executed_transaction(*, trace_id: str = "trace-17") -> BrainTransaction:
    action = LeverAction(
        lever=LeverType.BAND_SELECTION,
        params={
            "bands": [1, 3, 20],
            "_selected_action_proof_id": f"proof-{trace_id}",
        },
        reason="candidate",
        objective_id="objective-17",
    )
    action.command_id = f"command-{trace_id}"
    bind_selected_action_context(
        action,
        origin_kind=ActionOriginKind.SMART_LOOP,
        selection_evidence={},
    )
    bind_action_transaction_key(
        action,
        build_action_transaction_key(
            action,
            session_scope_key="session-17",
            identity_key="modem-17",
            source_generation=17,
            source="smart_loop_controller",
            origin_kind=ActionOriginKind.SMART_LOOP.value,
        ),
    )
    trace = DecisionTrace(
        decision_trace_id=trace_id,
        source="state:WORKING",
        state="WORKING",
        proposed="BAND_SELECTION",
        final="BAND_SELECTION",
        passed=4,
        blocked=0,
    )
    return (
        BrainTransaction.from_facts(_facts())
        .with_decision(
            candidate_action=action,
            selected_action=action,
            trace=trace,
        )
        .with_execution(
            execution=ExecutionOutcome.from_runtime(
                decision_trace_id=trace_id,
                action=action,
                action_suppressed=False,
                action_result=SimpleNamespace(
                    success=True,
                    error="",
                    radio_drop_detected=False,
                ),
            ),
            readback={
                "write_accepted": True,
                "target_confirmed": True,
                "chart_fresh": True,
            },
        )
    )


def _terminal_reward_proof(*, trace_id: str = "trace-17", key: str = "reward-17") -> dict:
    action_key = _executed_transaction(trace_id=trace_id).action_key
    assert action_key is not None
    return {
        "action_root_id": action_key.root_id,
        "parent_trace_id": trace_id,
        "label": "good",
        "referee_code": "roi_positive",
        "learning_commit_status": "proof_closed",
        "terminal": True,
        "grade_kind": "terminal_layered_reward",
        "roi_horizon_s": 600,
        "reward_admitted": True,
        "reward_ledger_key": key,
    }


def test_brain_transaction_register_keeps_successful_action_open_until_matching_proof():
    register = BrainTransactionRegister()
    transaction = register.register_executed(_executed_transaction(), now=100.0)

    assert transaction.stage is BrainTransactionStage.EXECUTED
    assert register.status(now=103.0)["pending_count"] == 1

    closure = register.close_from_terminal_reward(_terminal_reward_proof())

    assert closure is not None
    assert closure.matched_by == "action_root_id"
    assert closure.transaction.stage is BrainTransactionStage.CLOSED
    assert closure.transaction.grade == "good"
    assert closure.transaction.learning_admission == "admitted"
    assert register.status()["pending_count"] == 0


def test_brain_transaction_register_does_not_close_on_unmatched_proof():
    register = BrainTransactionRegister()
    transaction = register.register_executed(_executed_transaction(), now=100.0)

    assert register.close_from_terminal_reward(
        {
            "action_root_id": "action:different",
            "parent_trace_id": "different",
            "terminal": True,
            "grade_kind": "terminal_layered_reward",
            "roi_horizon_s": 600,
            "reward_admitted": True,
            "reward_ledger_key": "reward-different",
        }
    ) is None
    assert register.pending_transaction(transaction.transaction_id) is transaction
    assert register.status()["unmatched_proof_count"] == 1


def test_brain_transaction_register_closes_terminal_non_deferred_outcome_immediately():
    register = BrainTransactionRegister()
    closure = register.close_immediate(
        _executed_transaction(),
        proof={"reason": "action_failed"},
        grade="failed",
        learning_admission="blocked",
    )

    assert closure.transaction.stage is BrainTransactionStage.CLOSED
    assert closure.matched_by == "immediate_terminal_outcome"
    assert register.status()["pending_count"] == 0


def test_brain_transaction_status_never_scans_durable_journal(tmp_path, monkeypatch):
    register = BrainTransactionRegister(persistence_dir=tmp_path)
    register.close_immediate(
        _executed_transaction(),
        proof={"reason": "action_failed"},
        grade="failed",
        learning_admission="blocked",
    )
    assert register._journal is not None
    monkeypatch.setattr(
        register._journal,
        "stats",
        lambda: (_ for _ in ()).throw(AssertionError("full scan forbidden")),
    )
    monkeypatch.setattr(
        register._journal,
        "read_all",
        lambda: (_ for _ in ()).throw(AssertionError("full scan forbidden")),
    )

    status = register.status()

    assert status["journal"]["row_enumeration"] == "not_performed"
    assert status["journal"]["current_size_bytes"] > 0


def test_terminal_receipt_retains_correlation_without_full_decision_graph(tmp_path):
    transaction = _executed_transaction()
    full_bytes = len(json.dumps(transaction.to_event_payload()).encode("utf-8"))
    register = BrainTransactionRegister(persistence_dir=tmp_path)

    closure = register.close_immediate(
        transaction,
        proof={"reason": "action_failed", "large_private_graph": "x" * 4096},
        grade="failed",
        learning_admission="blocked",
    )
    receipt = register.status()["last_terminal_receipt"]
    compact = receipt["transaction"]

    assert closure.transaction.action_key is not None
    assert compact["action_transaction"]["root_id"] == closure.transaction.action_key.root_id
    assert compact["action_transaction"]["correlation_id"]
    assert compact["action_transaction"]["correlation_source"]
    assert compact["arbitration"] is None
    assert compact["proof"] == {}
    assert compact["candidates"] == []
    assert compact["execution"]["action_origin"] == ActionOriginKind.SMART_LOOP.value
    assert "operator_initiated" not in compact["execution"]
    assert "manual_action" not in compact["execution"]
    assert "brain_decision_receipt" not in compact["action_transaction"]
    assert "bandit_selection_receipt" not in compact["action_transaction"]
    assert "opportunity_id" not in compact["action_transaction"]
    assert len(json.dumps(compact).encode("utf-8")) < full_bytes

    restarted = BrainTransactionRegister(persistence_dir=tmp_path)
    restored = restarted.status()["last_terminal_receipt"]["transaction"]
    assert restored["action_transaction"]["root_id"] == closure.transaction.action_key.root_id
    assert restored["learning_admission"] == "blocked"


def test_complete_state_journal_compacts_and_compaction_failure_keeps_append(
    tmp_path,
    monkeypatch,
):
    import arch4.brain.transaction_register as transaction_register_module

    monkeypatch.setattr(transaction_register_module, "JOURNAL_COMPACTION_INTERVAL", 2)
    register = BrainTransactionRegister(persistence_dir=tmp_path)
    register.close_immediate(
        _executed_transaction(trace_id="trace-compact-1"),
        proof={"reason": "no_action"},
        grade="no_action",
        learning_admission="not_applicable",
    )
    register.close_immediate(
        _executed_transaction(trace_id="trace-compact-2"),
        proof={"reason": "no_action"},
        grade="no_action",
        learning_admission="not_applicable",
    )
    assert register._journal is not None
    assert register._journal.count() == 1

    def fail_rewrite(_events):
        raise OSError("compaction unavailable")

    monkeypatch.setattr(register._journal, "rewrite", fail_rewrite)
    register.close_immediate(
        _executed_transaction(trace_id="trace-compact-3"),
        proof={"reason": "no_action"},
        grade="no_action",
        learning_admission="not_applicable",
    )
    register.close_immediate(
        _executed_transaction(trace_id="trace-compact-4"),
        proof={"reason": "no_action"},
        grade="no_action",
        learning_admission="not_applicable",
    )
    assert "compaction unavailable" in register.status()["compaction_error"]

    restarted = BrainTransactionRegister(persistence_dir=tmp_path)
    assert restarted.status()["sequence"] == 4
    assert restarted._journal is not None
    assert restarted._journal.count() == 1


def test_durable_pending_and_terminal_receipts_survive_both_crash_gaps(tmp_path):
    class _InjectedCrash(RuntimeError):
        pass

    register = BrainTransactionRegister(persistence_dir=tmp_path)
    transaction = _executed_transaction()
    assert register._journal is not None
    append = register._journal.append

    def _crash_after_pending_append(event):
        append(event)
        raise _InjectedCrash("pending")

    register._journal.append = _crash_after_pending_append
    with pytest.raises(_InjectedCrash, match="pending"):
        register.register_executed(transaction, now=100.0)
    assert register.pending_transaction(transaction.transaction_id) is None

    restarted = BrainTransactionRegister(persistence_dir=tmp_path)
    restored = restarted.pending_transaction(transaction.transaction_id)
    assert restored is not None
    assert restored.stage is BrainTransactionStage.EXECUTED
    assert restored.facts is None
    assert dict(restored.readback) == {
        "chart_fresh": True,
        "target_confirmed": True,
        "write_accepted": True,
    }
    pending_receipt = restarted.pending_receipt(transaction.transaction_id)
    assert pending_receipt["status"] == "pending_terminal_referee"
    assert pending_receipt["terminal_due_ts"] == 700.0
    assert pending_receipt["timeout_at_ts"] == 1000.0

    assert restarted._journal is not None
    append = restarted._journal.append

    def _crash_after_terminal_append(event):
        append(event)
        raise _InjectedCrash("terminal")

    restarted._journal.append = _crash_after_terminal_append
    with pytest.raises(_InjectedCrash, match="terminal"):
        restarted.close_from_terminal_reward(_terminal_reward_proof())
    assert restarted.pending_transaction(transaction.transaction_id) is not None

    terminal_restart = BrainTransactionRegister(persistence_dir=tmp_path)
    assert terminal_restart.pending_transaction(transaction.transaction_id) is None
    assert terminal_restart.status()["reward_closed_count"] == 1
    duplicate = terminal_restart.close_from_terminal_reward(_terminal_reward_proof())
    assert duplicate is not None
    assert duplicate.duplicate is True
    assert terminal_restart.status()["closed_count"] == 1
    assert terminal_restart.status()["duplicate_terminal_count"] == 1


def test_runtime_requirement_binds_memory_persistence_without_composition_adapter(tmp_path):
    def _engine(register):
        brain = SimpleNamespace(
            transaction_register=register,
            memory=SimpleNamespace(memory_dir=tmp_path),
        )
        return SimpleNamespace(controller=SimpleNamespace(brain=brain))

    first = BrainTransactionRegister()
    require_brain_transaction_register(_engine(first)).register_executed(
        _executed_transaction(),
        now=100.0,
    )
    assert first.status()["durable"] is True

    restarted = BrainTransactionRegister()
    require_brain_transaction_register(_engine(restarted))
    assert restarted.status()["durable"] is True
    assert restarted.status()["pending_count"] == 1


def test_target_and_chart_readback_never_close_before_terminal_reward():
    register = BrainTransactionRegister()
    transaction = register.register_executed(_executed_transaction(), now=100.0)

    assert dict(transaction.readback)["target_confirmed"] is True
    assert dict(transaction.readback)["chart_fresh"] is True
    assert transaction.stage is BrainTransactionStage.EXECUTED
    assert register.status(now=699.0)["pending_count"] == 1


def test_terminal_rejection_and_timeout_close_without_reward():
    rejected_register = BrainTransactionRegister()
    rejected = rejected_register.register_executed(_executed_transaction(), now=100.0)
    rejected_proof = {
        "action_root_id": rejected.action_key.root_id,
        "parent_trace_id": "trace-17",
        "terminal": True,
        "grade_kind": "terminal_layered_reward",
        "roi_horizon_s": 600,
        "reward_admitted": False,
        "reward_ledger_key": "",
        "reason": "terminal_reward_not_admitted",
    }
    rejection = rejected_register.close_rejected_terminal(rejected_proof)
    assert rejection is not None
    assert rejection.terminal_kind == "rejected"
    assert rejection.reward_ledger_key == ""
    assert rejection.transaction.learning_admission == "blocked"
    assert rejected_register.pending_transaction(rejected.transaction_id) is None
    duplicate_rejection = rejected_register.close_rejected_terminal(
        {**rejected_proof, "received_ts": 701.0}
    )
    assert duplicate_rejection is not None
    assert duplicate_rejection.duplicate is True
    assert rejected_register.status()["closed_count"] == 1

    timeout_register = BrainTransactionRegister()
    timed = timeout_register.register_executed(_executed_transaction(trace_id="trace-timeout"), now=100.0)
    assert timeout_register.close_expired(now=999.0) == []
    closures = timeout_register.close_expired(now=1000.0)
    assert len(closures) == 1
    assert closures[0].terminal_kind == "timeout"
    assert closures[0].reward_ledger_key == ""
    assert closures[0].transaction.learning_admission == "blocked"
    assert timeout_register.pending_transaction(timed.transaction_id) is None


@pytest.mark.parametrize("terminal_kind", ("rejected", "timeout"))
def test_nonreward_terminal_receipt_is_durable(tmp_path, terminal_kind):
    register = BrainTransactionRegister(persistence_dir=tmp_path)
    transaction = register.register_executed(_executed_transaction(), now=100.0)
    if terminal_kind == "rejected":
        closure = register.close_rejected_terminal(
            {
                "action_root_id": transaction.action_key.root_id,
                "parent_trace_id": "trace-17",
                "terminal": True,
                "grade_kind": "terminal_layered_reward",
                "roi_horizon_s": 600,
                "reward_admitted": False,
                "reward_ledger_key": "",
                "reason": "terminal_reward_not_admitted",
            }
        )
        assert closure is not None
    else:
        closure = register.close_expired(now=1000.0)[0]
    assert closure.terminal_kind == terminal_kind

    restarted = BrainTransactionRegister(persistence_dir=tmp_path)
    assert restarted.pending_transaction(transaction.transaction_id) is None
    terminal = restarted.status()["last_terminal_receipt"]
    assert terminal["terminal_kind"] == terminal_kind
    assert terminal["reward_ledger_key"] == ""
    assert terminal["transaction"]["learning_admission"] == "blocked"


def test_static_transaction_lifecycle_has_only_canonical_close_callers():
    root = Path(__file__).resolve().parents[2]
    transaction_register = (
        root / "arch4/brain/transaction_register.py"
    ).read_text(encoding="utf-8")
    proof_register = (root / "arch4/brain/action_proof_register.py").read_text(
        encoding="utf-8"
    )
    assert not (root / "arch4/engine_tick_finalize.py").exists()
    assert "close_from_terminal_reward(row)" in proof_register
    assert "close_rejected_terminal(row)" in proof_register
    assert "close_from_proof(" not in proof_register

    status_body = transaction_register.split(
        "    def status(", 1
    )[1].split("\n    def ", 1)[0]
    terminal_receipt_body = transaction_register.split(
        "    def _terminal_receipt(", 1
    )[1].split("\n    @staticmethod", 1)[0]
    assert "self._journal.writer_status()" in status_body
    assert "self._journal.stats()" not in status_body
    assert "read_all(" not in status_body
    assert "iter_events(" not in status_body
    assert "transaction.to_event_payload()" not in terminal_receipt_body
    assert "self._compact_terminal_transaction(transaction)" in terminal_receipt_body

    reward_callers = []
    rejection_callers = []
    timeout_callers = []
    for path in (root / "arch4").rglob("*.py"):
        if path.name == "transaction_register.py" or "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if ".close_from_terminal_reward(" in source:
            reward_callers.append(path.relative_to(root).as_posix())
        if ".close_rejected_terminal(" in source:
            rejection_callers.append(path.relative_to(root).as_posix())
        if ".close_expired(" in source:
            timeout_callers.append(path.relative_to(root).as_posix())
    assert reward_callers == ["arch4/brain/action_proof_register.py"]
    assert rejection_callers == ["arch4/brain/action_proof_register.py"]
    assert timeout_callers == []


def test_action_proof_event_closes_registered_transaction_and_publishes_typed_events():
    events = []

    class _Logger:
        def log_event(self, kind, **payload):
            events.append((kind, payload))

    register = BrainTransactionRegister()
    transaction = register.register_executed(_executed_transaction(), now=100.0)
    journal = ForensicEventJournal(_Logger())
    feed_bank = BrainFeedRegisterBank()
    brain = type(
        "_Brain",
        (),
        {"transaction_register": register, "feed_registers": feed_bank},
    )()
    reporting = type("_Reporting", (), {"event_journal": journal})()
    engine = type(
        "_Engine",
        (),
        {
            "controller": type(
                "_Controller",
                (),
                {"brain": brain, "reporting": reporting},
            )(),
        },
        )()

    feedback = record_action_proof_feedback_event(
        engine,
        BrainEvent(
            kind="action_proof_feedback",
            payload={
                "action_root_id": transaction.action_key.root_id,
                "parent_trace_id": "trace-17",
                "roi_horizon_s": 60,
                "label": "neutral",
                "referee_code": "staged_roi_inconclusive",
                "terminal": False,
                "memory_admitted": False,
                "grade_kind": "smart_loop_interim",
                "evidence": {"grade_evidence_valid": True},
            },
            source="staged_roi",
            event_id="feedback-17-60",
        ),
        now=120.0,
    )

    assert feedback["terminal"] is False
    assert feedback["memory_admitted"] is False
    assert register.pending_transaction(transaction.transaction_id) is transaction
    assert events == []
    assert action_proof_surface_from_engine(engine)["feedback_count"] == 1

    record_action_proof_event(
        engine,
        BrainEvent(
            kind="action_proof_closed",
            payload={
                "action_root_id": transaction.action_key.root_id,
                "parent_trace_id": "trace-17",
                "label": "good",
                "referee_code": "roi_positive",
                "learning_commit_status": "proof_closed",
                "roi_horizon_s": 600,
                "terminal": True,
                "grade_kind": "terminal_layered_reward",
                "reward_ledger_key": "reward-17",
                "secondary_learning": {
                    "reward_admitted": True,
                    "reward_ledger_key": "reward-17",
                    "memory_committed": True,
                },
            },
            source="staged_roi",
            event_id="proof-17",
        ),
        now=130.0,
    )

    assert register.pending_transaction(transaction.transaction_id) is None
    assert [kind for kind, _payload in events] == [
        "controller_proof",
        "controller_learning",
    ]
    assert events[0][1]["transaction_id"] == transaction.transaction_id
    assert events[0][1]["matched_by"] == "action_root_id"

    record_action_proof_event(
        engine,
        BrainEvent(
            kind="action_proof_closed",
            payload={
                "action_root_id": transaction.action_key.root_id,
                "parent_trace_id": "trace-17",
                "label": "good",
                "referee_code": "roi_positive",
                "learning_commit_status": "proof_closed",
                "roi_horizon_s": 600,
                "terminal": True,
                "grade_kind": "terminal_layered_reward",
                "reward_ledger_key": "reward-17",
                "secondary_learning": {
                    "reward_admitted": True,
                    "reward_ledger_key": "reward-17",
                    "memory_committed": True,
                },
            },
            source="staged_roi",
            event_id="proof-17-retry",
        ),
        now=131.0,
    )
    assert len(events) == 2
    assert register.status()["closed_count"] == 1
    assert register.status()["duplicate_terminal_count"] == 1
