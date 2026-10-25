"""Tests for systematic BAD_RF band probing.

Verifies that sustained BAD_RF (step-ladder exhausted + 30 min persist)
escalates to systematic band probing via _decide_systematic_bad_rf_probe.
"""

import time
import pytest
from dataclasses import replace
from types import SimpleNamespace

from arch4.runtime_decision_projection import build_runtime_decision_projection
from unittest.mock import patch

from arch4.interfaces import (
    Arch4Config, ModemInput, ComputedSignals, CarrierInfo,
    SystemState, Constraints, LeverType,
)
from arch4.brain.decision import DecisionEngine as _ProductionDecisionEngine
from arch4.tests.controller_posture_test_support import TestControllerPostureAuthority
from arch4.brain.memory import Memory
from arch4.brain.memory_band_learning_query_store import marker_scoped_band_learning_key
from arch4.brain.aggressiveness import AggressivenessEngine
from arch4.brain.historical import HistoricalJournal
from arch4.tests.controller_generation_factory import cellular_test_inputs
from arch4.tests.decision_owner_test_support import with_decision_owner_ports
from arch4.smart_loop.normal_behavior_opportunities import (
    build_normal_behavior_source_facts,
    systematic_probe_candidate_sets,
)
from arch4.smart_loop.normal_behavior_progress import NormalBehaviorProgress


class TestDecisionEngine(_ProductionDecisionEngine):
    __test__ = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **with_decision_owner_ports(kwargs))

    @staticmethod
    def _typed(modem, computed=None):
        return cellular_test_inputs(
            modem,
            computed if computed is not None else ComputedSignals(),
        )

    def _decide_bad_rf(self, modem, computed):
        intelligence, derived = self._typed(modem, computed)
        return super()._decide_bad_rf(intelligence, derived)

    def _decide_systematic_bad_rf_probe(self, modem, computed):
        intelligence, derived = self._typed(modem, computed)
        return super()._decide_systematic_bad_rf_probe(
            intelligence,
            derived,
        )

    def _should_hold_known_outage_window(self, modem, **kwargs):
        intelligence, _ = self._typed(modem)
        return super()._should_hold_known_outage_window(
            intelligence,
            **kwargs,
        )


def _make_engine(tmp_path, *, deployed_bands=None):
    cfg = Arch4Config(modem_backend="b818")
    if deployed_bands:
        cfg.deployed_bands = deployed_bands
    mem = Memory(cfg, memory_dir=str(tmp_path / "mem"))
    sm = TestControllerPostureAuthority(cfg, mem)
    hist = HistoricalJournal(cfg, str(tmp_path / "mem"))
    agg = AggressivenessEngine(cfg)
    constraints = Constraints(pci_lock_available=False)
    # Mark band selection as available
    constraints.band_select_available = True  # type: ignore[attr-defined]
    engine = TestDecisionEngine(
        cfg, mem, sm, constraints,
        aggressiveness=agg,
        historical=hist,
    )
    engine.deployed_bands = set(deployed_bands or [1, 3, 20])
    return engine


def _make_modem(*, sinr=2.0, rsrp=-100.0, band=3, pci=255, connected=True):
    return ModemInput(
        sinr_db=sinr, rsrp_dbm=rsrp, rsrq_db=-14.0, rssi_dbm=-70.0,
        mcs_dl=5, mcs_ul=5, cqi=4, ri=1,
        carriers=[CarrierInfo(band=band, earfcn=1650, pci=pci,
                              bandwidth_mhz=10.0, sinr_db=sinr,
                              rsrp_dbm=rsrp, state=1, is_pcell=True)],
        ca_active=False, ca_band_signature=f"B{band}",
        primary_band=band, pci=pci, earfcn=1650, enb_id=10101,
        radio_connected=connected, session_uptime_s=3600,
        rx_bytes_per_sec=10000, tx_bytes_per_sec=1000,
    )


