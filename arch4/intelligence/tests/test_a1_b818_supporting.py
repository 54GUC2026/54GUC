"""arch4/tests/test_a1_b818_supporting.py
Merged: test_a1_b818_antithrash.py + test_a1_b818_sequences.py
        + test_a1_b818_windows.py + test_hilink_rotate_policy.py
"""
import tempfile
import time
import unittest
from types import SimpleNamespace

from arch4.brain.aggressiveness import AggressivenessEngine
from arch4.brain.decision import DecisionEngine
from arch4.brain.historical import HistoricalJournal
from arch4.brain.memory import Memory
from arch4.brain.memory_band_learning_query_store import marker_scoped_band_learning_key
from arch4.tests.controller_posture_test_support import TestControllerPostureAuthority
from arch4.interfaces import (
    Arch4Config, ComputedSignals, Constraints, LeverAction, LeverType, SystemState,
    select_policy_profile,
)
from arch4.policy.a1_b818_antithrash import (
    _legacy_apply_a1_restricted_band_filter_oracle as apply_a1_restricted_band_filter,
    _legacy_guard_a1_antithrash_oracle as guard_a1_antithrash,
    a1_pick_freeze_bands,
    is_strict_a1_band_action,
)
from arch4.policy.a1_b818_windows import (
    a1_restricted_band_pool,
    filter_a1_restricted_bands,
    hour_in_window,
    is_a1_overnight_profile,
    is_a1_hard_night_freeze_window,
    is_a1_restricted_night_window,
    policy_provider_plmn,
)
from arch4.policy.a1_b818_sequences import (
    _legacy_a1_morning_routine_guard_status_oracle as a1_morning_routine_guard_status,
)
from arch4.policy.hilink_rotate_policy import (
    allow_hilink_priority_rotate,
    hilink_rotation_has_confidence,
    note_hilink_priority_rotate_reverted,
    note_hilink_priority_rotate_stable,
)
from arch4.tests.test_brain import make_modem
from arch4.tests.controller_generation_factory import cellular_test_inputs


# ── A1 antithrash ───────────────────────────────────────────────

class _A1AntithrashLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)


class _A1AntithrashMemory:
    def __init__(self, known=None):
        self._known = known or []

    def get_known_good_bandset(self):
        return list(self._known)


class _A1AntithrashEngine:
    def __init__(self):
        self.cfg = SimpleNamespace(
            a1_night_antithrash_window_s=300.0,
            a1_night_antithrash_max_band_actions=1,
            a1_night_antithrash_hold_s=120.0,
            a1_night_antithrash_strict_max_actions=1,
            a1_night_antithrash_strict_step_wait_s=60.0,
        )
        self.posture_authority = SimpleNamespace(
            state=SystemState.E_RF_COLLAPSE,
            current_posture=SimpleNamespace(time_in_state_s=30.0),
        )
        self._a1_antithrash = {
            "day": "",
            "hold_started": False,
            "hold_until": 0.0,
            "retest_done": False,
            "freeze_bands": [],
            "freeze_applied": False,
            "action_times": [],
            "strict_remaining": 0,
            "strict_last_at": 0.0,
        }
        self.memory = _A1AntithrashMemory([3, 20])
        self._logger = _A1AntithrashLogger()
        self.events = []

    def _is_a1_restricted_night_window(self, now, modem):
        return True

    def _filter_a1_restricted_bands(self, bands, modem, now):
        return [b for b in bands if b in {3, 20}]

    def _a1_restricted_band_pool(self, modem, now):
        return [3, 20]

    def _active_bands(self, modem):
        return [1, 3, 20]

    def _emit_runtime_event(self, kind, **payload):
        self.events.append({"kind": kind, **payload})

    def _decision_policy_log(self):
        return self._logger

    def _make_band_action(self, **kwargs):
        return LeverAction(
            lever=LeverType.BAND_SELECTION,
            params=dict(kwargs["params"], bands=kwargs["bands"]),
            reason=kwargs["reason"],
            intent=kwargs["intent"],
            objective_id=kwargs["objective_id"],
        )

    def _objective_id_for_bands(self, prefix, bands):
        return f"{prefix}:{'+'.join(str(b) for b in bands)}"

    def _is_strict_a1_band_action(self, action):
        return is_strict_a1_band_action(action)

    def _a1_pick_freeze_bands(self, modem, now):
        return a1_pick_freeze_bands(self, modem, now)


