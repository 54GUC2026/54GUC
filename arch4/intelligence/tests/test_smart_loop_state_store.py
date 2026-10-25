from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from arch4.smart_loop.clock_lane import (
    _apply_optimization_selected_action_receipt,
    _persist_smart_loop_state_and_ack_terminal,
)
from arch4.smart_loop.closed_loop import (
    ObjectiveHold,
    ModemContext,
    ProofWindow,
    SmartLoopObservation,
    SmartLoopState,
)
from arch4.action_transaction_authority import ActionTransactionAuthority
from arch4.brain.feed_registers import BrainFeedRegisterBank
from arch4.smart_loop.state_store import SmartLoopStateStore
from arch4.tests.smart_loop_test_helpers import (
    claim_selected_action_for_test,
    publish_smart_loop_radio_command_for_test,
    terminalize_selected_action_for_test,
)


CONTEXT = "0x0000000000000001|22005|2050|255|1795|6||||"


def _state(*, observed_ts: float = 100.0, proof: bool = False) -> SmartLoopState:
    pending = None
    if proof:
        pending = ProofWindow(
            action="APPLY_PBAND_CA_POOL",
            lever="PBAND",
            target_shape={
                "pband": 3,
                "ca_pool": [1, 3, 20],
                "dl_bands": [1, 20],
            },
            target_band=3,
            target_bands=(1, 3, 20),
            context_key=CONTEXT,
            reason_ref="learned_anchor_outperforms_current",
            proof_id="proof_test_1",
            command_id="command_test_1",
            before_cq=0.4,
            before_congestion_pct=70.0,
            opened_ts=101.0,
        )
    hold_key = f"{CONTEXT}|apply_radio_configuration|B20"
    return SmartLoopState(
        pending_proof=pending,
        observations=(
            SmartLoopObservation(
                ts=observed_ts,
                pband=3,
                cq=0.72,
                congestion_pct=24.0,
                ca_active=True,
                context_key=CONTEXT,
                identity_code="0x0000000000000001",
                provider_plmn="22005",
                cell_id=2050,
                pci=255,
                earfcn=1795,
            ),
        ),
        objective_holds={
            hold_key: ObjectiveHold(
                key=hold_key,
                action="APPLY_PBAND_CA_POOL",
                lever="PBAND",
                target_shape={
                    "pband": 20,
                    "ca_pool": [1, 20],
                    "dl_bands": [1],
                },
                target_band=20,
                target_bands=(1, 20),
                until_ts=250.0,
                reason_code="command_rejected:test",
            )
        },
    )


def _proof() -> dict:
    return {
        "action": "APPLY_PBAND_CA_POOL",
        "target_band": 3,
        "target_bands": [1, 3, 20],
        "context_key": CONTEXT,
        "reason_ref": "learned_anchor_outperforms_current",
        "objective_key": f"{CONTEXT}|apply_radio_configuration|B3",
        "command_id": "command_test_1",
        "proof_id": "proof_test_1",
        "opened_ts": 0.0,
    }


def _terminal_register(root) -> ActionTransactionAuthority:
    register = ActionTransactionAuthority(persistence_dir=root)
    feed = BrainFeedRegisterBank()
    engine = SimpleNamespace(
        _smart_loop_strategy_feed_port=feed,
        action_transaction_authority=register,
        controller=SimpleNamespace(
            brain=SimpleNamespace(),
            runtime=SimpleNamespace(),
        ),
    )
    publish_smart_loop_radio_command_for_test(
        engine,
        target_band=3,
        target_bands=[1, 3, 20],
        reason_code="learned_anchor_outperforms_current",
        now=100.0,
    )
    claim_selected_action_for_test(
        register,
        consumer="controller",
        now=100.1,
    )
    terminalize_selected_action_for_test(engine, now=100.2)
    return register


def _engine(register, store):
    return SimpleNamespace(
        action_transaction_authority=register,
        controller=SimpleNamespace(
            brain=SimpleNamespace(
                smart_loop_state_store=store,
            ),
            runtime=SimpleNamespace(),
        )
    )


def test_state_store_round_trip_and_previous_good_recovery(tmp_path):
    store = SmartLoopStateStore(tmp_path)
    first = _state(observed_ts=100.0)
    second = _state(observed_ts=110.0, proof=True)
    assert store.save(first, now=100.0, force=True) is True
    assert store.save(second, now=110.0, force=True) is True

    store.path.write_text('{"corrupt":true}\n', encoding="utf-8")
    restored = SmartLoopStateStore(tmp_path)

    assert restored.status()["restore_source"] == "previous_good"
    assert "current:snapshot_contract_mismatch" in restored.status()["restore_error"]
    assert restored.restore() == first


def test_observation_only_writes_are_cadenced_but_critical_state_is_immediate(tmp_path):
    store = SmartLoopStateStore(tmp_path, observation_save_interval_s=5.0)
    first = _state(observed_ts=100.0)
    assert store.save(first, now=100.0, force=True) is True

    observation_only = replace(
        first,
        observations=(replace(first.observations[0], ts=101.0, cq=0.73),),
    )
    assert store.save(observation_only, now=101.0) is False
    assert store.status()["coalesced_count"] == 1
    assert store.save(observation_only, now=105.0) is True

    critical = replace(observation_only, pending_proof=_state(proof=True).pending_proof)
    assert store.save(critical, now=105.1) is True


def test_recent_observations_are_replaced_at_context_boundary():
    first = _state(observed_ts=100.0)
    next_context = ModemContext(
        identity_code="0x0000000000000002",
        provider_plmn="22001",
        cell_id=999,
        pci=10,
        earfcn=1300,
        hour=6,
        current_pband=20,
        cq=0.51,
        congestion_pct=45.0,
        ca_active=False,
        observed_ts=101.0,
    )

    updated = first.with_observation(next_context)

    assert len(updated.observations) == 1
    assert (
        updated.observations[0].context_key
        == next_context.learning_context_key
    )
    assert updated.observations[0].identity_code == "0x0000000000000002"


def test_terminal_receipt_survives_state_write_fault_and_restart_boundary(
    tmp_path, monkeypatch
):
    register = _terminal_register(tmp_path)
    store = SmartLoopStateStore(tmp_path)
    engine = _engine(register, store)
    state = _apply_optimization_selected_action_receipt(
        engine,
        state=SmartLoopState(),
        now=101.0,
    )
    assert state.pending_proof is not None
    assert register.read_terminal() is not None

    real_save = store.save

    def fail_save(*args, **kwargs):
        raise OSError("injected_state_write_failure")

    monkeypatch.setattr(store, "save", fail_save)
    with pytest.raises(OSError, match="injected_state_write_failure"):
        _persist_smart_loop_state_and_ack_terminal(engine, state=state, now=101.1)
    assert register.read_terminal() is not None

    monkeypatch.setattr(store, "save", real_save)
    assert store.save(state, now=101.2, force=True) is True

    restored_register = ActionTransactionAuthority(persistence_dir=tmp_path)
    restored_store = SmartLoopStateStore(tmp_path)
    restored_engine = _engine(restored_register, restored_store)
    replayed = _apply_optimization_selected_action_receipt(
        restored_engine,
        state=restored_store.restore(),
        now=101.3,
    )
    _persist_smart_loop_state_and_ack_terminal(
        restored_engine,
        state=replayed,
        now=101.4,
    )

    assert restored_register.read_terminal() is None
    assert restored_store.restore().pending_proof is not None
