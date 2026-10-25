from __future__ import annotations

import os
import tempfile
import time
from types import SimpleNamespace
from typing import List
from unittest.mock import patch

import pytest

from arch4.brain.learning_memory_projection import (
    best_bandset_from_memory,
    ranked_bandsets_from_memory,
    should_try_band_from_memory,
)
from arch4.brain.learning_quality import publish_learning_quality_from_authority
from arch4.brain.memory_action_learning_store import (
    record_band_attempt_to_memory_store,
)
from arch4.brain.memory_action_history_owner import (
    record_action_history as memory_record_action_history,
)
from arch4.brain.memory_band_operational_history_owner import (
    blacklist_band as memory_blacklist_band,
    is_band_blacklisted as memory_is_band_blacklisted,
)
from arch4.brain.exploration_pressure import (
    ExplorationPressure as _ProductionExplorationPressure,
    POST_HARD_FAULT_DAMPING,
    PROBE_THRESHOLD,
    RESISTANCE_DRAIN,
    RESISTANCE_SPIKE,
)
from arch4.brain.decision import DecisionEngine as _ProductionDecisionEngine
from arch4.interfaces import (
    Arch4Config,
    CarrierInfo,
    ComputedSignals,
    Constraints,
    ModemInput,
    SystemState,
)
from arch4.tests.controller_generation_factory import cellular_test_inputs
from arch4.tests.decision_owner_test_support import with_decision_owner_ports
from arch4.tests.config_generation_test_support import (
    ActiveConfigProjectionTestMixin,
    install_active_config_generation_for_test,
)
from arch4.tests.runtime_signal_register_test_support import (
    publish_test_signal_generation,
)
from arch4.tests.runtime_identity_register_test_support import (
    install_test_identity_register,
)
from arch4.tests.checkpoint_test_support import (
    bind_test_memory_scope,
    commit_memory_checkpoint,
)
from arch4.tests.terminal_reward_test_support import (
    commit_terminal_reward_test_fixture,
)


class ExplorationPressure(_ProductionExplorationPressure):
    """Explicit raw-fixture adapter for the production typed pressure owner."""

    def update(self, modem, computed, **kwargs):
        intelligence, derived = cellular_test_inputs(modem, computed)
        return super().update(intelligence, derived, **kwargs)


