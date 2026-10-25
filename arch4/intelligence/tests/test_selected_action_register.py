"""Core selected-action register and optional customer handoff proof."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import arch4.core.selected_action_register as selected_action_register_module

from arch4.action_session_scope import CurrentSessionActionScope
from arch4.action_admission import bind_selected_action_context
from arch4.action_origin import ActionOriginKind
from arch4.action_transaction import (
    bind_action_transaction_key,
    build_action_transaction_key,
)
from arch4.brain.feed_registers import BrainFeedRegisterBank
from arch4.customer_contracts.selected_action import SelectedActionContract
from arch4.core.selected_action_register import (
    ARCHIVE_SCHEMA,
    SelectedActionClaimContext,
    SelectedActionStage,
    SelectedActionTransaction,
    DEFAULT_CLAIM_LEASE_S,
    JOURNAL_SCHEMA,
    MAX_SELECTED_ACTION_JOURNAL_FILE_BYTES,
    MAX_SELECTED_ACTION_JOURNAL_ROW_BYTES,
    SelectedActionArchiveReceipt,
    create_selected_action_store,
)
from arch4.action_transaction_authority import ActionTransactionAuthority
from arch4.brain.selected_action_command_projection import (
    project_command_terminal_receipt,
    reconcile_selected_action_command,
)
from arch4.command_transactions import (
    COMMAND_AUTHORITY_OWNER,
    CommandAuthority,
    CommandTerminalStatus,
)
from arch4.tests.core_command_test_support import (
    apply_core_lever_authority_for_test,
)
from arch4.interfaces import Arch4Config, LeverAction, LeverType
from arch4.memory_store.event_journal import JsonlEventJournal
from arch4.smart_loop.clock_lane import (
    _apply_optimization_selected_action_receipt,
)
from arch4.smart_loop.closed_loop import SmartLoopState
from arch4.tests.smart_loop_test_helpers import (
    publish_smart_loop_radio_command_for_test,
)


ROOT = Path(__file__).resolve().parents[2]


def _selected_transaction(register: ActionTransactionAuthority):
    store = getattr(register, "_selection_store", None)
    return getattr(store, "_active", None)


def _claim_context(
    register: ActionTransactionAuthority,
    *,
    now: float,
    **changes,
) -> SelectedActionClaimContext:
    transaction = _selected_transaction(register)
    assert transaction is not None
    selected = SelectedActionContract.from_mapping(
        transaction.selected_action
    )
    receipt = selected.brain_receipt_dict()
    identity_code = str(
        changes.get(
            "identity_code",
            receipt.get("identity_code") or "",
        )
    )
    identity_status = (
        "exact"
        if identity_code not in {"", "0x0000000000000000"}
        else "unknown"
    )
    scope = CurrentSessionActionScope(
        session_scope_key=str(
            changes.get(
                "session_scope_key",
                receipt.get("session_scope_key") or "",
            )
        ),
        source_epoch=str(
            changes.get(
                "source_epoch",
                receipt.get("source_epoch") or "",
            )
        ),
        source_sequence=int(
            changes.get(
                "source_sequence",
                receipt.get("source_sequence") or 0,
            )
        ),
        observed_at=float(now),
        evidence_age_s=0.0,
        identity_code=(
            identity_code or "0x0000000000000000"
        ),
        identity_status=identity_status,
        identity_catalog_version=(
            "test-catalog-v1" if identity_status == "exact" else ""
        ),
        identity_catalog_checksum=(
            "a" * 64 if identity_status == "exact" else ""
        ),
        access_type_id=(
            "0x0000000000000002"
            if identity_status == "exact"
            else "0x0000000000000000"
        ),
    )
    return SelectedActionClaimContext(
        action_scope=scope,
        context_key=str(
            changes.get(
                "context_key",
                receipt.get("action_context_key") or "",
            )
        ),
    )


def _claim(
    register: ActionTransactionAuthority,
    *,
    consumer: str,
    now: float,
    lease_s: float | None = None,
    **scope_changes,
):
    return register.claim(
        consumer=consumer,
        claim_context=_claim_context(
            register,
            now=now,
            **scope_changes,
        ),
        now=now,
        lease_s=lease_s,
    )


def _engine_with_register(
    register: ActionTransactionAuthority | None = None,
) -> SimpleNamespace:
    selected_register = register or ActionTransactionAuthority()
    command_authority = CommandAuthority()
    return SimpleNamespace(
        _smart_loop_strategy_feed_port=BrainFeedRegisterBank(),
        action_transaction_authority=selected_register,
        controller=SimpleNamespace(
            brain=SimpleNamespace(),
            runtime=SimpleNamespace(
                command_authority=command_authority,
            ),
        ),
    )


def _published_register(
    *,
    register: ActionTransactionAuthority | None = None,
    now: float = 100.0,
    target_band: int = 3,
) -> tuple[SimpleNamespace, ActionTransactionAuthority]:
    engine = _engine_with_register(register)
    publish_smart_loop_radio_command_for_test(
        engine,
        now=now,
        target_band=target_band,
        target_bands=(1, target_band),
    )
    return engine, engine.action_transaction_authority


def _selected_action_and_proof() -> tuple[dict, dict]:
    _engine, register = _published_register()
    transaction = _claim(
        register,
        consumer="fixture",
        now=101.0,
    )
    assert transaction is not None
    return (
        dict(transaction.selected_action),
        dict(transaction.proof_template),
    )


def _terminal_selected_action_transaction() -> SelectedActionTransaction:
    selected_action, proof = _selected_action_and_proof()
    return SelectedActionTransaction(
        selected_action=selected_action,
        proof_template=proof,
        stage=SelectedActionStage.REJECTED,
        published_ts=100.0,
        terminal_ts=101.0,
        terminal_reason="test_terminal_rejection",
    )


def _v4_journal_row(
    transaction: SelectedActionTransaction,
) -> dict:
    row = {
        "schema": "SelectedActionStoreEvent.v4",
        "owner": "arch4.action_transaction_authority",
        "event": "terminal_acknowledged",
        "sequence": 4,
        "active": None,
        "recent": [
            transaction.as_dict(include_selected_action=True)
        ],
        "counters": {
            "published_count": 1,
            "claimed_count": 0,
            "reclaimed_count": 0,
            "handed_off_count": 0,
            "terminal_count": 0,
            "rejected_count": 1,
            "context_rejected_count": 0,
            "duplicate_count": 0,
        },
    }
    encoded = json.dumps(
        row,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    row["checksum"] = hashlib.sha256(
        encoded.encode("utf-8")
    ).hexdigest()
    return row


def _action(command_id: str) -> LeverAction:
    action = LeverAction(
        lever=LeverType.PBAND,
        params={
            "pband": 3,
            "ca_pool": [1, 3],
            "_selected_action_command": True,
            "_selected_action_command_id": command_id,
            "_selected_action_owner_class": "optimization",
        },
    )
    action.command_id = command_id
    return action


def _settle_selected_command(
    engine: SimpleNamespace,
    register: ActionTransactionAuthority,
    *,
    now: float = 102.0,
):
    claimed = _selected_transaction(register)
    assert claimed is not None
    assert claimed.stage is SelectedActionStage.CLAIMED
    selected = SelectedActionContract.from_mapping(
        claimed.selected_action
    )
    receipt = selected.brain_receipt_dict()
    action = selected.to_lever_action()
    bind_selected_action_context(
        action,
        origin_kind=ActionOriginKind.SMART_LOOP,
        selection_evidence={
            "brain_decision_receipt": receipt,
            "contextual_bandit_selection_receipt": selected.bandit_receipt_dict(),
        },
        opportunity_id=selected.opportunity_id,
        now=now,
    )
    action = apply_core_lever_authority_for_test(
        action,
        source="smart_loop_controller",
        config=Arch4Config(modem_backend="b818"),
        now=now,
    )
    source_generation = int(receipt.get("source_sequence") or 0)
    action = bind_action_transaction_key(
        action,
        build_action_transaction_key(
            action,
            session_scope_key=str(
                receipt.get("session_scope_key") or ""
            ),
            identity_key=(
                str(receipt.get("identity_code") or "")
                + ":0x0000000000000002"
            ),
            source_generation=source_generation,
            source="smart_loop_controller",
            origin_kind=ActionOriginKind.SMART_LOOP.value,
        ),
    )
    authority = engine.controller.runtime.command_authority
    admission = authority.claim_cellular_action(
        action,
        execution_owner="selected_action_controller",
        now=now,
    )
    assert admission.lease is not None
    register.handoff_to_command_authority(
        command_id=claimed.command_id,
        action_key=admission.lease.transaction.action_key,
        receipt={"owner": "test"},
        now=now,
    )
    authority.mark_dispatched(admission.lease, now=now + 0.1)
    authority.acknowledge_modem_result(
        admission.lease,
        accepted=True,
        reason="modem_command_accepted",
        physical_stages=(),
        physical_stage_error="test_physical_stage_not_exercised",
        now=now + 0.2,
    )
    authority.acknowledge_target_confirmed(
        root_id=admission.lease.root_id,
        source_generation=source_generation + 1,
        now=now + 0.3,
    )
    terminal = authority.acknowledge_settled(
        root_id=admission.lease.root_id,
        source_generation=source_generation + 2,
        now=now + 0.4,
    )
    lifecycle_receipt = register.accept_command_terminal(
        command_id=claimed.command_id,
        terminal_receipt=terminal,
    )
    assert lifecycle_receipt.stage.value == "stream_settled"
    assert project_command_terminal_receipt(engine, terminal) is True
    selected_terminal = register.read_terminal()
    assert selected_terminal is not None
    return selected_terminal


def test_selection_stays_latched_through_handoff_until_terminal():
    engine, register = _published_register()
    claimed = _claim(
        register,
        consumer="arch4.brain.decision_pipeline",
        now=101.0,
    )
    assert claimed is not None
    assert claimed.stage is SelectedActionStage.CLAIMED

    terminal = _settle_selected_command(engine, register)
    assert terminal.stage is SelectedActionStage.TERMINAL
    command_outcome = terminal.terminal_outcome()
    assert command_outcome is not None
    assert command_outcome.terminal_status is (
        CommandTerminalStatus.SETTLED
    )
    assert command_outcome.proof_ready is True
    register.acknowledge_optimization_terminal(
        command_id=terminal.command_id
    )
    assert register.selection_status()["active"] is None


def test_learning_context_cannot_be_used_as_action_claim_context():
    _engine, register = _published_register()
    transaction = _selected_transaction(register)
    assert transaction is not None
    selected = SelectedActionContract.from_mapping(transaction.selected_action)
    receipt = selected.brain_receipt_dict()
    assert receipt["learning_context_key"]
    assert receipt["action_context_key"]
    assert receipt["learning_context_key"] != receipt["action_context_key"]

    claimed = _claim(
        register,
        consumer="arch4.brain.decision_pipeline",
        now=101.0,
    )

    assert claimed is not None
    assert claimed.stage is SelectedActionStage.CLAIMED


def test_selection_store_retains_only_bounded_command_outcome():
    engine, register = _published_register()
    claimed = _claim(register, consumer="controller", now=101.0)
    assert claimed is not None

    terminal = _settle_selected_command(engine, register)
    outcome = terminal.terminal_outcome()
    assert outcome is not None
    retained = outcome.as_dict()

    assert retained["source_owner"] == COMMAND_AUTHORITY_OWNER
    assert retained["terminal_status"] == "settled"
    assert retained["proof_ready"] is True
    assert len(retained["command_receipt_checksum"]) == 64
    assert "transaction" not in retained
    assert "action_transaction" not in retained
    assert "receipts" not in retained
    assert len(json.dumps(retained, sort_keys=True)) < 4096


def test_deleted_full_terminal_receipt_field_fails_loudly():
    engine, register = _published_register()
    claimed = _claim(register, consumer="controller", now=101.0)
    assert claimed is not None
    terminal = _settle_selected_command(engine, register)
    persisted = terminal.as_dict(include_selected_action=True)
    persisted["command_terminal_receipt"] = {}

    with pytest.raises(
        ValueError,
        match="selected_action_full_terminal_receipt_deleted",
    ):
        SelectedActionTransaction.from_dict(persisted)


def test_selection_cannot_be_rejected_after_command_handoff():
    engine, register = _published_register()
    claimed = _claim(register, consumer="controller", now=101.0)
    assert claimed is not None
    terminal = _settle_selected_command(engine, register)
    assert terminal.stage is SelectedActionStage.TERMINAL
    with pytest.raises(
        RuntimeError,
        match="rejection_after_terminal",
    ):
        register.reject_selection(
            command_id=terminal.command_id,
            reason="late_competing_owner",
        )


def test_rejected_selection_is_terminal_and_not_republishable():
    selected_action, proof = _selected_action_and_proof()
    register = ActionTransactionAuthority()
    register.publish(
        selected_action=selected_action,
        proof_template=proof,
        now=100.0,
    )
    claimed = _claim(register, consumer="controller", now=101.0)
    assert claimed is not None
    rejected = register.reject_selection(
        command_id=claimed.command_id,
        reason="action_availability_blocked",
        now=102.0,
    )
    assert rejected.stage is SelectedActionStage.REJECTED
    register.acknowledge_optimization_terminal(
        command_id=rejected.command_id
    )

    duplicate = register.publish(
        selected_action=selected_action,
        proof_template=proof,
        now=103.0,
    )
    assert duplicate.accepted is False
    assert duplicate.duplicate is True


def test_rejected_optimization_action_creates_hold_without_proof():
    engine, register = _published_register()
    claimed = _claim(
        register,
        consumer="arch4.brain.decision_pipeline",
        now=101.0,
    )
    assert claimed is not None
    register.reject_selection(
        command_id=claimed.command_id,
        reason="outer_guard:mutating_inflight",
        receipt={"remaining_s": 30.0},
        now=102.0,
    )

    state = _apply_optimization_selected_action_receipt(
        engine,
        state=SmartLoopState(),
        now=102.1,
    )
    assert state.pending_proof is None
    hold = next(iter(state.objective_holds.values()))
    assert hold.until_ts == 132.0


def test_settled_command_opens_roi_proof_after_physical_lease_is_free():
    engine, register = _published_register()
    claimed = _claim(
        register,
        consumer="arch4.brain.decision_pipeline",
        now=101.0,
    )
    assert claimed is not None
    terminal = _settle_selected_command(engine, register)
    assert (
        engine.controller.runtime.command_authority.active_lifecycle()
        is None
    )

    state = _apply_optimization_selected_action_receipt(
        engine,
        state=SmartLoopState(),
        now=103.0,
    )
    assert state.pending_proof is not None
    outcome = terminal.terminal_outcome()
    assert outcome is not None
    assert state.pending_proof.opened_ts == outcome.settled_ts


def test_terminal_projection_recovers_crash_gap_from_command_authority():
    engine, register = _published_register()
    claimed = _claim(
        register,
        consumer="arch4.brain.decision_pipeline",
        now=101.0,
    )
    assert claimed is not None
    selected = SelectedActionContract.from_mapping(
        claimed.selected_action
    )
    brain_receipt = selected.brain_receipt_dict()
    source_generation = int(
        brain_receipt.get("source_sequence") or 0
    )
    action = selected.to_lever_action()
    bind_selected_action_context(
        action,
        origin_kind=ActionOriginKind.SMART_LOOP,
        selection_evidence={
            "brain_decision_receipt": brain_receipt,
            "contextual_bandit_selection_receipt": selected.bandit_receipt_dict(),
        },
        opportunity_id=selected.opportunity_id,
        now=102.0,
    )
    action = apply_core_lever_authority_for_test(
        action,
        source="smart_loop_controller",
        config=Arch4Config(modem_backend="b818"),
        now=102.0,
    )
    action = bind_action_transaction_key(
        action,
        build_action_transaction_key(
            action,
            session_scope_key=str(
                brain_receipt.get("session_scope_key") or ""
            ),
            identity_key="test-recovery-identity",
            source_generation=source_generation,
            source="smart_loop_controller",
            origin_kind=ActionOriginKind.SMART_LOOP.value,
        ),
    )
    authority = engine.controller.runtime.command_authority
    admission = authority.claim_cellular_action(
        action,
        execution_owner="test_controller",
        now=102.0,
    )
    assert admission.lease is not None
    authority.mark_dispatched(admission.lease, now=102.1)
    authority.acknowledge_modem_result(
        admission.lease,
        accepted=True,
        reason="accepted",
        physical_stages=(),
        physical_stage_error="test_physical_stage_not_exercised",
        now=102.2,
    )
    authority.acknowledge_target_confirmed(
        root_id=admission.lease.root_id,
        source_generation=source_generation + 1,
        now=102.3,
    )
    authority.acknowledge_settled(
        root_id=admission.lease.root_id,
        source_generation=source_generation + 2,
        now=102.4,
    )
    assert register.selection_status()["active"]["stage"] == "claimed"

    assert reconcile_selected_action_command(engine) is True
    assert register.selection_status()["active"]["stage"] == "terminal"


def test_selection_owner_rejects_only_before_handoff():
    engine, register = _published_register()
    claimed = _claim(
        register,
        consumer="arch4.brain.decision_pipeline",
        now=101.0,
    )
    assert claimed is not None
    register.reject_selection(
        command_id=claimed.command_id,
        reason="outer_guard:mutating_inflight",
        receipt={"remaining_s": 15},
        now=102.0,
    )
    active = register.selection_status()["active"]
    assert active["stage"] == "rejected"
    assert active["terminal_reason"] == (
        "outer_guard:mutating_inflight"
    )


def test_claim_lease_recovers_only_after_expiry():
    selected_action, proof = _selected_action_and_proof()
    register = ActionTransactionAuthority()
    register.publish(
        selected_action=selected_action,
        proof_template=proof,
        now=100.0,
    )
    first = _claim(register, consumer="first", now=101.0)
    assert first is not None
    with pytest.raises(
        RuntimeError,
        match="selected_action_claimed_by:first",
    ):
        _claim(
            register,
            consumer="second",
            now=101.0 + DEFAULT_CLAIM_LEASE_S - 0.1,
        )
    recovered = _claim(
        register,
        consumer="second",
        now=101.0 + DEFAULT_CLAIM_LEASE_S,
    )
    assert recovered is not None
    assert recovered.claim_attempts == 2


def test_durable_register_restores_handoff_not_physical_stages(tmp_path):
    selected_action, proof = _selected_action_and_proof()
    first = ActionTransactionAuthority(persistence_dir=tmp_path)
    first.publish(
        selected_action=selected_action,
        proof_template=proof,
        now=100.0,
    )
    claimed = _claim(first, consumer="controller", now=101.0)
    assert claimed is not None
    selected = SelectedActionContract.from_mapping(
        claimed.selected_action
    )
    receipt = selected.brain_receipt_dict()
    action = selected.to_lever_action()
    bind_selected_action_context(
        action,
        origin_kind=ActionOriginKind.SMART_LOOP,
        selection_evidence={
            "brain_decision_receipt": receipt,
            "contextual_bandit_selection_receipt": selected.bandit_receipt_dict(),
        },
        opportunity_id=selected.opportunity_id,
        now=102.0,
    )
    action = apply_core_lever_authority_for_test(
        action,
        source="smart_loop_controller",
        config=Arch4Config(modem_backend="b818"),
        now=102.0,
    )
    action = bind_action_transaction_key(
        action,
        build_action_transaction_key(
            action,
            session_scope_key=str(
                receipt.get("session_scope_key") or ""
            ),
            identity_key="test-identity",
            source_generation=int(
                receipt.get("source_sequence") or 0
            ),
            source="smart_loop_controller",
            origin_kind=ActionOriginKind.SMART_LOOP.value,
        ),
    )
    key = build_action_transaction_key(
        action,
        session_scope_key=str(
            receipt.get("session_scope_key") or ""
        ),
        identity_key="test-identity",
        source_generation=int(receipt.get("source_sequence") or 0),
        source="smart_loop_controller",
        origin_kind=ActionOriginKind.SMART_LOOP.value,
    )
    first.handoff_to_command_authority(
        command_id=claimed.command_id,
        action_key=key,
        now=102.0,
    )

    restored = ActionTransactionAuthority(persistence_dir=tmp_path)
    active = restored.selection_status()["active"]
    assert active["stage"] == "handed_off"
    assert active["command_root_id"] == key.root_id


@pytest.mark.parametrize(
    ("scope_change", "reason"),
    (
        (
            {"identity_code": "0x0000000000000099"},
            "identity_code_changed",
        ),
        (
            {"session_scope_key": "replacement-session"},
            "session_scope_changed",
        ),
        (
            {"source_epoch": "replacement-epoch"},
            "source_epoch_changed",
        ),
        (
            {"context_key": "replacement-context"},
            "brain_context_changed",
        ),
    ),
)
def test_claim_rejects_selection_outside_current_context(
    scope_change,
    reason,
):
    _engine, register = _published_register()
    claimed = _claim(
        register,
        consumer="controller",
        now=101.0,
        **scope_change,
    )
    assert claimed is None
    active = register.selection_status()["active"]
    assert active["terminal_reason"] == (
        f"claim_context_rejected:{reason}"
    )
    public = next(
        row
        for row in register.recent_receipts()
        if row.command_id == active["command_id"]
    )
    assert public.stage.value == "rejected"
    assert "timed_out" not in public.timeline


def test_register_has_no_physical_command_lifecycle_methods():
    source = (
        ROOT / "arch4" / "core" / "selected_action_register.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "def mark_dispatched(",
        "def acknowledge_write(",
        "def acknowledge_target_confirmed(",
        "def acknowledge_chart_fresh(",
        "def acknowledge_proof_open(",
    )
    assert [token for token in forbidden if token in source] == []
    assert "def handoff_to_command_authority(" in source
    assert "def accept_command_terminal(" in source


def test_strategy_feed_cannot_be_action_transport():
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "arch4").rglob("*.py")
        if "tests" not in path.parts
    )
    assert "class CommandEnvelope" not in production
    assert "smart_loop.command_envelope.v1" not in production
    assert not (
        ROOT / "arch4" / "smart_loop" / "command_register.py"
    ).exists()


def test_terminal_archive_is_compact_and_duplicate_has_no_full_transaction():
    selected_action, proof = _selected_action_and_proof()
    store = create_selected_action_store()
    published = store.publish(
        selected_action=selected_action,
        proof_template=proof,
        now=100.0,
    )
    assert published.transaction is not None
    store.reject_selection(
        command_id=published.transaction.command_id,
        reason="test_rejection",
        now=101.0,
    )
    store.acknowledge_terminal_consumed(
        command_id=published.transaction.command_id
    )

    status = store.status()
    archive = status["recent"][-1]
    assert archive["schema"] == ARCHIVE_SCHEMA
    assert len(json.dumps(archive, sort_keys=True)) < 4096
    assert "selected_action" not in archive
    assert "proof_template" not in archive
    assert "brain_decision_receipt" not in archive
    assert "contextual_bandit_selection_receipt" not in archive

    duplicate = store.publish(
        selected_action=selected_action,
        proof_template=proof,
        now=102.0,
    )
    assert duplicate.accepted is False
    assert duplicate.transaction is None
    assert duplicate.archive_receipt is not None
    assert duplicate.archive_receipt.receipt_id == archive["receipt_id"]


def test_same_command_with_changed_content_fails_for_active_and_archive():
    engine = _engine_with_register()
    publish_smart_loop_radio_command_for_test(
        engine,
        now=100.0,
        owner_class="maintenance",
    )
    transaction = _selected_transaction(
        engine.action_transaction_authority
    )
    assert transaction is not None
    selected = SelectedActionContract.from_mapping(transaction.selected_action)
    changed = replace(
        selected,
        reason=selected.reason + ":changed-content",
    ).as_dict()

    active_store = create_selected_action_store()
    active_store.publish(
        selected_action=transaction.selected_action,
        proof_template={},
        now=100.0,
    )
    with pytest.raises(
        RuntimeError,
        match="selected_action_active_content_collision",
    ):
        active_store.publish(
            selected_action=changed,
            proof_template={},
            now=101.0,
        )

    archive_store = create_selected_action_store()
    published = archive_store.publish(
        selected_action=transaction.selected_action,
        proof_template={},
        now=100.0,
    )
    assert published.transaction is not None
    archive_store.reject_selection(
        command_id=published.transaction.command_id,
        reason="test_rejection",
        now=101.0,
    )
    archive_store.acknowledge_terminal_consumed(
        command_id=published.transaction.command_id
    )
    with pytest.raises(
        RuntimeError,
        match="selected_action_archive_content_collision",
    ):
        archive_store.publish(
            selected_action=changed,
            proof_template={},
            now=102.0,
        )


def test_maximum_archive_state_stays_under_v5_row_budget():
    terminal = _terminal_selected_action_transaction()
    base = SelectedActionArchiveReceipt.from_transaction(terminal)
    recent = deque(
        (
            replace(base, command_id=f"test-command-{index}")
            for index in range(64)
        ),
        maxlen=64,
    )
    store = create_selected_action_store(max_recent=64)
    payload = type(store)._journal_payload(
        event="budget_proof",
        sequence=1,
        active=None,
        recent=recent,
        counters=store._counters,
    )
    row_bytes = len(
        (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
    )

    assert row_bytes < MAX_SELECTED_ACTION_JOURNAL_ROW_BYTES
    assert len(payload["recent"]) == 64
    assert all("selected_action" not in row for row in payload["recent"])
    assert all("proof_template" not in row for row in payload["recent"])


def test_repeated_duplicate_transitions_remain_byte_bounded(tmp_path):
    selected_action, proof = _selected_action_and_proof()
    store = create_selected_action_store(persistence_dir=tmp_path)
    published = store.publish(
        selected_action=selected_action,
        proof_template=proof,
        now=100.0,
    )
    assert published.transaction is not None
    store.reject_selection(
        command_id=published.transaction.command_id,
        reason="test_rejection",
        now=101.0,
    )
    store.acknowledge_terminal_consumed(
        command_id=published.transaction.command_id
    )

    for index in range(200):
        duplicate = store.publish(
            selected_action=selected_action,
            proof_template=proof,
            now=102.0 + index,
        )
        assert duplicate.accepted is False

    journal_path = tmp_path / "selected_action_store.jsonl"
    rows = journal_path.read_text(encoding="utf-8").splitlines()
    assert 1 <= len(rows) <= 32
    assert journal_path.stat().st_size < (
        MAX_SELECTED_ACTION_JOURNAL_FILE_BYTES // 8
    )
    assert store.status()["compaction_count"] >= 6


def test_restore_uses_reverse_reader_without_full_materialization(
    tmp_path,
    monkeypatch,
):
    selected_action, proof = _selected_action_and_proof()
    first = create_selected_action_store(persistence_dir=tmp_path)
    first.publish(
        selected_action=selected_action,
        proof_template=proof,
        now=100.0,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("full_journal_materialization_forbidden")

    monkeypatch.setattr(JsonlEventJournal, "read_all", forbidden)
    monkeypatch.setattr(JsonlEventJournal, "iter_events", forbidden)
    restored = create_selected_action_store(persistence_dir=tmp_path)

    assert restored.status()["active"]["command_id"] == (
        selected_action["command_id"]
    )
    assert restored.status()["restore_error"] == ""


def test_compaction_failure_keeps_appended_truth_and_reports_fault(
    tmp_path,
    monkeypatch,
):
    selected_action, proof = _selected_action_and_proof()
    store = create_selected_action_store(persistence_dir=tmp_path)
    assert store._journal is not None

    def fail_rewrite(_events):
        raise OSError("test_compaction_failure")

    monkeypatch.setattr(
        selected_action_register_module,
        "JOURNAL_COMPACTION_INTERVAL",
        1,
    )
    monkeypatch.setattr(store._journal, "rewrite", fail_rewrite)
    store.publish(
        selected_action=selected_action,
        proof_template=proof,
        now=100.0,
    )

    assert "test_compaction_failure" in store.status()["compaction_error"]
    assert store.status()["active"]["command_id"] == (
        selected_action["command_id"]
    )
    restored = create_selected_action_store(persistence_dir=tmp_path)
    assert restored.status()["active"]["command_id"] == (
        selected_action["command_id"]
    )


def test_v4_schema_is_rejected_without_decoder_or_rewrite(tmp_path):
    terminal = _terminal_selected_action_transaction()
    journal = JsonlEventJournal(tmp_path, "selected_action_store.jsonl")
    journal.append(_v4_journal_row(terminal))
    original = journal.path.read_bytes()

    store = create_selected_action_store(persistence_dir=tmp_path)
    status = store.status()
    assert status["persistence_write_blocked"] is True
    assert status["restore_error"] == (
        "illegal_selected_action_journal_schema:SelectedActionStoreEvent.v4"
    )
    assert journal.path.read_bytes() == original
    with pytest.raises(RuntimeError, match="selected_action_persistence_blocked"):
        store.publish(
            selected_action=terminal.selected_action,
            proof_template=terminal.proof_template,
            now=102.0,
        )


def test_v4_after_v5_generation_is_blocked_not_migrated(tmp_path):
    selected_action, proof = _selected_action_and_proof()
    first = create_selected_action_store(persistence_dir=tmp_path)
    first.publish(
        selected_action=selected_action,
        proof_template=proof,
        now=100.0,
    )
    journal = JsonlEventJournal(tmp_path, "selected_action_store.jsonl")
    journal.append(_v4_journal_row(_terminal_selected_action_transaction()))

    blocked = create_selected_action_store(persistence_dir=tmp_path)
    status = blocked.status()
    assert status["persistence_write_blocked"] is True
    assert status["restore_error"] == (
        "illegal_selected_action_journal_schema:SelectedActionStoreEvent.v4"
    )
    with pytest.raises(
        RuntimeError,
        match="selected_action_persistence_blocked",
    ):
        blocked.publish(
            selected_action=selected_action,
            proof_template=proof,
            now=101.0,
        )


def test_invalid_v5_never_falls_back_to_v4_migration(tmp_path):
    terminal = _terminal_selected_action_transaction()
    journal = JsonlEventJournal(tmp_path, "selected_action_store.jsonl")
    journal.append(_v4_journal_row(terminal))
    journal.append(
        {
            "schema": JOURNAL_SCHEMA,
            "owner": "arch4.action_transaction_authority",
            "event": "corrupt_v5",
            "sequence": 5,
            "active": None,
            "recent": [],
            "counters": {},
            "checksum": "invalid",
        }
    )

    blocked = create_selected_action_store(persistence_dir=tmp_path)
    status = blocked.status()
    assert status["persistence_write_blocked"] is True
    assert status["restore_error"] == "no_valid_selected_action_v5_event"


def test_selected_action_journal_static_audit_forbids_old_runtime_paths():
    selected_source = (
        ROOT / "arch4" / "core" / "selected_action_register.py"
    ).read_text(encoding="utf-8")
    store_source = selected_source.split("class _SelectedActionStore", 1)[1]
    clock_source = (
        ROOT / "arch4" / "smart_loop" / "clock_lane.py"
    ).read_text(encoding="utf-8")

    assert ".iter_events(" not in store_source
    assert ".read_all(" not in store_source
    assert "V4_JOURNAL_SCHEMA" not in store_source
    assert '"SelectedActionStoreEvent.v4"' not in selected_source
    assert "item.as_dict(include_selected_action=True)" not in store_source
    assert "result.transaction.terminal_reason" not in clock_source
    assert "SelectedActionStoreV4ReadOnlyMigrationAdapter" not in selected_source
    assert "iter_events_reverse(" in store_source
