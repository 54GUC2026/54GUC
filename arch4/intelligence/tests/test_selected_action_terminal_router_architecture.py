from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from arch4.action_admission import bind_selected_action_context
from arch4.action_origin import ActionOriginKind
from arch4.action_transaction import (
    bind_action_transaction_key,
    build_action_transaction_key,
)
from arch4.action_transaction_authority import ActionTransactionAuthority
from arch4.brain.feed_registers import BrainFeedRegisterBank
from arch4.brain.selected_action_command_projection import (
    project_command_terminal_receipt,
)
from arch4.customer_contracts.selected_action import SelectedActionContract
from arch4.core.selected_action_register import SelectedActionStage
from arch4.core.selected_action_terminal_router import (
    record_selected_action_domain_result,
    route_selected_action_terminal,
)
from arch4.command_transactions import CommandAuthority
from arch4.tests.core_command_test_support import (
    apply_core_lever_authority_for_test,
)
from arch4.interfaces import Arch4Config
from arch4.tests.smart_loop_test_helpers import (
    claim_selected_action_for_test,
    publish_smart_loop_radio_command_for_test,
    terminalize_selected_action_for_test,
)


ROOT = Path(__file__).resolve().parents[2]


def _engine(
    authority: ActionTransactionAuthority | None = None,
) -> SimpleNamespace:
    action_transaction_authority = authority or ActionTransactionAuthority()
    return SimpleNamespace(
        _smart_loop_strategy_feed_port=BrainFeedRegisterBank(),
        action_transaction_authority=action_transaction_authority,
        controller=SimpleNamespace(
            brain=SimpleNamespace(),
            runtime=SimpleNamespace(
                command_authority=CommandAuthority(),
            ),
        ),
    )


def _publish_non_optimization(
    engine: SimpleNamespace,
    *,
    now: float = 100.0,
) -> ActionTransactionAuthority:
    publish_smart_loop_radio_command_for_test(
        engine,
        now=now,
        owner_class="maintenance",
    )
    authority = engine.action_transaction_authority
    claimed = claim_selected_action_for_test(
        authority,
        consumer="arch4.brain.decision_pipeline",
        now=now + 1.0,
    )
    assert claimed is not None
    return authority


def _selected_action(authority: ActionTransactionAuthority):
    transaction = authority.read_selected_transaction()
    assert transaction is not None
    return SelectedActionContract.from_mapping(transaction.selected_action)


def _command_terminal_without_selection_projection(
    engine: SimpleNamespace,
    authority: ActionTransactionAuthority,
    *,
    now: float,
):
    selected = _selected_action(authority)
    brain_receipt = selected.brain_receipt_dict()
    action = selected.to_lever_action()
    bind_selected_action_context(
        action,
        origin_kind=ActionOriginKind.SMART_LOOP,
        selection_evidence={"brain_decision_receipt": brain_receipt},
        opportunity_id=selected.opportunity_id,
        now=now,
    )
    action = apply_core_lever_authority_for_test(
        action,
        source="smart_loop_controller",
        config=Arch4Config(modem_backend="b818"),
        now=now,
    )
    source_generation = int(brain_receipt.get("source_sequence") or 0)
    action = bind_action_transaction_key(
        action,
        build_action_transaction_key(
            action,
            session_scope_key=str(
                brain_receipt.get("session_scope_key") or ""
            ),
            identity_key=(
                str(brain_receipt.get("identity_code") or "")
                + ":0x0000000000000002"
            ),
            source_generation=source_generation,
            source="smart_loop_controller",
            origin_kind=ActionOriginKind.SMART_LOOP.value,
        ),
    )
    command_authority = engine.controller.runtime.command_authority
    admission = command_authority.claim_cellular_action(
        action,
        execution_owner="selected_action_controller",
        now=now,
    )
    assert admission.lease is not None
    authority.handoff_to_command_authority(
        command_id=selected.command_id,
        action_key=admission.lease.transaction.action_key,
        receipt={"owner": "test"},
        now=now,
    )
    command_authority.mark_dispatched(admission.lease, now=now + 0.1)
    command_authority.acknowledge_modem_result(
        admission.lease,
        accepted=True,
        reason="accepted",
        physical_stages=(),
        physical_stage_error="test_physical_stage_not_exercised",
        now=now + 0.2,
    )
    command_authority.acknowledge_target_confirmed(
        root_id=admission.lease.root_id,
        source_generation=source_generation + 1,
        now=now + 0.3,
    )
    terminal = command_authority.acknowledge_settled(
        root_id=admission.lease.root_id,
        source_generation=source_generation + 2,
        now=now + 0.4,
    )
    return action, terminal


def test_non_optimization_terminal_waits_for_domain_result() -> None:
    engine = _engine()
    authority = _publish_non_optimization(engine)
    terminal = terminalize_selected_action_for_test(
        engine,
        now=102.0,
    )

    assert terminal.stage is SelectedActionStage.TERMINAL
    assert authority.selection_status()["active"]["stage"] == "terminal"

    action = SelectedActionContract.from_mapping(
        terminal.selected_action
    ).to_lever_action()
    routed = record_selected_action_domain_result(
        engine.action_transaction_authority,
        action=action,
        success=True,
        result_owner="arch4.brain.decision_pipeline.note_action_result",
        now=103.0,
    )

    assert routed.routed is True
    assert authority.selection_status()["active"] is None


