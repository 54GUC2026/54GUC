from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from arch4.runtime_decision_projection import build_runtime_decision_projection
from arch4.runtime_registers import (
    REGISTER_CAPACITY,
    REGISTER_DECISION,
    RuntimeRegisterBank,
)
from arch4.smart_loop.capacity_probe import (
    CapacityProbeConfig,
    _runtime_snapshot,
    capacity_probe_gate,
    run_capacity_probe_clock_lane,
)


ROOT = Path(__file__).resolve().parents[1]


def _engine(bank: RuntimeRegisterBank) -> SimpleNamespace:
    return SimpleNamespace(
        register_bank=bank,
        controller=SimpleNamespace(
            runtime=SimpleNamespace(register_bank=bank),
        )
    )


def test_capacity_probe_reads_exact_values_without_register_envelope_keys() -> None:
    bank = RuntimeRegisterBank()
    bank.write_decision(
        value=build_runtime_decision_projection({}),
        source="decision_engine",
        updated_at=100.0,
    )

    runtime_snapshot = _runtime_snapshot(_engine(bank))
    values = runtime_snapshot.values

    assert values[REGISTER_DECISION]["schema"] == "runtime_decision_projection.v1"
    assert all(
        not str(key).startswith("_register_")
        for value in values.values()
        for key in value
    )
    assert runtime_snapshot.metadata[REGISTER_DECISION] == {
        "updated_at": 100.0,
        "valid": True,
        "stale": False,
        "source_kind": "live",
    }


def test_absent_decision_is_dependency_not_ready_without_contract_fault() -> None:
    bank = RuntimeRegisterBank()
    engine = _engine(bank)

    allowed, reason, _context = capacity_probe_gate(
        engine,
        values=_runtime_snapshot(engine).values,
        cfg=CapacityProbeConfig(enabled=True, quiet_window_s=30.0),
        now=100.0,
    )

    assert allowed is False
    assert reason == "quiet_window_not_met"


def test_capacity_lane_reports_startup_backpressure_without_decision_fault() -> None:
    bank = RuntimeRegisterBank()
    engine = _engine(bank)

    outcome = run_capacity_probe_clock_lane(engine, now=100.0)

    assert outcome.ran is False
    assert outcome.last_error == ""
    assert outcome.backpressure_reason == "quiet_window_not_met"
    assert bank.read(REGISTER_CAPACITY).value["active_probe_status"] == "skipped"


def test_invalid_nonempty_decision_still_hard_fails() -> None:
    with pytest.raises(ValueError, match="runtime_decision_projection_required"):
        capacity_probe_gate(
            SimpleNamespace(),
            values={REGISTER_DECISION: {"brain": {"state": "WORKING"}}},
            cfg=CapacityProbeConfig(enabled=True),
            now=100.0,
        )


def test_exact_roi_observation_gate_remains_active() -> None:
    decision = build_runtime_decision_projection(
        {
            "brain": {
                "smart_loop_roi_observation_active": True,
                "smart_loop_roi_observation_remaining_s": 60.0,
            }
        }
    )

    allowed, reason, _context = capacity_probe_gate(
        SimpleNamespace(),
        values={REGISTER_DECISION: decision},
        cfg=CapacityProbeConfig(enabled=True),
        now=100.0,
    )

    assert allowed is False
    assert reason == "roi_observation_active"


def test_static_audit_bans_register_metadata_in_value_namespace() -> None:
    capacity_source = (ROOT / "smart_loop" / "capacity_probe.py").read_text(
        encoding="utf-8"
    )
    passive_source = (ROOT / "smart_loop" / "passive_dl_peak.py").read_text(
        encoding="utf-8"
    )

    for forbidden in (
        'value["_register_updated_at"]',
        'value["_register_valid"]',
        'value["_register_stale"]',
        'value["_register_source_kind"]',
    ):
        assert forbidden not in capacity_source
    for forbidden in (
        'traffic.get("_register_updated_at")',
        'traffic.get("_register_valid")',
        'traffic.get("_register_stale")',
    ):
        assert forbidden not in passive_source
    assert "def _runtime_values(" not in capacity_source
    assert "def _runtime_snapshot(" in capacity_source
    assert 'row.get("value")' in capacity_source
    assert "traffic_register_metadata" in passive_source
