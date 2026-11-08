"""
arch4/tests/test_decision_policy.py

Consolidated decision policy tests — merged from:
  test_state_dispatch_policy.py
  test_pre_state_policy.py
  test_emergency_backend_policy.py
  test_decision_health_hold.py
"""
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from arch4.brain.aggressiveness import AggressivenessEngine
from arch4.brain.decision import DecisionEngine as _ProductionDecisionEngine
from arch4.brain.feed_registers import BrainFeedRegisterBank
from arch4.brain.historical import HistoricalJournal
from arch4.brain.memory import Memory
from arch4.tests.controller_posture_test_support import TestControllerPostureAuthority
from arch4.interfaces import (
    Arch4Config,
    ActionRecord,
    CarrierInfo,
    ComputedSignals,
    Constraints,
    LeverAction,
    LeverType,
    ModemInput,
    SystemState,
)
from arch4.policy.emergency_backend_policy import (
    emergency_backend,
    make_emergency_backend_action,
)
from arch4.policy.pre_state_policy import run_pre_state_policy
from arch4.policy.state_dispatch_policy import run_state_dispatch
from arch4.tests.smart_loop_test_helpers import publish_smart_loop_radio_command_for_test
from arch4.tests.controller_generation_factory import cellular_test_inputs
from arch4.tests.decision_owner_test_support import with_decision_owner_ports


