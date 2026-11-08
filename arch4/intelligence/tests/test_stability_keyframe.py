"""Tests for stability keyframe — skip decide() when nothing meaningful changed.

Vector 3: When consecutive ticks produce the same keyframe (state, CA, band,
RTT bucket, pressure, traffic, hour, congestion, cooldowns, tick counters),
lanes 2-4 are skipped. Max 4 consecutive skips before forcing a full run.
"""
from __future__ import annotations

import os
import tempfile
import time
from typing import List
from unittest.mock import patch

import pytest

from arch4.brain.aggressiveness import AggressivenessEngine
from arch4.brain.decision import DecisionEngine
from arch4.brain.historical import HistoricalJournal
from arch4.brain.memory import Memory
from arch4.tests.controller_posture_test_support import TestControllerPostureAuthority
from arch4.interfaces import (
    Arch4Config, CarrierInfo, ComputedSignals, Constraints,
    ModemInput, SystemState,
)
from arch4.tests.controller_generation_factory import cellular_test_inputs


def _modem(
    primary_band: int = 3,
    ca_active: bool = True,
    sinr: float = 14.0,
    rsrp: float = -78.0,
    has_traffic: bool = False,
    extra_bands: List[int] | None = None,
) -> ModemInput:
    carriers = [
        CarrierInfo(
            band=primary_band, earfcn=1300, pci=303,
            bandwidth_mhz=20.0, sinr_db=sinr, rsrp_dbm=rsrp,
            state=1, is_pcell=True,
        )
    ]
    for b in (extra_bands or []):
        carriers.append(CarrierInfo(
            band=b, earfcn=100, pci=303, bandwidth_mhz=10.0,
            sinr_db=sinr - 2, rsrp_dbm=rsrp - 5, state=1, is_pcell=False,
        ))
    rx = 500_000.0 if has_traffic else 0.0
    return ModemInput(
        sinr_db=sinr, rsrp_dbm=rsrp, rsrq_db=-9.0, rssi_dbm=-60.0,
        mcs_dl=12, mcs_ul=0, cqi=9, ri=2, carriers=carriers,
        ca_active=ca_active,
        ca_band_signature=f"B{primary_band}" + ("+" + "+".join(f"B{b}" for b in (extra_bands or [])) if extra_bands else ""),
        primary_band=primary_band, pci=303, earfcn=1300, enb_id=1,
        radio_connected=True, session_uptime_s=300.0,
        rx_bytes_per_sec=rx, tx_bytes_per_sec=0.0,
    )


def _computed(rtt: float = 35.0) -> ComputedSignals:
    cs = ComputedSignals()
    cs.rtt_sliding_avg = rtt
    cs.rtt_ema = rtt
    cs.rtt_ms = rtt
    cs.rtt_pressure_level = "normal"
    cs.cq_cell = 0.8
    cs.cq_vector_a_valid = True
    cs.cq_cell = 0.8
    cs.cq_vector_a_valid = True
    return cs


def _decide(engine: DecisionEngine, modem: ModemInput, computed: ComputedSignals):
    intelligence, derived = cellular_test_inputs(modem, computed)
    return engine.decide(intelligence, derived)


def _make_engine(state: SystemState = SystemState.WORKING) -> DecisionEngine:
    cfg = Arch4Config(
        deployed_bands=[3],  # match modem's single band — no exploration candidates
        autonomous_connect_settle_s=0.0,
        radio_change_settle_s=0.0,
    )
    tmp = tempfile.mkdtemp()
    prev = os.environ.pop("ARCH_RUNTIME_MEMORY_DIR", None)
    os.environ["ARCH_RUNTIME_MEMORY_DIR"] = tmp
    try:
        mem = Memory(cfg, memory_dir=tmp)
    finally:
        if prev is None:
            os.environ.pop("ARCH_RUNTIME_MEMORY_DIR", None)
        else:
            os.environ["ARCH_RUNTIME_MEMORY_DIR"] = prev
    hist = HistoricalJournal(cfg, tmp)
    sm = TestControllerPostureAuthority(cfg, mem)
    sm.private_state_for_test.state = state
    return DecisionEngine(
        cfg, mem, sm, Constraints(),
        aggressiveness=AggressivenessEngine(cfg),
        historical=hist,
    )