def _make_computed():
    return ComputedSignals(
        rtt_ms=50.0, rtt_ema=50.0, rtt_sliding_avg=50.0,
        rtt_baseline=40.0, rtt_pressure_level="normal",
        cq_cell=0.3, cq_vector_a_valid=True,
    )


def _systematic_facts(
    *,
    time_in_state_s: float = 2_000.0,
    probe_after_s: float = 1_800.0,
    supports_band_selection: bool = True,
    blocked_reason: str = "",
    progress: NormalBehaviorProgress | None = None,
):
    authority = (1, 3, 20)
    candidates = systematic_probe_candidate_sets(
        authority=authority,
        current_band=3,
    )
    rows = build_normal_behavior_source_facts(
        context=SimpleNamespace(
            current_pband=3,
            authority_bands=authority,
            ca_pool=(3,),
        ),
        identity={"backend_family": "huawei_hilink_like"},
        radio={
            "_register_valid": True,
            "_register_stale": False,
            "radio_connected": True,
            "serving_band": 3,
            "primary_band": 3,
            "configured_band_pool": [3],
            "active_band_set": [3],
            "pci": 255,
            "earfcn_dl": 1650,
            "rsrp_dbm": -100.0,
            "sinr_db": 2.0,
        },
        cellular_slow={
            "_register_valid": True,
            "_register_stale": False,
            "neighbors": [],
        },
        latency={"rtt_baseline": 40.0},
        quality={"cq_cell": 0.3},
        ca={"_register_valid": True, "_register_stale": False, "ca_active": False},
        traffic={"has_traffic": False, "dl_mbps": 0.0},
        decision=build_runtime_decision_projection(
            {"brain": {"normal_policy_safety": {}}}
        ),
        health={
            "_register_valid": True,
            "_register_stale": False,
            "controller_posture": {
                "state": "BAD_RF",
                "progress_scope_id": "episode:bad-rf",
                "time_in_state_s": time_in_state_s,
                "bad_rf_recovery_step": 2,
            },
        },
        capabilities={
            "supports_pband": False,
            "supports_band_selection": supports_band_selection,
            "supports_cell_lock": False,
            "supports_nr_band_lock": False,
        },
        known_good_bands=(3,),
        cell_memory=(),
        band_memory={},
        systematic_probe_after_s=probe_after_s,
        systematic_candidate_admission={
            bands: {
                "allowed": not blocked_reason,
                "reason": blocked_reason,
            }
            for bands in candidates
        },
        progress=progress or NormalBehaviorProgress(),
        now=10_000.0,
    )
    return tuple(
        row for row in rows if row.behavior_id == "systematic_bad_rf_band_probe"
    )


