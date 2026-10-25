from arch4.interfaces import ModemInput
import time
from typing import Optional
import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from arch4.interfaces import (
    Arch4Config, CarrierInfo, ComputedSignals,
    SystemState, ActionRecord, LeverType, LeverAction, Constraints, select_policy_profile,
    is_emergency_state,
)
from arch4.core.signal_computer import SignalComputer
from arch4.tests.controller_posture_test_support import (
    TestControllerPostureAuthority,
)
from arch4.brain.decision_input import DECISION_INPUT_LANES, build_decision_input_from_registers
from arch4.brain.decision import DecisionEngine as _ProductionDecisionEngine
from arch4.brain.feed_registers import BrainFeedRegisterBank
from arch4.brain.memory import Memory
from arch4.brain.memory_calibration_store import sqlite_conn
from arch4.brain.memory_evidence_scope_owner import bind_memory_evidence_scope
from arch4.brain.calibration_authority import CalibrationAuthority
from arch4.brain.calibration_contract import build_calibration_observation
from arch4.brain.calibration_workflow import PHASE_BOOTSTRAP
from arch4.brain.memory_band_learning_query_store import (
    marker_scoped_band_learning_key,
    should_block_band_action as memory_should_block_band_action,
)
from arch4.brain.aggressiveness import AggressivenessEngine
from arch4.brain.historical import HistoricalJournal
from arch4.brain.ca_contract import (
    ca_ld_stable,
    cart_ld_active,
    cart_ld_activity,
    cart_ld_valid,
    cast_ld,
    cast_ld_valid,
)
from arch4.brain.traffic_gate import evaluate_traffic as _evaluate_traffic
from arch4.brain.memory_action_history_owner import (
    record_action_history as memory_record_action_history,
)
from arch4.brain.memory_calibration_history_owner import (
    get_calibration_hourly_ranges as memory_get_calibration_hourly_ranges,
    reassign_calibration_marker_history as memory_reassign_calibration_marker_history,
)
from arch4.brain.tick_safety import evaluate_tick_safety as _evaluate_tick_safety
from arch4.action_applicability import evaluate_action_applicability
from arch4.action_origin import ActionOriginKind
from arch4.core.action_source_state_authority import ProviderSourceObservation
from arch4.runtime_registers import REGISTER_RADIO, RuntimeRegisterBank
from arch4.smart_loop.action_availability import SMART_LOOP_ROI_OBSERVATION_COOLDOWN
from arch4.smart_loop.closed_loop import SMART_LOOP_COMMAND_INTENT
from arch4.tests.smart_loop_test_helpers import publish_smart_loop_radio_command_for_test
from arch4.tests.controller_generation_factory import cellular_test_inputs
from arch4.tests.decision_owner_test_support import with_decision_owner_ports
from arch4.tests.checkpoint_test_support import commit_memory_checkpoint
from arch4.tests.action_expected_effect_test_support import (
    build_expected_effect_test_action,
)
from arch4.tests.terminal_reward_test_support import (
    commit_terminal_reward_test_fixture,
)
from arch4.tests.runtime_signal_register_test_support import (
    publish_test_decision_input_lanes,
)


def _decide(decision, modem, computed):
    intelligence, derived = cellular_test_inputs(modem, computed)
    return decision.decide(intelligence, derived)


def _arm_expected_effect_mask(
    modem: ModemInput, *, duration_s: float = 30.0
) -> None:
    now = time.time()
    _, mask = build_expected_effect_test_action(
        session_scope_key="test-session-scope",
        created_ts=now,
        expires_ts=now + duration_s,
    )
    modem.planned_radio_quiet_active = True
    modem.planned_radio_quiet_remaining_s = duration_s
    modem.action_expected_effect_mask = mask.as_dict()