def test_non_optimization_rejection_is_public_and_archived_once() -> None:
    engine = _engine()
    authority = _publish_non_optimization(engine)
    selected = _selected_action(authority)

    rejected = authority.reject_selection(
        command_id=selected.command_id,
        reason="outer_guard:mutating_inflight",
        now=102.0,
    )

    assert rejected.stage is SelectedActionStage.REJECTED
    assert authority.selection_status()["active"] is None
    public = next(
        row
        for row in authority.recent_receipts()
        if row.command_id == selected.command_id
    )
    assert public.stage.value == "rejected"
    assert "timed_out" not in public.timeline


def test_domain_result_after_restart_routes_same_terminal(tmp_path) -> None:
    first = ActionTransactionAuthority(persistence_dir=tmp_path)
    first_engine = _engine(first)
    _publish_non_optimization(first_engine)
    terminalize_selected_action_for_test(first_engine, now=102.0)

    restored = ActionTransactionAuthority(persistence_dir=tmp_path)
    restored_engine = _engine(restored)
    terminal = restored.read_terminal()
    assert terminal is not None
    action = SelectedActionContract.from_mapping(
        terminal.selected_action
    ).to_lever_action()

    routed = record_selected_action_domain_result(
        restored_engine.action_transaction_authority,
        action=action,
        success=True,
        result_owner="arch4.brain.decision_pipeline.note_action_result",
        now=103.0,
    )

    assert routed.routed is True
    assert restored.selection_status()["active"] is None


def test_terminal_after_domain_result_restart_routes_once(tmp_path) -> None:
    first = ActionTransactionAuthority(persistence_dir=tmp_path)
    first_engine = _engine(first)
    _publish_non_optimization(first_engine)
    action, terminal = _command_terminal_without_selection_projection(
        first_engine,
        first,
        now=102.0,
    )
    pending = record_selected_action_domain_result(
        first_engine.action_transaction_authority,
        action=action,
        success=True,
        result_owner="arch4.brain.decision_pipeline.note_action_result",
        now=102.5,
    )
    assert pending.routed is False
    assert first.selection_status()["active"]["stage"] == "handed_off"
    replay = record_selected_action_domain_result(
        first_engine.action_transaction_authority,
        action=action,
        success=True,
        result_owner="arch4.brain.decision_pipeline.note_action_result",
        now=102.7,
    )
    assert replay.routed is False
    assert replay.reason == "selected_action_not_terminal"

    restored = ActionTransactionAuthority(persistence_dir=tmp_path)
    restored_engine = _engine(restored)
    restored.accept_command_terminal(
        command_id=terminal.transaction.command_id,
        terminal_receipt=terminal,
    )
    assert project_command_terminal_receipt(restored_engine, terminal) is True
    assert restored.selection_status()["active"] is None


def test_old_generic_acknowledgement_hard_fails() -> None:
    engine = _engine()
    authority = _publish_non_optimization(engine)
    terminalize_selected_action_for_test(engine, now=102.0)

    with pytest.raises(
        RuntimeError,
        match="old_selected_terminal_acknowledgement_path_deleted",
    ):
        authority.acknowledge_terminal_consumed(
            command_id=_selected_action(authority).command_id
        )


def test_terminal_router_rejects_owner_without_lifecycle_port() -> None:
    with pytest.raises(
        TypeError,
        match="selected_action_terminal_authority_port_required",
    ):
        route_selected_action_terminal(SimpleNamespace(), now=100.0)


def test_static_terminal_acknowledgement_and_owner_routing_contract() -> None:
    production_paths = [
        path
        for path in (ROOT / "arch4").rglob("*.py")
        if "tests" not in path.parts
    ]
    non_optimization_calls: list[Path] = []
    optimization_calls: list[Path] = []
    old_calls: list[Path] = []
    for path in production_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if node.func.attr == "acknowledge_non_optimization_terminal":
                non_optimization_calls.append(path)
            elif node.func.attr == "acknowledge_optimization_terminal":
                optimization_calls.append(path)
            elif node.func.attr == "acknowledge_terminal_consumed":
                old_calls.append(path)

    assert non_optimization_calls == [
        ROOT / "arch4/core/selected_action_terminal_router.py"
    ]
    assert optimization_calls == [ROOT / "arch4/smart_loop/clock_lane.py"]
    assert set(old_calls) == {
        ROOT / "arch4/action_transaction_authority.py"
    }

    command_projection = (
        ROOT / "arch4/brain/selected_action_command_projection.py"
    ).read_text(encoding="utf-8")
    decision_pipeline = (
        ROOT / "arch4/brain/decision_pipeline.py"
    ).read_text(encoding="utf-8")
    authority = (ROOT / "arch4/action_transaction_authority.py").read_text(
        encoding="utf-8"
    )
    router = (
        ROOT / "arch4/core/selected_action_terminal_router.py"
    ).read_text(encoding="utf-8")
    assert "from arch4.action_transaction_authority import" not in router
    assert "ActionTransactionAuthority" not in router
    assert "SelectedActionTerminalAuthorityPort" in router
    assert "acknowledge_terminal_consumed" not in command_projection
    assert "acknowledge_terminal_consumed" not in decision_pipeline
    expire_body = authority.split("    def expire_overdue", 1)[1].split(
        "    def read_terminal", 1
    )[0]
    assert "ActionLifecycleStage.TIMED_OUT" not in expire_body