class TestSystematicBadRfProbe:

    def test_systematic_probe_bridge_stamps_blocked_learning_gate_on_calibration_signal(self, tmp_path):
        facts = _systematic_facts(blocked_reason="planned_radio_quiet")

        assert len(facts) == 1
        assert facts[0].active is False
        assert facts[0].source_unavailability_reason == (
            "systematic_candidates_blocked_by_learning"
        )
        assert {
            row["reason"] for row in facts[0].evidence["blocked_candidates"]
        } == {"planned_radio_quiet"}

    def test_probe_untested_settled_skip_event_only_emits_on_activation(self, tmp_path):
        before_activation = _systematic_facts(
            time_in_state_s=600.0,
            blocked_reason="untested_settled",
        )
        assert "blocked_candidates" not in before_activation[0].evidence

        active_window = _systematic_facts(
            time_in_state_s=2_000.0,
            blocked_reason="untested_settled",
        )
        assert len(active_window[0].evidence["blocked_candidates"]) > 0

    def test_no_probe_before_threshold(self, tmp_path):
        """BAD_RF under 30 min should NOT trigger systematic probing."""
        engine = _make_engine(tmp_path)
        modem = _make_modem()
        computed = _make_computed()

        # Put engine in BAD_RF with step-ladder exhausted
        engine.posture_authority.private_state_for_test.state = SystemState.BAD_RF
        engine.posture_authority.private_state_for_test.entered_at = time.time() - 600  # 10 min in BAD_RF
        engine.posture_authority.private_state_for_test.bad_rf_recovery_step = 2

        action = engine._decide_bad_rf(modem, computed)
        assert action is None, "Should not probe — only 10 min in BAD_RF"

    def test_probe_triggers_after_threshold(self, tmp_path):
        """BAD_RF > 30 min with exhausted step-ladder should trigger probing."""
        facts = _systematic_facts(time_in_state_s=2_000.0)

        assert facts
        assert all(row.active for row in facts)
        assert all(row.lever == "BAND_SELECTION" for row in facts)
        assert all(
            row.evidence["systematic_probe_after_s"] == 1_800.0
            for row in facts
        )

    def test_known_outage_hold_skips_systematic_probe(self, tmp_path):
        engine = _make_engine(tmp_path)
        modem = _make_modem(sinr=-12.0)
        computed = _make_computed()
        now = 10_000.0
        hour = time.localtime(now).tm_hour
        marker = engine.memory.current_calibration_marker
        for bands in ([3], [3, 20]):
            key = marker_scoped_band_learning_key(engine.memory, marker, hour, bands)
            engine.memory.band_learning_stm[key] = {
                "attempts": 1.0,
                "good": 0.0,
                "bad": 1.0,
                "hard_fail": 1.0,
                "last_ts": now,
                "last_sinr": -13.0,
            }
        engine.memory.current_context_seen_actions = engine.cfg.learning_marker_warmup_attempts

        engine.posture_authority.private_state_for_test.state = SystemState.BAD_RF
        engine.posture_authority.private_state_for_test.entered_at = now - 2000.0
        engine.posture_authority.private_state_for_test.bad_rf_recovery_step = 2
        engine.posture_authority.private_state_for_test.bad_rf_next_action_at = 0.0

        with patch("arch4.brain.decision_faults.time.time", return_value=now):
            with patch("arch4.brain.decision_support.time.time", return_value=now):
                action = engine._decide_bad_rf(modem, computed)

        assert action is None, "Known outage hold should skip systematic probing"
        assert engine.posture_authority.private_state_for_test.bad_rf_next_action_at >= now + engine.cfg.learning_retry_min_elapsed_s - 1.0
        assert any(e.get("kind") == "known_outage_hold" for e in engine._runtime_events)

    def test_known_outage_hold_blocks_risky_exploration_without_suppression_event(self, tmp_path):
        engine = _make_engine(tmp_path)
        modem = _make_modem(sinr=-12.0)
        now = 10_000.0
        hour = time.localtime(now).tm_hour
        marker = engine.memory.current_calibration_marker
        for bands in ([3], [3, 20]):
            key = marker_scoped_band_learning_key(engine.memory, marker, hour, bands)
            engine.memory.band_learning_stm[key] = {
                "attempts": 1.0,
                "good": 0.0,
                "bad": 1.0,
                "hard_fail": 1.0,
                "last_ts": now,
                "last_sinr": -13.0,
            }
        engine.memory.current_context_seen_actions = engine.cfg.learning_marker_warmup_attempts

        with patch("arch4.brain.decision_support.time.time", return_value=now):
            held = engine._should_hold_known_outage_window(modem, lane="tick_safety")

        assert held is True
        kinds = [e.get("kind") for e in engine._runtime_events]
        assert "known_outage_hold" in kinds
        assert "exploration_suppressed" not in kinds

    def test_step_ladder_not_exhausted(self, tmp_path):
        """Step 0 should run normal recovery, not systematic probing."""
        engine = _make_engine(tmp_path)
        modem = _make_modem()
        computed = _make_computed()

        engine.posture_authority.private_state_for_test.state = SystemState.BAD_RF
        engine.posture_authority.private_state_for_test.entered_at = time.time() - 2000  # 33 min
        engine.posture_authority.private_state_for_test.bad_rf_recovery_step = 0
        engine.posture_authority.private_state_for_test.bad_rf_next_action_at = 0.0

        action = engine._decide_bad_rf(modem, computed)
        # Should be step-ladder action (known-good restore), not systematic probe
        if action is not None:
            assert "systematic" not in (action.reason or "").lower()

    def test_bad_rf_a1_night_prefers_b20_coverage_rescue(self, tmp_path):
        engine = _make_engine(tmp_path)
        modem = _make_modem(band=3, pci=472)
        computed = _make_computed()

        engine._is_a1_overnight_profile = lambda: True
        engine._is_a1_restricted_night_window = lambda now=None, modem=None: True
        engine.posture_authority.private_state_for_test.state = SystemState.BAD_RF
        engine.posture_authority.private_state_for_test.entered_at = time.time() - 600
        engine.posture_authority.private_state_for_test.bad_rf_recovery_step = 1
        engine.posture_authority.private_state_for_test.bad_rf_next_action_at = 0.0

        action = engine._decide_bad_rf(modem, computed)
        assert action is not None
        assert action.lever == LeverType.PBAND
        assert action.params.get("pband") == 20
        assert action.params.get("dl_bands") == [1, 3]
        assert "preserving the current CA pool" in (action.reason or "")

    def test_bad_rf_non_a1_keeps_generic_reopen(self, tmp_path):
        engine = _make_engine(tmp_path)
        modem = _make_modem(band=3, pci=472)
        computed = _make_computed()

        engine._is_a1_overnight_profile = lambda: False
        engine._is_a1_restricted_night_window = lambda now=None, modem=None: False
        engine.posture_authority.private_state_for_test.state = SystemState.BAD_RF
        engine.posture_authority.private_state_for_test.entered_at = time.time() - 600
        engine.posture_authority.private_state_for_test.bad_rf_recovery_step = 1
        engine.posture_authority.private_state_for_test.bad_rf_next_action_at = 0.0

        action = engine._decide_bad_rf(modem, computed)
        assert action is not None
        assert action.lever == LeverType.BAND_SELECTION
        assert action.params.get("bands") != [20]
        assert "reopen full deployed" in (action.reason or "")

    def test_configurable_threshold(self, tmp_path):
        """Custom threshold should be respected."""
        facts = _systematic_facts(
            time_in_state_s=400.0,
            probe_after_s=300.0,
        )

        assert facts
        assert all(row.active for row in facts)
        assert all(
            row.evidence["systematic_probe_after_s"] == 300.0
            for row in facts
        )

    def test_no_band_selection_capability(self, tmp_path):
        """Backend without band selection should not probe."""
        facts = _systematic_facts(supports_band_selection=False)

        assert len(facts) == 1
        assert facts[0].active is False

    def test_reset_clears_probe_state(self, tmp_path):
        """A new progress scope has no hidden probe-cycle state."""
        paced = replace(
            NormalBehaviorProgress(),
            behavior_next_allowed_ts={
                "systematic_bad_rf_band_probe": 12_000.0,
            },
        )
        held = _systematic_facts(progress=paced)
        reset = _systematic_facts(progress=NormalBehaviorProgress())

        assert all(row.active is False for row in held)
        assert all(
            row.source_unavailability_reason == "normal_behavior_pacing_active"
            for row in held
        )
        assert reset and all(row.active for row in reset)

    def test_retired_systematic_selector_fails_loudly(self, tmp_path):
        engine = _make_engine(tmp_path)
        assert not hasattr(engine, "_decide_a1_b818_night_training")
        with pytest.raises(
            RuntimeError,
            match="old_systematic_bad_rf_action_selector_deleted",
        ):
            engine._decide_systematic_bad_rf_probe(
                _make_modem(),
                _make_computed(),
            )