class DecisionEngine(_ProductionDecisionEngine):
    """Test-only Decision Engine with explicitly composed Core owner ports."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **with_decision_owner_ports(kwargs))


def _decide(decision, modem, computed):
    intelligence, derived = cellular_test_inputs(modem, computed)
    return decision.decide(intelligence, derived)


# ── State dispatch policy ───────────────────────────────────────

class _FakeEngine:
    def __init__(self):
        self.working_action = None
        self.degraded_action = None
        self.congested_action = None
        self.e_radio_down_action = None
        self.e_rf_collapse_action = None
        self.e_session_reset_action = None
        self.e_congestion_storm_action = None
        self.big_dip_action = None
        self.big_dip_blocked = False
        self.suppressed = []

    def _decide_working(self, modem, computed):
        return self.working_action

    def _decide_degraded(self, modem, computed):
        return self.degraded_action

    def _decide_congested(self, modem, computed):
        return self.congested_action

    def _decide_e_radio_down(self, modem, computed):
        return self.e_radio_down_action

    def _decide_e_rf_collapse(self, modem, computed):
        return self.e_rf_collapse_action

    def _decide_e_session_reset(self, modem, computed):
        return self.e_session_reset_action

    def _decide_e_congestion_storm(self, modem, computed):
        return self.e_congestion_storm_action

    def _decide_a1_b818_big_dip(self, modem, now):
        return self.big_dip_action, self.big_dip_blocked

    def _suppress_b818_daytime_band_action(self, action):
        self.suppressed.append(action)
        return action


class TestStateDispatchPolicy(unittest.TestCase):
    def test_working_old_state_dispatch_path_hard_fails(self):
        engine = _FakeEngine()
        engine.working_action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [3]}, reason="working")

        with self.assertRaisesRegex(
            RuntimeError,
            "old_state_dispatch_autonomous_action_path_deleted",
        ):
            run_state_dispatch(engine, modem=None, computed=None, state=SystemState.WORKING, now=1.0)

    def test_degraded_old_state_dispatch_path_hard_fails(self):
        engine = _FakeEngine()
        engine.degraded_action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [20]}, reason="degraded")

        with self.assertRaisesRegex(
            RuntimeError,
            "old_state_dispatch_autonomous_action_path_deleted",
        ):
            run_state_dispatch(engine, modem=None, computed=None, state=SystemState.DEGRADED, now=1.0)

    def test_congested_old_state_dispatch_path_hard_fails(self):
        engine = _FakeEngine()
        engine.congested_action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [1, 3]}, reason="congested")

        with self.assertRaisesRegex(
            RuntimeError,
            "old_state_dispatch_autonomous_action_path_deleted",
        ):
            run_state_dispatch(engine, modem=None, computed=None, state=SystemState.CONGESTED, now=1.0)

    def test_e_radio_down_uses_generic_fault_handler_not_provider_override(self):
        engine = _FakeEngine()
        engine.big_dip_action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [20]}, reason="big_dip")
        engine.e_radio_down_action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [1]}, reason="fault")

        action = run_state_dispatch(engine, modem=None, computed=None, state=SystemState.E_RADIO_DOWN, now=1.0)

        self.assertIs(action, engine.e_radio_down_action)
        self.assertEqual(engine.suppressed, [])

    def test_e_radio_down_provider_block_cannot_suppress_generic_fault_handler(self):
        engine = _FakeEngine()
        engine.big_dip_blocked = True
        engine.e_radio_down_action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [1]}, reason="fault")

        action = run_state_dispatch(engine, modem=None, computed=None, state=SystemState.E_RADIO_DOWN, now=1.0)

        self.assertIs(action, engine.e_radio_down_action)
        self.assertEqual(engine.suppressed, [])

    def test_bad_rf_falls_back_to_fault_handler_without_daytime_suppression(self):
        engine = _FakeEngine()
        engine.e_rf_collapse_action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [1]}, reason="fault")

        action = run_state_dispatch(engine, modem=None, computed=None, state=SystemState.BAD_RF, now=1.0)

        self.assertIs(action, engine.e_rf_collapse_action)
        self.assertEqual(engine.suppressed, [])

    def test_e_radio_down_falls_back_to_fault_handler_when_no_big_dip(self):
        engine = _FakeEngine()
        engine.e_radio_down_action = LeverAction(lever=LeverType.RESTART, params={}, reason="radio down")

        action = run_state_dispatch(engine, modem=None, computed=None, state=SystemState.E_RADIO_DOWN, now=1.0)

        self.assertIs(action, engine.e_radio_down_action)
        self.assertEqual(engine.suppressed, [])

    def test_e_session_reset_routes_to_explicit_handler(self):
        engine = _FakeEngine()
        engine.e_session_reset_action = LeverAction(lever=LeverType.RESTART, params={}, reason="session reset")

        action = run_state_dispatch(engine, modem=None, computed=None, state=SystemState.E_SESSION_RESET, now=1.0)

        self.assertIs(action, engine.e_session_reset_action)
        self.assertEqual(engine.suppressed, [])

    def test_e_congestion_storm_routes_to_explicit_handler(self):
        engine = _FakeEngine()
        engine.e_congestion_storm_action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [3]}, reason="storm")

        action = run_state_dispatch(engine, modem=None, computed=None, state=SystemState.E_CONGESTION_STORM, now=1.0)

        self.assertIs(action, engine.e_congestion_storm_action)
        self.assertEqual(engine.suppressed, [])


# ── Pre-state policy ────────────────────────────────────────────

class _PreStateFakeEngine:
    def __init__(self):
        self.freeze = False
        self.morning_action = None
        self.morning_guard = {"active": False}
        self.pipeline_calls = []
        self.traces = []
        self.events = []

    def _is_a1_hard_night_freeze_window(self, now):
        return self.freeze

    def _decide_provider_overnight_recovery(self, modem, now):
        return None, False

    def _decide_provider_morning_sequence(self, modem, now):
        return self.morning_action

    def _provider_morning_routine_guard_status(self, now):
        return dict(self.morning_guard)

    def _run_special_band_pipeline(self, **kwargs):
        self.pipeline_calls.append(dict(kwargs))
        return kwargs["action"]

    def _emit_decision_trace(self, **kwargs):
        self.traces.append(dict(kwargs))

    def _emit_runtime_event(self, kind, **kwargs):
        self.events.append({"kind": kind, **kwargs})


class TestPreStatePolicy(unittest.TestCase):
    def test_old_provider_pre_state_path_hard_fails(self):
        engine = _PreStateFakeEngine()
        engine.morning_action = LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [3]}, reason="morning")

        with self.assertRaisesRegex(
            RuntimeError,
            "old_provider_pre_state_action_path_deleted",
        ):
            run_pre_state_policy(
                engine,
                modem=None,
                state=SystemState.WORKING,
                now=1.0,
            )
        self.assertEqual(engine.pipeline_calls, [])

    def test_legacy_a1_b818_hooks_are_not_selected_by_shared_pre_state_policy(self):
        class _LegacyOnly:
            def __init__(self):
                self.calls = []
                self.pipeline_calls = []

            def _decide_a1_overnight_recovery(self, modem, now):
                self.calls.append("legacy_overnight")
                return LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [20]}, reason="legacy"), False

            def _decide_a1_b818_morning_sequence(self, modem, now):
                self.calls.append("legacy_morning")
                return LeverAction(lever=LeverType.BAND_SELECTION, params={"bands": [3]}, reason="legacy")

            def _run_special_band_pipeline(self, **kwargs):
                self.pipeline_calls.append(dict(kwargs))
                return kwargs["action"]

        engine = _LegacyOnly()

        with self.assertRaisesRegex(
            RuntimeError,
            "old_provider_pre_state_action_path_deleted",
        ):
            run_pre_state_policy(
                engine,
                modem=None,
                state=SystemState.WORKING,
                now=1.0,
            )
        self.assertEqual(engine.calls, [])
        self.assertEqual(engine.pipeline_calls, [])


# ── Emergency backend policy ────────────────────────────────────

class _Engine:
    def __init__(self, backend=""):
        self.policy_profile = SimpleNamespace(modem_backend=backend)


class TestEmergencyBackendPolicy(unittest.TestCase):
    def test_backend_detection(self):
        self.assertEqual(emergency_backend(_Engine("mikrotik")), "mikrotik")
        self.assertEqual(emergency_backend(_Engine("b818")), "b818")
        self.assertEqual(emergency_backend(_Engine("")), "")

    def test_mikrotik_radio_down_is_passive_hold(self):
        action = make_emergency_backend_action(
            _Engine("mikrotik"),
            emergency_class="E_RADIO_DOWN",
        )

        self.assertIsNone(action)

    def test_b818_session_reset_storm_is_passive_reconnect(self):
        action = make_emergency_backend_action(
            _Engine("b818"),
            emergency_class="E_SESSION_RESET_STORM",
        )

        self.assertIsNone(action)

    def test_generic_effective_outage_uses_plain_restart(self):
        action = make_emergency_backend_action(
            _Engine(""),
            emergency_class="E_EFFECTIVE_OUTAGE",
        )

        self.assertEqual(action.lever, LeverType.UE_CAPABILITY)
        self.assertTrue(action.params.get("restart"))
        self.assertEqual(action.intent, "emergency_restart")

    def test_mikrotik_timeout_fallback_is_passive_hold(self):
        action = make_emergency_backend_action(
            _Engine("mikrotik"),
            emergency_class="EMERGENCY_TIMEOUT",
        )

        self.assertIsNone(action)


# ── Decision health hold ────────────────────────────────────────

def make_modem() -> ModemInput:
    return ModemInput(
        sinr_db=15.0,
        rsrp_dbm=-85.0,
        rsrq_db=-9.0,
        rssi_dbm=-60.0,
        mcs_dl=12,
        mcs_ul=0,
        cqi=9,
        ri=2,
        carriers=[CarrierInfo(3, 1300, 123, 20.0, 15.0, -85.0, 1, True)],
        ca_active=True,
        ca_band_signature="B3+B1",
        primary_band=3,
        pci=123,
        earfcn=1300,
        enb_id=77,
        radio_connected=True,
        session_uptime_s=300.0,
        rx_bytes_per_sec=120_000.0,
        tx_bytes_per_sec=10_000.0,
        timestamp=time.time(),
    )


def make_computed() -> ComputedSignals:
    return ComputedSignals(
        rtt_ms=45.0,
        rtt_ema=45.0,
        rtt_sliding_avg=45.0,
        rtt_baseline=40.0,
        rtt_elevated=False,
        rtt_alarm=False,
        cq_cell=0.8,
    )


def seed_known_good_bandset(memory: Memory, bands: list[int]) -> None:
    normalized = [int(band) for band in bands]
    ca_signature = "+".join(f"B{band}" for band in normalized)
    memory.action_history.append(ActionRecord(
        timestamp=time.time(),
        sinr_db=15.0,
        rsrq_db=-9.0,
        mcs_dl=20,
        ca_signature=ca_signature,
        rtt_sliding_avg=40.0,
        rtt_ms=40.0,
        lever=LeverType.BAND_SELECTION,
        action_params={"bands": list(normalized)},
        success=True,
        throughput_before_mbps=50.0,
        throughput_after_mbps=60.0,
        sinr_after=15.0,
        ca_signature_after=ca_signature,
        improvement=10.0,
    ))


class TestDecisionHealthHold(unittest.TestCase):
    def _decision(self):
        cfg = Arch4Config(
            min_action_interval_s=0.0,
            autonomous_connect_settle_s=0.0,
            radio_change_settle_s=0.0,
        )
        td = tempfile.TemporaryDirectory()
        mem = Memory(cfg, memory_dir=td.name)
        sm = TestControllerPostureAuthority(cfg, mem)
        sm.private_state_for_test.state = SystemState.WORKING
        sm.private_state_for_test.entered_at = time.time() - 60.0
        decision = DecisionEngine(
            cfg,
            mem,
            sm,
            Constraints(pci_lock_available=True),
            aggressiveness=AggressivenessEngine(cfg),
            historical=HistoricalJournal(cfg),
            smart_loop_strategy_feed=BrainFeedRegisterBank(),
        )
        self.addCleanup(td.cleanup)
        return decision

    def test_health_warning_blocks_explore_band_action(self):
        decision = self._decision()
        decision.note_health_status({"stage": "warning", "action_hint": "observe"})
        intelligence, derived = cellular_test_inputs(make_modem(), make_computed())
        publish_smart_loop_radio_command_for_test(
            decision,
            target_band=3,
            target_bands=(1, 3),
            reason_code="background_explore",
            availability_intent="explore",
            accepted_frame=intelligence,
        )

        action = decision.decide(intelligence, derived)

        self.assertIsNone(action)
        trace = decision.get_last_decision_trace()
        trace_obj = decision.get_last_decision_trace_object()
        result_obj = decision.get_last_decision_result_object()
        context_obj = decision.get_last_decision_context_object()
        self.assertEqual(trace.get("primary_blocker_gate"), "health_hold")
        self.assertIn("suppresses risky exploration", trace.get("primary_blocker_reason", ""))
        self.assertIsNotNone(trace_obj)
        self.assertIsNotNone(result_obj)
        self.assertIsNotNone(context_obj)
        self.assertEqual(trace_obj.primary_blocker_gate, "health_hold")
        self.assertEqual(trace_obj.source, trace.get("source"))
        self.assertEqual(result_obj.primary_blocker_gate, "health_hold")
        self.assertEqual(result_obj.proposed_action.intent, "smart_loop_candidate")
        self.assertEqual(result_obj.proposed_action.availability_intent, "explore")
        self.assertFalse(result_obj.action_selected)
        self.assertEqual(context_obj.state, "WORKING")
        self.assertEqual(context_obj.health_stage, "warning")
        self.assertEqual(context_obj.health_action_hint, "observe")
        self.assertEqual(context_obj.incident_class, "healthy")
        self.assertEqual(context_obj.decision_trace_id, trace_obj.decision_trace_id)
        runtime = decision.get_runtime_status()
        assert runtime["decision_trace"]["decision_trace_id"] == trace_obj.decision_trace_id
        assert runtime["decision_result"]["primary_blocker_gate"] == "health_hold"
        assert runtime["decision_context"]["health_stage"] == "warning"

    def test_health_warning_blocks_explore_cell_lock(self):
        decision = self._decision()
        decision.note_health_status({"stage": "warning", "action_hint": "observe"})
        intelligence, derived = cellular_test_inputs(make_modem(), make_computed())
        publish_smart_loop_radio_command_for_test(
            decision,
            target_band=3,
            target_bands=(1, 3),
            reason_code="background_explore_cell_lock",
            availability_intent="explore_cell_lock",
            accepted_frame=intelligence,
        )

        action = decision.decide(intelligence, derived)

        self.assertIsNone(action)
        trace = decision.get_last_decision_trace()
        self.assertEqual(trace.get("primary_blocker_gate"), "health_hold")

    def test_health_warning_does_not_block_restore_ca(self):
        decision = self._decision()
        decision.note_health_status({"stage": "warning", "action_hint": "observe"})
        intelligence, derived = cellular_test_inputs(make_modem(), make_computed())
        publish_smart_loop_radio_command_for_test(
            decision,
            target_band=3,
            target_bands=(1, 3),
            reason_code="restore_ca_candidate",
            availability_intent="restore_ca",
            accepted_frame=intelligence,
        )

        action = decision.decide(intelligence, derived)

        self.assertIsNotNone(action)
        self.assertEqual(action.intent, "smart_loop_candidate")

    def test_healthy_stage_allows_explore_action(self):
        decision = self._decision()
        decision.note_health_status({"stage": "healthy", "action_hint": "none"})
        intelligence, derived = cellular_test_inputs(make_modem(), make_computed())
        publish_smart_loop_radio_command_for_test(
            decision,
            target_band=3,
            target_bands=(1, 3),
            reason_code="background_explore",
            availability_intent="explore",
            accepted_frame=intelligence,
        )

        action = decision.decide(intelligence, derived)

        self.assertIsNotNone(action)
        self.assertEqual(action.intent, "smart_loop_candidate")

    def test_bad_rf_capacity_winner_suppresses_fault_band_escape(self):
        decision = self._decision()
        decision.posture_authority.private_state_for_test.state = SystemState.BAD_RF
        decision.posture_authority.private_state_for_test.emergency_class = "BAD_RF"
        decision.posture_authority.private_state_for_test.bad_rf_next_action_at = 0.0
        decision.posture_authority.private_state_for_test.bad_rf_recovery_step = 0
        seed_known_good_bandset(decision.memory, [20])
        decision.deployed_bands = {3, 20}

        modem = make_modem()
        modem.sinr_db = -7.0
        modem.rsrp_dbm = -95.0
        modem.rsrq_db = -13.0
        modem.primary_band = 3
        modem.rx_bytes_per_sec = 4_000_000.0
        modem.ca_active = True
        modem.ca_band_signature = "B3+B20"
        modem.selected_band_set = [1, 3, 20]
        computed = make_computed()
        computed.rtt_sliding_avg = 35.0
        computed.congestion_pct = 20.0
        computed.congestion_pct_valid = True
        computed.rtt_dl_jitter_pct = 1.0
        computed.rtt_dl_jitter_valid = True

        intelligence, derived = cellular_test_inputs(modem, computed)
        action = decision._decide_bad_rf(intelligence, derived)

        self.assertIsNone(action)

    def test_incident_assessment_blocks_explore_before_health_gate(self):
        decision = self._decision()
        decision.note_health_status({"stage": "healthy", "action_hint": "none"})
        decision.note_incident_assessment(
            {
                "incident_class": "post_action_instability",
                "recommended_next_step": "settle_backoff",
                "suppress_exploration": True,
                "self_induced": True,
            }
        )
        intelligence, derived = cellular_test_inputs(make_modem(), make_computed())
        publish_smart_loop_radio_command_for_test(
            decision,
            target_band=3,
            target_bands=(1, 3),
            reason_code="background_explore",
            availability_intent="explore",
            accepted_frame=intelligence,
        )

        action = decision.decide(intelligence, derived)

        self.assertIsNone(action)
        trace = decision.get_last_decision_trace()
        context_obj = decision.get_last_decision_context_object()
        self.assertEqual(trace.get("primary_blocker_gate"), "incident_hold")
        self.assertIn("post_action_instability", trace.get("primary_blocker_reason", ""))
        self.assertIsNotNone(context_obj)
        self.assertEqual(context_obj.incident_class, "post_action_instability")
        self.assertTrue(context_obj.incident_suppress_exploration)


class TestEmergencyActionGuard(unittest.TestCase):
    def test_emergency_guard_honors_zero_window_and_cooldowns(self):
        cfg = Arch4Config(
            min_action_interval_s=0.0,
            emergency_action_window_s=0.0,
            emergency_action_global_cooldown_s=0.0,
            emergency_action_cooldown_s=0.0,
            emergency_action_max_per_window=1,
        )
        td = tempfile.TemporaryDirectory()
        mem = Memory(cfg, memory_dir=td.name)
        sm = TestControllerPostureAuthority(cfg, mem)
        sm.private_state_for_test.state = SystemState.E_RADIO_DOWN
        decision = DecisionEngine(
            cfg,
            mem,
            sm,
            Constraints(),
            aggressiveness=AggressivenessEngine(cfg),
            historical=HistoricalJournal(cfg),
        )
        self.addCleanup(td.cleanup)

        action = LeverAction(lever=LeverType.RESTART, params={}, reason="guard test")
        with patch("arch4.brain.decision_policy.time.time", side_effect=[100.0, 101.0]):
            first = decision._guard_emergency_action(action)
            second = decision._guard_emergency_action(action)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)


if __name__ == "__main__":
    unittest.main()