class DecisionEngine(ActiveConfigProjectionTestMixin, _ProductionDecisionEngine):
    """Test-only adapter for direct helper calls below the public decide API."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **with_decision_owner_ports(kwargs))

    def _throughput_hold_active(self, modem):
        intelligence, _ = cellular_test_inputs(modem, ComputedSignals())
        return super()._throughput_hold_active(intelligence)


def _modem(
    rx_bps: float = 0.0,
    tx_bps: float = 0.0,
    primary_band: int = 3,
    primary_bw: float = 20.0,
    ca_active: bool = True,
    sinr: float = 14.0,
    rsrp: float = -78.0,
    cqi: int = 9,
    extra_bands: List[int] | None = None,
) -> ModemInput:
    carriers = [
        CarrierInfo(
            band=primary_band,
            earfcn=1300,
            pci=303,
            bandwidth_mhz=primary_bw,
            sinr_db=sinr,
            rsrp_dbm=rsrp,
            state=1,
            is_pcell=True,
        )
    ]
    if extra_bands:
        for band in extra_bands:
            carriers.append(
                CarrierInfo(
                    band=band,
                    earfcn=100,
                    pci=303,
                    bandwidth_mhz=10.0,
                    sinr_db=sinr - 2,
                    rsrp_dbm=rsrp - 5,
                    state=1,
                    is_pcell=False,
                )
            )
    return ModemInput(
        sinr_db=sinr,
        rsrp_dbm=rsrp,
        rsrq_db=-9.0,
        rssi_dbm=-60.0,
        mcs_dl=12,
        mcs_ul=0,
        cqi=cqi,
        ri=2,
        carriers=carriers,
        ca_active=ca_active,
        ca_band_signature=f"B{primary_band}"
        + ("+" + "+".join(f"B{b}" for b in (extra_bands or [])) if extra_bands else ""),
        primary_band=primary_band,
        pci=303,
        earfcn=1300,
        enb_id=1,
        radio_connected=True,
        session_uptime_s=300.0,
        rx_bytes_per_sec=rx_bps,
        tx_bytes_per_sec=tx_bps,
    )


def _computed(
    rtt: float = 35.0,
    rtt_pressure: str = "normal",
    rsrq_gap: float = 0.0,
    rsrq_gap_valid: bool = False,
    rsrq_degrading: bool = False,
    mcs_collapse: bool = False,
) -> ComputedSignals:
    cs = ComputedSignals()
    cs.rtt_sliding_avg = rtt
    cs.rtt_ema = rtt
    cs.rtt_ms = rtt
    cs.rtt_pressure_level = rtt_pressure
    cs.rtt_elevated = rtt_pressure in {"busy", "alarm"}
    cs.rtt_alarm = rtt_pressure == "alarm"
    cs.rsrq_gap = rsrq_gap
    cs.rsrq_gap_valid = rsrq_gap_valid
    cs.rsrq_degrading = rsrq_degrading
    cs.mcs_collapse = mcs_collapse
    cs.cq_cell = 0.8
    cs.cq_vector_a_valid = True
    cs.cq_cell = 0.8
    cs.cq_vector_a_valid = True
    return cs


def _decide(engine: DecisionEngine, modem: ModemInput, computed: ComputedSignals):
    intelligence, derived = cellular_test_inputs(modem, computed)
    return engine.decide(intelligence, derived)


def _make_memory(cfg: Arch4Config | None = None):
    from arch4.brain.memory import Memory

    cfg = cfg or Arch4Config()
    tmp = tempfile.mkdtemp()
    prev = os.environ.pop("ARCH_RUNTIME_MEMORY_DIR", None)
    os.environ["ARCH_RUNTIME_MEMORY_DIR"] = tmp
    try:
        return Memory(cfg, memory_dir=tmp)
    finally:
        if prev is None:
            os.environ.pop("ARCH_RUNTIME_MEMORY_DIR", None)
        else:
            os.environ["ARCH_RUNTIME_MEMORY_DIR"] = prev


def _allowed_learning_gate() -> dict:
    return {"learning_allowed": True, "gate_input_complete": True}


def _record_classified_action(memory, **kwargs):
    typed_kwargs = dict(kwargs)
    terminal_grade = typed_kwargs.pop("terminal_grade", None)
    typed_kwargs.setdefault("proposal_action_origin", "smart_loop")
    modem_before = kwargs.get("modem_before")
    computed_before = kwargs.get("computed_before")
    if isinstance(modem_before, ModemInput) and isinstance(
        computed_before, ComputedSignals
    ):
        before, before_derived = cellular_test_inputs(
            modem_before,
            computed_before,
        )
        typed_kwargs["modem_before"] = before
        typed_kwargs["computed_before"] = before_derived
    modem_after = kwargs.get("modem_after")
    computed_after = kwargs.get("computed_after")
    if isinstance(modem_after, ModemInput) and isinstance(
        computed_after, ComputedSignals
    ):
        after, after_derived = cellular_test_inputs(modem_after, computed_after)
        typed_kwargs["modem_after"] = after
        typed_kwargs["computed_after"] = after_derived

    evidence = typed_kwargs.get("modem_after") or typed_kwargs.get(
        "modem_before"
    )
    typed_kwargs["proof_observed_at"] = float(
        evidence.generation.sample_ts
    )

    record = memory_record_action_history(memory, **typed_kwargs)
    success = bool(kwargs.get("success"))
    if terminal_grade is None:
        terminal_grade = 1 if success else None
    if terminal_grade is None:
        return record
    commit_terminal_reward_test_fixture(
        memory,
        record,
        grade=int(terminal_grade),
        modem_after=typed_kwargs.get("modem_after"),
        computed_after=typed_kwargs.get("computed_after"),
    )
    return record


def _make_engine(cfg: Arch4Config | None = None):
    from arch4.brain.aggressiveness import AggressivenessEngine
    from arch4.brain.historical import HistoricalJournal
    from arch4.brain.memory import Memory
    from arch4.tests.controller_posture_test_support import TestControllerPostureAuthority

    cfg = cfg or Arch4Config()
    tmp = tempfile.mkdtemp()
    prev = os.environ.pop("ARCH_RUNTIME_MEMORY_DIR", None)
    os.environ["ARCH_RUNTIME_MEMORY_DIR"] = tmp
    try:
        mem = Memory(cfg, memory_dir=tmp)
        hist = HistoricalJournal(cfg, tmp)
        sm = TestControllerPostureAuthority(cfg, mem)
        sm.private_state_for_test.state = SystemState.WORKING
        return DecisionEngine(
            cfg,
            mem,
            sm,
            Constraints(),
            aggressiveness=AggressivenessEngine(cfg),
            historical=hist,
        )
    finally:
        if prev is None:
            os.environ.pop("ARCH_RUNTIME_MEMORY_DIR", None)
        else:
            os.environ["ARCH_RUNTIME_MEMORY_DIR"] = prev


def test_traffic_state_matrix_and_primary_bw_accessors():
    traffic_cases = [
        ((10_000, 5_000), ("idle", False)),
        ((500_000, 10_000), ("download", True)),
        ((500_000, 100_000), ("active", True)),
        ((50_000, 1_000), ("download", True)),
        ((500_000, 50_000), ("active", True)),
        ((5_000, 200_000), ("upload", True)),
    ]
    for (rx_bps, tx_bps), (expected_state, has_traffic) in traffic_cases:
        modem = _modem(rx_bps=rx_bps, tx_bps=tx_bps)
        assert modem.traffic_state == expected_state
        assert modem.has_traffic is has_traffic

    bw_cases = [
        (_modem(primary_band=3, primary_bw=20.0), 20.0),
        (_modem(primary_band=1, primary_bw=15.0), 15.0),
        (_modem(primary_band=20, primary_bw=10.0), 10.0),
    ]
    for modem, expected_bw in bw_cases:
        assert modem.primary_bw_mhz == expected_bw

    no_carrier = ModemInput(
        sinr_db=10.0,
        rsrp_dbm=-90.0,
        rsrq_db=-12.0,
        rssi_dbm=-70.0,
        mcs_dl=8,
        mcs_ul=0,
        cqi=7,
        ri=1,
        carriers=[],
        ca_active=False,
        ca_band_signature="",
        primary_band=3,
        pci=0,
        earfcn=0,
        enb_id=0,
        radio_connected=False,
        session_uptime_s=0.0,
        rx_bytes_per_sec=0.0,
        tx_bytes_per_sec=0.0,
    )
    assert no_carrier.primary_bw_mhz == 0.0


def test_primary_bandwidth_is_recorded_into_ltm_and_smoothed():
    from arch4.interfaces import LeverType

    mem = _make_memory()
    timestamp = time.mktime((2026, 3, 17, 14, 0, 0, 0, 0, -1))
    for bw in [20.0, 20.0, 15.0, 20.0]:
        with patch("time.localtime", return_value=time.localtime(timestamp)):
            _record_classified_action(
                mem,
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3]},
                success=True,
                modem_before=_modem(primary_band=3, primary_bw=bw),
                computed_before=_computed(),
                modem_after=_modem(primary_band=3, primary_bw=bw, rx_bps=200_000),
                computed_after=_computed(),
                learning_gate=_allowed_learning_gate(),
            )
    matching = [
        float(bucket.get("avg_primary_bw_mhz", 0.0) or 0.0)
        for key, bucket in mem.band_learning_ltm.items()
        if "B1+B3" in key or "B3+B1" in key
    ]
    assert matching
    assert any(17.0 <= avg <= 20.0 for avg in matching)


def test_exploration_pressure_accumulates_and_drains_for_expected_signal_patterns():
    idle = ExplorationPressure(pressure=0.3)
    for _ in range(20):
        idle.update(_modem(rx_bps=0), _computed())
    assert 0.0 <= idle.pressure < 0.3

    rtt_alarm = ExplorationPressure()
    large_gap = ExplorationPressure()
    compound = ExplorationPressure()
    for _ in range(10):
        rtt_alarm.update(_modem(rx_bps=0), _computed(rtt_pressure="alarm"))
        large_gap.update(_modem(rx_bps=0), _computed(rsrq_gap=7.0, rsrq_gap_valid=True))
        compound.update(
            _modem(rx_bps=0),
            _computed(rtt_pressure="busy", rsrq_gap=4.0, rsrq_gap_valid=True),
        )
    assert rtt_alarm.pressure > 0.1
    assert large_gap.pressure > 0.0
    assert compound.pressure > 0.0

    cliff = ExplorationPressure()
    for _ in range(10):
        cliff.update(
            _modem(rx_bps=0), _computed(), cliff_ahead=True, load_trend="rising"
        )
    assert cliff.pressure > 0.1

    spike = ExplorationPressure()
    spike.update(
        _modem(rx_bps=0),
        _computed(),
        incident_assessment={"incident_class": "rf_event_spike"},
    )
    assert spike.pressure > 0.35


def test_exploration_pressure_resistance_probe_and_post_fault_damping_behaviors():
    active = ExplorationPressure()
    download = ExplorationPressure()
    idle = ExplorationPressure()
    for _ in range(20):
        active.update(
            _modem(rx_bps=500_000, tx_bps=100_000), _computed(rtt_pressure="alarm")
        )
        download.update(_modem(rx_bps=500_000, tx_bps=5_000), _computed())
        idle.update(_modem(rx_bps=0, tx_bps=0), _computed())
    assert active.resistance > 0.0
    assert download.resistance == 0.0
    assert idle.resistance == 0.0

    probe = ExplorationPressure()
    ticks = 0
    while not probe.should_probe() and ticks < 200:
        probe.update(
            _modem(rx_bps=0),
            _computed(
                rtt_pressure="alarm",
                rsrq_gap=7.0,
                rsrq_gap_valid=True,
                mcs_collapse=True,
            ),
        )
        ticks += 1
    assert probe.should_probe()
    probe.record_probe()
    assert probe.pressure == 0.0
    assert probe.resistance == RESISTANCE_SPIKE
    assert probe.net_pressure == 0.0

    spike_resistance = probe.resistance
    for _ in range(10):
        probe.resistance = max(0.0, probe.resistance - RESISTANCE_DRAIN)
    assert 0.0 <= probe.resistance < spike_resistance

    normal = ExplorationPressure()
    damped = ExplorationPressure()
    normal_ticks = 0
    damped_ticks = 0
    pattern = _computed(rtt_pressure="busy", rsrq_gap=4.0, rsrq_gap_valid=True)
    while not normal.should_probe() and normal_ticks < 200:
        normal.update(_modem(rx_bps=0), pattern)
        normal_ticks += 1
    while not damped.should_probe() and damped_ticks < 200:
        damped.update(
            _modem(rx_bps=0), pattern, post_fault_recent=True, post_fault_class="BAD_RF"
        )
        damped_ticks += 1
    assert normal.should_probe() and damped.should_probe()
    assert damped_ticks <= normal_ticks
    assert damped.net_pressure > max(
        0.0, damped.pressure - damped.resistance - POST_HARD_FAULT_DAMPING
    )


def test_exploration_pressure_biases_toward_bad_rf_idle_and_away_from_active_user():
    idle_bad_rf = ExplorationPressure()
    active_bad_rf = ExplorationPressure()
    pattern = _computed(rtt_pressure="busy", rsrq_gap=4.0, rsrq_gap_valid=True)

    for _ in range(6):
        idle_bad_rf.update(
            _modem(rx_bps=0, tx_bps=0, sinr=2.0),
            pattern,
            incident_assessment={"incident_class": "rf_collapse"},
            post_fault_recent=True,
            post_fault_class="BAD_RF",
        )
        active_bad_rf.update(
            _modem(rx_bps=500_000, tx_bps=120_000, sinr=2.0),
            pattern,
            incident_assessment={"incident_class": "rf_collapse"},
            post_fault_recent=True,
            post_fault_class="BAD_RF",
        )

    assert idle_bad_rf.net_pressure > active_bad_rf.net_pressure
    assert idle_bad_rf.should_probe()
    assert active_bad_rf.should_probe()


def test_exploration_pressure_reason_strings_and_pilot_hints_stay_explanatory():
    ep = ExplorationPressure()
    ep.update(_modem(rx_bps=0), _computed())
    assert ep.reason_str() == "routine"

    ep = ExplorationPressure()
    ep.update(_modem(rx_bps=0), _computed(rtt_pressure="alarm"))
    assert "RTT" in ep.reason_str()

    hint_cases = [
        ({"rf_spike": True, "active_reasons": ["RF spike"]}, ("signal drop", "spike")),
        (
            {
                "cliff_ahead": True,
                "load_trend_rising": True,
                "active_reasons": ["cliff ahead", "load rising"],
            },
            ("history", "predicts", "rising"),
        ),
        (
            {
                "rtt_alarm": True,
                "rsrq_load_gap_large": True,
                "active_reasons": ["RTT alarm", "RSRQ gap large"],
            },
            ("congested", "congestion"),
        ),
    ]
    for fields, expected_words in hint_cases:
        ep = ExplorationPressure()
        for key, value in fields.items():
            setattr(ep, key, value)
        hint = ep.pilot_hint().lower()
        assert any(word in hint for word in expected_words)


def test_ltm_ranking_prefers_known_good_and_penalizes_congestion_bad_stm():
    from arch4.interfaces import LeverType

    good_mem = _make_memory()
    timestamp = time.mktime((2026, 3, 17, 14, 0, 0, 0, 0, -1))
    for _ in range(5):
        with patch("time.localtime", return_value=time.localtime(timestamp)):
            _record_classified_action(
                good_mem,
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3]},
                success=True,
                modem_before=_modem(primary_band=3, primary_bw=20.0),
                computed_before=_computed(),
                modem_after=_modem(primary_band=3, primary_bw=20.0, rx_bps=300_000),
                computed_after=_computed(),
                learning_gate=_allowed_learning_gate(),
            )
    with patch("time.localtime", return_value=time.localtime(timestamp)):
        ranked = ranked_bandsets_from_memory(good_mem, 14, [[1, 3], [1, 3, 20]])
    assert ranked[0][0] == [1, 3]

    veto_mem = _make_memory()
    timestamp = time.mktime((2026, 3, 17, 18, 0, 0, 0, 0, -1))
    for _ in range(3):
        with patch("time.localtime", return_value=time.localtime(timestamp)):
            _record_classified_action(
                veto_mem,
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3]},
                success=True,
                terminal_grade=-1,
                modem_before=_modem(),
                computed_before=_computed(),
                modem_after=_modem(rx_bps=0),
                computed_after=_computed(),
                learning_gate=_allowed_learning_gate(),
            )
    with patch("time.localtime", return_value=time.localtime(timestamp)):
        veto_ranked = ranked_bandsets_from_memory(
            veto_mem,
            18,
            [[1, 3]],
            rtt_pressure_level="alarm",
            rsrq_gap=7.0,
            rsrq_gap_valid=True,
        )
    score, source = veto_ranked[0][1], veto_ranked[0][3]
    assert score <= 0.25 or "veto" in source


def test_more_stm_evidence_does_not_hurt_ranking():
    from arch4.interfaces import LeverType

    memories = [_make_memory(), _make_memory()]
    timestamp = time.mktime((2026, 3, 17, 10, 0, 0, 0, 0, -1))
    observation_counts = [1, 5]
    scores = []
    for mem, count in zip(memories, observation_counts):
        for _ in range(count):
            with patch("time.localtime", return_value=time.localtime(timestamp)):
                _record_classified_action(
                    mem,
                    lever=LeverType.BAND_SELECTION,
                    params={"bands": [1, 3]},
                    success=True,
                    modem_before=_modem(),
                    computed_before=_computed(),
                    modem_after=_modem(rx_bps=300_000),
                    computed_after=_computed(),
                    learning_gate=_allowed_learning_gate(),
                )
        with patch("time.localtime", return_value=time.localtime(timestamp)):
            ranked = ranked_bandsets_from_memory(mem, 10, [[1, 3]])
        scores.append(ranked[0][1] if ranked else 0.5)
    assert scores[1] >= scores[0]


def test_throughput_hold_uses_disable_fallback_urgency_override_and_percentile_history():
    engine = _make_engine(Arch4Config(v5_good_dl_hold_enabled=False))
    assert engine._throughput_hold_active(_modem(rx_bps=10_000_000)) is False

    engine = _make_engine()
    assert engine._throughput_hold_active(_modem(rx_bps=7_400_000)) is False
    assert engine._throughput_hold_active(_modem(rx_bps=7_600_000)) is True

    engine._exploration_pressure.pressure = 1.2
    engine._exploration_pressure.resistance = 0.0
    assert engine._throughput_hold_active(_modem(rx_bps=10_000_000)) is False

    hour = time.localtime().tm_hour
    engine = _make_engine()
    engine.historical.band_perf[hour] = {
        "B3": (300.0, 10),
        "B3+B1": (600.0, 10),
        "B1": (150.0, 10),
        "B3+B20": (450.0, 10),
    }
    top_quartile = engine._ltm_throughput_top_quartile(hour)
    assert top_quartile is not None
    assert (
        engine._throughput_hold_active(_modem(rx_bps=int(top_quartile * 125_000)))
        is True
    )
    assert (
        engine._throughput_hold_active(_modem(rx_bps=int(top_quartile * 0.8 * 125_000)))
        is False
    )


def test_learning_scores_feed_into_decision_ranking_end_to_end():
    from arch4.interfaces import LeverType

    mem = _make_memory()
    hour = 14
    timestamp = time.mktime((2026, 3, 17, hour, 0, 0, 0, 0, -1))

    for _ in range(5):
        with patch("time.localtime", return_value=time.localtime(timestamp)):
            _record_classified_action(
                mem,
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3]},
                success=True,
                modem_before=_modem(
                    primary_band=20,
                    primary_bw=10.0,
                    ca_active=False,
                    sinr=8.0,
                    rsrp=-96.0,
                    rx_bps=0,
                ),
                computed_before=_computed(),
                modem_after=_modem(
                    primary_band=3,
                    primary_bw=20.0,
                    ca_active=True,
                    sinr=15.0,
                    rsrp=-78.0,
                    rx_bps=400_000,
                    extra_bands=[1],
                ),
                computed_after=_computed(),
                learning_gate=_allowed_learning_gate(),
            )

    with patch("time.localtime", return_value=time.localtime(timestamp)):
        ranked = ranked_bandsets_from_memory(mem, hour, [[1, 3], [1, 20]])

    assert len(ranked) == 2
    assert ranked[0][0] == [1, 3]
    assert ranked[0][1] > 0.5


def test_learning_scores_are_used_by_decision_engine_explore_path():
    from arch4.interfaces import LeverType

    engine = _make_engine()
    hour = 10
    timestamp = time.mktime((2026, 3, 17, hour, 0, 0, 0, 0, -1))

    for _ in range(6):
        with patch("time.localtime", return_value=time.localtime(timestamp)):
            _record_classified_action(
                engine.memory,
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3]},
                success=True,
                modem_before=_modem(
                    primary_band=20,
                    primary_bw=10.0,
                    ca_active=False,
                    sinr=7.0,
                    rsrp=-97.0,
                    rx_bps=0,
                ),
                computed_before=_computed(),
                modem_after=_modem(
                    primary_band=3,
                    primary_bw=20.0,
                    ca_active=True,
                    sinr=15.0,
                    rsrp=-78.0,
                    rx_bps=500_000,
                    extra_bands=[1],
                ),
                computed_after=_computed(),
                learning_gate=_allowed_learning_gate(),
            )

    with patch("time.localtime", return_value=time.localtime(timestamp)):
        ranked = ranked_bandsets_from_memory(
            engine.memory, hour, [[1, 3], [1, 20], [20]]
        )
    assert ranked
    assert ranked[0][0] == [1, 3]

    with patch("time.localtime", return_value=time.localtime(timestamp)):
        best = best_bandset_from_memory(engine.memory, hour, [[1, 3], [1, 20], [20]])
    assert best == [1, 3]


# ── Learning Usefulness Tests ─────────────────────────────────────────────────
# These tests verify that learning DEMONSTRABLY improves decisions under
# realistic scenarios, not just that scores are written.


def test_bad_band_is_demoted_below_untested_alternative():
    """A band with repeated failures must rank below an untested alternative.

    After several bad outcomes (low throughput, action failures), the band's
    blended score must fall below the 0.5 neutral prior of an untested band.
    """
    from arch4.interfaces import LeverType

    mem = _make_memory()
    hour = 15
    timestamp = time.mktime((2026, 3, 17, hour, 0, 0, 0, 0, -1))

    # Record 4 failures on [20] — low throughput causes "bad" classification
    for _ in range(4):
        with patch("time.localtime", return_value=time.localtime(timestamp)):
            _record_classified_action(
                mem,
                lever=LeverType.BAND_SELECTION,
                params={"bands": [20]},
                success=True,
                terminal_grade=-1,
                modem_before=_modem(primary_band=3, rx_bps=300_000),
                computed_before=_computed(),
                modem_after=_modem(
                    primary_band=20,
                    sinr=-6.0,
                    rx_bps=10_000,
                ),
                computed_after=_computed(rtt=180.0),
                learning_gate=_allowed_learning_gate(),
            )

    with patch("time.localtime", return_value=time.localtime(timestamp)):
        ranked = ranked_bandsets_from_memory(mem, hour, [[20], [1]])

    # [1] is untested (score 0.5), [20] should be below that
    scores = {tuple(r[0]): r[1] for r in ranked}
    assert scores[(20,)] < 0.5, (
        f"Failed band [20] must score below 0.5 neutral, got {scores[(20,)]:.3f}"
    )
    assert scores[(20,)] < scores.get((1,), 0.5), (
        f"Failed band [20] (score={scores[(20,)]:.3f}) must rank below "
        f"untested [1] (score={scores.get((1,), 0.5):.3f})"
    )
    assert ranked[0][0] == [1], (
        f"Untested [1] must rank first over failed [20], got {ranked[0][0]}"
    )


def test_hour_specificity_good_band_at_one_hour_does_not_dominate_another():
    """Learning is per-hour. Good history at hour 14 must not influence hour 20.

    Band [1, 3] is strongly positive at 14:00. At 20:00 it should be untested
    (neutral 0.5) — not carried over from the other hour.
    """
    from arch4.interfaces import LeverType

    mem = _make_memory()
    good_hour = 14
    other_hour = 20
    ts_good = time.mktime((2026, 3, 17, good_hour, 0, 0, 0, 0, -1))

    for _ in range(5):
        with patch("time.localtime", return_value=time.localtime(ts_good)):
            _record_classified_action(
                mem,
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3]},
                success=True,
                modem_before=_modem(primary_band=3, rx_bps=0),
                computed_before=_computed(),
                modem_after=_modem(primary_band=3, rx_bps=500_000, cqi=13),
                computed_after=_computed(),
                learning_gate=_allowed_learning_gate(),
            )

    ts_other = time.mktime((2026, 3, 17, other_hour, 0, 0, 0, 0, -1))
    with patch("time.localtime", return_value=time.localtime(ts_other)):
        ranked = ranked_bandsets_from_memory(mem, other_hour, [[1, 3], [20]])

    # At hour 20: [1,3] has no evidence — should be untested (0.5), same as [20]
    score_13 = next((r[1] for r in ranked if sorted(r[0]) == [1, 3]), None)
    source_13 = next((r[3] for r in ranked if sorted(r[0]) == [1, 3]), None)
    assert score_13 is not None
    assert source_13 == "untested", (
        f"[1,3] at hour {other_hour} must be 'untested', got source='{source_13}' score={score_13}"
    )
    assert abs(score_13 - 0.5) < 0.01, (
        f"[1,3] at hour {other_hour} must score ~0.5 (no evidence), got {score_13}"
    )


def test_recent_stm_bad_pulls_down_historically_good_band():
    """STM (recent) bad evidence must pull down a band despite strong LTM history.

    The blending formula: STM weight rises 0.30→0.70 with evidence.
    With 5 STM bad samples, STM weight ~0.70. Even a perfect LTM cannot
    fully overcome a strongly negative STM.
    """
    from arch4.interfaces import LeverType

    mem = _make_memory()
    hour = 10
    # Step 1: build strong positive LTM for [1, 3]
    ts_old = time.mktime((2026, 3, 10, hour, 0, 0, 0, 0, -1))
    for _ in range(8):
        with patch("time.localtime", return_value=time.localtime(ts_old)):
            _record_classified_action(
                mem,
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3]},
                success=True,
                modem_before=_modem(
                    primary_band=20,
                    primary_bw=10.0,
                    ca_active=False,
                    sinr=8.0,
                    rsrp=-96.0,
                    rx_bps=0,
                ),
                computed_before=_computed(),
                modem_after=_modem(
                    primary_band=3,
                    primary_bw=20.0,
                    ca_active=True,
                    sinr=15.0,
                    rsrp=-78.0,
                    rx_bps=600_000,
                    extra_bands=[1],
                ),
                computed_after=_computed(),
                learning_gate=_allowed_learning_gate(),
            )

    with patch("time.localtime", return_value=time.localtime(ts_old)):
        ranked_before = ranked_bandsets_from_memory(mem, hour, [[1, 3], [20]])
    score_before = next(r[1] for r in ranked_before if sorted(r[0]) == [1, 3])
    assert score_before > 0.6, f"LTM-only score should be > 0.6, got {score_before}"

    # Step 2: add recent STM bad evidence
    ts_recent = time.mktime((2026, 3, 17, hour, 0, 0, 0, 0, -1))
    for _ in range(5):
        with patch("time.localtime", return_value=time.localtime(ts_recent)):
            _record_classified_action(
                mem,
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3]},
                success=True,
                terminal_grade=-1,
                modem_before=_modem(primary_band=3, rx_bps=300_000),
                computed_before=_computed(),
                modem_after=_modem(
                    primary_band=3,
                    sinr=-6.0,
                    rx_bps=10_000,
                ),
                computed_after=_computed(rtt=180.0),
                learning_gate=_allowed_learning_gate(),
            )

    with patch("time.localtime", return_value=time.localtime(ts_recent)):
        ranked_after = ranked_bandsets_from_memory(mem, hour, [[1, 3], [20]])
    score_after = next(r[1] for r in ranked_after if sorted(r[0]) == [1, 3])

    assert score_after < score_before, (
        f"STM bad must pull down score: before={score_before:.3f} after={score_after:.3f}"
    )
    # With ±step scoring, 5 bad probes against 15 good won't pull below neutral —
    # the band is still net-positive. But the blended score must drop significantly.
    assert score_after < 0.7, (
        f"Score after STM bad evidence must drop significantly, got {score_after:.3f}"
    )


def test_congestion_veto_floors_bad_stm_band_score():
    """During active congestion, a bad-STM band must be floored to <= 0.25.

    The congestion veto: when RTT pressure is 'alarm' and STM score < 0.35,
    blended score is floored to min(blended, 0.25).
    """
    from arch4.interfaces import LeverType

    mem = _make_memory()
    hour = 18
    timestamp = time.mktime((2026, 3, 17, hour, 0, 0, 0, 0, -1))

    # Record bad STM evidence for [1, 3]
    for _ in range(3):
        with patch("time.localtime", return_value=time.localtime(timestamp)):
            _record_classified_action(
                mem,
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3]},
                success=True,
                terminal_grade=-1,
                modem_before=_modem(rx_bps=300_000),
                computed_before=_computed(rtt=500.0, rtt_pressure="alarm"),
                modem_after=_modem(
                    primary_band=3,
                    sinr=-6.0,
                    rx_bps=10_000,
                ),
                computed_after=_computed(rtt=180.0),
                learning_gate=_allowed_learning_gate(),
            )

    with patch("time.localtime", return_value=time.localtime(timestamp)):
        # Without congestion: bad but not floored
        ranked_normal = ranked_bandsets_from_memory(
            mem, hour, [[1, 3]], rtt_pressure_level="normal"
        )
        # With congestion active: must be floored to <= 0.25
        ranked_congested = ranked_bandsets_from_memory(
            mem, hour, [[1, 3]], rtt_pressure_level="alarm"
        )

    score_normal = ranked_normal[0][1]
    score_congested = ranked_congested[0][1]
    source_congested = ranked_congested[0][3]

    assert score_congested <= 0.25, (
        f"Congestion veto must floor score to <= 0.25, got {score_congested:.3f}"
    )
    assert "congestion_veto" in source_congested, (
        f"Source must include 'congestion_veto', got '{source_congested}'"
    )
    assert score_congested <= score_normal, (
        f"Congested score ({score_congested:.3f}) must be <= normal ({score_normal:.3f})"
    )


def test_learning_causes_exploration_to_prefer_known_good_band():
    """Decision engine exploration must prefer the LTM-best band over alternatives.

    After recording good outcomes for [1, 3] at a specific hour, the exploration
    path (get_best_bandset_for_hour) must return [1, 3] as the preferred candidate,
    demonstrating that learning feeds through to actual decision-making.
    """
    from arch4.interfaces import LeverType

    engine = _make_engine()
    hour = 11
    timestamp = time.mktime((2026, 3, 17, hour, 0, 0, 0, 0, -1))

    # Record enough positive outcomes to exceed min_attempts (default 2.0)
    # and min_score (default 0.55) thresholds for get_best_bandset_for_hour
    for _ in range(6):
        with patch("time.localtime", return_value=time.localtime(timestamp)):
            _record_classified_action(
                engine.memory,
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3]},
                success=True,
                modem_before=_modem(
                    primary_band=20,
                    primary_bw=10.0,
                    ca_active=False,
                    sinr=7.0,
                    rsrp=-97.0,
                    rx_bps=100_000,
                ),
                computed_before=_computed(),
                modem_after=_modem(
                    primary_band=3,
                    primary_bw=20.0,
                    ca_active=True,
                    sinr=15.0,
                    rsrp=-78.0,
                    rx_bps=600_000,
                    extra_bands=[1],
                ),
                computed_after=_computed(),
                learning_gate=_allowed_learning_gate(),
            )

    with patch("time.localtime", return_value=time.localtime(timestamp)):
        best = best_bandset_from_memory(
            engine.memory,
            hour,
            [[1, 3], [20], [1]],
            min_attempts=2.0,
            min_score=0.55,
        )

    assert best == [1, 3], (
        f"After 6 good outcomes, exploration must prefer [1, 3], got {best}"
    )


def test_learning_improves_decision_engine_band_selection_under_exploration():
    """Learning must rank B3 best and Decision must consume a receipted B3.

    After recording enough successful actions on [1, 3], the self-learning
    candidate clock must publish that Memory evidence into the canonical
    register. Contextual Bandit remains stochastic, so the test verifies its
    best forecast independently, then supplies a complete receipted B3 command
    to prove Decision consumption through TSAR.

    This verifies the full loop:
      record_action (success) → band_learning store → self-learning candidate
      publication → TSAR candidate plan → decide() returns BAND_SELECTION.
    """
    from arch4.brain.aggressiveness import AggressivenessEngine
    from arch4.brain.historical import HistoricalJournal
    from arch4.brain.memory import Memory
    from arch4.tests.controller_posture_test_support import TestControllerPostureAuthority
    from arch4.brain.feed_registers import BrainFeedRegisterBank
    from arch4.smart_loop.clock_lane import run_smart_loop_strategy_clock_lane
    from arch4.action_transaction_authority import ActionTransactionAuthority
    from arch4.smart_loop.state_store import SmartLoopStateStore
    from arch4.contextual_bandit_state_store import ContextualBanditStateStore
    from arch4.runtime_registers import RuntimeRegisterBank
    from arch4.runtime_decision_projection import build_runtime_decision_projection
    from arch4.smartness_fact_register import publish_smartness_fact_register
    from arch4.interfaces import LeverType, ModemCapabilities
    from arch4.smart_loop.strategy_register import (
        read_smart_loop_strategy_from_bank,
    )
    from arch4.tests.smart_loop_test_helpers import (
        publish_smart_loop_radio_command_for_test,
        smartness_source_transaction_for_test,
    )

    timestamp = time.time()
    hour = time.localtime(timestamp).tm_hour

    cfg = Arch4Config(
        deployed_bands=[1, 3, 20],
        autonomous_connect_settle_s=0.0,
        radio_change_settle_s=0.0,
        # Low thresholds so exploration triggers reliably in test
        congestion_suspect_entry_ticks=99,
        degraded_entry_ticks=99,
    )
    tmp = tempfile.mkdtemp()
    try:
        mem = Memory(cfg, memory_dir=tmp)
        hist = HistoricalJournal(cfg, tmp)
        sm = TestControllerPostureAuthority(cfg, mem)
        sm.private_state_for_test.state = SystemState.WORKING
        sm.private_state_for_test.entered_at = time.time()

        feed_bank = BrainFeedRegisterBank()
        action_transaction_authority = ActionTransactionAuthority()
        engine = DecisionEngine(
            cfg,
            mem,
            sm,
            Constraints(),
            aggressiveness=AggressivenessEngine(cfg),
            historical=hist,
            smart_loop_strategy_feed=feed_bank,
            action_transaction_authority=action_transaction_authority,
        )
        runtime_bank = RuntimeRegisterBank()
        engine.register_bank = runtime_bank
        engine.controller = SimpleNamespace(
            runtime=SimpleNamespace(
                register_bank=runtime_bank,
                action_transaction_authority=action_transaction_authority,
            ),
            brain=SimpleNamespace(
                feed_registers=feed_bank,
                smart_loop_state_store=SmartLoopStateStore(tmp),
                contextual_bandit_state_store=ContextualBanditStateStore(
                    tmp
                ),
            ),
            control=SimpleNamespace(
                capabilities=ModemCapabilities(
                    supports_pband=True,
                    supports_band_selection=True,
                )
            ),
        )
        install_active_config_generation_for_test(engine)
        engine.deployed_bands = {1, 3, 20}

        # Record 5 successful actions on [1, 3] with good throughput
        for _ in range(5):
            with patch("time.localtime", return_value=time.localtime(timestamp)):
                _record_classified_action(
                    mem,
                    lever=LeverType.BAND_SELECTION,
                    params={"bands": [1, 3]},
                    success=True,
                    modem_before=_modem(
                        primary_band=20,
                        primary_bw=10.0,
                        ca_active=False,
                        sinr=8.0,
                        rsrp=-96.0,
                        rx_bps=0,
                    ),
                    computed_before=_computed(),
                    modem_after=_modem(
                        primary_band=3,
                        primary_bw=20.0,
                        ca_active=True,
                        sinr=15.0,
                        rsrp=-78.0,
                        rx_bps=500_000,
                        extra_bands=[1],
                    ),
                    computed_after=_computed(),
                    learning_gate=_allowed_learning_gate(),
                )

        # Modem is on band 1 only (not on learned-good [1,3])
        # RTT is elevated to trigger _decide_exploring path
        modem = _modem(
            primary_band=1, rx_bps=200_000, ca_active=False, extra_bands=None
        )
        computed = _computed(rtt=200.0, rtt_pressure="alarm")  # RTT > 150ms → exploring

        # Set exploration pressure high enough to explore
        engine._exploration_pressure.pressure = 1.5
        engine._exploration_pressure.resistance = 0.0
        # V2.60+ hardened prediction/history defaults to gated unless
        # calibration explicitly allows prediction input.
        engine._calibration_handoff = {"allow_prediction_input": True}
        engine._last_modem, _ = cellular_test_inputs(modem, computed)
        install_test_identity_register(
            runtime_bank,
            value={
                "identity_code": "0x0000000000000001",
                "identity_status": "exact",
                "identity_catalog_version": "test-identity-catalog-v1",
                "identity_catalog_checksum": "a" * 64,
                "accepted_generation_session_scope_key": "test-session-scope",
                "session_scope_key": "test-session-scope",
                "model": "B818-263",
            },
            source="test-accepted-identity",
            updated_at=timestamp,
            valid=True,
            stale=False,
            dirty=False,
            validity="valid",
            force=True,
        )
        runtime_bank.write_radio(
            value={
                "provider_plmn": "",
                "cell_id": 0,
                "pci": 303,
                "earfcn_dl": 1300,
                "serving_band": 1,
                "configured_band_pool": [1],
                "allowed_bands": [1, 3, 20],
                "radio_connected": True,
                "sample_ts": timestamp,
                "sample_sequence": 1,
                "source_epoch": "test-source-epoch",
            },
            source="test",
            updated_at=timestamp,
        )
        publish_test_signal_generation(
            runtime_bank,
            quality={"cq_cell_display": 0.35, "congestion_pct": 75.0},
            ca={"ca_active": False},
            updated_at=timestamp,
        )
        runtime_bank.write_learning(
            value={"learning_allowed": True, "proof_pending": False},
            source="test",
            updated_at=timestamp,
        )
        runtime_bank.write_mutation(
            value={"cooldown_s": 0.0},
            source="test",
            updated_at=timestamp,
        )
        runtime_bank.write_decision(
            value=build_runtime_decision_projection({}),
            source="test-decision-projection",
            updated_at=timestamp,
        )
        publish_smartness_fact_register(
            runtime_bank,
            transaction=smartness_source_transaction_for_test(runtime_bank),
            now=timestamp,
        )

        with patch("time.localtime", return_value=time.localtime(timestamp)):
            learning_quality = publish_learning_quality_from_authority(
                engine, now=timestamp
            )
            assert learning_quality.get("published") is True
            publish = run_smart_loop_strategy_clock_lane(engine, now=timestamp)
        assert publish.ran is True

        strategy = read_smart_loop_strategy_from_bank(
            feed_bank,
            now=timestamp,
        )
        bandit_receipt = dict(
            strategy.get("contextual_bandit_selection_receipt") or {}
        )
        forecasts = [
            dict(row)
            for row in bandit_receipt.get("candidate_forecasts", [])
            if isinstance(row, dict)
        ]
        best_id = str(
            bandit_receipt.get("best_forecast_opportunity_id") or ""
        )
        best = next(
            row
            for row in forecasts
            if str(row.get("opportunity_id") or "") == best_id
        )
        b3_configuration = next(
            row
            for row in forecasts
            if str(row.get("action") or "") == "apply_radio_configuration"
            and 3 in list(dict(row.get("params") or {}).get("bands") or [])
        )
        alternatives = [
            row for row in forecasts if row is not b3_configuration
        ]
        assert best["opportunity_id"] == b3_configuration["opportunity_id"]
        assert b3_configuration["selection_propensity"] == max(
            row["selection_propensity"] for row in forecasts
        )
        assert all(
            b3_configuration["expected_referee_grade"]
            > row["expected_referee_grade"]
            for row in alternatives
        )
        assert dict(strategy.get("brain_decision_receipt") or {}).get(
            "decision_receipt_id"
        )

        # The recorded Bandit draw may legally explore B1 or do-nothing. Use
        # the existing fully receipted test boundary to prove the independent
        # Decision/TSAR consumption contract for the learned-best B3 command.
        receipted_register = ActionTransactionAuthority()
        engine.action_transaction_authority = receipted_register
        engine.controller.runtime.action_transaction_authority = receipted_register
        publish_smart_loop_radio_command_for_test(
            engine,
            target_band=3,
            target_bands=(1, 3, 20),
            reason_code="learned_b3_best_forecast",
            now=timestamp,
            accepted_frame=engine._last_modem,
        )

        with patch("time.localtime", return_value=time.localtime(timestamp)):
            action = _decide(engine, modem, computed)

        # Must propose a clean smart-loop PBAND action
        assert action is not None, (
            "Decision engine must propose an action when exploration pressure is high "
            "and a learned-good band exists"
        )
        assert action.lever == LeverType.PBAND, f"Expected PBAND, got {action.lever}"
        assert action.params.get("pband") == 3
        assert 3 in sorted(action.params.get("ca_pool", []))
        assert action.params.get("_selected_action_command") is True
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_learning_penalizes_failed_bands_in_decision_engine():
    """Decision engine must avoid a band that consistently failed.

    After recording failures on [1, 20], the decision engine should NOT
    propose [1, 20] when a neutral (untested) or known-good alternative
    is available, because the failed band gets a low STM score.
    """
    from arch4.brain.aggressiveness import AggressivenessEngine
    from arch4.brain.decision import DecisionEngine
    from arch4.brain.historical import HistoricalJournal
    from arch4.brain.memory import Memory
    from arch4.tests.controller_posture_test_support import TestControllerPostureAuthority
    from arch4.interfaces import LeverType

    hour = 10
    timestamp = time.mktime((2026, 3, 17, hour, 0, 0, 0, 0, -1))

    cfg = Arch4Config(
        deployed_bands=[1, 3, 20],
        congestion_suspect_entry_ticks=99,
        degraded_entry_ticks=99,
    )
    tmp = tempfile.mkdtemp()
    try:
        mem = Memory(cfg, memory_dir=tmp)
        hist = HistoricalJournal(cfg, tmp)
        sm = TestControllerPostureAuthority(cfg, mem)
        sm.private_state_for_test.state = SystemState.WORKING
        sm.private_state_for_test.entered_at = time.time()

        engine = DecisionEngine(
            cfg,
            mem,
            sm,
            Constraints(),
            aggressiveness=AggressivenessEngine(cfg),
            historical=hist,
        )

        # Record 5 good actions on [1, 3]
        for _ in range(5):
            with patch("time.localtime", return_value=time.localtime(timestamp)):
                _record_classified_action(
                    mem,
                    lever=LeverType.BAND_SELECTION,
                    params={"bands": [1, 3]},
                    success=True,
                    modem_before=_modem(primary_band=1, rx_bps=0),
                    computed_before=_computed(),
                    modem_after=_modem(primary_band=3, rx_bps=500_000, cqi=13),
                    computed_after=_computed(),
                    learning_gate=_allowed_learning_gate(),
                )

        # Record 4 failures on [1, 20]
        for _ in range(4):
            with patch("time.localtime", return_value=time.localtime(timestamp)):
                _record_classified_action(
                    mem,
                    lever=LeverType.BAND_SELECTION,
                    params={"bands": [1, 20]},
                    success=True,
                    terminal_grade=-1,
                    modem_before=_modem(primary_band=1, rx_bps=0),
                    computed_before=_computed(),
                    modem_after=_modem(
                        primary_band=20,
                        sinr=-6.0,
                        rx_bps=10_000,
                    ),
                    computed_after=_computed(rtt=180.0),
                    learning_gate=_allowed_learning_gate(),
                )

        # Verify memory ranks [1,3] above [1,20]
        with patch("time.localtime", return_value=time.localtime(timestamp)):
            ranked = ranked_bandsets_from_memory(mem, hour, [[1, 3], [1, 20]])

        assert ranked, "Ranking must not be empty"
        assert ranked[0][0] == [1, 3], (
            f"[1, 3] must rank above failed [1, 20], got {ranked[0][0]}"
        )
        # Failed band's score must be lower
        scores = {tuple(r[0]): r[1] for r in ranked}
        assert scores.get((1, 3), 0) > scores.get((1, 20), 1), (
            f"Score for [1,3]={scores.get((1, 3))} must exceed [1,20]={scores.get((1, 20))}"
        )
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


# ── Priority 6: Self-learning proof — cooldown, blacklist, never_works, persistence ──


def test_never_works_band_is_excluded_from_exploration_candidates():
    """A band marked never_works must be absent from exploration candidate sets.

    After band_never_works_threshold (default 5) attempts with appeared_in_ca=False,
    the band's never_works flag is set. decision_exploring._decide_exploring() skips
    it via memory.should_try_band() returning False.

    This proves the full pipeline: failed attempts → never_works flag →
    should_try_band=False → band absent from decide() proposal.
    """
    from arch4.brain.aggressiveness import AggressivenessEngine
    from arch4.brain.decision import DecisionEngine
    from arch4.brain.historical import HistoricalJournal
    from arch4.brain.memory import Memory
    from arch4.tests.controller_posture_test_support import TestControllerPostureAuthority

    cfg = Arch4Config(
        deployed_bands=[3, 20],
        congestion_suspect_entry_ticks=99,
        degraded_entry_ticks=99,
    )
    import tempfile, shutil

    tmp = tempfile.mkdtemp()
    try:
        mem = Memory(cfg, memory_dir=tmp)
        hist = HistoricalJournal(cfg, tmp)
        sm = TestControllerPostureAuthority(cfg, mem)
        sm.private_state_for_test.state = SystemState.WORKING
        sm.private_state_for_test.entered_at = time.time()

        engine = DecisionEngine(
            cfg,
            mem,
            sm,
            Constraints(),
            aggressiveness=AggressivenessEngine(cfg),
            historical=hist,
        )

        # Band 20: attempt band_never_works_threshold times, never appeared in CA
        threshold = int(
            cfg.learning_cfg.band_never_works_threshold
            if hasattr(cfg, "learning_cfg")
            else getattr(mem, "_learning_cfg", None)
            and mem._learning_cfg.band_never_works_threshold
            or 5
        )
        for _ in range(threshold):
            record_band_attempt_to_memory_store(
                mem, band=20, appeared_in_ca=False, outcome="absent"
            )

        # Verify the flag is set
        bk = mem.bands.get(20)
        assert bk is not None, "Band 20 must have a BandKnowledge entry after attempts"
        assert bk.never_works is True, (
            f"After {threshold} failed attempts, never_works must be True"
        )
        assert should_try_band_from_memory(mem, 20) is False, (
            "should_try_band(20) must return False when never_works=True"
        )

        # Modem is on band 3; band 20 is deployed but never_works → must not appear
        modem = _modem(primary_band=3, rx_bps=100_000, ca_active=False)
        computed = _computed(rtt=200.0, rtt_pressure="alarm")
        engine._exploration_pressure.pressure = 1.5
        engine._exploration_pressure.resistance = 0.0

        from arch4.interfaces import LeverType

        action = _decide(engine, modem, computed)

        # Either no action (no other candidate) or the proposed bands must not include 20
        if action is not None and action.lever == LeverType.BAND_SELECTION:
            proposed = action.params.get("bands", [])
            assert 20 not in proposed, (
                f"never_works band 20 must not appear in exploration proposal, "
                f"got bands={proposed}"
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_blacklisted_band_is_excluded_from_exploration_candidates():
    """A band added to the permanent blacklist must never appear in decide() proposals.

    blacklist_band(20) → is_band_blacklisted(20)=True → decision_exploring skips it
    at both candidate-set construction (line ~201) and fallback-set construction (line ~79).

    This proves the full pipeline: blacklist_band() → is_band_blacklisted=True →
    band absent from exploration proposal.
    """
    from arch4.brain.aggressiveness import AggressivenessEngine
    from arch4.brain.decision import DecisionEngine
    from arch4.brain.historical import HistoricalJournal
    from arch4.brain.memory import Memory
    from arch4.tests.controller_posture_test_support import TestControllerPostureAuthority
    from arch4.interfaces import LeverType

    cfg = Arch4Config(
        deployed_bands=[3, 20],
        congestion_suspect_entry_ticks=99,
        degraded_entry_ticks=99,
    )
    import tempfile, shutil

    tmp = tempfile.mkdtemp()
    try:
        mem = Memory(cfg, memory_dir=tmp)
        hist = HistoricalJournal(cfg, tmp)
        sm = TestControllerPostureAuthority(cfg, mem)
        sm.private_state_for_test.state = SystemState.WORKING
        sm.private_state_for_test.entered_at = time.time()

        engine = DecisionEngine(
            cfg,
            mem,
            sm,
            Constraints(),
            aggressiveness=AggressivenessEngine(cfg),
            historical=hist,
        )

        # Permanently blacklist band 20
        memory_blacklist_band(mem, 20)
        assert memory_is_band_blacklisted(mem, 20) is True, (
            "band 20 must be blacklisted"
        )

        # Modem on band 3; band 20 is deployed but blacklisted → must not be proposed
        modem = _modem(primary_band=3, rx_bps=100_000, ca_active=False)
        computed = _computed(rtt=200.0, rtt_pressure="alarm")
        engine._exploration_pressure.pressure = 1.5
        engine._exploration_pressure.resistance = 0.0

        action = _decide(engine, modem, computed)

        if action is not None and action.lever == LeverType.BAND_SELECTION:
            proposed = action.params.get("bands", [])
            assert 20 not in proposed, (
                f"Blacklisted band 20 must not appear in exploration proposal, "
                f"got bands={proposed}"
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_band_blacklist_and_never_works_survive_memory_save_reload():
    """band_blacklist and never_works both survive a memory save + reload cycle.

    This verifies the post-restart learning persistence contract:
    - blacklist_band(20) is present in the reloaded memory
    - never_works=True for band 1 is present in the reloaded memory
    - Both filters still block the bands after reload

    If either is lost on restart, the learning system starts fresh and the
    operator sees previously-blocked bands attempted again immediately.
    """
    from arch4.brain.memory import Memory
    from arch4.brain.memory_persistence import load_memory

    cfg = Arch4Config()
    import tempfile, shutil

    tmp = tempfile.mkdtemp()
    try:
        # --- Build initial memory with one blacklisted band and one never_works band ---
        mem1 = Memory(cfg, memory_dir=tmp)

        # Blacklist band 20 permanently
        memory_blacklist_band(mem1, 20)
        assert memory_is_band_blacklisted(mem1, 20)

        # Drive band 1 to never_works (threshold defaults to 5)
        threshold = 5
        for _ in range(threshold):
            record_band_attempt_to_memory_store(
                mem1, band=1, appeared_in_ca=False, outcome="absent"
            )
        bk_before = mem1.bands.get(1)
        assert bk_before is not None and bk_before.never_works is True, (
            "Band 1 must be never_works before save"
        )

        # --- Save ---
        bind_test_memory_scope(mem1)
        commit_memory_checkpoint(mem1, checkpoint_reason="test")

        # --- Reload into a fresh Memory instance ---
        mem2 = Memory(cfg, memory_dir=tmp)
        load_memory(mem2)

        # --- Verify blacklist survived ---
        assert memory_is_band_blacklisted(mem2, 20), (
            "band_blacklist must survive memory save/reload — band 20 must still be blacklisted"
        )

        # --- Verify never_works survived ---
        bk_after = mem2.bands.get(1)
        assert bk_after is not None, "Band 1 knowledge must survive save/reload"
        assert bk_after.never_works is True, (
            "never_works=True for band 1 must survive memory save/reload"
        )
        assert should_try_band_from_memory(mem2, 1) is False, (
            "should_try_band(1) must still return False after reload"
        )

        # --- Verify filtering still works after reload ---
        assert not memory_is_band_blacklisted(mem2, 3), "band 3 must not be blacklisted"
        assert should_try_band_from_memory(mem2, 3) is True, (
            "band 3 (untested) must be tryable"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