class TestA1B818Antithrash(unittest.TestCase):
    def test_apply_restricted_band_filter(self):
        engine = _A1AntithrashEngine()
        action = LeverAction(
            lever=LeverType.BAND_SELECTION,
            params={"bands": [1, 3, 7, 20], "sequential": True},
            reason="test",
        )

        out = apply_a1_restricted_band_filter(engine, action, modem=object(), now=time.time())

        self.assertIsNotNone(out)
        self.assertEqual(out.params["bands"], [3, 20])
        self.assertIn("B3/B20 only", out.reason)

    def test_restricted_band_filter_blocks_tsar_ca_pool_target_mutation(self):
        engine = _A1AntithrashEngine()
        action = LeverAction(
            lever=LeverType.BAND_SELECTION,
            params={
                "bands": [1, 20, 28],
                "sequential": True,
                "_ca_pool_reduction": True,
                "_tsar_candidate_action": "reduce_ca_pool",
                "_tsar_target_pool_owner": "arch4.smart_loop.closed_loop",
            },
            reason="TSAR candidate reduce CA pool",
        )

        out = apply_a1_restricted_band_filter(engine, action, modem=object(), now=time.time())

        self.assertIsNone(out)
        self.assertEqual(
            engine.events[-1]["kind"],
            "a1_restricted_filter_blocked_tsar_ca_pool_mutation",
        )
        self.assertEqual(engine.events[-1]["selected_bands"], [1, 20, 28])
        self.assertEqual(engine.events[-1]["restricted_bands"], [20])

    def test_restricted_band_filter_blocks_smart_loop_pband_target_outside_night_pool(self):
        engine = _A1AntithrashEngine()
        action = LeverAction(
            lever=LeverType.PBAND,
            params={
                "pband": 3,
                "ca_pool": [1, 3, 20],
                "_selected_action_command": True,
            },
            reason="SmartLoop full-radio target",
            intent="smart_loop_candidate",
        )

        out = apply_a1_restricted_band_filter(engine, action, modem=object(), now=time.time())

        self.assertIsNone(out)
        self.assertEqual(
            engine.events[-1]["kind"],
            "a1_restricted_filter_blocked_tsar_ca_pool_mutation",
        )
        self.assertEqual(engine.events[-1]["selected_bands"], [1, 3, 20])
        self.assertEqual(engine.events[-1]["restricted_bands"], [3, 20])

    def test_restricted_band_filter_allows_smart_loop_pband_inside_night_pool(self):
        engine = _A1AntithrashEngine()
        action = LeverAction(
            lever=LeverType.PBAND,
            params={
                "pband": 3,
                "ca_pool": [3, 20],
                "_selected_action_command": True,
                "_selected_action_owner_class": "optimization",
            },
            reason="SmartLoop restricted-night target",
            intent="smart_loop_candidate",
        )

        out = apply_a1_restricted_band_filter(engine, action, modem=object(), now=time.time())

        self.assertIs(out, action)

    def test_pick_freeze_bands_prefers_known_good(self):
        engine = _A1AntithrashEngine()
        out = a1_pick_freeze_bands(engine, modem=object(), now=time.time())
        self.assertEqual(out, [3, 20])

    def test_antithrash_hold_and_freeze_behavior(self):
        engine = _A1AntithrashEngine()
        action = LeverAction(
            lever=LeverType.BAND_SELECTION,
            params={"bands": [3, 20], "sequential": True},
            reason="churn test",
        )
        base = time.mktime((2026, 2, 28, 2, 10, 0, 5, 59, -1))

        first = guard_a1_antithrash(engine, action, modem=object(), now=base)
        second = guard_a1_antithrash(engine, action, modem=object(), now=base + 10)
        third = guard_a1_antithrash(engine, action, modem=object(), now=base + 20)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(third)
        self.assertGreater(engine._a1_antithrash["hold_until"], base + 10)
        kinds = {e["kind"] for e in engine.events}
        self.assertIn("a1_antithrash_hold_started", kinds)
        self.assertTrue(engine._logger.warnings)

    def test_antithrash_honors_short_hold_and_strict_step_wait(self):
        engine = _A1AntithrashEngine()
        engine.cfg.a1_night_antithrash_window_s = 10.0
        engine.cfg.a1_night_antithrash_hold_s = 10.0
        engine.cfg.a1_night_antithrash_strict_max_actions = 2
        engine.cfg.a1_night_antithrash_strict_step_wait_s = 1.0
        action = LeverAction(
            lever=LeverType.BAND_SELECTION,
            params={"bands": [3, 20], "sequential": True},
            reason="churn test",
        )
        strict = LeverAction(
            lever=LeverType.BAND_SELECTION,
            params={"bands": [3], "forced_sequence": True},
            reason="Morning sequence step 1",
        )
        base = time.mktime((2026, 2, 28, 2, 10, 0, 5, 59, -1))

        first = guard_a1_antithrash(engine, action, modem=object(), now=base)
        second = guard_a1_antithrash(engine, action, modem=object(), now=base + 2.0)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertAlmostEqual(engine._a1_antithrash["hold_until"], base + 12.0, delta=0.1)

        engine._a1_antithrash["strict_remaining"] = 2
        engine._a1_antithrash["strict_last_at"] = base + 2.0
        blocked = guard_a1_antithrash(engine, strict, modem=object(), now=base + 2.5)
        allowed = guard_a1_antithrash(engine, strict, modem=object(), now=base + 3.5)

        self.assertIsNone(blocked)
        self.assertIs(allowed, strict)

    def test_antithrash_counts_and_rejects_smart_loop_pband_without_substitution(self):
        engine = _A1AntithrashEngine()
        action = LeverAction(
            lever=LeverType.PBAND,
            params={
                "pband": 3,
                "ca_pool": [3, 20],
                "_selected_action_command": True,
                "_selected_action_owner_class": "optimization",
            },
            reason="SmartLoop full-radio target",
            intent="smart_loop_candidate",
        )
        base = time.mktime((2026, 2, 28, 2, 10, 0, 5, 59, -1))

        first = guard_a1_antithrash(engine, action, modem=object(), now=base)
        second = guard_a1_antithrash(engine, action, modem=object(), now=base + 10)

        self.assertIs(first, action)
        self.assertIsNone(second)
        self.assertTrue(engine._a1_antithrash["hold_started"])
        self.assertFalse(engine._a1_antithrash["freeze_applied"])
        self.assertIn("a1_antithrash_hold_started", {event["kind"] for event in engine.events})