class TestStabilityKeyframe:
    """decide() stability keyframe skipping."""

    def test_first_tick_always_runs(self):
        """First call to decide() always runs the full pipeline."""
        eng = _make_engine()
        result = _decide(eng, _modem(), _computed())
        # No action expected in stable WORKING — but pipeline ran
        assert eng._stability_keyframe is not None
        assert eng._stability_skip_count == 0

    def test_identical_tick_skips(self):
        """Second call with identical signals skips pipeline."""
        eng = _make_engine()
        m, c = _modem(), _computed()
        _decide(eng, m, c)
        kf_after_first = eng._stability_keyframe

        # Second call — same signals
        _decide(eng, m, c)
        assert eng._stability_skip_count == 1
        # Keyframe unchanged
        assert eng._stability_keyframe == kf_after_first

    def test_state_change_forces_reeval(self):
        """State change invalidates keyframe."""
        eng = _make_engine()
        m, c = _modem(), _computed()
        _decide(eng, m, c)
        assert eng._stability_skip_count == 0

        # Change state
        eng.posture_authority.private_state_for_test.state = (
            SystemState.DEGRADED
        )
        _decide(eng, m, c)
        # Should have run full pipeline (skip_count reset)
        assert eng._stability_skip_count == 0

    def test_ca_lost_state_change_forces_reeval(self):
        """CA lost state (filtered sliding window) change invalidates keyframe."""
        eng = _make_engine()
        m, c = _modem(), _computed()
        _decide(eng, m, c)
        assert eng._stability_skip_count == 0

        # Flip the filtered CA lost signal
        eng._ca_lost_state = True
        _decide(eng, m, c)
        assert eng._stability_skip_count == 0

    def test_rtt_bucket_change_forces_reeval(self):
        """RTT crossing warning threshold invalidates keyframe."""
        eng = _make_engine()
        _decide(eng, _modem(), _computed(rtt=35.0))  # normal bucket
        assert eng._stability_skip_count == 0

        _decide(eng, _modem(), _computed(rtt=160.0))  # warning bucket
        assert eng._stability_skip_count == 0

    def test_traffic_change_forces_reeval(self):
        """Traffic state change invalidates keyframe."""
        eng = _make_engine()
        _decide(eng, _modem(has_traffic=False), _computed())
        assert eng._stability_skip_count == 0

        _decide(eng, _modem(has_traffic=True), _computed())
        assert eng._stability_skip_count == 0

    def test_skip_limit_forces_reeval(self):
        """After 4 consecutive skips, force a full pipeline run."""
        eng = _make_engine()
        m, c = _modem(), _computed()

        _decide(eng, m, c)  # tick 1: full run
        assert eng._stability_skip_count == 0

        for i in range(4):
            _decide(eng, m, c)  # ticks 2-5: skipped
        assert eng._stability_skip_count == 4

        _decide(eng, m, c)  # tick 6: forced full run (skip_count was 4 >= limit)
        assert eng._stability_skip_count == 0

    def test_action_invalidates_keyframe(self):
        """When an action is produced, keyframe is cleared for next tick."""
        # Use full deployed_bands so exploration can fire
        cfg = Arch4Config(
            deployed_bands=[1, 3, 20],
            autonomous_connect_settle_s=0.0,
            radio_change_settle_s=0.0,
        )
        tmp = tempfile.mkdtemp()
        prev = os.environ.pop("ARCH_RUNTIME_MEMORY_DIR", None)
        os.environ["ARCH_RUNTIME_MEMORY_DIR"] = tmp
        try:
            mem = Memory(cfg, memory_dir=tmp)
        finally:
            if prev is None:
                os.environ.pop("ARCH_RUNTIME_MEMORY_DIR", None)
            else:
                os.environ["ARCH_RUNTIME_MEMORY_DIR"] = prev
        hist = HistoricalJournal(cfg, tmp)
        sm = TestControllerPostureAuthority(cfg, mem)
        sm.private_state_for_test.state = SystemState.WORKING
        eng = DecisionEngine(
            cfg, mem, sm, Constraints(),
            aggressiveness=AggressivenessEngine(cfg),
            historical=hist,
        )
        m = _modem(ca_active=True, sinr=15.0, extra_bands=[])
        c = _computed()

        result = _decide(eng, m, c)
        if result is not None:
            # Action was produced — keyframe should be cleared
            assert eng._stability_keyframe is None
        # If no action produced, keyframe should be stored
        else:
            assert eng._stability_keyframe is not None

    def test_cooldown_expiry_forces_reeval(self):
        """Cooldown count changing invalidates keyframe."""
        eng = _make_engine()
        m, c = _modem(), _computed()
        now = time.time()

        # Set a cooldown
        eng.cooldowns.set("test_cd", hold_s=5.0, now=now)

        with patch("time.time", return_value=now):
            _decide(eng, m, c)
        assert eng._stability_skip_count == 0

        # Same signals, but cooldown has expired (now + 10s)
        with patch("time.time", return_value=now + 10.0):
            _decide(eng, m, c)
        # Cooldown expired → count changed → keyframe mismatch → full run
        assert eng._stability_skip_count == 0

    def test_primary_band_change_forces_reeval(self):
        """Primary band change invalidates keyframe."""
        eng = _make_engine()
        _decide(eng, _modem(primary_band=3), _computed())
        assert eng._stability_skip_count == 0

        _decide(eng, _modem(primary_band=1), _computed())
        assert eng._stability_skip_count == 0

    def test_skip_counter_resets_on_full_run(self):
        """Skip counter resets to 0 when a full pipeline run occurs."""
        eng = _make_engine()
        m, c = _modem(), _computed()

        _decide(eng, m, c)  # full run
        _decide(eng, m, c)  # skip 1
        _decide(eng, m, c)  # skip 2
        assert eng._stability_skip_count == 2

        # Change a signal → forces full run → counter resets
        _decide(eng, _modem(has_traffic=True), _computed())
        assert eng._stability_skip_count == 0