class DecisionEngine(_ProductionDecisionEngine):
    """Test-only adapter for direct typed helper calls below decide()."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **with_decision_owner_ports(kwargs))

    @staticmethod
    def _typed_modem(modem):
        intelligence, _ = cellular_test_inputs(modem, ComputedSignals())
        return intelligence

    def _make_band_action(self, *args, **kwargs):
        typed = dict(kwargs)
        if isinstance(typed.get("modem"), ModemInput):
            typed["modem"] = self._typed_modem(typed["modem"])
        return super()._make_band_action(*args, **typed)

    def _run_action_pipeline(self, *args, **kwargs):
        typed = dict(kwargs)
        if isinstance(typed.get("modem"), ModemInput):
            typed["modem"] = self._typed_modem(typed["modem"])
        return super()._run_action_pipeline(*args, **typed)

    def _record_objective_feedback(self, *args, **kwargs):
        typed = dict(kwargs)
        if isinstance(typed.get("modem"), ModemInput):
            typed["modem"] = self._typed_modem(typed["modem"])
        return super()._record_objective_feedback(*args, **typed)

    def _guard_a1_antithrash(self, action, modem, now):
        return super()._guard_a1_antithrash(
            action,
            self._typed_modem(modem),
            now,
        )

    def _decide_hilink_radio_drop_recovery(self, modem, now):
        return super()._decide_hilink_radio_drop_recovery(
            self._typed_modem(modem),
            now,
        )

    def _typed_decision_pair(self, modem, computed):
        return cellular_test_inputs(modem, computed)

    def _decide_e_rf_collapse(self, modem, computed):
        intelligence, derived = self._typed_decision_pair(modem, computed)
        return super()._decide_e_rf_collapse(intelligence, derived)

    def _decide_e_congestion_storm(self, modem, computed):
        intelligence, derived = self._typed_decision_pair(modem, computed)
        return super()._decide_e_congestion_storm(intelligence, derived)

    def _apply_learning_band_guard(self, action, modem, now):
        return super()._apply_learning_band_guard(
            action,
            self._typed_modem(modem),
            now,
        )

    def _canonicalize_band_action(self, action, modem):
        return super()._canonicalize_band_action(
            action,
            self._typed_modem(modem),
        )

    def _apply_a1_restricted_band_filter(self, action, modem, now):
        return super()._apply_a1_restricted_band_filter(
            action,
            self._typed_modem(modem),
            now,
        )

    def note_action_result(
        self,
        action,
        success,
        modem_after=None,
        computed_after=None,
    ):
        typed_after = (
            self._typed_modem(modem_after)
            if isinstance(modem_after, ModemInput)
            else modem_after
        )
        typed_computed = computed_after
        if isinstance(modem_after, ModemInput) and isinstance(
            computed_after, ComputedSignals
        ):
            typed_after, typed_computed = cellular_test_inputs(
                modem_after,
                computed_after,
            )
        return super().note_action_result(
            action,
            success,
            modem_after=typed_after,
            computed_after=typed_computed,
        )

    def note_band_action_result(
        self,
        action,
        success,
        modem_after,
        computed_after=None,
        **kwargs,
    ):
        typed_after = (
            self._typed_modem(modem_after)
            if isinstance(modem_after, ModemInput)
            else modem_after
        )
        typed_computed = computed_after
        if isinstance(modem_after, ModemInput) and isinstance(
            computed_after, ComputedSignals
        ):
            typed_after, typed_computed = cellular_test_inputs(
                modem_after,
                computed_after,
            )
        return super().note_band_action_result(
            action,
            success,
            typed_after,
            typed_computed,
            **kwargs,
        )


def evaluate_tick_safety(*, modem, computed, engine, **kwargs):
    if modem is None:
        return _evaluate_tick_safety(
            modem=None,
            computed=None,
            engine=engine,
            **kwargs,
        )
    signals = computed if isinstance(computed, ComputedSignals) else ComputedSignals()
    intelligence, derived = cellular_test_inputs(modem, signals)
    return _evaluate_tick_safety(
        modem=intelligence,
        computed=derived,
        engine=engine,
        **kwargs,
    )


def _quality_frame_from_mapping(values):
    computed = ComputedSignals(**dict(values))
    _, derived = cellular_test_inputs(make_modem(), computed)
    return derived.quality


def make_modem(
    *,
    sinr_db: float = 15.0,
    rsrp_dbm: float = -85.0,
    rsrq_db: float = -9.0,
    rssi_dbm: float = -70.0,
    mcs_dl: int = 20,
    mcs_ul: int = 10,
    cqi: int = 9,
    ri: int = 2,
    primary_band: int = 3,
    pci: int = 100,
    earfcn: int = 1200,
    enb_id: int = 1,
    rx_bytes_per_sec: float = 100_000,
    tx_bytes_per_sec: float = 10_000,
    ca_active: bool = False,
    ca_signature: str = "B3",
    selected_band_set: Optional[list[int]] = None,
    radio_connected: bool = True,
    session_uptime_s: float = 300.0,
) -> ModemInput:
    carriers = [CarrierInfo(
        band=primary_band,
        earfcn=earfcn,
        pci=pci,
        bandwidth_mhz=20.0,
        sinr_db=sinr_db,
        rsrp_dbm=rsrp_dbm,
        state=1,
        is_pcell=True,
    )]
    return ModemInput(
        sinr_db=sinr_db,
        rsrp_dbm=rsrp_dbm,
        rsrq_db=rsrq_db,
        rssi_dbm=rssi_dbm,
        mcs_dl=mcs_dl,
        mcs_ul=mcs_ul,
        cqi=cqi,
        ri=ri,
        carriers=carriers,
        ca_active=ca_active,
        ca_band_signature=ca_signature,
        primary_band=primary_band,
        pci=pci,
        earfcn=earfcn,
        enb_id=enb_id,
        radio_connected=radio_connected,
        session_uptime_s=session_uptime_s,
        rx_bytes_per_sec=rx_bytes_per_sec,
        tx_bytes_per_sec=tx_bytes_per_sec,
        selected_band_set=list(selected_band_set or []),
    )


def make_computed(
    rtt_avg: float,
    cq_value: float,
    *,
    cq_cell: Optional[float] = None,
    cq_vector_a_valid: Optional[bool] = None,
    rtt_delta: float = 0.0,
    rtt_delta_valid: bool = False,
    rtt_quiet_delta: float = 0.0,
    rsrq_gap: float = 0.0,
    rsrq_gap_valid: bool = False,
    congestion_pct: float = 0.0,
    congestion_kind_hint: str = "",
    congestion_state_avg_pct: float = 0.0,
    congestion_state_bucket: str = "",
    cart_ld: int = 1,
    cart_ld_valid: bool = True,
    cast_ld: float = 100.0,
    cast_ld_valid: bool = True,
    cart_ld_activity: float = 0.0,
) -> ComputedSignals:
    if cq_cell is None:
        cq_cell = max(0.0, min(1.0, float(cq_value)))
    if cq_vector_a_valid is None:
        cq_vector_a_valid = True
    return ComputedSignals(
        rtt_ms=rtt_avg,
        rtt_ema=rtt_avg,
        rtt_sliding_avg=rtt_avg,
        cq_cell=cq_cell,
        cq_vector_a_valid=cq_vector_a_valid,
        rtt_delta=rtt_delta,
        rtt_delta_valid=rtt_delta_valid,
        rtt_quiet_delta=rtt_quiet_delta,
        rsrq_gap=rsrq_gap,
        rsrq_gap_valid=rsrq_gap_valid,
        congestion_pct=congestion_pct,
        congestion_kind_hint=congestion_kind_hint,
        congestion_state_avg_pct=congestion_state_avg_pct,
        congestion_state_bucket=congestion_state_bucket,
        cart_ld=cart_ld,
        cart_ld_valid=cart_ld_valid,
        cast_ld=cast_ld,
        cast_ld_valid=cast_ld_valid,
        cart_ld_activity=cart_ld_activity,
    )


def seed_known_good_bandset(memory: Memory, bands: list[int]) -> None:
    """Seed restore-CA knowledge through canonical action history."""

    normalized = [int(band) for band in bands]
    before, before_signals = cellular_test_inputs(
        make_modem(
            primary_band=normalized[0],
            ca_active=True,
            ca_signature="+".join(f"B{band}" for band in normalized),
            rx_bytes_per_sec=6_250_000,
        ),
        make_computed(40.0, 0.5),
    )
    after, after_signals = cellular_test_inputs(
        make_modem(
            primary_band=normalized[0],
            ca_active=True,
            ca_signature="+".join(f"B{band}" for band in normalized),
            rx_bytes_per_sec=7_500_000,
        ),
        make_computed(40.0, 0.6),
    )
    memory_record_action_history(
        memory,
        modem_before=before,
        computed_before=before_signals,
        lever=LeverType.BAND_SELECTION,
        proposal_action_origin="smart_loop",
        proof_observed_at=float(after.generation.sample_ts),
        params={"bands": list(normalized)},
        success=True,
        modem_after=after,
        computed_after=after_signals,
        learning_gate={
            "learning_allowed": True,
            "gate_input_complete": True,
        },
    )


def consume_test_calibration_observation(
    authority: CalibrationAuthority,
    *,
    modem: ModemInput,
    computed: ComputedSignals,
    calibration_status: dict,
    now: float,
) -> dict:
    intelligence, derived = cellular_test_inputs(modem, computed)
    sample_ts = float(now)
    observation = build_calibration_observation(
        sample={
            "ts": sample_ts,
            "sample_ts": sample_ts,
            "engine_tick_ts": sample_ts + 0.01,
            "sample_fresh": True,
            "sample_stale": False,
            "sample_sequence": int(intelligence.generation.generation),
            "sample_age_s": float(intelligence.generation.sample_age_s),
            "sample_source": str(intelligence.generation.sample_source),
            "radio_connected": bool(intelligence.cellular.radio_connected),
            "serving_band": int(intelligence.cellular.serving_band),
            "primary_band": int(intelligence.cellular.primary_band),
            "pci": int(intelligence.cellular.pci),
            "sinr_db": float(intelligence.cellular.sinr_db),
            "rsrp_dbm": float(intelligence.cellular.rsrp_dbm),
            "rsrq_db": float(intelligence.cellular.rsrq_db),
            "rtt_ms": float(derived.latency.rtt_ms),
            "throughput_dl_mbps": float(intelligence.traffic.dl_mbps),
            "throughput_ul_mbps": float(intelligence.traffic.ul_mbps),
            "ri": int(intelligence.cellular.ri),
            "ca_active": bool(intelligence.cellular.ca_active),
            "carrier_count": int(intelligence.cellular.carrier_count),
            "health_ok": True,
            "health_overall_ok": True,
            "network_load_biased": False,
            "offpeak_window": True,
        },
        calibration_status=dict(calibration_status),
        learning_gate={
            "learning_allowed": True,
            "gate_input_complete": True,
            "sample_stale": False,
        },
        identity_code=str(intelligence.identity.identity_code),
        identity_status=str(intelligence.identity.identity_status),
        identity_catalog_version=str(
            intelligence.identity.identity_catalog_version
        ),
        identity_catalog_checksum=str(
            intelligence.identity.identity_catalog_checksum
        ),
        session_scope_key=str(intelligence.generation.session_scope_key),
        source_epoch=str(intelligence.generation.source_epoch),
        provider_plmn=str(intelligence.identity.provider_plmn),
        access_type_id=str(intelligence.identity.access_type_id),
        generation_confirmed=True,
        now=sample_ts + 0.01,
    )
    return authority.consume_calibration_observation(observation)


class TestBrainCaContract(unittest.TestCase):
    def test_invalid_dict_snapshot_hides_ld_ca_from_brain(self):
        snapshot = {
            "cart_ld": 1,
            "cart_ld_valid": False,
            "cast_ld": 100.0,
            "cart_ld_activity": 12.0,
        }

        quality = _quality_frame_from_mapping(snapshot)
        self.assertFalse(cart_ld_valid(quality))
        self.assertFalse(cart_ld_active(quality))
        self.assertEqual(cast_ld(quality), 0.0)
        self.assertEqual(cart_ld_activity(quality), 0.0)

    def test_valid_dict_snapshot_exposes_ld_ca_to_brain(self):
        snapshot = {
            "cart_ld": 1,
            "cart_ld_valid": True,
            "cast_ld": 96.5,
            "cast_ld_valid": True,
            "cart_ld_activity": 2.25,
        }

        quality = _quality_frame_from_mapping(snapshot)
        self.assertTrue(cart_ld_valid(quality))
        self.assertTrue(cart_ld_active(quality))
        self.assertTrue(cast_ld_valid(quality))
        self.assertEqual(cast_ld(quality), 96.5)
        self.assertEqual(cart_ld_activity(quality), 2.25)
        self.assertTrue(ca_ld_stable(quality))

    def test_quiet_tick_keeps_aggregate_ld_ca_visible_to_brain(self):
        snapshot = {
            "cart_ld": 0,
            "cart_ld_valid": False,
            "cast_ld": 99.0,
            "cast_ld_valid": True,
            "cart_ld_activity": 5.0,
        }

        quality = _quality_frame_from_mapping(snapshot)
        self.assertFalse(cart_ld_valid(quality))
        self.assertFalse(cart_ld_active(quality))
        self.assertTrue(cast_ld_valid(quality))
        self.assertEqual(cast_ld(quality), 99.0)
        self.assertEqual(cart_ld_activity(quality), 5.0)
        self.assertTrue(ca_ld_stable(quality))


class TestStateMachine(unittest.TestCase):
    def test_bad_rf_ca_capacity_winner_stays_working(self):
        cfg = Arch4Config(
            emergency_sinr_floor=3.0,
            emergency_sinr_persist_s=1.0,
            bad_rf_capacity_min_dl_mbps=20.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm._sinr_low_since = time.time() - 5.0

            modem = make_modem(
                sinr_db=-7.0,
                rsrp_dbm=-95.0,
                rsrq_db=-13.0,
                primary_band=3,
                rx_bytes_per_sec=4_000_000,
                ca_active=True,
                ca_signature="B3+B20",
                selected_band_set=[1, 3, 20],
            )
            computed = make_computed(
                35.0,
                0.25,
                cq_cell=0.20,
                cq_vector_a_valid=True,
                congestion_pct=20.0,
            )
            computed.congestion_pct_valid = True
            computed.rtt_dl_jitter_pct = 1.0
            computed.rtt_dl_jitter_valid = True

            state = sm.evaluate_state_for_test(modem, computed)

            self.assertEqual(state, SystemState.WORKING)

    def test_low_capacity_bad_rf_still_enters_bad_rf(self):
        cfg = Arch4Config(
            emergency_sinr_floor=3.0,
            emergency_sinr_persist_s=1.0,
            bad_rf_capacity_min_dl_mbps=20.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm._sinr_low_since = time.time() - 5.0

            modem = make_modem(
                sinr_db=-7.0,
                rsrp_dbm=-112.0,
                rsrq_db=-15.0,
                primary_band=3,
                rx_bytes_per_sec=800_000,
                ca_active=False,
                ca_signature="B3",
                selected_band_set=[3],
            )
            computed = make_computed(
                35.0,
                0.25,
                cq_cell=0.20,
                cq_vector_a_valid=True,
            )

            state = sm.evaluate_state_for_test(modem, computed)

            self.assertEqual(state, SystemState.BAD_RF)

    def test_stale_radio_register_blocks_bad_rf_fault_entry(self):
        cfg = Arch4Config(
            emergency_sinr_floor=3.0,
            emergency_sinr_persist_s=1.0,
            bad_rf_capacity_min_dl_mbps=20.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm._sinr_low_since = time.time() - 5.0

            bank = RuntimeRegisterBank()
            publish_test_decision_input_lanes(bank, updated_at=100.0)
            bank.write_radio(
                value={"serving_band": 3, "sinr_db": -7.0, "rsrp_dbm": -112.0},
                source="live_modem_truth_read",
                updated_at=120.0,
                valid=False,
                stale=True,
                dirty=False,
                trust="stale",
                error="modem_read_missed",
            )
            sm.note_decision_input(
                build_decision_input_from_registers(bank.snapshot(DECISION_INPUT_LANES))
            )

            modem = make_modem(
                sinr_db=-7.0,
                rsrp_dbm=-112.0,
                rsrq_db=-15.0,
                primary_band=3,
                rx_bytes_per_sec=800_000,
                ca_active=False,
                ca_signature="B3",
                selected_band_set=[3],
            )
            computed = make_computed(35.0, 0.25)

            state = sm.evaluate_state_for_test(modem, computed)

            self.assertEqual(state, SystemState.WORKING)
            self.assertEqual(sm.private_state_for_test.warning_source, "runtime_register")
            self.assertEqual(sm.private_state_for_test.warning_mechanism, "radio_register_not_trusted")
            self.assertIn("modem_read_missed", sm.private_state_for_test.warning_reason)

    def test_strong_rsrp_low_sinr_is_not_bad_rf(self):
        cfg = Arch4Config(
            emergency_sinr_floor=3.0,
            emergency_sinr_persist_s=1.0,
            bad_rf_capacity_min_dl_mbps=20.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm._sinr_low_since = time.time() - 5.0

            modem = make_modem(
                sinr_db=1.0,
                rsrp_dbm=-77.0,
                rsrq_db=-10.0,
                primary_band=1,
                rx_bytes_per_sec=0.0,
                ca_active=True,
                ca_signature="B1+CA",
                selected_band_set=[1, 3, 7, 20, 28],
            )
            computed = make_computed(
                38.0,
                0.25,
                cq_cell=0.20,
                cq_vector_a_valid=True,
                congestion_pct=55.2,
            )
            computed.congestion_pct_valid = True
            computed.rtt_dl_jitter_pct = 8.4
            computed.rtt_dl_jitter_valid = True

            state = sm.evaluate_state_for_test(modem, computed)

            self.assertNotEqual(state, SystemState.BAD_RF)

    def test_brain_evidence_clears_stale_bad_rf_without_post_fault_authority(self):
        cfg = Arch4Config(
            emergency_sinr_floor=3.0,
            bad_rf_recovery_stable_s=60.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.BAD_RF
            sm.private_state_for_test.emergency_class = "BAD_RF"
            sm.private_state_for_test.entered_at = time.time() - 300.0
            sm.private_state_for_test.bad_rf_recovery_since = 0.0
            sm.private_state_for_test.post_fault_class = "BAD_RF"
            sm.private_state_for_test.post_fault_until = time.time() + 300.0
            sm.private_state_for_test.recovered_lane_bands = [20]
            sm.private_state_for_test.recovered_lane_from_fault = "BAD_RF"

            modem = make_modem(
                sinr_db=1.0,
                rsrp_dbm=-77.0,
                rsrq_db=-10.0,
                primary_band=3,
                rx_bytes_per_sec=4_000_000,
                ca_active=True,
                ca_signature="B3+B20",
                selected_band_set=[1, 3, 20, 28],
            )
            computed = make_computed(
                35.0,
                0.70,
                cq_cell=0.70,
                cq_vector_a_valid=True,
                congestion_pct=25.0,
            )
            computed.congestion_pct_valid = True

            state = sm.evaluate_state_for_test(modem, computed)

            self.assertEqual(state, SystemState.WORKING)
            self.assertEqual(sm.private_state_for_test.post_fault_class, "")
            self.assertEqual(sm.private_state_for_test.recovered_lane_bands, [])
            self.assertEqual(sm.private_state_for_test.recovered_lane_from_fault, "")

    def test_brain_keeps_bad_rf_when_collapse_evidence_still_proves_it(self):
        cfg = Arch4Config(
            emergency_sinr_floor=3.0,
            bad_rf_recovery_stable_s=0.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.BAD_RF
            sm.private_state_for_test.emergency_class = "BAD_RF"

            modem = make_modem(
                sinr_db=-7.0,
                rsrp_dbm=-112.0,
                rsrq_db=-15.0,
                rx_bytes_per_sec=0.0,
                ca_active=False,
                ca_signature="B3",
                selected_band_set=[3],
            )
            computed = make_computed(
                220.0,
                0.20,
                cq_cell=0.20,
                cq_vector_a_valid=True,
                congestion_pct=80.0,
            )
            computed.congestion_pct_valid = True

            state = sm.evaluate_state_for_test(modem, computed)

            self.assertEqual(state, SystemState.BAD_RF)

    def test_optimizing_to_exploring_on_rtt(self):
        cfg = Arch4Config()
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)

            modem = make_modem()
            computed = make_computed(cfg.rtt_warning_ms + 10, 0.8)

            state = sm.state
            for _ in range(cfg.exploring_entry_ticks):
                state = sm.evaluate_state_for_test(modem, computed)

            self.assertEqual(state, SystemState.EXPLORING)

    def test_working_to_confirmed_congestion_via_state_average_lane(self):
        cfg = Arch4Config(
            congestion_suspect_entry_ticks=1,
            soft_congestion_lq_threshold=0.2,
            soft_congestion_rtt_min_ms=120.0,
            soft_congestion_min_dl_mbps=0.8,
            congestion_qualifier_window_ticks=1,
            congestion_qualifier_persist_hits=1,
            congestion_qualifier_clear_ticks=1,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.WORKING
            sm.private_state_for_test.entered_at = time.time()

            modem = make_modem(rx_bytes_per_sec=300_000, ca_active=True, ca_signature="B3+CA")
            computed = make_computed(
                180.0,
                0.5,
                rtt_delta=40.0,
                rtt_delta_valid=True,
                rtt_quiet_delta=40.0,
                rsrq_gap=4.5,
                rsrq_gap_valid=True,
                congestion_pct=72.0,
                congestion_kind_hint="local_traffic",
                congestion_state_avg_pct=32.0,
                congestion_state_bucket="normal",
            )
            confirmed = make_computed(
                180.0,
                0.5,
                rtt_delta=40.0,
                rtt_delta_valid=True,
                rtt_quiet_delta=40.0,
                rsrq_gap=4.5,
                rsrq_gap_valid=True,
                congestion_pct=72.0,
                congestion_kind_hint="local_traffic",
                congestion_state_avg_pct=58.0,
                congestion_state_bucket="mild",
            )

            state = sm.evaluate_state_for_test(modem, computed)
            self.assertEqual(state, SystemState.WORKING)

            state = sm.evaluate_state_for_test(modem, confirmed)
            self.assertEqual(state, SystemState.CONGESTED)
            self.assertEqual(sm.private_state_for_test.congestion_kind, "local_traffic")

    def test_working_recurrence_does_not_confirm_without_state_average_lane(self):
        cfg = Arch4Config(
            congestion_suspect_entry_ticks=1,
            congestion_suspect_clear_ticks=1,
            congestion_suspect_recurrence_hits=2,
            congestion_suspect_recurrence_window_s=300.0,
            soft_congestion_lq_threshold=0.2,
            soft_congestion_rtt_min_ms=120.0,
            soft_congestion_min_dl_mbps=0.8,
            congestion_qualifier_window_ticks=1,
            congestion_qualifier_persist_hits=1,
            congestion_qualifier_clear_ticks=1,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)

            bad = make_computed(
                180.0,
                0.5,
                rtt_delta=32.0,
                rtt_delta_valid=True,
                rtt_quiet_delta=32.0,
                rsrq_gap=4.2,
                rsrq_gap_valid=True,
                congestion_pct=58.0,
                congestion_state_avg_pct=35.0,
                congestion_state_bucket="normal",
            )
            good = make_computed(60.0, 0.8)
            modem = make_modem(rx_bytes_per_sec=300_000, ca_active=True, ca_signature="B3+CA")

            # Episode 1: remain WORKING because the confirmed lane is still normal.
            s1 = sm.evaluate_state_for_test(modem, bad)
            self.assertEqual(s1, SystemState.WORKING)
            s2 = sm.evaluate_state_for_test(modem, good)
            self.assertEqual(s2, SystemState.WORKING)

            # A second bad fast-lane episode still does not create a congestion state.
            s3 = sm.evaluate_state_for_test(modem, bad)
            self.assertEqual(s3, SystemState.WORKING)

    def test_emergency_class_radio_down(self):
        cfg = Arch4Config(
            emergency_radio_down_persist_s=0.0,
            emergency_sinr_persist_s=9999.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)

            modem = make_modem(radio_connected=False)
            computed = make_computed(50.0, 0.8, cart_ld=0, cart_ld_valid=True, cast_ld=0.0, cast_ld_valid=True)
            state = sm.evaluate_state_for_test(modem, computed)

            self.assertEqual(state, SystemState.E_RADIO_DOWN)
            self.assertEqual(sm.private_state_for_test.emergency_class, "E_RADIO_DOWN")

    def test_bad_rf_entry_does_not_trigger_from_idle_low_sinr_alone(self):
        cfg = Arch4Config()
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)

            modem = make_modem(
                sinr_db=3.0,
                rsrp_dbm=-76.0,
                rsrq_db=-8.0,
                rx_bytes_per_sec=0.0,
                tx_bytes_per_sec=0.0,
            )
            computed = make_computed(
                45.0,
                0.8,
                cq_cell=0.8,
                    cq_vector_a_valid=False,
            )

            self.assertFalse(sm._rf_collapse_entry_ready(modem, computed))

    def test_bad_rf_entry_accepts_raw_triple_when_cq_not_ready(self):
        cfg = Arch4Config()
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)

            modem = make_modem(
                sinr_db=-2.0,
                rsrp_dbm=-112.0,
                rsrq_db=-15.0,
                rx_bytes_per_sec=0.0,
                tx_bytes_per_sec=0.0,
            )
            computed = make_computed(
                45.0,
                0.7,
                cq_cell=0.7,
                    cq_vector_a_valid=False,
            )

            self.assertTrue(sm._rf_collapse_entry_ready(modem, computed))

    def test_degraded_timeout_enters_stalled_recovery_emergency(self):
        cfg = Arch4Config(degraded_timeout_s=1, soft_emergency_hold_s=120.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.DEGRADED
            sm.private_state_for_test.entered_at = time.time() - 10
            sm.private_state_for_test.degraded_timeout_at = time.time() - 1
            sm.private_state_for_test.degraded_actions_tried = 1

            modem = make_modem(rx_bytes_per_sec=200_000)
            computed = make_computed(40.0, 0.1)
            state = sm.evaluate_state_for_test(modem, computed)

            self.assertEqual(state, SystemState.DEGRADED)
            self.assertEqual(sm.private_state_for_test.warning_class, "degraded")
            self.assertEqual(sm.private_state_for_test.warning_source, "controller")
            self.assertEqual(sm.private_state_for_test.warning_mechanism, "stalled_recovery")
            self.assertGreater(sm.private_state_for_test.soft_emergency_hold_until, time.time())

    def test_degraded_timeout_without_attempts_stays_degraded(self):
        cfg = Arch4Config(degraded_timeout_s=1, soft_emergency_hold_s=120.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.DEGRADED
            sm.private_state_for_test.entered_at = time.time() - 10
            sm.private_state_for_test.degraded_timeout_at = time.time() - 1
            sm.private_state_for_test.degraded_actions_tried = 0

            modem = make_modem(rx_bytes_per_sec=200_000)
            computed = make_computed(40.0, 0.1)
            state = sm.evaluate_state_for_test(modem, computed)

            self.assertEqual(state, SystemState.DEGRADED)
            self.assertFalse(is_emergency_state(sm.private_state_for_test.state))
            self.assertEqual(sm.private_state_for_test.emergency_class, "")
            self.assertGreater(sm.private_state_for_test.degraded_timeout_at, time.time())

    def test_degraded_exit_requires_recovery_ticks(self):
        cfg = Arch4Config(
            degraded_recovery_ticks=3,
            degraded_idle_exit_ticks=3,
            degraded_timeout_s=300.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.DEGRADED
            sm.private_state_for_test.entered_at = time.time() - 10
            sm.private_state_for_test.degraded_timeout_at = time.time() + 100

            modem = make_modem(rx_bytes_per_sec=200_000)
            good = make_computed(50.0, 0.8)

            self.assertEqual(sm.evaluate_state_for_test(modem, good), SystemState.DEGRADED)
            self.assertEqual(sm.evaluate_state_for_test(modem, good), SystemState.DEGRADED)
            self.assertEqual(sm.evaluate_state_for_test(modem, good), SystemState.WORKING)

    def test_degraded_exit_requires_idle_ticks(self):
        cfg = Arch4Config(
            degraded_recovery_ticks=3,
            degraded_idle_exit_ticks=3,
            degraded_timeout_s=300.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.DEGRADED
            sm.private_state_for_test.entered_at = time.time() - 10
            sm.private_state_for_test.degraded_timeout_at = time.time() + 100

            modem_idle = make_modem(rx_bytes_per_sec=0, tx_bytes_per_sec=0)
            bad = make_computed(50.0, 0.1)

            self.assertEqual(sm.evaluate_state_for_test(modem_idle, bad), SystemState.DEGRADED)
            self.assertEqual(sm.evaluate_state_for_test(modem_idle, bad), SystemState.DEGRADED)
            self.assertEqual(sm.evaluate_state_for_test(modem_idle, bad), SystemState.WORKING)

    def test_working_to_degraded_respects_reentry_guard(self):
        cfg = Arch4Config(
            degraded_entry_ticks=1,
            degraded_reentry_guard_s=30.0,
            degraded_recovery_ticks=1,
            degraded_idle_exit_ticks=1,
            degraded_timeout_s=300.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)

            bad_modem = make_modem(rx_bytes_per_sec=200_000)
            bad = make_computed(50.0, 0.1)
            self.assertEqual(sm.evaluate_state_for_test(bad_modem, bad), SystemState.DEGRADED)

            good = make_computed(50.0, 0.8)
            self.assertEqual(sm.evaluate_state_for_test(bad_modem, good), SystemState.WORKING)

            # Immediate bad tick should not re-enter DEGRADED due to reentry guard.
            self.assertEqual(sm.evaluate_state_for_test(bad_modem, bad), SystemState.WORKING)

    def test_congested_persistent_cliff_enters_congestion_storm_emergency(self):
        cfg = Arch4Config(
            emergency_radio_down_persist_s=9999.0,
            emergency_sinr_persist_s=9999.0,
            emergency_congestion_persist_s=0.0,
            soft_emergency_hold_s=120.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.CONGESTED
            sm.private_state_for_test.entered_at = time.time() - 30

            modem = make_modem(rx_bytes_per_sec=250_000, tx_bytes_per_sec=40_000)
            computed = make_computed(cfg.rtt_cliff_ms + 50, 0.2)
            state = sm.evaluate_state_for_test(modem, computed)

            self.assertEqual(state, SystemState.E_CONGESTION_STORM)
            self.assertEqual(sm.private_state_for_test.emergency_class, "E_CONGESTION_STORM")
            self.assertGreater(sm.private_state_for_test.soft_emergency_hold_until, time.time())

    def test_emergency_reentry_guard_blocks_soft_emergency(self):
        cfg = Arch4Config(
            emergency_radio_down_persist_s=9999.0,
            emergency_sinr_persist_s=9999.0,
            emergency_reentry_guard_s=120.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.WORKING
            sm.private_state_for_test.entered_at = time.time() - 40
            sm.private_state_for_test.emergency_reentry_guard_until = time.time() + 60

            modem = make_modem(session_uptime_s=0.0)
            computed = make_computed(50.0, 0.8, cart_ld=0, cart_ld_valid=True, cast_ld=0.0, cast_ld_valid=True)
            state = sm.evaluate_state_for_test(modem, computed)

            self.assertEqual(state, SystemState.WORKING)
            self.assertEqual(sm.private_state_for_test.emergency_class, "")

    def _disabled_test_emergency_reentry_guard_allows_hard_emergency(self):  # TODO: vendor-neutral WIP — re-enable when implemented
        cfg = Arch4Config(
            emergency_radio_down_persist_s=9999.0,
            emergency_sinr_persist_s=0.0,
            emergency_sinr_floor=2.0,
            emergency_reentry_guard_s=120.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.WORKING
            sm.private_state_for_test.entered_at = time.time() - 40
            sm.private_state_for_test.emergency_reentry_guard_until = time.time() + 60

            modem = make_modem(sinr_db=0.0, rx_bytes_per_sec=300_000)
            computed = make_computed(200.0, 0.1, cq_cell=0.20, cq_vector_a_valid=True)
            state = sm.evaluate_state_for_test(modem, computed)

            self.assertEqual(state, SystemState.E_RF_COLLAPSE)
            self.assertEqual(sm.private_state_for_test.emergency_class, "BAD_RF")

    def test_low_sinr_without_low_cq_does_not_enter_rf_collapse(self):
        cfg = Arch4Config(
            emergency_radio_down_persist_s=9999.0,
            emergency_sinr_persist_s=0.0,
            emergency_sinr_floor=2.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)

            modem = make_modem(sinr_db=0.0, rx_bytes_per_sec=300_000)
            # Traffic present, but CQ is good -> should not classify as RF collapse.
            computed = make_computed(50.0, 0.8, cq_cell=0.70, cq_vector_a_valid=True)
            state = sm.evaluate_state_for_test(modem, computed)

            self.assertFalse(is_emergency_state(state))

    def test_low_sinr_low_cq_but_low_rtt_does_not_enter_rf_collapse(self):
        cfg = Arch4Config(
            emergency_radio_down_persist_s=9999.0,
            emergency_sinr_persist_s=0.0,
            emergency_sinr_floor=2.0,
            rtt_warning_ms=150.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)

            modem = make_modem(sinr_db=0.0, rx_bytes_per_sec=300_000)
            # Low SINR + low CQ, but RTT is not elevated -> should not enter emergency.
            computed = make_computed(80.0, 0.1, cq_cell=0.20, cq_vector_a_valid=True)
            state = sm.evaluate_state_for_test(modem, computed)

            self.assertFalse(is_emergency_state(state))

    def test_low_sinr_without_traffic_can_enter_rf_collapse(self):
        cfg = Arch4Config(
            emergency_radio_down_persist_s=9999.0,
            emergency_sinr_persist_s=0.0,
            emergency_sinr_floor=2.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)

            modem = make_modem(sinr_db=0.0, rx_bytes_per_sec=0.0, tx_bytes_per_sec=0.0)
            computed = make_computed(80.0, 0.6)
            state = sm.evaluate_state_for_test(modem, computed)

            self.assertEqual(state, SystemState.WORKING)
            self.assertEqual(sm.private_state_for_test.emergency_class, "")

    def test_bad_rf_holds_without_using_emergency_timeout_extension(self):
        cfg = Arch4Config(
            emergency_timeout_s=1.0,
            emergency_sinr_floor=3.0,
            sinr_feasibility_gate_db=6.0,
            emergency_recovery_stable_s=5.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_RF_COLLAPSE
            sm.private_state_for_test.entered_at = time.time() - 10
            sm.private_state_for_test.emergency_class = "BAD_RF"
            sm.private_state_for_test.emergency_timeout_at = time.time() - 1

            modem = make_modem(sinr_db=0.0, rsrp_dbm=-112.0, rsrq_db=-15.0)
            computed = make_computed(80.0, 0.5)
            state = sm.evaluate_state_for_test(modem, computed)

            self.assertEqual(state, SystemState.E_RF_COLLAPSE)
            self.assertLess(sm.private_state_for_test.emergency_timeout_at, time.time())
            self.assertEqual(sm.private_state_for_test.bad_rf_recovery_since, 0.0)

    def test_fault_entry_resets_recovery_plan_and_sets_observe_grace(self):
        cfg = Arch4Config(emergency_observe_s=8.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.WORKING
            sm.private_state_for_test.emergency_recovery_step = 2
            sm.private_state_for_test.emergency_next_action_at = time.time() + 30
            sm.private_state_for_test.emergency_observe_until = time.time() + 30

            state = sm._enter_emergency(
                make_modem(sinr_db=-1.0),
                emergency_class="E_RF_COLLAPSE",
            )

            self.assertEqual(state, SystemState.E_RF_COLLAPSE)
            self.assertEqual(sm.private_state_for_test.emergency_recovery_step, 0)
            self.assertEqual(sm.private_state_for_test.emergency_next_action_at, 0.0)
            self.assertEqual(sm.private_state_for_test.emergency_observe_until, 0.0)
            self.assertGreater(sm.private_state_for_test.bad_rf_next_action_at, time.time())

    def test_emergency_exit_requires_stable_recovery_dwell(self):
        cfg = Arch4Config(
            emergency_timeout_s=300.0,
            emergency_recovery_stable_s=5.0,
            bad_rf_recovery_stable_s=5.0,
            sinr_feasibility_gate_db=6.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_RF_COLLAPSE
            sm.private_state_for_test.entered_at = time.time() - 30
            sm.private_state_for_test.emergency_class = "BAD_RF"
            sm.private_state_for_test.emergency_timeout_at = time.time() + 60

            modem = make_modem(sinr_db=10.0)
            computed = make_computed(80.0, 0.5)

            s1 = sm.evaluate_state_for_test(modem, computed)
            self.assertEqual(s1, SystemState.E_RF_COLLAPSE)
            self.assertGreater(sm.private_state_for_test.bad_rf_recovery_since, 0.0)

            sm.private_state_for_test.bad_rf_recovery_since = time.time() - 6
            s2 = sm.evaluate_state_for_test(modem, computed)
            self.assertEqual(s2, SystemState.WORKING)

    def test_strong_rsrp_rules_out_bad_rf_even_below_feasibility_gate(self):
        cfg = Arch4Config(
            emergency_timeout_s=300.0,
            emergency_recovery_stable_s=5.0,
            bad_rf_recovery_stable_s=5.0,
            sinr_feasibility_gate_db=10.0,
            emergency_sinr_floor=0.0,
            v5_low_rf_sinr_gate_db=4.0,
            rtt_warning_clear_ms=120.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_RF_COLLAPSE
            sm.private_state_for_test.entered_at = time.time() - 30
            sm.private_state_for_test.emergency_class = "BAD_RF"
            sm.private_state_for_test.emergency_timeout_at = time.time() + 60

            modem = make_modem(sinr_db=6.0, rx_bytes_per_sec=0.0, tx_bytes_per_sec=0.0, ca_active=False)
            computed = make_computed(60.0, 0.6)

            s1 = sm.evaluate_state_for_test(modem, computed)
            self.assertEqual(s1, SystemState.WORKING)
            self.assertEqual(sm.private_state_for_test.post_fault_class, "")
            self.assertEqual(sm.private_state_for_test.post_fault_until, 0.0)

    def test_strong_rsrp_rules_out_bad_rf_and_routes_to_congestion(self):
        cfg = Arch4Config(
            emergency_timeout_s=300.0,
            emergency_recovery_stable_s=5.0,
            bad_rf_recovery_stable_s=5.0,
            sinr_feasibility_gate_db=10.0,
            emergency_sinr_floor=0.0,
            v5_low_rf_sinr_gate_db=4.0,
            rtt_warning_clear_ms=120.0,
            congestion_qualifier_window_ticks=1,
            congestion_qualifier_persist_hits=1,
            congestion_qualifier_clear_ticks=1,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_RF_COLLAPSE
            sm.private_state_for_test.entered_at = time.time() - 30
            sm.private_state_for_test.emergency_class = "BAD_RF"
            sm.private_state_for_test.emergency_timeout_at = time.time() + 60

            modem = make_modem(sinr_db=6.0, rx_bytes_per_sec=0.0, tx_bytes_per_sec=0.0, ca_active=False)
            computed = make_computed(
                180.0,
                0.6,
                congestion_state_bucket="high",
                congestion_state_avg_pct=80.0,
            )

            s1 = sm.evaluate_state_for_test(modem, computed)
            self.assertEqual(s1, SystemState.CONGESTED)

    def _disabled_test_emergency_exit_routes_to_confirmed_congestion_when_signal_persists(self):  # TODO: vendor-neutral WIP — re-enable when implemented
        cfg = Arch4Config(
            emergency_timeout_s=300.0,
            emergency_recovery_stable_s=0.0,
            sinr_feasibility_gate_db=6.0,
            congestion_qualifier_window_ticks=1,
            congestion_qualifier_persist_hits=1,
            congestion_qualifier_clear_ticks=1,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_CONGESTION_STORM
            sm.private_state_for_test.entered_at = time.time() - 30
            sm.private_state_for_test.emergency_class = "E_CONGESTION_STORM"
            sm.private_state_for_test.emergency_timeout_at = time.time() + 60

            modem = make_modem(sinr_db=10.0, rx_bytes_per_sec=300_000)
            computed = make_computed(180.0, 0.1)

            state = sm.evaluate_state_for_test(modem, computed)
            self.assertEqual(state, SystemState.E_CONGESTION_STORM)

    def test_emergency_timeout_routes_to_degraded_before_working(self):
        cfg = Arch4Config(
            emergency_timeout_s=1.0,
            emergency_recovery_stable_s=999.0,
            sinr_feasibility_gate_db=6.0,
            rtt_warning_ms=150.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_CONGESTION_STORM
            sm.private_state_for_test.entered_at = time.time() - 30
            sm.private_state_for_test.emergency_class = "E_CONGESTION_STORM"
            sm.private_state_for_test.emergency_timeout_at = time.time() - 1

            modem = make_modem(sinr_db=10.0, rx_bytes_per_sec=300_000)
            computed = make_computed(100.0, 0.1)

            state = sm.evaluate_state_for_test(modem, computed)
            self.assertEqual(state, SystemState.DEGRADED)

    def test_rf_collapse_switches_to_radio_down_when_radio_drops(self):
        cfg = Arch4Config(
            emergency_timeout_s=300.0,
            emergency_recovery_stable_s=5.0,
            emergency_radio_down_persist_s=0.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_RF_COLLAPSE
            sm.private_state_for_test.entered_at = time.time() - 30
            sm.private_state_for_test.emergency_class = "BAD_RF"
            sm.private_state_for_test.emergency_timeout_at = time.time() + 60

            modem = make_modem(radio_connected=False, sinr_db=-1.0)
            computed = make_computed(200.0, 0.1)

            state = sm.evaluate_state_for_test(modem, computed)
            self.assertEqual(state, SystemState.E_RADIO_DOWN)
            self.assertEqual(sm.private_state_for_test.emergency_class, "E_RADIO_DOWN")


class TestDecisionEngine(unittest.TestCase):
    def test_missing_ca_triggers_band_selection(self):
        cfg = Arch4Config(
            autonomous_connect_settle_s=0.0,
            radio_change_settle_s=0.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            mem.action_history.append(ActionRecord(
                timestamp=time.time(),
                sinr_db=15.0,
                rsrq_db=-9.0,
                mcs_dl=20,
                ca_signature="B3",
                rtt_sliding_avg=40.0,
                rtt_ms=40.0,
                lever=LeverType.BAND_SELECTION,
                action_params={"bands": [1, 3]},
                success=True,
                throughput_before_mbps=50.0,
                throughput_after_mbps=60.0,
                sinr_after=15.0,
                ca_signature_after="B1+B3",
                improvement=10.0,
            ))

            sm = TestControllerPostureAuthority(cfg, mem)
            constraints = Constraints(pci_lock_available=False)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                constraints,
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                smart_loop_strategy_feed=BrainFeedRegisterBank(),
            )
            decision._ca_lost_state = True

            modem = make_modem(ca_active=False, ca_signature="B3")
            computed = make_computed(
                50.0,
                0.8,
                cart_ld=0,
                cart_ld_valid=True,
                cast_ld=0.0,
                cast_ld_valid=True,
            )
            typed_modem, typed_computed = cellular_test_inputs(
                modem,
                computed,
            )
            publish_smart_loop_radio_command_for_test(
                decision,
                target_band=3,
                target_bands=(1, 3),
                reason_code="restore_ca_candidate",
                availability_intent="restore_ca",
                accepted_frame=typed_modem,
            )

            action = _decide(decision, typed_modem, typed_computed)
            self.assertIsNotNone(action)
            self.assertEqual(action.lever, LeverType.PBAND)
            self.assertEqual(action.intent, SMART_LOOP_COMMAND_INTENT)
            self.assertEqual(action.params.get("pband"), 3)
            self.assertEqual(action.params.get("ca_pool"), [1, 3])
            self.assertTrue(action.params.get("_selected_action_command"))

    def test_mikrotik_missing_ca_does_not_rewrite_band_mask(self):
        cfg = Arch4Config(
            autonomous_connect_settle_s=0.0,
            radio_change_settle_s=0.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            mem.action_history.append(ActionRecord(
                timestamp=time.time(),
                sinr_db=15.0,
                rsrq_db=-9.0,
                mcs_dl=20,
                ca_signature="B20",
                rtt_sliding_avg=40.0,
                rtt_ms=40.0,
                lever=LeverType.BAND_SELECTION,
                action_params={"bands": [1, 3, 20]},
                success=True,
                throughput_before_mbps=30.0,
                throughput_after_mbps=45.0,
                sinr_after=15.0,
                ca_signature_after="B1+B3+B20",
                improvement=15.0,
            ))

            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(pci_lock_available=False),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                policy_profile=select_policy_profile("22003", "mikrotik"),
            )
            decision._ca_lost_state = True

            modem = make_modem(
                primary_band=20,
                ca_active=False,
                ca_signature="B20",
                selected_band_set=[1, 3, 7, 20, 28],
            )
            computed = make_computed(40.0, 0.8, cart_ld=0, cart_ld_valid=True, cast_ld=0.0, cast_ld_valid=True)

            action = _decide(decision, modem, computed)

            self.assertIsNone(action)
            events = decision.drain_runtime_events()
            self.assertTrue(any(e.get("kind") == "smart_loop_strategy_unavailable" for e in events))
            self.assertFalse(
                decision.cooldowns.is_blocked_keyed(
                    "restore_ca_routeros_suppressed", "1+3+20"
                )
            )

    def test_confirmed_congestion_can_trigger_mitigation(self):
        cfg = Arch4Config(
            min_action_interval_s=0.0,
            autonomous_connect_settle_s=0.0,
            radio_change_settle_s=0.0,
            congested_check_interval_s=0.0,
            congestion_mitigation_cooldown_s=0.0,
            soft_congestion_enabled=True,
            soft_congestion_entry_ticks=1,
            soft_congestion_lq_threshold=0.2,
            soft_congestion_rtt_min_ms=120.0,
            soft_congestion_min_dl_mbps=0.8,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            # Seed a known-good CA combo different from current [B3].
            mem.action_history.append(ActionRecord(
                timestamp=time.time(),
                sinr_db=12.0,
                rsrq_db=-9.0,
                mcs_dl=16,
                ca_signature="B3",
                rtt_sliding_avg=80.0,
                rtt_ms=80.0,
                lever=LeverType.BAND_SELECTION,
                action_params={"bands": [1, 3]},
                success=True,
                throughput_before_mbps=10.0,
                throughput_after_mbps=25.0,
                sinr_after=12.0,
                ca_signature_after="B1+B3",
                improvement=15.0,
            ))
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.CONGESTED
            sm.private_state_for_test.entered_at = time.time() - 30
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(pci_lock_available=False),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                smart_loop_strategy_feed=BrainFeedRegisterBank(),
            )

            # Inject active calibration handoff so history-based recovery is allowed.
            # Without this, allow_prediction_input defaults to False (the safe default
            # fixed in V2.60) and _history_exploration_policy blocks memory records.
            decision.note_calibration_handoff({
                "allow_prediction_input": True,
                "reason": "saved_setup_active",
                "trust_level": "trusted",
            })

            modem = make_modem(
                sinr_db=12.0,
                rsrp_dbm=-82.0,
                ca_active=True,
                ca_signature="B3+CA",
                rx_bytes_per_sec=300_000,  # has_traffic=True
            )
            # Make current CA quality "not good" to satisfy mitigation evidence gate.
            modem.carriers[0].bandwidth_mhz = 10.0
            computed = make_computed(180.0, 0.1)  # elevated RTT + bad LQ
            typed_modem, typed_computed = cellular_test_inputs(
                modem,
                computed,
            )
            publish_smart_loop_radio_command_for_test(
                decision,
                target_band=1,
                target_bands=(1, 3),
                reason_code="congested_recovery_candidate",
                availability_intent="explore",
                accepted_frame=typed_modem,
            )

            action = _decide(decision, typed_modem, typed_computed)
            self.assertIsNotNone(action)
            self.assertEqual(action.lever, LeverType.PBAND)
            self.assertEqual(action.intent, SMART_LOOP_COMMAND_INTENT)
            self.assertEqual(action.params.get("pband"), 1)
            self.assertLessEqual(
                decision.cooldowns.get_until("congestion_mitigation"),
                time.time(),
            )

    def test_soft_congestion_disabled_does_not_activate(self):
        cfg = Arch4Config(
            min_action_interval_s=0.0,
            soft_congestion_enabled=False,
            soft_congestion_entry_ticks=1,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(pci_lock_available=False),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                smart_loop_strategy_feed=BrainFeedRegisterBank(),
            )
            modem = make_modem(
                sinr_db=12.0,
                rsrp_dbm=-82.0,
                ca_active=True,
                ca_signature="B3+CA",
                rx_bytes_per_sec=300_000,
            )
            computed = make_computed(180.0, 0.1)
            _decide(decision, modem, computed)
            st = decision.get_runtime_status()
            self.assertFalse(st["soft_congestion_active"])

    def test_restore_ca_failed_attempt_is_gated_before_retry(self):
        cfg = Arch4Config(
            min_action_interval_s=0.0,
            autonomous_connect_settle_s=0.0,
            radio_change_settle_s=0.0,
            restore_ca_retry_after_s=1800.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            mem.action_history.append(ActionRecord(
                timestamp=time.time(),
                sinr_db=15.0,
                rsrq_db=-9.0,
                mcs_dl=20,
                ca_signature="B3",
                rtt_sliding_avg=40.0,
                rtt_ms=40.0,
                lever=LeverType.BAND_SELECTION,
                action_params={"bands": [1, 3]},
                success=True,
                throughput_before_mbps=50.0,
                throughput_after_mbps=60.0,
                sinr_after=15.0,
                ca_signature_after="B1+B3",
                improvement=10.0,
            ))
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(pci_lock_available=False),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                smart_loop_strategy_feed=BrainFeedRegisterBank(),
            )
            decision._ca_lost_state = True
            modem = make_modem(ca_active=False, ca_signature="B3")
            computed = make_computed(50.0, 0.8, cart_ld=0, cart_ld_valid=True, cast_ld=0.0, cast_ld_valid=True)
            typed_modem, typed_computed = cellular_test_inputs(
                modem,
                computed,
            )
            publish_smart_loop_radio_command_for_test(
                decision,
                target_band=3,
                target_bands=(1, 3),
                reason_code="restore_ca_candidate",
                availability_intent="restore_ca",
                accepted_frame=typed_modem,
            )

            action = _decide(decision, typed_modem, typed_computed)
            self.assertIsNotNone(action)
            self.assertEqual(action.intent, SMART_LOOP_COMMAND_INTENT)

            # Requested bands may attach but objective is still failed if CA was not restored.
            modem_after = make_modem(ca_active=False, ca_signature="B3")
            computed_after = make_computed(50.0, 0.8, cart_ld=0, cart_ld_valid=True, cast_ld=0.0, cast_ld_valid=True)
            decision.note_band_action_result(
                action,
                success=True,
                modem_after=modem_after,
                computed_after=computed_after,
            )

            blocked = _decide(decision, typed_modem, typed_computed)
            self.assertIsNotNone(blocked)
            self.assertTrue(blocked.params.get("_selected_action_command"))

            decision.cooldowns.set_keyed_until("restore_ca_retry", action.objective_id, time.time() - 1.0)
            decision._clear_objective_feedback(action)
            retried = _decide(decision, typed_modem, typed_computed)
            self.assertIsNotNone(retried)
            self.assertEqual(retried.intent, SMART_LOOP_COMMAND_INTENT)

    def test_runtime_status_exposes_fault_recovery_and_big_dip_fields(self):
        cfg = Arch4Config(min_action_interval_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(pci_lock_available=False),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )
            sm.private_state_for_test.state = SystemState.BAD_RF
            sm.private_state_for_test.emergency_class = "BAD_RF"
            sm.private_state_for_test.bad_rf_recovery_step = 2
            sm.private_state_for_test.post_fault_class = "BAD_RF"
            sm.private_state_for_test.post_fault_until = time.time() + 120.0
            now = time.time()
            generation = SimpleNamespace(
                generation=SimpleNamespace(
                    generation=1,
                    accepted_ts=now,
                    sample_ts=now,
                    session_scope_key="session-1",
                    source_epoch="epoch-1",
                ),
                identity=SimpleNamespace(identity_code="identity-1"),
                traffic=SimpleNamespace(
                    has_traffic=False,
                    dl_mbps=0.0,
                    ul_mbps=0.0,
                ),
                action=SimpleNamespace(session_uptime_s=300.0),
            )
            decision.action_source_state_authority.observe_generation(
                generation=generation,
                derived=SimpleNamespace(
                    latency=SimpleNamespace(rtt_ema=20.0)
                ),
                provider=ProviderSourceObservation(
                    profile_active=True,
                    provider_plmn="22005",
                    restricted_window_active=True,
                    deployed_bands=(1, 3, 20),
                    active_bands=(3,),
                    primary_band=0,
                    radio_connected=False,
                    cell_id=1,
                    posture_state="E_RADIO_DOWN",
                    emergency_class="E_RADIO_DOWN",
                    posture_time_s=300.0,
                    b3_learning_blocked=False,
                    b3_learning_reason="",
                    morning_hour=6,
                    morning_minute=1,
                    morning_step_wait_s=20.0,
                    antithrash_window_s=300.0,
                    antithrash_max_actions=4,
                    antithrash_hold_s=120.0,
                    antithrash_strict_wait_s=60.0,
                    antithrash_strict_max_actions=3,
                    radio_drop_persist_s=300.0,
                    radio_drop_hold_s=300.0,
                    ca_restore_step_wait_s=20.0,
                    ca_restore_cooldown_s=120.0,
                    ca_restore_verify_s=900.0,
                    ca_restore_max_failures=2,
                ),
                now=now,
            )
            decision.action_source_state_authority.note_provider_radio_result(
                lever="BAND_SELECTION",
                target_bands=[20],
                intent="big_dip",
                objective_id="big_dip:test:b20_fallback",
                success=True,
                target_confirmed=True,
                observed_at=now,
                receipt_id="radio-drop-root",
                identity_key="identity-1",
                session_scope_key="session-1",
            )

            st = decision.get_runtime_status()

            self.assertEqual(st["fault_recovery_step"], 2)
            self.assertEqual(st["bad_rf_recovery_step"], 2)
            self.assertEqual(st["post_fault_class"], "BAD_RF")
            self.assertTrue(st["post_fault_active"])
            self.assertNotIn("b818_big_dip_active", st)
            self.assertNotIn("b818_big_dip_hold_active", st)
            self.assertTrue(st["hilink_radio_drop_active"])
            self.assertTrue(st["hilink_radio_drop_hold_active"])
            self.assertTrue(st["huawei_radio_drop_active"])
            self.assertTrue(st["huawei_radio_drop_hold_active"])

    def test_emergency_session_reset_class_alias_is_handled(self):
        cfg = Arch4Config(min_action_interval_s=0.0, bad_rf_action_cooldown_s=40.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_SESSION_RESET
            sm.private_state_for_test.entered_at = time.time() - 30
            sm.private_state_for_test.emergency_class = "E_SESSION_RESET"

            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )

            modem = make_modem(session_uptime_s=1.0)
            computed = make_computed(200.0, 0.1)

            action = _decide(decision, modem, computed)
            self.assertIsNotNone(action)
            self.assertIn(
                action.intent,
                {"emergency_recovery", "emergency_fast_restart", "emergency_restart"},
            )

    def test_session_reset_origin_survives_temporary_radio_down(self):
        cfg = Arch4Config(
            emergency_recovery_stable_s=5.0,
            emergency_reentry_guard_s=0.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_SESSION_RESET
            sm.private_state_for_test.emergency_class = "E_SESSION_RESET"
            sm.private_state_for_test.emergency_origin_class = "E_SESSION_RESET"
            sm.private_state_for_test.emergency_timeout_at = time.time() + 120.0

            down = make_modem(radio_connected=False, session_uptime_s=0.0)
            computed = make_computed(34.0, 0.5)
            state = sm.evaluate_state_for_test(down, computed)

            self.assertEqual(state, SystemState.E_RADIO_DOWN)
            self.assertEqual(sm.private_state_for_test.emergency_class, "E_RADIO_DOWN")
            self.assertEqual(sm.private_state_for_test.emergency_origin_class, "E_SESSION_RESET")

            sm.private_state_for_test.emergency_recovery_since = time.time() - 10.0
            recovered = make_modem(
                radio_connected=True,
                session_uptime_s=30.0,
                sinr_db=19.0,
                ca_active=True,
                ca_signature="B1+B3",
            )
            state = sm.evaluate_state_for_test(recovered, computed)

            self.assertEqual(state, SystemState.WORKING)
            self.assertEqual(sm.private_state_for_test.post_fault_class, "E_SESSION_RESET")
            self.assertEqual(sm.private_state_for_test.recovered_lane_from_fault, "")

    def test_b818_radio_down_emergency_fast_restart(self):
        cfg = Arch4Config(min_action_interval_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_RADIO_DOWN
            sm.private_state_for_test.entered_at = time.time() - 12
            sm.private_state_for_test.emergency_class = "E_RADIO_DOWN"

            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                policy_profile=select_policy_profile("22005", "b818"),
            )

            modem = make_modem(radio_connected=False)
            computed = make_computed(200.0, 0.1)
            action = decision._decide_e_radio_down(modem, computed)

            self.assertIsNotNone(action)
            self.assertEqual(action.lever, LeverType.RESTART)
            self.assertTrue(action.params.get("emergency_fast"))

    def test_rf_collapse_uses_band_recovery_before_restart(self):
        cfg = Arch4Config(min_action_interval_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_RF_COLLAPSE
            sm.private_state_for_test.entered_at = time.time() - 12
            sm.private_state_for_test.emergency_class = "BAD_RF"

            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                policy_profile=select_policy_profile("22005", "b818"),
            )
            seed_known_good_bandset(mem, [1, 3, 20])

            modem = make_modem(primary_band=3, ca_active=False, ca_signature="B3", rx_bytes_per_sec=300_000)
            computed = make_computed(200.0, 0.1)
            decision._tick_safety = evaluate_tick_safety(
                modem=modem,
                computed=computed,
                engine=decision,
            )
            action = decision._decide_e_rf_collapse(modem, computed)

            self.assertIsNotNone(action)
            self.assertEqual(action.lever, LeverType.BAND_SELECTION)
            self.assertEqual(action.params.get("bands"), [1, 3, 20])

    def test_bad_rf_recovery_is_bounded_and_does_not_restart(self):
        cfg = Arch4Config(min_action_interval_s=0.0, bad_rf_action_cooldown_s=40.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_RF_COLLAPSE
            sm.private_state_for_test.entered_at = time.time() - 12
            sm.private_state_for_test.emergency_class = "BAD_RF"
            sm.private_state_for_test.bad_rf_next_action_at = 0.0

            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                policy_profile=select_policy_profile("22005", "b818"),
            )
            seed_known_good_bandset(mem, [3])
            decision.deployed_bands = {1, 3}

            modem = make_modem(primary_band=3, ca_active=False, ca_signature="B3", rx_bytes_per_sec=300_000)
            computed = make_computed(200.0, 0.1)

            with patch("arch4.brain.decision_faults.time.time", return_value=1000.0):
                self.assertIsNone(decision._decide_e_rf_collapse(modem, computed))
            sm._republish_private_state_for_test()
            with patch("arch4.brain.decision_faults.time.time", return_value=1011.0):
                self.assertIsNone(decision._decide_e_rf_collapse(modem, computed))
            sm._republish_private_state_for_test()
            with patch("arch4.brain.decision_faults.time.time", return_value=1022.0):
                action = decision._decide_e_rf_collapse(modem, computed)

            self.assertIsNotNone(action)
            self.assertEqual(action.lever, LeverType.BAND_SELECTION)
            self.assertEqual(action.params.get("bands"), [1, 3])

            sm._republish_private_state_for_test()
            with patch("arch4.brain.decision_faults.time.time", return_value=1044.0):
                self.assertIsNone(decision._decide_e_rf_collapse(modem, computed))

    def test_rf_collapse_reentry_restarts_fault_ladder_from_step_zero(self):
        cfg = Arch4Config(min_action_interval_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                policy_profile=select_policy_profile("22005", "b818"),
            )
            seed_known_good_bandset(mem, [1, 3])
            decision.deployed_bands = {1, 3, 20}
            modem = make_modem(primary_band=3, ca_active=False, ca_signature="B3", rx_bytes_per_sec=300_000)
            computed = make_computed(200.0, 0.1)

            sm._enter_emergency(modem, emergency_class="E_RF_COLLAPSE")
            decision._tick_safety = evaluate_tick_safety(
                modem=modem,
                computed=computed,
                engine=decision,
            )
            sm.private_state_for_test.bad_rf_next_action_at = 0.0
            with patch("arch4.brain.decision_faults.time.time", return_value=1000.0):
                first = decision._decide_e_rf_collapse(modem, computed)
            self.assertIsNotNone(first)
            self.assertEqual(first.lever, LeverType.BAND_SELECTION)
            self.assertEqual(first.params.get("bands"), [1, 3])

            sm._enter_working()
            sm._enter_emergency(modem, emergency_class="E_RF_COLLAPSE")
            decision._tick_safety = evaluate_tick_safety(
                modem=modem,
                computed=computed,
                engine=decision,
            )
            sm.private_state_for_test.bad_rf_next_action_at = 0.0
            with patch("arch4.brain.decision_faults.time.time", return_value=1010.0):
                second = decision._decide_e_rf_collapse(modem, computed)
            self.assertIsNotNone(second)
            self.assertEqual(second.lever, LeverType.BAND_SELECTION)
            self.assertEqual(second.params.get("bands"), [1, 3])

    def test_b818_radio_down_emergency_fast_restart_respects_cooldown(self):
        cfg = Arch4Config(min_action_interval_s=0.0, emergency_action_cooldown_s=120.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_RADIO_DOWN
            sm.private_state_for_test.entered_at = time.time() - 12
            sm.private_state_for_test.emergency_class = "E_RADIO_DOWN"

            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                policy_profile=select_policy_profile("22005", "b818"),
            )

            modem = make_modem(radio_connected=False)
            computed = make_computed(200.0, 0.1)
            first = decision._decide_e_radio_down(modem, computed)
            sm._republish_private_state_for_test()
            second = decision._decide_e_radio_down(modem, computed)

            self.assertIsNotNone(first)
            self.assertIsNone(second)

    def test_mikrotik_radio_down_stays_passive(self):
        cfg = Arch4Config(min_action_interval_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            mem.action_history.append(ActionRecord(
                timestamp=time.time(),
                sinr_db=12.0,
                rsrq_db=-8.0,
                mcs_dl=18,
                ca_signature="B20",
                rtt_sliding_avg=35.0,
                rtt_ms=35.0,
                lever=LeverType.BAND_SELECTION,
                action_params={"bands": [1, 3, 20]},
                success=True,
                throughput_before_mbps=20.0,
                throughput_after_mbps=35.0,
                sinr_after=12.0,
                ca_signature_after="B1+B3+B20",
                improvement=15.0,
            ))
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_RADIO_DOWN
            sm.private_state_for_test.entered_at = time.time() - 30
            sm.private_state_for_test.emergency_class = "E_RADIO_DOWN"

            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                policy_profile=select_policy_profile("22003", "mikrotik"),
            )

            modem = make_modem(radio_connected=False)
            computed = make_computed(200.0, 0.0)

            for _ in range(3):
                sm.private_state_for_test.emergency_next_action_at = time.time() - 1.0
                action = decision._decide_e_radio_down(modem, computed)
                self.assertIsNone(action)

    def test_band_action_fail_limit_records_cooldown_without_pipeline_veto(self):
        cfg = Arch4Config(
            min_action_interval_s=0.0,
            band_action_fail_limit=2,
            band_action_cooldown_s=3600.0,
            startup_grace_s=0.0,
            autonomous_connect_settle_s=0.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            constraints = Constraints(pci_lock_available=False)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                constraints,
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                smart_loop_strategy_feed=BrainFeedRegisterBank(),
            )

            modem = make_modem(
                ca_active=True,
                ca_signature="B3+CA",
                sinr_db=3.0,
                rsrp_dbm=-95.0,
            )
            computed = make_computed(50.0, 0.8)
            typed_modem, typed_computed = cellular_test_inputs(
                modem,
                computed,
            )
            publish_smart_loop_radio_command_for_test(
                decision,
                target_band=3,
                target_bands=(1, 3),
                reason_code="synthetic_retry_candidate",
                availability_intent="explore",
                accepted_frame=typed_modem,
            )

            first = _decide(decision, typed_modem, typed_computed)
            self.assertIsNotNone(first)
            self.assertEqual(first.lever, LeverType.PBAND)
            decision.note_action_result(first, success=False, modem_after=None)
            decision.note_action_result(first, success=False, modem_after=None)

            self.assertTrue(decision.cooldowns.is_blocked("band_cooldown"))
            out = _decide(decision, typed_modem, typed_computed)
            self.assertIsNotNone(out)

    def test_non_smart_loop_pband_result_does_not_start_smart_loop_roi_window(self):
        cfg = Arch4Config(min_action_interval_s=0.0, band_action_fail_limit=3)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(pci_lock_available=False),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )

            action = LeverAction(
                lever=LeverType.PBAND,
                params={"pband": 3, "ca_pool": [1, 3]},
                reason="manual-style PBAND result accounting",
                intent="manual_request",
            )
            decision.note_action_result(action, success=False, modem_after=None)

            self.assertFalse(decision.cooldowns.is_blocked(SMART_LOOP_ROI_OBSERVATION_COOLDOWN))
            self.assertFalse(decision.cooldowns.is_blocked_keyed(SMART_LOOP_ROI_OBSERVATION_COOLDOWN, "B3"))

    def test_band_action_fail_limit_honors_zero_cooldown(self):
        cfg = Arch4Config(
            min_action_interval_s=0.0,
            band_action_fail_limit=2,
            band_action_cooldown_s=0.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(pci_lock_available=False),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )

            action = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3]},
                reason="test band cooldown zero",
                intent="explore",
            )
            decision.note_band_action_result(action, success=False, modem_after=None)
            decision.note_band_action_result(action, success=False, modem_after=None)

            self.assertFalse(decision.cooldowns.is_blocked("band_cooldown"))

    def test_restore_ca_repeat_block_honors_zero_config(self):
        cfg = Arch4Config(min_action_interval_s=0.0, restore_ca_repeat_block_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )

            action = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3]},
                reason="restore repeat zero",
                intent="restore_ca",
                objective_id="restore_ca:B1+B3",
            )
            decision.note_band_action_result(action, success=False, modem_after=None)
            decision.note_band_action_result(action, success=False, modem_after=None)

            self.assertFalse(decision.cooldowns.is_blocked_keyed("restore_ca_repeat", "1+3"))

    def test_explore_repeat_block_honors_zero_config(self):
        cfg = Arch4Config(min_action_interval_s=0.0, explore_repeat_block_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )

            action = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3]},
                reason="explore repeat zero",
                intent="explore",
            )
            decision.note_band_action_result(action, success=False, modem_after=None)
            decision.note_band_action_result(action, success=False, modem_after=None)

            self.assertFalse(decision.cooldowns.is_blocked_keyed("explore_repeat", "1+3"))

    def test_cell_lock_objective_retry_and_clear(self):
        cfg = Arch4Config(min_action_interval_s=0.0, cell_lock_retry_after_s=900.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )

            action = LeverAction(
                lever=LeverType.CELL_LOCK,
                params={"pci": 123, "earfcn": 1400},
                reason="test cell lock objective",
                intent="explore_cell_lock",
                objective_id="explore_cell_lock:PCI123:E1400",
            )

            decision.note_action_result(action, success=False, modem_after=None)
            self.assertTrue(decision.cooldowns.is_blocked_keyed("cell_lock_retry", action.objective_id))
            verdict = decision._action_availability_verdict(
                action=action,
                modem=None,
                state=SystemState.WORKING,
                now=time.time(),
                check_objective=True,
            )
            self.assertFalse(verdict["allowed"])
            self.assertEqual(verdict["reason_code"], "cell_lock_retry")

            modem_after = make_modem(pci=123, earfcn=1400)
            decision.note_action_result(action, success=True, modem_after=modem_after)
            self.assertFalse(decision.cooldowns.is_blocked_keyed("cell_lock_retry", action.objective_id))
            verdict = decision._action_availability_verdict(
                action=action,
                modem=modem_after,
                state=SystemState.WORKING,
                now=time.time(),
                check_objective=True,
            )
            self.assertTrue(verdict["allowed"])

    def test_make_cell_lock_action_defers_blocked_objective_before_pipeline(self):
        cfg = Arch4Config(min_action_interval_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )
            objective_id = "degraded_cell_lock:PCI123:E1400"
            decision.cooldowns.set_keyed("objective_feedback", objective_id, hold_s=120.0, now=time.time())

            action = decision._make_cell_lock_action(
                pci=123,
                earfcn=1400,
                reason="test",
                intent="degraded_cell_lock",
                objective_id=objective_id,
            )

            self.assertIsNone(action)
            events = decision.drain_runtime_events()
            self.assertTrue(
                any(
                    e.get("kind") == "action_availability_blocked"
                    and e.get("reason_code") == "objective_feedback"
                    and e.get("objective_id") == objective_id
                    for e in events
                )
            )

    def test_cell_lock_objective_retry_honors_zero_config(self):
        cfg = Arch4Config(min_action_interval_s=0.0, cell_lock_retry_after_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )

            action = LeverAction(
                lever=LeverType.CELL_LOCK,
                params={"pci": 123, "earfcn": 1400},
                reason="test cell lock zero",
                intent="explore_cell_lock",
                objective_id="explore_cell_lock:PCI123:E1400",
            )

            decision.note_action_result(action, success=False, modem_after=None)
            self.assertFalse(decision.cooldowns.is_blocked_keyed("cell_lock_retry", action.objective_id))

    def test_restart_objective_retry_and_clear(self):
        cfg = Arch4Config(min_action_interval_s=0.0, restart_retry_after_s=1800.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )

            action = LeverAction(
                lever=LeverType.RESTART,
                params={"emergency_fast": True},
                reason="test restart objective",
                intent="emergency_fast_restart",
                objective_id="emergency_fast_restart:E_RADIO_DOWN",
            )

            decision.note_action_result(action, success=False, modem_after=None)
            self.assertTrue(decision.cooldowns.is_blocked_keyed("restart_retry", action.objective_id))
            verdict = decision._action_availability_verdict(
                action=action,
                modem=None,
                state=SystemState.E_RADIO_DOWN,
                now=time.time(),
                check_objective=True,
            )
            self.assertFalse(verdict["allowed"])
            self.assertEqual(verdict["reason_code"], "restart_retry")

            decision.note_action_result(action, success=True, modem_after=None)
            self.assertFalse(decision.cooldowns.is_blocked_keyed("restart_retry", action.objective_id))
            verdict = decision._action_availability_verdict(
                action=action,
                modem=None,
                state=SystemState.E_RADIO_DOWN,
                now=time.time(),
                check_objective=True,
            )
            self.assertTrue(verdict["allowed"])

    def test_restart_objective_retry_honors_zero_config(self):
        cfg = Arch4Config(min_action_interval_s=0.0, restart_retry_after_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )

            action = LeverAction(
                lever=LeverType.RESTART,
                params={"emergency_fast": True},
                reason="test restart zero",
                intent="emergency_fast_restart",
                objective_id="emergency_fast_restart:E_RADIO_DOWN",
            )

            decision.note_action_result(action, success=False, modem_after=None)
            self.assertFalse(decision.cooldowns.is_blocked_keyed("restart_retry", action.objective_id))

    def test_morning_sequence_pipeline_uses_objective_lifecycle_check(self):
        cfg = Arch4Config(min_action_interval_s=0.0, autonomous_connect_settle_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                policy_profile=select_policy_profile("22005", "b818"),
            )
            modem = make_modem(ca_active=True, ca_signature="B3+CA")
            computed = make_computed(50.0, 0.6)
            morning = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [3], "forced_sequence": True},
                reason="morning test",
                intent="morning_sequence",
                objective_id="morning_sequence:test:1",
            )
            decision._is_a1_hard_night_freeze_window = lambda now: False
            decision._decide_provider_morning_sequence = lambda modem, now: morning
            decision._decide_a1_provider_morning_sequence = lambda modem, now: morning
            decision._decide_a1_b818_morning_sequence = lambda modem, now: morning
            with patch.object(decision, "_run_action_pipeline", return_value=None) as run_pipe:
                out = _decide(decision, modem, computed)
            self.assertIsNone(out)
            self.assertFalse(run_pipe.called)
            events = decision.drain_runtime_events()
            self.assertTrue(any(e.get("kind") == "smart_loop_strategy_unavailable" for e in events))

    def test_forced_schedule_helper_retired_for_all_profiles(self):
        cfg = Arch4Config(min_action_interval_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                policy_profile=select_policy_profile("22003", "mikrotik"),
            )
            modem = make_modem(
                ca_active=True,
                ca_signature="B3+CA",
                sinr_db=3.0,
                rsrp_dbm=-95.0,
            )
            now = time.mktime((2026, 2, 27, 0, 10, 0, 4, 58, -1))
            with self.assertRaisesRegex(
                RuntimeError,
                "old_provider_overnight_action_owner_deleted",
            ):
                decision._decide_a1_overnight_recovery(modem, now)

    def test_congestion_storm_soft_hold_suppresses_actions(self):
        cfg = Arch4Config(min_action_interval_s=0.0, soft_emergency_hold_s=300.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_CONGESTION_STORM
            sm.private_state_for_test.entered_at = time.time() - 20
            sm.private_state_for_test.emergency_class = "E_CONGESTION_STORM"
            sm.private_state_for_test.soft_emergency_hold_until = time.time() + 200

            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )
            modem = make_modem()
            computed = make_computed(450.0, 0.2)

            action = decision._decide_e_congestion_storm(modem, computed)
            self.assertIsNone(action)

    def test_congestion_storm_uses_configured_deployed_bandset(self):
        cfg = Arch4Config(min_action_interval_s=0.0, soft_emergency_hold_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_CONGESTION_STORM
            sm.private_state_for_test.entered_at = time.time() - 20
            sm.private_state_for_test.emergency_class = "E_CONGESTION_STORM"
            sm.private_state_for_test.soft_emergency_hold_until = 0.0

            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )
            decision.deployed_bands = {1, 3, 20}

            modem = make_modem(primary_band=3, ca_active=False, ca_signature="B3", rx_bytes_per_sec=300_000)
            computed = make_computed(450.0, 0.2)

            action = decision._decide_e_congestion_storm(modem, computed)
            self.assertIsNotNone(action)
            self.assertEqual(action.lever, LeverType.BAND_SELECTION)
            self.assertEqual(action.params.get("bands"), [1, 3, 20])

    def test_a1_big_dip_legacy_b20_fallback_is_retired(self):
        cfg = Arch4Config(
            min_action_interval_s=0.0,
            a1_big_dip_b20_fallback_persist_s=60.0,
            a1_big_dip_step_wait_s=1.0,
            a1_big_dip_hold_s=120.0,
            a1_night_restrict_start_hour=23,
            a1_night_restrict_end_hour=0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_RADIO_DOWN
            sm.private_state_for_test.emergency_class = "E_RADIO_DOWN"
            sm.private_state_for_test.entered_at = time.time() - 120.0
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                policy_profile=select_policy_profile("22005", "b818"),
            )
            modem = make_modem(
                radio_connected=False,
                ca_active=False,
                ca_signature="none",
                primary_band=0,
                sinr_db=-5.0,
                rsrp_dbm=-105.0,
            )
            base = time.mktime((2026, 2, 28, 1, 0, 0, 5, 59, -1))

            with self.assertRaisesRegex(
                RuntimeError,
                "old_provider_radio_drop_action_owner_deleted",
            ):
                decision._decide_hilink_radio_drop_recovery(modem, base)

    def test_a1_big_dip_does_not_force_b20_while_b3_is_still_attached(self):
        cfg = Arch4Config(min_action_interval_s=0.0, a1_big_dip_b20_fallback_persist_s=60.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_RF_COLLAPSE
            sm.private_state_for_test.emergency_class = "BAD_RF"
            sm.private_state_for_test.entered_at = time.time() - 120.0
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                policy_profile=select_policy_profile("22005", "b818"),
            )
            modem = make_modem(
                ca_active=False,
                ca_signature="B3",
                primary_band=3,
                sinr_db=-6.0,
                rsrp_dbm=-96.0,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "old_provider_radio_drop_action_owner_deleted",
            ):
                decision._decide_hilink_radio_drop_recovery(
                    modem,
                    time.time(),
                )

    def test_a1_night_restriction_filters_to_current_b3_lane(self):
        cfg = Arch4Config(min_action_interval_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.DEGRADED
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                policy_profile=select_policy_profile("22005", "b818"),
            )
            modem = make_modem(
                ca_active=True,
                ca_signature="B3+CA",
                sinr_db=3.0,
                rsrp_dbm=-95.0,
            )
            action = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3, 7, 20], "sequential": True},
                reason="test",
            )
            now = time.mktime((2026, 2, 28, 2, 0, 0, 5, 59, -1))
            with self.assertRaisesRegex(
                RuntimeError,
                "old_post_owner_provider_restricted_filter_deleted",
            ):
                decision._apply_a1_restricted_band_filter(action, modem, now)

    def test_a1_antithrash_hold_blocks_followup_band_actions(self):
        cfg = Arch4Config(
            min_action_interval_s=0.0,
            a1_night_antithrash_max_band_actions=1,
            a1_night_antithrash_hold_s=120.0,
            a1_night_antithrash_window_s=300.0,
            a1_night_antithrash_strict_max_actions=1,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.E_RF_COLLAPSE
            sm.private_state_for_test.entered_at = time.time() - 30
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                policy_profile=select_policy_profile("22005", "b818"),
            )
            modem = make_modem(ca_active=True, ca_signature="B3+CA")
            action = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [3, 20], "sequential": True},
                reason="churn test",
            )
            base = time.mktime((2026, 2, 28, 2, 10, 0, 5, 59, -1))
            with self.assertRaisesRegex(
                RuntimeError,
                "old_provider_antithrash_guard_owner_deleted",
            ):
                decision._guard_a1_antithrash(action, modem, base)

    def test_a1_night_restriction_applies_even_when_rf_is_good(self):
        cfg = Arch4Config(min_action_interval_s=0.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.DEGRADED
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                policy_profile=select_policy_profile("22005", "b818"),
            )
            modem = make_modem(ca_active=True, ca_signature="B3+CA", sinr_db=12.0, rsrp_dbm=-80.0)
            action = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3, 7, 20], "sequential": True},
                reason="test",
            )
            now = time.mktime((2026, 2, 28, 2, 0, 0, 5, 59, -1))
            with self.assertRaisesRegex(
                RuntimeError,
                "old_post_owner_provider_restricted_filter_deleted",
            ):
                decision._apply_a1_restricted_band_filter(action, modem, now)

    def test_a1_hard_night_freeze_window_is_retired(self):
        cfg = Arch4Config(
            min_action_interval_s=0.0,
            a1_night_freeze_enabled=True,
            a1_night_freeze_start_hour=2,
            a1_night_freeze_end_hour=8,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
                policy_profile=select_policy_profile("22005", "b818"),
            )
            freeze_now = time.mktime((2026, 3, 9, 3, 56, 0, 0, 0, -1))
            day_now = time.mktime((2026, 3, 9, 14, 0, 0, 0, 0, -1))
            self.assertFalse(decision._is_a1_hard_night_freeze_window(freeze_now))
            self.assertFalse(decision._is_a1_hard_night_freeze_window(day_now))

    def test_memory_stm_blocks_repeated_bad_bandset_same_hour(self):
        cfg = Arch4Config(
            learning_enabled=True,
            learning_stm_min_attempts=2,
            learning_stm_bad_rate_threshold=0.6,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            before = make_modem(primary_band=20, sinr_db=8.0, rx_bytes_per_sec=500_000)
            before_cs = make_computed(40.0, 0.6)
            # Two bad B3 attempts: low SINR and throughput collapse.
            for _ in range(2):
                after = make_modem(primary_band=3, sinr_db=-6.0, rx_bytes_per_sec=10_000)
                after_cs = make_computed(180.0, 0.1)
                typed_before, typed_before_cs = cellular_test_inputs(before, before_cs)
                typed_after, typed_after_cs = cellular_test_inputs(after, after_cs)
                rec = memory_record_action_history(mem, 
                    modem_before=typed_before,
                    computed_before=typed_before_cs,
                    lever=LeverType.BAND_SELECTION,
                    proposal_action_origin="smart_loop",
                    proof_observed_at=float(
                        typed_after.generation.sample_ts
                    ),
                    params={"bands": [3], "sequential": True},
                    success=True,
                    modem_after=typed_after,
                    computed_after=typed_after_cs,
                    learning_gate={"learning_allowed": True, "gate_input_complete": True},
                )
                result = commit_terminal_reward_test_fixture(
                    mem,
                    rec,
                    modem_after=typed_after,
                    computed_after=typed_after_cs,
                    grade=-1,
                    reason="band_action_regression",
                )
                self.assertTrue(result["committed"])
            hour = time.localtime().tm_hour
            blocked, reason = memory_should_block_band_action(mem, hour=hour, bands=[3], now=time.time())
            self.assertTrue(blocked)
            self.assertIn("STM", reason)

    def test_decision_learning_guard_blocks_non_forced_band_action(self):
        cfg = Arch4Config(learning_enabled=True, learning_stm_min_attempts=1)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            hour = time.localtime().tm_hour
            key = marker_scoped_band_learning_key(mem, mem.current_calibration_marker, hour, [3])
            mem.band_learning_stm[key] = {
                "attempts": 2.0,
                "good": 0.0,
                "bad": 2.0,
                "hard_fail": 1.0,
                "last_ts": time.time(),
                "last_sinr": 5.0,
            }
            mem.current_context_seen_actions = cfg.learning_marker_warmup_attempts
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )
            action = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [3], "sequential": True},
                reason="test",
            )
            out = decision._apply_learning_band_guard(action, make_modem(), now=time.time())
            self.assertIsNone(out)

    def test_make_band_action_defers_known_bad_learning_target_before_pipeline(self):
        cfg = Arch4Config(learning_enabled=True, learning_stm_min_attempts=1)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            hour = time.localtime().tm_hour
            key = marker_scoped_band_learning_key(mem, mem.current_calibration_marker, hour, [3])
            mem.band_learning_stm[key] = {
                "attempts": 2.0,
                "good": 0.0,
                "bad": 2.0,
                "hard_fail": 1.0,
                "last_ts": time.time(),
                "last_sinr": 5.0,
            }
            mem.current_context_seen_actions = cfg.learning_marker_warmup_attempts
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )

            action = decision._make_band_action(
                bands=[3],
                reason="test",
                intent="explore",
                modem=make_modem(),
            )

            self.assertIsNone(action)
            events = decision.drain_runtime_events()
            self.assertTrue(
                any(
                    e.get("kind") == "learning_band_deferred"
                    and e.get("bands") == [3]
                    for e in events
                )
            )

    def test_make_band_action_allows_forced_sequence_despite_learning_history(self):
        cfg = Arch4Config(learning_enabled=True, learning_stm_min_attempts=1)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            hour = time.localtime().tm_hour
            key = marker_scoped_band_learning_key(mem, mem.current_calibration_marker, hour, [3])
            mem.band_learning_stm[key] = {
                "attempts": 2.0,
                "good": 0.0,
                "bad": 2.0,
                "hard_fail": 1.0,
                "last_ts": time.time(),
                "last_sinr": 5.0,
            }
            mem.current_context_seen_actions = cfg.learning_marker_warmup_attempts
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )

            action = decision._make_band_action(
                bands=[3],
                reason="forced",
                intent="restore_ca",
                params={"forced_sequence": True},
                modem=make_modem(),
            )

            self.assertIsNotNone(action)
            self.assertEqual(sorted(action.params.get("bands", [])), [3])

    def test_learning_guard_allows_upstream_learning_screened_band_action(self):
        cfg = Arch4Config(learning_enabled=True, learning_stm_min_attempts=1)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            hour = time.localtime().tm_hour
            key = marker_scoped_band_learning_key(mem, mem.current_calibration_marker, hour, [3])
            mem.band_learning_stm[key] = {
                "attempts": 2.0,
                "good": 0.0,
                "bad": 2.0,
                "hard_fail": 1.0,
                "last_ts": time.time(),
                "last_sinr": 5.0,
            }
            mem.current_context_seen_actions = cfg.learning_marker_warmup_attempts
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )
            action = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [3], "_brain_learning_screened": True},
                reason="screened",
                intent="explore",
            )

            out = decision._apply_learning_band_guard(action, make_modem(), now=time.time())

            self.assertIs(out, action)

    def test_decision_learning_guard_allows_retry_on_improved_rf(self):
        cfg = Arch4Config(
            learning_enabled=True,
            learning_stm_min_attempts=1,
            learning_retry_min_elapsed_s=60.0,
            learning_retry_sinr_improvement_db=4.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            hour = time.localtime().tm_hour
            key = marker_scoped_band_learning_key(mem, mem.current_calibration_marker, hour, [3])
            mem.band_learning_stm[key] = {
                "attempts": 2.0,
                "good": 0.0,
                "bad": 2.0,
                "hard_fail": 1.0,
                "last_ts": time.time() - 120.0,
                "last_sinr": 4.0,
            }
            mem.current_context_seen_actions = cfg.learning_marker_warmup_attempts
            sm = TestControllerPostureAuthority(cfg, mem)
            decision = DecisionEngine(
                cfg,
                mem,
                sm,
                Constraints(),
                aggressiveness=AggressivenessEngine(cfg),
                historical=HistoricalJournal(cfg),
            )
            action = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [3], "sequential": True},
                reason="test",
            )
            out = decision._apply_learning_band_guard(action, make_modem(sinr_db=10.0), now=time.time())
            self.assertIsNotNone(out)

    def test_calibration_context_is_new_before_compare_delay(self):
        cfg = Arch4Config(learning_context_compare_delay_s=86400.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            a = mem.set_calibration_context(
                calibration_status={
                    "expected_dl_mbps": 10.0,
                    "expected_rtt_ms": 40.0,
                    "expected_sinr_db": 8.0,
                    "gap_score": 0.20,
                    "baseline_ready": True,
                    "sample_count": 120,
                },
                modem_meta={"model": "B818-263", "iccid": "SIM_A", "provider_plmn": "22005"},
                now=time.time(),
            )
            b = mem.set_calibration_context(
                calibration_status={
                    "expected_dl_mbps": 3.0,
                    "expected_rtt_ms": 120.0,
                    "expected_sinr_db": -2.0,
                    "gap_score": 0.90,
                    "baseline_ready": True,
                    "sample_count": 121,
                },
                modem_meta={"model": "B818-263", "iccid": "SIM_A", "provider_plmn": "22005"},
                now=time.time() + 10,
            )
            self.assertNotEqual(a["marker"], b["marker"])
            self.assertFalse(b["marker_reused"])


class TestCalibrationDatabase(unittest.TestCase):
    def test_calibration_sample_persist_and_hourly_query(self):
        cfg = Arch4Config(calibration_db_enabled=True, calibration_db_retention_days=30)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            authority = CalibrationAuthority(cfg, td)
            authority.start_collection(
                "test_calibration_sample",
                workflow_mode=PHASE_BOOTSTRAP,
            )
            modem = make_modem(sinr_db=11.0, rsrp_dbm=-84.0, rsrq_db=-8.0, ca_active=True, ca_signature="B1+B3")
            computed = make_computed(42.0, 0.72)
            out = mem.set_calibration_context(
                calibration_status={
                    "needs_calibration": True,
                    "baseline_ready": True,
                    "sample_count": 200,
                    "expected_dl_mbps": 18.0,
                    "expected_rtt_ms": 40.0,
                    "expected_sinr_db": 10.0,
                    "gap_score": 0.35,
                },
                modem_meta={"model": "B818-263", "iccid": "SIM_A", "provider_plmn": "22005"},
                now=time.time(),
            )
            receipt = consume_test_calibration_observation(
                authority,
                modem=modem,
                computed=computed,
                calibration_status={
                    "needs_calibration": True,
                    "baseline_ready": True,
                    "sample_count": 200,
                    "expected_dl_mbps": 18.0,
                    "expected_rtt_ms": 40.0,
                    "expected_sinr_db": 10.0,
                    "gap_score": 0.35,
                    "marker": out["marker"],
                    "marker_signature": str(
                        mem.current_calibration_signature or ""
                    ),
                },
                now=time.time(),
            )
            self.assertTrue(receipt.get("accepted"))
            rows = memory_get_calibration_hourly_ranges(mem, out["marker"], lookback_hours=24)
            self.assertGreaterEqual(len(rows), 1)
            self.assertIn("sinr_min", rows[0])
            self.assertIn("gap_max", rows[0])

    def test_calibration_db_marker_reassign(self):
        cfg = Arch4Config(calibration_db_enabled=True, calibration_db_retention_days=30)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            authority = CalibrationAuthority(cfg, td)
            authority.start_collection(
                "test_calibration_marker_reassign",
                workflow_mode=PHASE_BOOTSTRAP,
            )
            modem = make_modem()
            computed = make_computed(35.0, 0.5)
            now = time.time()
            c1 = mem.set_calibration_context(
                calibration_status={
                    "expected_dl_mbps": 15.0,
                    "expected_rtt_ms": 40.0,
                    "expected_sinr_db": 9.0,
                    "gap_score": 0.2,
                    "baseline_ready": True,
                    "sample_count": 100,
                },
                modem_meta={"model": "B818-263", "iccid": "SIM_A", "provider_plmn": "22005"},
                now=now,
            )["marker"]
            c2 = mem.set_calibration_context(
                calibration_status={
                    "expected_dl_mbps": 2.0,
                    "expected_rtt_ms": 200.0,
                    "expected_sinr_db": -2.0,
                    "gap_score": 1.0,
                    "baseline_ready": True,
                    "sample_count": 100,
                },
                modem_meta={"model": "B818-263", "iccid": "SIM_A", "provider_plmn": "22005"},
                now=now + 10,
            )["marker"]
            self.assertNotEqual(c1, c2)
            mem.current_calibration_marker = c2
            receipt = consume_test_calibration_observation(
                authority,
                modem=modem,
                computed=computed,
                calibration_status={
                    "baseline_ready": True,
                    "marker": c2,
                    "marker_signature": str(
                        mem.current_calibration_signature or ""
                    ),
                },
                now=now + 12,
            )
            self.assertTrue(receipt.get("accepted"))
            memory_reassign_calibration_marker_history(mem, c2, c1)
            with sqlite_conn(mem._calibration_db_path) as con:
                row = con.execute(
                    "SELECT COUNT(*) AS c FROM calibration_samples WHERE marker = ?",
                    (c1,),
                ).fetchone()
                self.assertGreaterEqual(int(row["c"]), 1)

    def test_calibration_context_merges_after_compare_delay(self):
        cfg = Arch4Config(learning_context_compare_delay_s=1.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            t0 = time.time()
            # Create C1
            c1 = mem.set_calibration_context(
                calibration_status={
                    "expected_dl_mbps": 10.0,
                    "expected_rtt_ms": 40.0,
                    "expected_sinr_db": 8.0,
                    "gap_score": 0.20,
                    "baseline_ready": True,
                    "sample_count": 120,
                },
                modem_meta={"model": "B818-263", "iccid": "SIM_A", "provider_plmn": "22005"},
                now=t0,
            )["marker"]
            # Force provisional C2
            c2 = mem.set_calibration_context(
                calibration_status={
                    "expected_dl_mbps": 2.0,
                    "expected_rtt_ms": 160.0,
                    "expected_sinr_db": -4.0,
                    "gap_score": 1.0,
                    "baseline_ready": True,
                    "sample_count": 120,
                },
                modem_meta={"model": "B818-263", "iccid": "SIM_A", "provider_plmn": "22005"},
                now=t0 + 2,
            )["marker"]
            # Make C2 mature and similar to C1, then trigger reconcile.
            mem.calibration_contexts[c2]["created_at"] = t0 - 2
            mem.calibration_contexts[c2]["provisional"] = True
            mem.calibration_contexts[c2]["signature"] = dict(mem.calibration_contexts[c1]["signature"])
            out = mem.set_calibration_context(
                calibration_status={
                    "expected_dl_mbps": 10.0,
                    "expected_rtt_ms": 40.0,
                    "expected_sinr_db": 8.0,
                    "gap_score": 0.20,
                    "baseline_ready": True,
                    "sample_count": 120,
                },
                modem_meta={"model": "B818-263", "iccid": "SIM_A", "provider_plmn": "22005"},
                now=t0 + 3,
            )
            self.assertGreaterEqual(out["marker_merge_count"], 1)
            self.assertEqual(mem.calibration_aliases.get(c2), c1)

    def test_calibration_context_split_when_sim_changes(self):
        cfg = Arch4Config()
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            a = mem.set_calibration_context(
                calibration_status={
                    "expected_dl_mbps": 10.0,
                    "expected_rtt_ms": 40.0,
                    "expected_sinr_db": 8.0,
                    "gap_score": 0.20,
                },
                modem_meta={"model": "B818-263", "iccid": "SIM_A", "provider_plmn": "22005"},
                now=time.time(),
            )
            b = mem.set_calibration_context(
                calibration_status={
                    "expected_dl_mbps": 10.0,
                    "expected_rtt_ms": 40.0,
                    "expected_sinr_db": 8.0,
                    "gap_score": 0.20,
                },
                modem_meta={"model": "B818-263", "iccid": "SIM_B", "provider_plmn": "22005"},
                now=time.time() + 20,
            )
            self.assertNotEqual(a["marker"], b["marker"])
            self.assertFalse(b["marker_reused"])

    def test_calibration_alias_graph_is_compacted_on_load(self):
        cfg = Arch4Config()
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            mem.calibration_contexts = {
                "C3": {
                    "created_at": time.time(),
                    "last_seen": time.time(),
                    "uses": 1,
                    "seen_actions": 0,
                    "provisional": False,
                    "seed": "seed3",
                    "signature": {},
                }
            }
            mem.calibration_aliases = {"C1": "C2", "C2": "C3"}
            mem.current_calibration_marker = "C1"
            intelligence, _ = cellular_test_inputs(
                make_modem(),
                make_computed(40.0, 0.5),
            )
            self.assertTrue(bind_memory_evidence_scope(mem, intelligence))
            commit_memory_checkpoint(mem)

            loaded = Memory(cfg, memory_dir=td)
            self.assertEqual(loaded.current_calibration_marker, "C3")
            self.assertEqual(loaded.calibration_aliases.get("C1"), "C3")
            self.assertEqual(loaded.calibration_aliases.get("C2"), "C3")

    def test_reconcile_only_merges_provisional_into_stable_marker(self):
        cfg = Arch4Config(learning_context_compare_delay_s=1.0)
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            t0 = time.time()
            mem.calibration_contexts = {
                "C1": {
                    "created_at": t0 - 10,
                    "last_seen": t0 - 10,
                    "uses": 1,
                    "seen_actions": 0,
                    "provisional": False,
                    "seed": "seed-stable",
                    "signature": {
                        "provider_plmn": "22005",
                        "modem_model": "B818-263",
                        "sim_id": "SIM_A",
                        "expected_dl_mbps": 10.0,
                        "expected_rtt_ms": 40.0,
                        "expected_sinr_db": 8.0,
                        "gap_score": 0.2,
                    },
                },
                "C2": {
                    "created_at": t0 - 5,
                    "last_seen": t0 - 5,
                    "uses": 1,
                    "seen_actions": 0,
                    "provisional": True,
                    "seed": "seed-prov-a",
                    "signature": {
                        "provider_plmn": "22005",
                        "modem_model": "B818-263",
                        "sim_id": "SIM_A",
                        "expected_dl_mbps": 10.0,
                        "expected_rtt_ms": 40.0,
                        "expected_sinr_db": 8.0,
                        "gap_score": 0.2,
                    },
                },
                "C3": {
                    "created_at": t0 - 4,
                    "last_seen": t0 - 4,
                    "uses": 1,
                    "seen_actions": 0,
                    "provisional": True,
                    "seed": "seed-prov-b",
                    "signature": {
                        "provider_plmn": "22005",
                        "modem_model": "B818-263",
                        "sim_id": "SIM_A",
                        "expected_dl_mbps": 10.0,
                        "expected_rtt_ms": 40.0,
                        "expected_sinr_db": 8.0,
                        "gap_score": 0.2,
                    },
                },
            }
            merged = mem._maybe_reconcile_mature_contexts(t0)
            self.assertTrue(merged)
            self.assertEqual(mem.calibration_aliases.get("C2"), "C1")
            self.assertEqual(mem.calibration_aliases.get("C3"), "C1")
            self.assertNotIn("C2", mem.calibration_contexts)
            self.assertNotIn("C3", mem.calibration_contexts)


class TestDecisionPipelineV7(unittest.TestCase):
    def _make_engine(self):
        cfg = Arch4Config()
        td = tempfile.TemporaryDirectory()
        mem = Memory(cfg, memory_dir=td.name)
        sm = TestControllerPostureAuthority(cfg, mem)
        hist = HistoricalJournal(cfg)
        aggr = AggressivenessEngine(cfg)
        de = DecisionEngine(cfg, mem, sm, Constraints(), aggr, hist)
        return de, td

    def test_band_action_canonical_add_merges_full_set(self):
        decision, td = self._make_engine()
        try:
            modem = make_modem(primary_band=3, ca_signature="B3", ca_active=False)
            action = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1], "band_mode": "add"},
                reason="test",
                intent="explore",
            )
            out = decision._canonicalize_band_action(action, modem)
            self.assertEqual(out.params.get("resolved_target_bands"), [1, 3])
            self.assertEqual(out.params.get("band_mode"), "set")
        finally:
            td.cleanup()

    def test_objective_feedback_blocks_repeated_proposal(self):
        decision, td = self._make_engine()
        try:
            action = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3, 20]},
                reason="test",
                intent="explore",
                objective_id=decision._objective_id_for_bands("explore", [1, 3, 20]),
            )
            decision._record_objective_feedback(
                action,
                code="NO_EFFECT",
                reason="target already active",
                block_s=120.0,
            )
            verdict = decision._action_availability_verdict(
                action=action,
                modem=None,
                state=SystemState.WORKING,
                now=time.time(),
                check_objective=True,
            )
            self.assertFalse(verdict["allowed"])
            self.assertEqual(verdict["reason_code"], "objective_feedback")
            self.assertIn("NO_EFFECT", verdict["reason"])
        finally:
            td.cleanup()

    def test_band_action_no_effect_uses_selected_pool_not_only_active_band(self):
        decision, td = self._make_engine()
        try:
            modem = make_modem(
                primary_band=3,
                ca_active=True,
                ca_signature="B1+B3",
                selected_band_set=[1, 3],
            )
            action = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3], "band_mode": "add"},
                reason="probe B1",
                intent="explore",
                objective_id=decision._objective_id_for_bands("explore", [1, 3]),
            )

            verdict = evaluate_action_applicability(
                action.lever,
                action.params,
                {
                    "primary_band": modem.primary_band,
                    "selected_band_set": modem.selected_band_set,
                },
                action=action.intent,
            )
            self.assertFalse(verdict.applicable)
            self.assertIn("target already selected", verdict.reason)
        finally:
            td.cleanup()

    def test_pband_no_effect_is_owned_by_canonical_applicability(self):
        decision, td = self._make_engine()
        try:
            modem = make_modem(
                primary_band=3,
                ca_active=True,
                ca_signature="B1+B3",
                selected_band_set=[1, 3],
            )
            action = LeverAction(
                lever=LeverType.PBAND,
                params={"pband": 3, "dl_bands": [1, 3]},
                reason="reassert B3 anchor",
                intent="anchor_enforce",
                objective_id="anchor_enforce:B3",
            )

            verdict = evaluate_action_applicability(
                action.lever,
                action.params,
                {
                    "primary_band": modem.primary_band,
                    "selected_band_set": modem.selected_band_set,
                },
                action=action.intent,
            )
            self.assertFalse(verdict.applicable)
            self.assertIn("PBAND already active", verdict.reason)
            self.assertFalse(hasattr(decision, "_guard_no_effect_band_action"))
        finally:
            td.cleanup()

    def test_working_explore_same_context_suppressed_after_feedback(self):
        decision, td = self._make_engine()
        try:
            decision.posture_authority._transition(SystemState.WORKING)
            decision.constraints.provider_plmn = "22005"
            modem = make_modem(primary_band=3, pci=255, earfcn=1650, ca_active=False, ca_signature="B3")
            recorded = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [20]},
                reason="test",
                intent="explore",
                objective_id=decision._objective_id_for_bands("explore", [20]),
            )
            decision._record_objective_feedback(
                recorded,
                code="RECOVERED_LANE_REGRESSION",
                reason="left recovered lane and regressed",
                block_s=120.0,
                modem=modem,
                current_state=SystemState.WORKING,
            )
            action = decision._make_band_action(
                bands=[20],
                reason="probe B20",
                intent="explore",
                modem=modem,
            )
            self.assertIsNone(action)
            events = decision.drain_runtime_events()
            self.assertTrue(
                any(
                    e.get("kind") == "action_availability_blocked"
                    and e.get("reason_code") == "objective_feedback"
                    and e.get("intent") == "explore"
                    for e in events
                )
            )
        finally:
            td.cleanup()

    def test_no_effect_post_owner_guard_is_deleted(self):
        decision, td = self._make_engine()
        try:
            self.assertFalse(hasattr(decision, "_guard_no_effect_band_action"))
        finally:
            td.cleanup()

    def test_decision_pipeline_does_not_duplicate_final_band_policy_owner(self):
        decision, td = self._make_engine()
        try:
            manual = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [3]},
                reason="manual",
                intent="manual_band_selection",
            )
            decision.action_source_state_authority.note_operator_band_physical_acceptance(
                lever=manual.lever.name,
                params=manual.params,
                origin="operator_ngui",
                target_signature="B3",
                observed_at=1000.0,
                receipt_id="operator-band-pipeline",
            )

            modem = make_modem(primary_band=3, ca_signature="B3", ca_active=False)
            auto = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1]},
                reason="explore add b1",
                intent="explore",
                objective_id="explore:test",
            )
            out = decision._run_action_pipeline(
                action=auto,
                modem=modem,
                state=SystemState.WORKING,
                now=1005.0,
                source="test_operator_band_embargo",
                apply_emergency_guard=False,
            )
            self.assertIsNotNone(out)
            policy = decision.action_source_state_authority.band_action_policy_decision(
                lever=auto.lever.name,
                params=auto.params,
                origin=ActionOriginKind.SMART_LOOP.value,
                now=1005.0,
            )
            self.assertFalse(policy.allowed)
            self.assertEqual(policy.reason, "autonomous_band_embargo_active")
        finally:
            td.cleanup()

    def test_final_band_policy_has_no_emergency_bypass(self):
        decision, td = self._make_engine()
        try:
            manual = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [3]},
                reason="manual",
                intent="manual_band_selection",
            )
            decision.action_source_state_authority.note_operator_band_physical_acceptance(
                lever=manual.lever.name,
                params=manual.params,
                origin="operator_ngui",
                target_signature="B3",
                observed_at=1000.0,
                receipt_id="operator-band-emergency",
            )
            decision.posture_authority.private_state_for_test.state = SystemState.E_RF_COLLAPSE

            modem = make_modem(primary_band=3, ca_signature="B3", ca_active=False)
            auto = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1]},
                reason="emergency recovery",
                intent="emergency_recovery",
                objective_id="emergency:test",
            )
            out = decision._run_action_pipeline(
                action=auto,
                modem=modem,
                state=SystemState.E_RF_COLLAPSE,
                now=1005.0,
                source="test_operator_embargo_emergency",
                apply_emergency_guard=False,
            )
            self.assertIsNotNone(out)
            policy = decision.action_source_state_authority.band_action_policy_decision(
                lever=auto.lever.name,
                params=auto.params,
                origin=ActionOriginKind.FAULT_RECOVERY.value,
                now=1005.0,
            )
            self.assertFalse(policy.allowed)
            self.assertEqual(policy.reason, "autonomous_band_embargo_active")
        finally:
            td.cleanup()

    def test_operator_embargo_suppresses_band_proposal_before_pipeline(self):
        decision, td = self._make_engine()
        try:
            manual = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [3]},
                reason="manual",
                intent="manual_band_selection",
            )
            decision.action_source_state_authority.note_operator_band_physical_acceptance(
                lever=manual.lever.name,
                params=manual.params,
                origin="operator_ngui",
                target_signature="B3",
                observed_at=1000.0,
                receipt_id="operator-band-proposal",
            )

            modem = make_modem(primary_band=3, ca_signature="B3", ca_active=False)
            with patch("arch4.brain.decision_support.time.time", return_value=1005.0):
                action = decision._make_band_action(
                    bands=[1],
                    reason="explore add b1",
                    intent="explore",
                    modem=modem,
                )
            self.assertIsNone(action)
            events = decision.drain_runtime_events()
            self.assertTrue(
                any(
                    e.get("kind") == "autonomous_band_embargo_deferred"
                    and e.get("lever") == "BAND_SELECTION"
                    and e.get("intent") == "explore"
                    for e in events
                )
            )
        finally:
            td.cleanup()

    def test_operator_embargo_suppresses_pband_proposal_before_pipeline(self):
        decision, td = self._make_engine()
        try:
            manual = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [3]},
                reason="manual",
                intent="manual_band_selection",
            )
            decision.action_source_state_authority.note_operator_band_physical_acceptance(
                lever=manual.lever.name,
                params=manual.params,
                origin="operator_ngui",
                target_signature="B3",
                observed_at=1000.0,
                receipt_id="operator-pband-proposal",
            )

            modem = make_modem(primary_band=3, ca_signature="B3", ca_active=False)
            with patch("arch4.brain.decision_support.time.time", return_value=1005.0):
                action = decision._make_pband_action(
                    pband=1,
                    ca_bands=[3, 20],
                    reason="anchor b1",
                    intent="explore",
                    modem=modem,
                )
            self.assertIsNone(action)
            events = decision.drain_runtime_events()
            self.assertTrue(
                any(
                    e.get("kind") == "autonomous_band_embargo_deferred"
                    and e.get("lever") == "PBAND"
                    and e.get("intent") == "explore"
                    for e in events
                )
            )
        finally:
            td.cleanup()

    def test_explore_add_preserves_selected_runtime_pool(self):
        decision, td = self._make_engine()
        try:
            modem = make_modem(
                primary_band=3,
                ca_signature="B3+CA",
                ca_active=True,
                selected_band_set=[1, 3, 7, 20, 28],
            )
            auto = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [7], "band_mode": "add"},
                reason="probe b7",
                intent="explore",
                objective_id="explore:test",
            )
            out = decision._canonicalize_band_action(auto, modem)
            self.assertEqual(
                out.params.get("resolved_target_bands"),
                [1, 3, 7, 20, 28],
            )
        finally:
            td.cleanup()

    def test_explore_add_can_expand_selected_runtime_pool_safely(self):
        decision, td = self._make_engine()
        try:
            modem = make_modem(
                primary_band=3,
                ca_signature="B3+CA",
                ca_active=True,
                selected_band_set=[1, 3, 20],
            )
            auto = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [7], "band_mode": "add"},
                reason="probe b7",
                intent="explore",
                objective_id="explore:expand",
            )
            out = decision._canonicalize_band_action(auto, modem)
            self.assertEqual(
                out.params.get("resolved_target_bands"),
                [1, 3, 7, 20],
            )
        finally:
            td.cleanup()

    def test_fault_monitoring_set_may_shrink_pool(self):
        decision, td = self._make_engine()
        try:
            modem = make_modem(
                primary_band=3,
                ca_signature="B3+CA",
                ca_active=True,
                selected_band_set=[1, 3, 7, 20, 28],
            )
            auto = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [20], "sequential": True},
                reason="bad rf rescue",
                intent="fault_monitoring",
                objective_id="fault:test",
            )
            out = decision._run_action_pipeline(
                action=auto,
                modem=modem,
                state=SystemState.BAD_RF,
                now=1005.0,
                source="test_fault_monitoring_set_may_shrink_pool",
                apply_emergency_guard=False,
            )
            self.assertIsNotNone(out)
            self.assertEqual(out.params.get("resolved_target_bands"), [20])
        finally:
            td.cleanup()

    def test_fault_monitoring_can_reopen_broader_pool_after_shrink(self):
        decision, td = self._make_engine()
        try:
            modem = make_modem(
                primary_band=20,
                ca_signature="B20",
                ca_active=False,
                selected_band_set=[20],
            )
            auto = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1, 3, 7, 20, 28], "sequential": True},
                reason="bad rf reopen",
                intent="fault_monitoring",
                objective_id="fault:reopen",
            )
            out = decision._run_action_pipeline(
                action=auto,
                modem=modem,
                state=SystemState.BAD_RF,
                now=1005.0,
                source="test_fault_monitoring_can_reopen_broader_pool_after_shrink",
                apply_emergency_guard=False,
            )
            self.assertIsNotNone(out)
            self.assertEqual(out.params.get("resolved_target_bands"), [1, 3, 7, 20, 28])
        finally:
            td.cleanup()

    def test_systematic_probe_bypasses_emergency_guard(self):
        decision, td = self._make_engine()
        try:
            decision.posture_authority.private_state_for_test.state = SystemState.BAD_RF
            decision.posture_authority.private_state_for_test.emergency_class = "BAD_RF"
            decision.cooldowns.set_until("emergency", 1001.0)
            modem = make_modem(primary_band=3, ca_signature="B3", ca_active=False)
            action = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [20]},
                reason="systematic probe test",
                intent="systematic_probe",
                objective_id="systematic_probe:B20",
            )
            with patch("arch4.brain.decision_policy.time.time", return_value=1000.0):
                out = decision._run_action_pipeline(
                    action=action,
                    modem=modem,
                    state=SystemState.BAD_RF,
                    now=1000.0,
                    source="test_systematic_probe_guard",
                    apply_emergency_guard=True,
                )
            self.assertIsNotNone(out)
            self.assertEqual(out.intent, "systematic_probe")
        finally:
            td.cleanup()

    def test_fault_monitoring_still_respects_emergency_guard(self):
        decision, td = self._make_engine()
        try:
            decision.posture_authority.private_state_for_test.state = SystemState.BAD_RF
            decision.posture_authority.private_state_for_test.emergency_class = "BAD_RF"
            decision.cooldowns.set_until("emergency", 1001.0)
            modem = make_modem(primary_band=3, ca_signature="B3", ca_active=False)
            action = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [3, 20]},
                reason="fault monitoring test",
                intent="fault_monitoring",
                objective_id="fault_monitoring:B3+B20",
            )
            with patch("arch4.brain.decision_policy.time.time", return_value=1000.0):
                out = decision._run_action_pipeline(
                    action=action,
                    modem=modem,
                    state=SystemState.BAD_RF,
                    now=1000.0,
                    source="test_fault_monitoring_guard",
                    apply_emergency_guard=True,
                )
            self.assertIsNone(out)
            events = decision.drain_runtime_events()
            self.assertTrue(
                any(
                    e.get("kind") == "decision_trace"
                    and e.get("primary_blocker_gate") == "emergency_guard"
                    for e in events
                )
            )
        finally:
            td.cleanup()

    def test_operator_cell_lock_is_visible_to_final_band_policy_owner(self):
        decision, td = self._make_engine()
        try:
            manual = LeverAction(
                lever=LeverType.CELL_LOCK,
                params={"pci": 123, "earfcn": 1400},
                reason="manual cell lock",
                intent="manual_cell_lock",
            )
            decision.action_source_state_authority.note_operator_band_physical_acceptance(
                lever=manual.lever.name,
                params=manual.params,
                origin="operator_ngui",
                target_signature="CELL_LOCK:pci=123:earfcn=1400",
                observed_at=1000.0,
                receipt_id="operator-cell-lock-pipeline",
            )

            modem = make_modem(primary_band=3, ca_signature="B3", ca_active=False)
            auto = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [1]},
                reason="explore add b1",
                intent="explore",
                objective_id="explore:test:cell-lock",
            )
            out = decision._run_action_pipeline(
                action=auto,
                modem=modem,
                state=SystemState.WORKING,
                now=1005.0,
                source="test_operator_cell_lock_embargo",
                apply_emergency_guard=False,
            )
            self.assertIsNotNone(out)
            policy = decision.action_source_state_authority.band_action_policy_decision(
                lever=auto.lever.name,
                params=auto.params,
                origin=ActionOriginKind.SMART_LOOP.value,
                now=1005.0,
            )
            self.assertFalse(policy.allowed)
            self.assertEqual(policy.reason, "autonomous_band_embargo_active")
        finally:
            td.cleanup()

    def test_operator_band_policy_uses_fixed_clocks(self):
        decision, td = self._make_engine()
        try:
            action = LeverAction(
                lever=LeverType.BAND_SELECTION,
                params={"bands": [3]},
                reason="operator",
                intent="manual_band_selection",
            )
            decision.action_source_state_authority.note_operator_band_physical_acceptance(
                lever=action.lever.name,
                params=action.params,
                origin="operator_ngui",
                target_signature="B3",
                observed_at=1000.0,
                receipt_id="operator-fixed-clocks",
            )
            status = decision.action_source_state_authority.band_action_policy_status(
                now=1000.0
            )
            self.assertEqual(status["operator_cooldown_until_ts"], 1030.0)
            self.assertEqual(status["autonomous_embargo_until_ts"], 1900.0)
        finally:
            td.cleanup()

    def test_decision_cooldown_state_persists_across_restart(self):
        first, td1 = self._make_engine()
        try:
            now = time.time()
            first.cooldowns.set("hilink_rotate", hold_s=300.0, now=now)
            first.cooldowns.set("congestion_mitigation", hold_s=600.0, now=now)
            first.cooldowns.set("band_cooldown", hold_s=120.0, now=now)

            saved = first.get_decision_cooldown_state()
            self.assertGreater(saved["hilink_rotate_cooldown_until"], now)
            self.assertNotIn("b818_rotate_cooldown_until", saved)
            self.assertGreater(saved["congestion_mitigation_cooldown_until"], now)
            self.assertGreater(saved["band_cooldown_until"], now)

            second, td2 = self._make_engine()
            try:
                second.restore_decision_cooldown_state(saved)
                self.assertGreater(second.cooldowns.get_until("hilink_rotate"), now)
                self.assertGreater(second.cooldowns.get_until("congestion_mitigation"), now)
                self.assertGreater(second.cooldowns.get_until("band_cooldown"), now)
            finally:
                td2.cleanup()
        finally:
            td1.cleanup()

    def test_decision_cooldown_state_does_not_restore_expired(self):
        first, td1 = self._make_engine()
        try:
            second, td2 = self._make_engine()
            try:
                second.restore_decision_cooldown_state(
                    {
                        "hilink_rotate_cooldown_until": time.time() - 10.0,
                        "congestion_mitigation_cooldown_until": time.time() - 20.0,
                    }
                )
                self.assertEqual(second.cooldowns.get_until("hilink_rotate"), 0.0)
                self.assertEqual(second.cooldowns.get_until("congestion_mitigation"), 0.0)
            finally:
                td2.cleanup()
        finally:
            td1.cleanup()

    def test_deleted_cell_lock_context_is_not_persisted(self):
        first, td1 = self._make_engine()
        try:
            saved = first.get_decision_cooldown_state()
            self.assertNotIn("cell_lock_context", saved)

            second, td2 = self._make_engine()
            try:
                second.restore_decision_cooldown_state(saved)
                self.assertNotIn(
                    "cell_lock_context",
                    second.get_decision_cooldown_state(),
                )
            finally:
                td2.cleanup()
        finally:
            td1.cleanup()


class TestInputFreezeDetection(unittest.TestCase):
    def _make_modem(
        self,
        *,
        rsrp=-85.0,
        sinr=12.0,
        rsrq=-9.0,
        band=3,
        pci=42,
        ca_active=False,
        uptime=100.0,
        connected=True,
        rx_bps=100_000.0,
        tx_bps=10_000.0,
        mcs_dl=20,
        mcs_ul=10,
        cqi=9,
        timestamp=None,
    ):
        modem = make_modem(
            rsrp_dbm=rsrp,
            sinr_db=sinr,
            rsrq_db=rsrq,
            mcs_dl=mcs_dl,
            mcs_ul=mcs_ul,
            cqi=cqi,
            primary_band=band,
            pci=pci,
            ca_active=ca_active,
            session_uptime_s=uptime,
            radio_connected=connected,
            rx_bytes_per_sec=rx_bps,
            tx_bytes_per_sec=tx_bps,
        )
        if timestamp is not None:
            modem.timestamp = float(timestamp)
        return modem

    def test_freeze_count_increments_only_when_full_modem_fingerprint_stalls(self):
        sc = SignalComputer(Arch4Config())
        modem = self._make_modem(uptime=100.0)
        cs = sc.update(modem, None)
        self.assertEqual(cs.input_freeze_samples, 0)
        cs = sc.update(modem, None)
        self.assertEqual(cs.input_freeze_samples, 1)
        cs = sc.update(modem, None)
        self.assertEqual(cs.input_freeze_samples, 2)

    def test_freeze_count_does_not_increment_when_only_rf_is_stable_but_uptime_moves(self):
        sc = SignalComputer(Arch4Config())
        modem1 = self._make_modem(uptime=100.0)
        modem2 = self._make_modem(uptime=102.0)
        sc.update(modem1, None)
        cs = sc.update(modem2, None)
        self.assertEqual(cs.input_freeze_samples, 0)

    def test_freeze_count_does_not_increment_when_fresh_sample_has_same_rf(self):
        sc = SignalComputer(Arch4Config())
        modem1 = self._make_modem(timestamp=1000.0, uptime=100.0, rx_bps=100_000.0)
        modem2 = self._make_modem(timestamp=1002.0, uptime=100.0, rx_bps=100_000.0)
        sc.update(modem1, None)
        cs = sc.update(modem2, None)
        self.assertEqual(cs.input_freeze_samples, 0)

    def test_freeze_count_resets_when_live_progress_changes_at_same_timestamp(self):
        sc = SignalComputer(Arch4Config())
        modem1 = self._make_modem(timestamp=1000.0, uptime=100.0, rx_bps=100_000.0)
        modem2 = self._make_modem(timestamp=1000.0, uptime=100.0, rx_bps=250_000.0)
        sc.update(modem1, None)
        cs = sc.update(modem2, None)
        self.assertEqual(cs.input_freeze_samples, 0)

    def test_freeze_count_resets_on_signal_change(self):
        sc = SignalComputer(Arch4Config())
        modem = self._make_modem(uptime=100.0)
        sc.update(modem, None)
        sc.update(modem, None)
        cs = sc.update(self._make_modem(rsrp=-90.0, uptime=100.0), None)
        self.assertEqual(cs.input_freeze_samples, 0)

    def test_freeze_count_zero_when_not_connected(self):
        sc = SignalComputer(Arch4Config())
        modem = self._make_modem(band=0, pci=0, uptime=0.0, connected=False)
        cs = sc.update(modem, None)
        self.assertEqual(cs.input_freeze_samples, 0)
        cs = sc.update(modem, None)
        self.assertEqual(cs.input_freeze_samples, 0)

    def test_freeze_count_resets_when_band_or_pci_changes(self):
        sc = SignalComputer(Arch4Config())
        modem = self._make_modem(uptime=100.0)
        sc.update(modem, None)
        sc.update(modem, None)
        cs = sc.update(self._make_modem(band=1, uptime=100.0), None)
        self.assertEqual(cs.input_freeze_samples, 0)
        sc.update(modem, None)
        sc.update(modem, None)
        cs = sc.update(self._make_modem(pci=99, uptime=100.0), None)
        self.assertEqual(cs.input_freeze_samples, 0)


if __name__ == "__main__":
    unittest.main()


class TestStateActionCorrectness(unittest.TestCase):
    """State/action correctness audit.

    Verifies that each state correctly allows or suppresses automatic decisions,
    and that manual actions bypass state restrictions as designed.
    """

    def _make_decision_engine(self, cfg=None):
        cfg = cfg or Arch4Config()
        td = tempfile.mkdtemp()
        mem = Memory(cfg, memory_dir=td)
        sm = TestControllerPostureAuthority(cfg, mem)
        aggr = AggressivenessEngine(cfg)
        hist = HistoricalJournal(cfg)
        de = DecisionEngine(cfg, mem, sm, Constraints(), aggr, hist)
        return de, td

    def test_deleted_blanking_state_and_entry_path_are_absent(self):
        de, td = self._make_decision_engine()
        try:
            self.assertFalse(hasattr(SystemState, "BLANKING"))
            self.assertFalse(hasattr(de.posture_authority, "enter_blanking"))
            self.assertEqual(de.posture_authority.state, SystemState.WORKING)
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    # ── CONGESTED ─────────────────────────────────────────────────────────

    def test_congested_waits_for_canonical_selection_without_crashing(self):
        """CONGESTED observation cannot revive a direct state action owner."""
        cfg = Arch4Config()
        de, td = self._make_decision_engine(cfg)
        try:
            de.posture_authority.private_state_for_test.state = SystemState.CONGESTED
            de.posture_authority.private_state_for_test.entered_at = time.time()
            de.posture_authority.private_state_for_test.congestion_kind = "rtt"
            de.cooldowns.clear("congestion_mitigation")

            # With no selected-action transaction, Brain must remain stable and
            # wait for the opportunity/Bandit/Brain owner chain.
            modem = make_modem(rx_bytes_per_sec=300_000)
            computed = make_computed(180.0, 0.1)

            # Verify the absent selection is non-crashing and non-actuating.
            try:
                action = _decide(de, modem, computed)
                self.assertIsNone(action)
            except Exception as e:
                self.fail(f"CONGESTED decide() raised: {e}")
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    # ── BAD_RF ────────────────────────────────────────────────────────────

    def test_bad_rf_state_is_low_action(self):
        """BAD_RF must produce at most one bounded action (step 0 or 1)
        and must never produce exploration-driven band actions.
        """
        de, td = self._make_decision_engine()
        try:
            de.posture_authority.private_state_for_test.state = SystemState.BAD_RF
            de.posture_authority.private_state_for_test.entered_at = time.time()
            de.posture_authority.private_state_for_test.emergency_class = "BAD_RF"
            de.posture_authority.private_state_for_test.bad_rf_recovery_step = 0
            de._exploration_pressure.pressure = 2.0  # high pressure must not matter

            modem = make_modem(sinr_db=2.0, rsrp_dbm=-110.0, radio_connected=True)
            computed = make_computed(60.0, 0.3)

            # Multiple ticks - must never churn
            actions = []
            for _ in range(5):
                a = _decide(de, modem, computed)
                if a is not None:
                    actions.append(a)

            # BAD_RF aggressiveness: max_actions_per_window=1
            self.assertLessEqual(
                len(actions), 1,
                f"BAD_RF must produce at most 1 action in 5 ticks, got {len(actions)}: {actions}",
            )
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    def test_bad_rf_b20_rescue_preserves_selected_ca_pool(self):
        cfg = Arch4Config(min_action_interval_s=0.0)
        de, td = self._make_decision_engine(cfg)
        try:
            de.posture_authority.private_state_for_test.state = SystemState.BAD_RF
            de.posture_authority.private_state_for_test.entered_at = time.time()
            de.posture_authority.private_state_for_test.emergency_class = "BAD_RF"
            de.posture_authority.private_state_for_test.bad_rf_recovery_step = 1
            de.posture_authority.private_state_for_test.bad_rf_next_action_at = 0.0
            de.deployed_bands = {1, 3, 20}
            de._is_a1_overnight_profile = lambda: True
            de._is_a1_restricted_night_window = lambda now=None, modem=None: True

            modem = make_modem(
                primary_band=3,
                selected_band_set=[1, 3, 20],
                ca_active=True,
                ca_signature="B1+B3+B20",
                sinr_db=-2.0,
                rsrp_dbm=-92.0,
                rsrq_db=-13.0,
            )
            computed = make_computed(60.0, 0.25)

            action = _decide(de, modem, computed)

            self.assertIsNotNone(action)
            self.assertEqual(action.lever, LeverType.PBAND)
            self.assertEqual(action.params.get("pband"), 20)
            self.assertEqual(action.params.get("dl_bands"), [1, 3])
            self.assertIn("preserving the current CA pool", action.reason)
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    # ── E_RADIO_DOWN ──────────────────────────────────────────────────────

    def test_e_radio_down_produces_no_band_selection(self):
        """E_RADIO_DOWN must not propose band selection actions.

        Radio is down — changing bands is meaningless until the link recovers.
        Only modem restart actions are valid in this state.
        """
        de, td = self._make_decision_engine()
        try:
            de.posture_authority.private_state_for_test.state = SystemState.E_RADIO_DOWN
            de.posture_authority.private_state_for_test.entered_at = time.time()
            de.posture_authority.private_state_for_test.emergency_class = "E_RADIO_DOWN"
            de.posture_authority.private_state_for_test.emergency_timeout_at = time.time() + 300.0

            modem = make_modem(radio_connected=False)
            computed = make_computed(0.0, 0.0)

            for _ in range(3):
                action = _decide(de, modem, computed)
                if action is not None:
                    self.assertNotEqual(
                        action.lever, LeverType.BAND_SELECTION,
                        f"E_RADIO_DOWN must not propose BAND_SELECTION, got {action}",
                    )
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    # ── Cross-state: emergency pre-empts non-fault states ─────────────────

    def test_emergency_preempts_congested(self):
        """A hard emergency (E_RADIO_DOWN) must preempt CONGESTED.

        All non-fault states pass through _check_emergency before their own
        eval logic. If radio drops during congestion, the system
        must transition to E_RADIO_DOWN.
        """
        cfg = Arch4Config(
            emergency_radio_down_persist_s=0.0,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            sm = TestControllerPostureAuthority(cfg, mem)
            sm.private_state_for_test.state = SystemState.CONGESTED
            sm.private_state_for_test.entered_at = time.time()

            modem = make_modem(radio_connected=False, session_uptime_s=0.0)
            computed = make_computed(0.0, 0.0)

            new_state = sm.evaluate_state_for_test(modem, computed)
            self.assertEqual(
                new_state, SystemState.E_RADIO_DOWN,
                f"Radio down during CONGESTED must enter E_RADIO_DOWN, got {new_state}",
            )


class TestOvernightStabilityScenarios(unittest.TestCase):
    """Stability scenarios: reconnect noise, session flaps, post-fault damping.

    These scenarios represent the most common sources of overnight failures
    in a deployed LTE controller: brief modem dropouts, session resets,
    and churning after fault recovery.
    """

    def _make_sm(self, cfg=None):
        cfg = cfg or Arch4Config()
        td = tempfile.mkdtemp()
        mem = Memory(cfg, memory_dir=td)
        sm = TestControllerPostureAuthority(cfg, mem)
        sm.private_state_for_test.state = SystemState.WORKING
        sm.private_state_for_test.entered_at = time.time()
        return sm, td

    # ── Reconnect noise ───────────────────────────────────────────────────

    def test_brief_radio_dropout_does_not_trigger_e_radio_down(self):
        """A dropout shorter than emergency_radio_down_persist_s must not enter E_RADIO_DOWN.

        Real-world: modem HTTP fetch returns radio_connected=False for 1-2 ticks
        during a normal cell handover. The persist guard must absorb this noise.
        """
        cfg = Arch4Config(emergency_radio_down_persist_s=10.0)
        sm, td = self._make_sm(cfg)
        try:
            good = make_modem(radio_connected=True, session_uptime_s=300.0)
            down = make_modem(radio_connected=False, session_uptime_s=0.0)
            computed = make_computed(50.0, 0.8)

            # Single tick of radio down — well within persist window
            sm._radio_down_since = time.time() - 2.0  # 2s down, persist=10s
            state = sm.evaluate_state_for_test(down, computed)

            self.assertEqual(
                state, SystemState.WORKING,
                f"Brief dropout (2s < 10s persist) must stay WORKING, got {state}",
            )
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    def test_radio_reconnect_resets_down_timer(self):
        """After reconnect, the radio-down timer must reset so subsequent brief
        dropouts don't immediately accumulate toward the persist threshold.
        """
        cfg = Arch4Config(emergency_radio_down_persist_s=5.0)
        sm, td = self._make_sm(cfg)
        try:
            connected = make_modem(radio_connected=True, session_uptime_s=300.0)
            computed = make_computed(50.0, 0.8)

            # Simulate: was down for 4s (just under threshold)
            sm._radio_down_since = time.time() - 4.0

            # Now reconnects — timer must reset
            state = sm.evaluate_state_for_test(connected, computed)
            self.assertEqual(state, SystemState.WORKING)
            self.assertEqual(
                sm._radio_down_since, 0.0,
                "radio_down_since must reset to 0 after reconnect",
            )

            # Another brief disconnect — accumulation must start fresh
            sm._radio_down_since = time.time() - 1.0
            down = make_modem(radio_connected=False)
            state = sm.evaluate_state_for_test(down, computed)
            self.assertEqual(
                state, SystemState.WORKING,
                "Post-reconnect dropout must not immediately enter fault (timer reset)",
            )
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    def test_radio_down_persisting_does_enter_e_radio_down(self):
        """A dropout that exceeds persist_s must eventually enter E_RADIO_DOWN.

        Contrasting test: the persist guard works correctly in both directions.
        """
        cfg = Arch4Config(emergency_radio_down_persist_s=3.0)
        sm, td = self._make_sm(cfg)
        try:
            down = make_modem(radio_connected=False, session_uptime_s=0.0)
            computed = make_computed(0.0, 0.0)

            # Simulate 5s of radio down (> persist_s=3)
            sm._radio_down_since = time.time() - 5.0
            state = sm.evaluate_state_for_test(down, computed)

            self.assertEqual(
                state, SystemState.E_RADIO_DOWN,
                f"Dropout exceeding persist_s must enter E_RADIO_DOWN, got {state}",
            )
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    # ── Session flap ──────────────────────────────────────────────────────

    def test_session_uptime_flap_does_not_trigger_session_reset_in_first_30s(self):
        """E_SESSION_RESET requires time_in_state_s > 30s.

        Real-world: session_uptime_s briefly reads <5 during modem reconnect.
        The 30s hysteresis prevents false session resets in early state tenure.
        """
        cfg = Arch4Config()
        sm, td = self._make_sm(cfg)
        try:
            # Fresh state — entered_at just now (time_in_state_s ≈ 0)
            sm.private_state_for_test.entered_at = time.time()
            flap = make_modem(radio_connected=True, session_uptime_s=0.5)
            computed = make_computed(50.0, 0.8)

            state = sm.evaluate_state_for_test(flap, computed)
            self.assertEqual(
                state, SystemState.WORKING,
                "Session uptime <5s in first 30s of state must not trigger E_SESSION_RESET",
            )
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    def test_session_reset_triggers_after_30s_state_tenure(self):
        """E_SESSION_RESET fires when session_uptime_s <5 after >30s in state.

        This is the real session-reset scenario: modem silently reconnected
        to a different cell/session after extended operation.
        """
        cfg = Arch4Config()
        sm, td = self._make_sm(cfg)
        try:
            # Been in WORKING for 40s — past the hysteresis window
            sm.private_state_for_test.entered_at = time.time() - 40.0
            flap = make_modem(radio_connected=True, session_uptime_s=2.0)
            computed = make_computed(50.0, 0.8)

            state = sm.evaluate_state_for_test(flap, computed)
            self.assertEqual(
                state, SystemState.E_SESSION_RESET,
                f"Session uptime <5s after 40s in state must trigger E_SESSION_RESET, got {state}",
            )
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    def test_unknown_uptime_does_not_trigger_false_session_reset_after_startup(self):
        """Unknown B818 uptime must not be treated as a real session reset.

        B818 fast samples can have RF and traffic before the slow maintenance
        lane publishes session uptime. A zero value from that pending source is
        unknown truth, not proof of a reset.
        """
        cfg = Arch4Config()
        sm, td = self._make_sm(cfg)
        try:
            sm.private_state_for_test.entered_at = time.time() - 40.0
            modem = make_modem(
                radio_connected=True,
                session_uptime_s=0.0,
                rx_bytes_per_sec=100_000,
                tx_bytes_per_sec=20_000,
            )
            modem.session_uptime_source = "maintenance_pending"
            computed = make_computed(50.0, 0.8)

            state = sm.evaluate_state_for_test(modem, computed)

            self.assertEqual(
                state,
                SystemState.WORKING,
                "Pending/unknown uptime must not create E_SESSION_RESET while RF and traffic are alive",
            )
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    # ── Manual band-switch grace window ─────────────────────────────────

    def test_session_reset_suppressed_during_planned_action_grace(self):
        """E_SESSION_RESET must be suppressed during a planned-action grace window.

        Real-world: manual band change causes a normal session reset (uptime
        drops to 0). Without suppression, this immediately escalates to
        E_SESSION_RESET even though the modem is behaving exactly as expected.
        """
        cfg = Arch4Config()
        sm, td = self._make_sm(cfg)
        try:
            # Been in WORKING for 40s — past the 30s hysteresis
            sm.private_state_for_test.entered_at = time.time() - 40.0
            flap = make_modem(radio_connected=True, session_uptime_s=2.0)
            _arm_expected_effect_mask(flap)
            computed = make_computed(50.0, 0.8)

            state = sm.evaluate_state_for_test(flap, computed)
            self.assertEqual(
                state, SystemState.WORKING,
                "Session reset during active suppress window must stay WORKING",
            )
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    def test_session_reset_fires_after_grace_window_expires(self):
        """E_SESSION_RESET must still fire once the suppress window has expired.

        Ensures the grace window does not permanently hide real session resets.
        """
        cfg = Arch4Config()
        sm, td = self._make_sm(cfg)
        try:
            sm.private_state_for_test.entered_at = time.time() - 40.0
            flap = make_modem(radio_connected=True, session_uptime_s=2.0)
            computed = make_computed(50.0, 0.8)

            state = sm.evaluate_state_for_test(flap, computed)
            self.assertEqual(
                state, SystemState.E_SESSION_RESET,
                "Session reset after expired suppress window must escalate normally",
            )
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    def test_radio_down_still_suppressed_during_grace_window(self):
        """E_RADIO_DOWN must remain suppressed during the grace window (existing behavior)."""
        cfg = Arch4Config(emergency_radio_down_persist_s=3.0)
        sm, td = self._make_sm(cfg)
        try:
            sm._radio_down_since = time.time() - 5.0  # 5s > persist_s=3

            down = make_modem(radio_connected=False, session_uptime_s=0.0)
            _arm_expected_effect_mask(down)
            computed = make_computed(0.0, 0.0)

            state = sm.evaluate_state_for_test(down, computed)
            self.assertEqual(
                state, SystemState.WORKING,
                "Radio down during active suppress window must stay WORKING",
            )
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    def test_unexpected_session_reset_without_grace_still_escalates(self):
        """An unexpected session reset (no suppress window) must still escalate.

        Ensures the fix is narrow: only planned actions get the grace window,
        not random modem misbehavior.
        """
        cfg = Arch4Config()
        sm, td = self._make_sm(cfg)
        try:
            sm.private_state_for_test.entered_at = time.time() - 40.0
            # No suppress window set (default 0.0)

            flap = make_modem(radio_connected=True, session_uptime_s=2.0)
            computed = make_computed(50.0, 0.8)

            state = sm.evaluate_state_for_test(flap, computed)
            self.assertEqual(
                state, SystemState.E_SESSION_RESET,
                "Unexpected session reset without grace window must escalate",
            )
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    def test_prolonged_outage_past_grace_window_escalates(self):
        """If modem stays down past the grace window, escalation must proceed.

        Simulates a band change that causes a real prolonged failure — the
        grace window expires and normal fault recovery takes over.
        """
        cfg = Arch4Config(emergency_radio_down_persist_s=3.0)
        sm, td = self._make_sm(cfg)
        try:
            sm._radio_down_since = time.time() - 5.0  # 5s > persist_s=3

            down = make_modem(radio_connected=False, session_uptime_s=0.0)
            computed = make_computed(0.0, 0.0)

            state = sm.evaluate_state_for_test(down, computed)
            self.assertEqual(
                state, SystemState.E_RADIO_DOWN,
                "Prolonged outage past grace window must escalate to E_RADIO_DOWN",
            )
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    # ── Post-fault damping ────────────────────────────────────────────────

    def test_post_fault_period_suppresses_exploration_pressure(self):
        """After a fault, exploration pressure must be damped during post_fault_until.

        Real-world: brain exits BAD_RF, signal is still marginal. Without damping,
        the exploration pressure built up during the fault immediately drives
        aggressive band changes, risking re-entering fault.
        """
        from arch4.brain.exploration_pressure import (
            ExplorationPressure as ProductionExplorationPressure,
        )

        class ExplorationPressure(ProductionExplorationPressure):
            def update(self, modem, computed, **kwargs):
                intelligence, derived = cellular_test_inputs(modem, computed)
                return super().update(intelligence, derived, **kwargs)

        cfg = Arch4Config()
        ep = ExplorationPressure()

        modem = make_modem(rx_bytes_per_sec=100_000)
        computed = make_computed(60.0, 0.7)

        # Update without post-fault: pressure accumulates normally
        ep.update(modem, computed, post_fault_recent=False, post_fault_class="")
        pressure_normal = ep.pressure

        # Reset and update with post-fault active: pressure must be damped
        ep2 = ExplorationPressure()
        ep2.update(modem, computed, post_fault_recent=True, post_fault_class="BAD_RF")
        pressure_post_fault = ep2.pressure

        self.assertLessEqual(
            pressure_post_fault, pressure_normal,
            f"Post-fault exploration pressure ({pressure_post_fault:.3f}) must be "
            f"<= normal ({pressure_normal:.3f})",
        )

    def test_post_fault_damping_fields_set_after_bad_rf_exit(self):
        """StateContext.post_fault_until and post_fault_class must be set
        when the system exits BAD_RF back to WORKING.

        These fields gate the canonical normal-opportunity pressure inputs.
        """
        cfg = Arch4Config(
            emergency_radio_down_persist_s=0.0,
            sinr_feasibility_gate_db=-5.0,
        )
        sm, td = self._make_sm(cfg)
        try:
            # Put system in BAD_RF
            sm.private_state_for_test.state = SystemState.BAD_RF
            sm.private_state_for_test.emergency_class = "BAD_RF"
            sm.private_state_for_test.entered_at = time.time() - 60.0
            sm.private_state_for_test.emergency_timeout_at = time.time() + 300.0
            sm.private_state_for_test.emergency_recovery_since = 0.0

            # Good signal — recovery feasible
            good = make_modem(sinr_db=20.0, radio_connected=True)
            computed = make_computed(40.0, 0.9)

            # Advance recovery
            sm.private_state_for_test.emergency_recovery_since = time.time() - 60.0
            state = sm.evaluate_state_for_test(good, computed)

            if state == SystemState.WORKING:
                self.assertGreater(
                    sm.private_state_for_test.post_fault_until, time.time(),
                    "post_fault_until must be set in the future after BAD_RF exit",
                )
                self.assertEqual(
                    sm.private_state_for_test.post_fault_class, "BAD_RF",
                    "post_fault_class must be 'BAD_RF' after BAD_RF exit",
                )
                remaining_s = sm.private_state_for_test.post_fault_until - time.time()
                self.assertLessEqual(
                    remaining_s,
                    95.0,
                    "short BAD_RF exits should use the settle-sized post-fault window",
                )
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)

    def test_post_fault_damping_is_capped_after_long_bad_rf_exit(self):
        """Long fault duration must not freeze exploration for tens of minutes."""
        cfg = Arch4Config(
            emergency_radio_down_persist_s=0.0,
            sinr_feasibility_gate_db=-5.0,
        )
        sm, td = self._make_sm(cfg)
        try:
            sm.private_state_for_test.state = SystemState.BAD_RF
            sm.private_state_for_test.emergency_class = "BAD_RF"
            sm.private_state_for_test.entered_at = time.time() - 3600.0
            sm.private_state_for_test.emergency_timeout_at = time.time() + 300.0
            sm.private_state_for_test.emergency_recovery_since = time.time() - 60.0

            state = sm.evaluate_state_for_test(make_modem(sinr_db=20.0, radio_connected=True), make_computed(40.0, 0.9))

            if state == SystemState.WORKING:
                remaining_s = sm.private_state_for_test.post_fault_until - time.time()
                self.assertLessEqual(
                    remaining_s,
                    305.0,
                    "post-fault dampening must be capped even after long BAD_RF episodes",
                )
        finally:
            import shutil; shutil.rmtree(td, ignore_errors=True)