# ── A1 sequences ────────────────────────────────────────────────

class TestA1B818Sequences(unittest.TestCase):
    def test_forced_schedule_helper_is_retired_noop(self):
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
            modem = make_modem(ca_active=True, ca_signature="B3+CA", sinr_db=3.0, rsrp_dbm=-95.0)
            base = time.mktime((2026, 2, 27, 0, 10, 0, 4, 58, -1))

            with self.assertRaisesRegex(
                RuntimeError,
                "old_provider_overnight_action_owner_deleted",
            ):
                decision._decide_a1_overnight_recovery(modem, base)

    def test_morning_sequence_helper_via_engine_wrapper(self):
        cfg = Arch4Config(
            min_action_interval_s=0.0,
            a1_morning_sequence_hour=6,
            a1_morning_sequence_minute=1,
            a1_morning_step_wait_s=5.0,
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
            modem = make_modem(primary_band=3, radio_connected=True)
            now = time.mktime((2026, 3, 9, 6, 1, 0, 0, 0, -1))

            with self.assertRaisesRegex(
                RuntimeError,
                "old_provider_morning_action_owner_deleted",
            ):
                decision._decide_a1_b818_morning_sequence(modem, now)

    def test_morning_routine_guard_spans_before_and_after_sequence(self):
        cfg = Arch4Config(
            a1_morning_sequence_hour=6,
            a1_morning_sequence_minute=1,
            a1_morning_step_wait_s=20.0,
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
            before = time.mktime((2026, 3, 9, 5, 50, 0, 0, 0, -1))
            after_sequence = time.mktime((2026, 3, 9, 6, 3, 0, 0, 0, -1))
            outside = time.mktime((2026, 3, 9, 6, 18, 0, 0, 0, -1))

            self.assertTrue(a1_morning_routine_guard_status(decision, before)["active"])
            self.assertTrue(a1_morning_routine_guard_status(decision, after_sequence)["active"])
            self.assertFalse(a1_morning_routine_guard_status(decision, outside)["active"])

    def test_morning_sequence_honors_short_step_wait(self):
        cfg = Arch4Config(
            min_action_interval_s=0.0,
            a1_morning_sequence_hour=6,
            a1_morning_sequence_minute=1,
            a1_morning_step_wait_s=1.0,
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
            modem = make_modem(primary_band=3, radio_connected=True)
            now = time.mktime((2026, 3, 9, 6, 1, 0, 0, 0, -1))

            with self.assertRaisesRegex(
                RuntimeError,
                "old_provider_morning_action_owner_deleted",
            ):
                decision._decide_a1_b818_morning_sequence(modem, now)

    def test_morning_sequence_skips_when_b3_learning_guard_blocks(self):
        cfg = Arch4Config(
            min_action_interval_s=0.0,
            a1_morning_sequence_hour=6,
            a1_morning_sequence_minute=1,
            learning_enabled=True,
            learning_stm_min_attempts=1,
        )
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(cfg, memory_dir=td)
            now = time.mktime((2026, 3, 9, 6, 1, 0, 0, 0, -1))
            hour = time.localtime(now).tm_hour
            key = marker_scoped_band_learning_key(mem, mem.current_calibration_marker, hour, [3])
            mem.band_learning_stm[key] = {
                "attempts": 2.0,
                "good": 0.0,
                "bad": 2.0,
                "hard_fail": 1.0,
                "last_ts": now,
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
                policy_profile=select_policy_profile("22005", "b818"),
            )
            modem, _ = cellular_test_inputs(
                make_modem(
                    primary_band=20,
                    radio_connected=True,
                    sinr_db=3.0,
                ),
                ComputedSignals(),
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "old_provider_morning_action_owner_deleted",
            ):
                decision._decide_a1_b818_morning_sequence(modem, now)

    def test_morning_sequence_aborts_mid_run_when_b3_becomes_blocked(self):
        cfg = Arch4Config(
            min_action_interval_s=0.0,
            a1_morning_sequence_hour=6,
            a1_morning_sequence_minute=1,
            a1_morning_step_wait_s=5.0,
            learning_enabled=True,
            learning_stm_min_attempts=1,
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
            modem, _ = cellular_test_inputs(
                make_modem(
                    primary_band=20,
                    radio_connected=True,
                    sinr_db=3.0,
                ),
                ComputedSignals(),
            )
            now = time.mktime((2026, 3, 9, 6, 1, 0, 0, 0, -1))

            hour = time.localtime(now).tm_hour
            key = marker_scoped_band_learning_key(mem, mem.current_calibration_marker, hour, [3])
            mem.band_learning_stm[key] = {
                "attempts": 2.0,
                "good": 0.0,
                "bad": 2.0,
                "hard_fail": 1.0,
                "last_ts": now,
                "last_sinr": 5.0,
            }
            mem.current_context_seen_actions = cfg.learning_marker_warmup_attempts

            with self.assertRaisesRegex(
                RuntimeError,
                "old_provider_morning_action_owner_deleted",
            ):
                decision._decide_a1_b818_morning_sequence(modem, now)


# ── A1 windows ──────────────────────────────────────────────────

class _A1WindowsEngine:
    def __init__(self):
        self.policy_profile = SimpleNamespace(
            modem_backend="b818",
            a1_overnight_recovery=True,
            provider_plmn="22005",
        )
        self.cfg = SimpleNamespace(
            a1_night_restrict_start_hour=0,
            a1_night_restrict_end_hour=8,
            night_probe_trigger_bad_rf_sinr_db=4.0,
            night_probe_trigger_bad_rsrp_dbm=-95.0,
            a1_night_freeze_enabled=True,
            a1_night_freeze_start_hour=2,
            a1_night_freeze_end_hour=8,
        )
        self.deployed_bands = {1, 3, 20}
        self.posture_authority = SimpleNamespace(
            state=SystemState.WORKING,
            current_posture=SimpleNamespace(time_in_state_s=0.0),
        )


class _A1WindowsModem:
    def __init__(self, radio_connected=True, sinr_db=10.0, rsrp_dbm=-80.0, primary_band=1):
        self.cellular = SimpleNamespace(
            radio_connected=radio_connected,
            sinr_db=sinr_db,
            rsrp_dbm=rsrp_dbm,
            primary_band=primary_band,
        )


class TestA1B818Windows(unittest.TestCase):
    def test_profile_and_provider_plmn(self):
        engine = _A1WindowsEngine()
        self.assertTrue(is_a1_overnight_profile(engine))
        self.assertEqual(policy_provider_plmn(engine), "22005")

    def test_hour_in_window_handles_wrap(self):
        self.assertTrue(hour_in_window(2, 2, 8))
        self.assertFalse(hour_in_window(14, 2, 8))
        self.assertTrue(hour_in_window(23, 22, 6))
        self.assertTrue(hour_in_window(2, 22, 6))
        self.assertFalse(hour_in_window(12, 22, 6))

    def test_restricted_window_is_unconditional_inside_a1_window(self):
        engine = _A1WindowsEngine()
        modem = _A1WindowsModem(sinr_db=10.0, rsrp_dbm=-80.0)
        now = time.mktime((2026, 3, 9, 1, 0, 0, 0, 0, -1))
        self.assertTrue(is_a1_restricted_night_window(engine, now=now, modem=modem))

        day_now = time.mktime((2026, 3, 9, 14, 0, 0, 0, 0, -1))
        self.assertFalse(is_a1_restricted_night_window(engine, now=day_now, modem=modem))

    def test_hard_freeze_window_detection_is_retired(self):
        engine = _A1WindowsEngine()
        freeze_now = time.mktime((2026, 3, 9, 3, 56, 0, 0, 0, -1))
        day_now = time.mktime((2026, 3, 9, 13, 0, 0, 0, 0, -1))
        self.assertFalse(is_a1_hard_night_freeze_window(engine, freeze_now))
        self.assertFalse(is_a1_hard_night_freeze_window(engine, day_now))

    def test_restricted_band_pool_and_filter(self):
        engine = _A1WindowsEngine()
        modem = _A1WindowsModem(sinr_db=3.0, rsrp_dbm=-96.0, primary_band=1)
        now = time.mktime((2026, 3, 9, 1, 0, 0, 0, 0, -1))
        pool = a1_restricted_band_pool(engine, modem, now)
        self.assertEqual(pool, [3, 20])
        filtered = filter_a1_restricted_bands(engine, [1, 3, 7, 20], modem, now)
        self.assertEqual(filtered, [3, 20])


# ── HiLink rotate ────────────────────────────────────────────────

class _HiLinkRotateBandKnowledge:
    def __init__(self, times_appeared_in_ca=0, avg_throughput_with=0.0):
        self.times_appeared_in_ca = times_appeared_in_ca
        self.avg_throughput_with = avg_throughput_with


class _HiLinkRotateMemory:
    def __init__(self, known=None, bands=None):
        self._known = known or []
        self.bands = bands or {}

    def get_known_good_bandset(self):
        return list(self._known)


class _HiLinkRotateEngine:
    def __init__(self):
        from arch4.brain.cooldown_registry import CooldownRegistry
        self.cfg = SimpleNamespace(
            hilink_priority_rotate_success_quiet_s=900.0,
            hilink_priority_rotate_fail_cooldown_s=1200.0,
        )
        self._thresholds = {"soft": {}}
        self.cooldowns = CooldownRegistry()
        self._hilink_rotate_success_quiet_until = 0.0
        self.memory = _HiLinkRotateMemory()
        self.events = []

    def _get_hilink_rotate_success_quiet_until(self):
        return self._hilink_rotate_success_quiet_until

    def _set_hilink_rotate_success_quiet_until(self, until):
        self._hilink_rotate_success_quiet_until = float(until or 0.0)

    def _emit_runtime_event(self, kind, **payload):
        self.events.append({"kind": kind, **payload})


class TestHiLinkRotatePolicy(unittest.TestCase):
    def test_gate_blocks_during_quiet_window(self):
        engine = _HiLinkRotateEngine()
        engine._hilink_rotate_success_quiet_until = 1500.0

        self.assertFalse(allow_hilink_priority_rotate(engine, now=1000.0))

    def test_confidence_ignores_legacy_known_good_without_band_history(self):
        engine = _HiLinkRotateEngine()
        engine.memory = _HiLinkRotateMemory(known=[3, 20])

        self.assertFalse(hilink_rotation_has_confidence(engine, 3))

    def test_confidence_accepts_learned_band_history(self):
        engine = _HiLinkRotateEngine()
        engine.memory = _HiLinkRotateMemory(
            bands={1: _HiLinkRotateBandKnowledge(times_appeared_in_ca=2, avg_throughput_with=1.5)}
        )

        self.assertTrue(hilink_rotation_has_confidence(engine, 1))

    def test_stable_result_sets_quiet_window(self):
        engine = _HiLinkRotateEngine()

        note_hilink_priority_rotate_stable(engine, target_band=3, now=1000.0)

        self.assertEqual(engine._hilink_rotate_success_quiet_until, 1900.0)
        self.assertEqual(engine.events[0]["kind"], "hilink_priority_rotate_stable_hold")

    def test_stable_result_honors_short_quiet_window(self):
        engine = _HiLinkRotateEngine()
        engine.cfg.hilink_priority_rotate_success_quiet_s = 30.0

        note_hilink_priority_rotate_stable(engine, target_band=3, now=1000.0)

        self.assertEqual(engine._hilink_rotate_success_quiet_until, 1030.0)
        self.assertEqual(engine.events[0]["quiet_s"], 30)

    def test_reverted_result_sets_cooldown(self):
        engine = _HiLinkRotateEngine()

        note_hilink_priority_rotate_reverted(
            engine,
            target_band=3,
            current_band=1,
            now=1000.0,
        )

        self.assertEqual(engine.cooldowns.get_until("hilink_rotate"), 2200.0)
        self.assertEqual(engine.events[0]["kind"], "hilink_priority_rotate_reverted")

    def test_reverted_result_honors_short_cooldown(self):
        engine = _HiLinkRotateEngine()
        engine.cfg.hilink_priority_rotate_fail_cooldown_s = 45.0

        note_hilink_priority_rotate_reverted(
            engine,
            target_band=3,
            current_band=1,
            now=1000.0,
        )

        self.assertEqual(engine.cooldowns.get_until("hilink_rotate"), 1045.0)
        self.assertEqual(engine.events[0]["cooldown_s"], 45)



if __name__ == "__main__":
    import unittest
    unittest.main()
