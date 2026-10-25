from __future__ import annotations

from arch4.brain.decision_input import DECISION_INPUT_LANES, build_decision_input_from_registers
from arch4.runtime_registers import (
    REGISTER_FAULTS,
    REGISTER_HEALTH,
    REGISTER_MUTATION,
    REGISTER_RADIO,
    REGISTER_TRAFFIC,
    RUNTIME_REGISTER_OWNER_MAP,
    RuntimeRegisterBank,
)
from arch4.tests.runtime_signal_register_test_support import (
    publish_test_decision_input_lanes,
)


def _healthy_snapshot() -> dict[str, dict[str, object]]:
    bank = RuntimeRegisterBank()
    publish_test_decision_input_lanes(bank, updated_at=100.0)
    return bank.snapshot(DECISION_INPUT_LANES)


def test_decision_input_from_healthy_registers_is_deterministic() -> None:
    snapshot = _healthy_snapshot()

    first = build_decision_input_from_registers(snapshot)
    second = build_decision_input_from_registers(snapshot)

    assert first.as_dict() == second.as_dict()
    assert first.blocking_reasons == ()
    assert first.ready is True
    assert first.severity == "ready"
    assert first.primary_blocking_reason == ""
    assert first.lane(REGISTER_RADIO).trust == "live"
    assert "radio:owner:modem_live" in first.trust_reasons
    summary = first.constraint_summary()
    assert summary["ready"] is True
    assert summary["severity"] == "ready"
    assert summary["lanes"][REGISTER_RADIO]["owner"] == "modem_live"


def test_decision_input_rejects_individually_valid_mixed_physical_generations() -> None:
    snapshot = _healthy_snapshot()
    snapshot[REGISTER_RADIO]["value"].update(
        {"sample_sequence": 11, "source_epoch": "epoch-a"}
    )
    snapshot[REGISTER_TRAFFIC]["value"].update(
        {"sample_sequence": 10, "source_epoch": "epoch-a"}
    )

    decision_input = build_decision_input_from_registers(
        snapshot,
        expected_sample_sequence=11,
        expected_source_epoch="epoch-a",
    )

    assert decision_input.lane(REGISTER_RADIO).generation_match is True
    assert decision_input.lane(REGISTER_TRAFFIC).generation_match is False
    assert any(
        reason.startswith("traffic:error:generation_mismatch:")
        for reason in decision_input.blocking_reasons
    )
    assert decision_input.ready is False
    assert decision_input.severity == "fault"


def test_decision_input_marks_missing_register_as_unknown_blocker() -> None:
    snapshot = _healthy_snapshot()
    snapshot.pop(REGISTER_HEALTH)

    decision_input = build_decision_input_from_registers(snapshot)

    assert decision_input.lane(REGISTER_HEALTH).valid is False
    assert decision_input.lane(REGISTER_HEALTH).stale is True
    assert "health:invalid" in decision_input.blocking_reasons
    assert "health:stale" in decision_input.blocking_reasons
    assert "health:unknown" in decision_input.blocking_reasons
    assert decision_input.ready is False
    assert decision_input.severity == "fault"
    assert decision_input.primary_blocking_reason in decision_input.blocking_reasons


def test_decision_input_cites_stale_radio_trust_reason() -> None:
    snapshot = _healthy_snapshot()
    snapshot[REGISTER_RADIO]["valid"] = False
    snapshot[REGISTER_RADIO]["stale"] = True
    snapshot[REGISTER_RADIO]["trust"] = "stale"
    snapshot[REGISTER_RADIO]["source_kind"] = "live"
    snapshot[REGISTER_RADIO]["validity"] = "stale"
    snapshot[REGISTER_RADIO]["error"] = "modem_read_missed"

    decision_input = build_decision_input_from_registers(snapshot)

    assert "radio:invalid" in decision_input.blocking_reasons
    assert "radio:stale" in decision_input.blocking_reasons
    assert "radio:validity:stale" in decision_input.blocking_reasons
    assert "radio:error:modem_read_missed" in decision_input.blocking_reasons
    assert "radio:trust:stale" in decision_input.trust_reasons
    assert "radio:source_kind:live" in decision_input.trust_reasons
    assert "radio:validity:stale" in decision_input.trust_reasons
    assert decision_input.ready is False
    assert decision_input.severity == "fault"
    assert decision_input.constraint_summary()["primary_blocking_reason"] in decision_input.blocking_reasons


