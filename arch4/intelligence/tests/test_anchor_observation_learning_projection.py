from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from arch4.brain.historical import HistoricalJournal
from arch4.brain.memory import Memory
from arch4.brain.memory_evidence_scope_owner import bind_memory_evidence_scope
from arch4.interfaces import Arch4Config, ComputedSignals, ModemInput
from arch4.smart_loop.clock_lane import _project_anchor_observation_learning
from arch4.smart_loop.normal_behavior_progress import (
    AnchorRotationProgress,
    NormalBehaviorProgress,
)
from arch4.tests.controller_generation_factory import cellular_test_inputs


def _engine(tmp_path):
    cfg = Arch4Config()
    memory = Memory(cfg, memory_dir=str(tmp_path))
    historical = HistoricalJournal(cfg, tmp_path)
    modem, computed = cellular_test_inputs(
        ModemInput.safe_defaults(
            timestamp=1000.0,
            provider_plmn="22003",
            primary_band=3,
            pci=123,
            earfcn=1300,
        ),
        ComputedSignals(),
        owner_values={
            "identity_code": "0x1234567890abcdef",
            "identity_status": "exact",
            "identity_catalog_version": "catalog-v1",
            "identity_catalog_checksum": "a" * 64,
            "session_scope_key": "session:anchor-projection",
            "source_epoch": "source:anchor-projection",
            "sample_sequence": 11,
            "access_type": "lte",
            "access_type_id": "lte",
        },
    )
    assert bind_memory_evidence_scope(memory, modem) is True
    historical.record_tick(modem, computed, hour=10)
    events: list[tuple[str, dict]] = []
    logger = SimpleNamespace(
        log_event=lambda name, **fields: events.append((name, fields))
    )
    return (
        SimpleNamespace(
            memory=memory,
            historical=historical,
            diagnostics=logger,
        ),
        events,
    )


def test_anchor_observation_projects_each_learning_owner_exactly_once(tmp_path):
    engine, events = _engine(tmp_path)
    pending = NormalBehaviorProgress(
        anchor=AnchorRotationProgress(
            command_id="command:priority:1",
            target_band=3,
            verify_at=900.0,
            backend_family="huawei_hilink_like",
            status="held",
            actual_band=3,
            observed_at=1000.0,
        )
    )

    projected = _project_anchor_observation_learning(
        engine,
        progress=pending,
        runtime_radio={"serving_band": 3, "serving_band_signature": "B3"},
        runtime_quality={"sinr_db": 12.0},
        runtime_ca={"ca_signature": "B3+B7"},
        now=1000.0,
    )

    assert projected.anchor is not None
    assert projected.anchor.learning_projected is True
    assert engine.memory.band_anchor_stability[3] == {"attempts": 1, "stuck": 1}
    historical_rows = [
        row
        for rows in engine.historical.band_drift_events.values()
        for row in rows
    ]
    assert len(historical_rows) == 1
    assert historical_rows[0]["target"] == 3
    assert historical_rows[0]["actual"] == 3
    assert historical_rows[0]["stuck"] is True
    assert historical_rows[0]["sinr"] == 12.0
    assert historical_rows[0]["ca_sig"] == "B3+B7"
    assert events[-1][0] == "anchor_observation_learning_projected"

    restart_before_state_save = replace(
        projected,
        anchor=replace(projected.anchor, learning_projected=False),
    )
    replayed = _project_anchor_observation_learning(
        engine,
        progress=restart_before_state_save,
        runtime_radio={"serving_band": 3},
        runtime_quality={"sinr_db": 12.0},
        runtime_ca={"ca_signature": "B3+B7"},
        now=1001.0,
    )
    assert replayed.anchor is not None and replayed.anchor.learning_projected
    assert engine.memory.band_anchor_stability[3] == {"attempts": 1, "stuck": 1}
    assert sum(len(rows) for rows in engine.historical.band_drift_events.values()) == 1