def test_decision_input_blocks_invalid_split_trust_axes_even_when_legacy_trust_is_live() -> None:
    snapshot = _healthy_snapshot()
    snapshot[REGISTER_RADIO]["valid"] = True
    snapshot[REGISTER_RADIO]["stale"] = False
    snapshot[REGISTER_RADIO]["dirty"] = False
    snapshot[REGISTER_RADIO]["trust"] = "live"
    snapshot[REGISTER_RADIO]["source_kind"] = "live"
    snapshot[REGISTER_RADIO]["validity"] = "stale"
    snapshot[REGISTER_RADIO]["transition"] = False

    decision_input = build_decision_input_from_registers(snapshot)
    lane = decision_input.lane(REGISTER_RADIO)

    assert lane.trust == "live"
    assert lane.source_kind == "live"
    assert lane.validity == "stale"
    assert "radio:validity:stale" in decision_input.blocking_reasons
    assert decision_input.ready is False
    assert decision_input.severity == "stale"


def test_decision_input_cites_dirty_mutation_transition() -> None:
    snapshot = _healthy_snapshot()
    snapshot[REGISTER_MUTATION]["value"] = {"active": True, "reason": "cell_lock"}
    snapshot[REGISTER_MUTATION]["dirty"] = True
    snapshot[REGISTER_MUTATION]["trust"] = "transition"
    snapshot[REGISTER_MUTATION]["validity"] = "dirty"
    snapshot[REGISTER_MUTATION]["transition"] = True

    decision_input = build_decision_input_from_registers(snapshot)

    assert decision_input.lane(REGISTER_MUTATION).value == {"active": True, "reason": "cell_lock"}
    assert "mutation:dirty" in decision_input.blocking_reasons
    assert "mutation:transition" in decision_input.blocking_reasons
    assert "mutation:validity:dirty" in decision_input.blocking_reasons
    assert "mutation:trust:transition" in decision_input.trust_reasons
    assert "mutation:transition:true" in decision_input.trust_reasons
    assert decision_input.ready is False
    assert decision_input.severity == "transition"


def test_decision_input_fault_lane_has_highest_severity() -> None:
    snapshot = _healthy_snapshot()
    snapshot[REGISTER_FAULTS]["dirty"] = True
    snapshot[REGISTER_FAULTS]["trust"] = "dirty"
    snapshot[REGISTER_FAULTS]["error"] = "sidecar_writer_degraded"

    decision_input = build_decision_input_from_registers(snapshot)

    assert decision_input.ready is False
    assert decision_input.severity == "fault"
    assert "faults:dirty" in decision_input.blocking_reasons
    assert "faults:error:sidecar_writer_degraded" in decision_input.blocking_reasons


def test_decision_input_enforces_declared_owner_and_safe_bool_contract() -> None:
    snapshot = _healthy_snapshot()
    snapshot[REGISTER_RADIO]["owner"] = "brain"
    snapshot[REGISTER_RADIO]["valid"] = "false"
    snapshot[REGISTER_RADIO]["stale"] = "false"
    snapshot[REGISTER_RADIO]["dirty"] = "false"

    decision_input = build_decision_input_from_registers(snapshot)
    lane = decision_input.lane(REGISTER_RADIO)

    assert lane.owner == RUNTIME_REGISTER_OWNER_MAP[REGISTER_RADIO]
    assert lane.valid is False
    assert lane.stale is False
    assert lane.dirty is False
    assert lane.error == "owner_mismatch:brain"
    assert "radio:error:owner_mismatch:brain" in decision_input.blocking_reasons
    assert decision_input.severity == "fault"
