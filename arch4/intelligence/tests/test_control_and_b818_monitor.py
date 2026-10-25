import json
import tempfile
import time
import unittest
from pathlib import Path

from arch4.brain.device_monitors import (
    create_device_monitor,
    NoopDeviceMonitor,
    MikroTikDeviceMonitor,
)
from arch4.control.manual_queue import (
    CalibrationControlQueue,
    build_calibration_queue_payload,
)
from arch4.interfaces import ComputedSignals, LeverAction, LeverType, ModemInput
from arch4.b818.supervisor import HuaweiHiLinkSupervisor
from arch4.ops_monitors import collect_ops_monitor_status
from arch4.status_payload import build_status_payload
from arch4.tests.controller_generation_factory import (
    materialize_cellular_intelligence,
    materialize_controller_generation,
)


def _accepted_status_input(modem, computed, base_status):
    return materialize_controller_generation(
        modem,
        computed,
        owner_values=base_status,
    )


def _calibration_line(command: str) -> str:
    return json.dumps(build_calibration_queue_payload(command, None))


class TestCalibrationControlQueue(unittest.TestCase):
    def test_calibration_queue_reads_commands_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "calibration_queue.jsonl"
            q = CalibrationControlQueue(p, start_at_eof=False)
            p.write_text(
                "not-json\n"
                + _calibration_line("bootstrap")
                + "\n"
                + _calibration_line("compare")
                + "\n",
                encoding="utf-8",
            )
            first = q.read_command()
            second = q.read_command()
            self.assertEqual(first, "bootstrap")
            self.assertEqual(second, "compare")

class TestHuaweiHiLinkSupervisorMonitor(unittest.TestCase):
    def test_monitor_recommends_restart_but_emits_no_action(self):
        sup = HuaweiHiLinkSupervisor()
        sup._last_ok_ts = time.time() - 11.0
        sup.observe(None, {})
        events = sup.drain_runtime_events()
        kinds = {e.get("kind") for e in events}
        self.assertIn("b818_monitor_recommend_restart", kinds)
        self.assertTrue(all(e.get("backend") == "b818" for e in events))

    def test_huawei_family_monitor_uses_generic_event_prefix_and_disables_morning_sequence(self):
        sup = HuaweiHiLinkSupervisor(backend="b535", enable_morning_sequence=False)
        sup._last_ok_ts = time.time() - 11.0
        sup.observe(None, {})
        events = sup.drain_runtime_events()
        kinds = {e.get("kind") for e in events}
        self.assertIn("huawei_monitor_recommend_restart", kinds)
        self.assertNotIn("huawei_monitor_recommend_morning_sequence", kinds)
        self.assertTrue(all(e.get("backend") == "b535" for e in events))

    def test_note_action_result_updates_cooldown(self):
        sup = HuaweiHiLinkSupervisor()
        now = time.time()
        action = LeverAction(lever=LeverType.UE_CAPABILITY, params={"restart": True}, reason="test")
        sup.note_action_result(action, success=True, now=now)
        self.assertGreaterEqual(sup._reboot_cooldown_until, now + 119.0)

    def test_restart_recommendation_identity_is_stable_until_cleared(self):
        sup = HuaweiHiLinkSupervisor()
        sup._last_ok_ts = time.time() - 11.0

        sup.observe(None, {})
        first_id = sup.get_runtime_status()["restart_recommendation_id"]
        sup.observe(None, {})
        repeated_id = sup.get_runtime_status()["restart_recommendation_id"]

        self.assertTrue(first_id)
        self.assertEqual(repeated_id, first_id)

        sup.note_modem_ok(time.time())
        self.assertEqual(
            sup.get_runtime_status()["restart_recommendation_id"],
            "",
        )
        sup._last_ok_ts = time.time() - 11.0
        sup.observe(None, {})
        next_id = sup.get_runtime_status()["restart_recommendation_id"]
        self.assertTrue(next_id)
        self.assertNotEqual(next_id, first_id)


class TestOpsMonitors(unittest.TestCase):
    def test_collect_ops_monitor_status_handles_missing_files(self):
        with tempfile.TemporaryDirectory() as td:
            out = collect_ops_monitor_status(td)
            self.assertIn("guardian", out)
            self.assertIn("night_watch", out)
            self.assertFalse(out["guardian"]["running"])


class TestDeviceMonitorFactory(unittest.TestCase):
    def test_factory_has_backend_coverage(self):
        b818 = create_device_monitor("b818")
        self.assertIsNotNone(b818)
        b535 = create_device_monitor("b535")
        self.assertIsNotNone(b535)
        mt = create_device_monitor("mikrotik")
        self.assertIsInstance(mt, MikroTikDeviceMonitor)
        ow = create_device_monitor("openwrt")
        self.assertIsInstance(ow, NoopDeviceMonitor)
        zte = create_device_monitor("mf296r")
        self.assertIsInstance(zte, NoopDeviceMonitor)
        zte_ng = create_device_monitor("zte_zwrt")
        self.assertIsInstance(zte_ng, NoopDeviceMonitor)

    def test_mikrotik_monitor_holds_routeros_recovery_on_persistent_down(self):
        mon = MikroTikDeviceMonitor(
            "mikrotik",
            restart_recommend_after_s=5.0,
            restart_cooldown_s=60.0,
        )
        modem, _ = materialize_cellular_intelligence(
            ModemInput.safe_defaults(radio_connected=False),
            ComputedSignals(),
        )
        mon.observe(modem, {})
        mon._last_ok_ts = time.time() - 6.0
        mon.observe(modem, {})
        events = mon.drain_runtime_events()
        kinds = {e.get("kind") for e in events}
        self.assertIn("mikrotik_monitor_link_down", kinds)
        self.assertIn("mikrotik_monitor_hold_routeros_recovery", kinds)
        st = mon.get_runtime_status()
        self.assertFalse(st["restart_recommend_active"])
        self.assertTrue(st["routeros_recovery_hold_active"])
        self.assertTrue(st["emergency_active"])
        self.assertFalse(st["reboot_pending"])
        self.assertIn("reboot_cooldown_active", st)
        self.assertIn("morning_sequence_recommend_active", st)

    def test_mikrotik_monitor_restart_result_is_observed_only(self):
        mon = MikroTikDeviceMonitor(
            "mikrotik",
            restart_recommend_after_s=5.0,
            restart_cooldown_s=120.0,
        )
        now = time.time()
        action = LeverAction(lever=LeverType.RESTART, params={}, reason="test")
        mon.note_action_result(action, success=True, now=now)
        st = mon.get_runtime_status()
        self.assertTrue(st["restart_cooldown_active"])
        self.assertTrue(st["reboot_cooldown_active"])
        self.assertFalse(st["restart_recommend_active"])
        events = mon.drain_runtime_events()
        kinds = {e.get("kind") for e in events}
        self.assertIn("mikrotik_monitor_restart_observed", kinds)

    def test_mikrotik_monitor_honors_short_hold_threshold(self):
        mon = MikroTikDeviceMonitor(
            "mikrotik",
            restart_recommend_after_s=1.0,
            restart_cooldown_s=10.0,
        )
        modem, _ = materialize_cellular_intelligence(
            ModemInput.safe_defaults(radio_connected=False),
            ComputedSignals(),
        )
        mon.observe(modem, {})
        mon._last_ok_ts = time.time() - 1.5
        mon.observe(modem, {})
        events = mon.drain_runtime_events()
        kinds = {e.get("kind") for e in events}
        self.assertIn("mikrotik_monitor_hold_routeros_recovery", kinds)

    def test_mikrotik_monitor_allows_zero_restart_cooldown(self):
        mon = MikroTikDeviceMonitor(
            "mikrotik",
            restart_recommend_after_s=1.0,
            restart_cooldown_s=0.0,
        )
        now = time.time()
        action = LeverAction(lever=LeverType.RESTART, params={}, reason="test")
        mon.note_action_result(action, success=True, now=now)
        st = mon.get_runtime_status()
        self.assertFalse(st["restart_cooldown_active"])
        self.assertFalse(st["reboot_cooldown_active"])


class TestStatusPayload(unittest.TestCase):
    def test_status_payload_includes_core_keys(self):
        modem = ModemInput.safe_defaults()
        modem.radio_connected = True
        modem.provider_name = "A1"
        modem.primary_band = 3
        modem.ca_active = False
        modem.ca_band_signature = "B3"
        modem.pci = 123
        modem.earfcn = 1300
        modem.enb_id = 77
        modem.sinr_db = 8.0
        modem.rsrp_dbm = -92.0
        modem.rsrq_db = -9.5
        modem.rssi_dbm = -67.0
        modem.mcs_dl = 10
        modem.mcs_ul = 8
        modem.cqi = 7
        modem.qam_dl_primary = 6
        modem.qam_dl_secondary = 4
        modem.qam_ul = 4
        modem.qam_source = "inferred"
        modem.qam_confidence = 0.7
        modem.rx_bytes_per_sec = 1_000_000
        modem.tx_bytes_per_sec = 500_000
        modem.carriers = []
        modem.neighbors = []

        computed = ComputedSignals()
        computed.rtt_ms = 35.0
        computed.rtt_sliding_avg = 40.0
        computed.cq_cell = 0.6

        base_status = {"operator": "A1"}
        out = build_status_payload(
            base_status=base_status,
            accepted=_accepted_status_input(modem, computed, base_status),
            constraints=None,
            state_name="OPTIMIZING",
            decision=None,
            historical=None,
            memory=None,
            ops_monitors={"guardian": {"running": True}},
        )
        self.assertEqual(out["provider_name"], "A1")
        self.assertEqual(out["state"], "WORKING")
        self.assertEqual(out["qam_dl_primary"], 6)
        self.assertEqual(out["qam_source"], "inferred")
        self.assertIn("mcs_ul_supported", out)
        self.assertIn("carrier_rows", out)
        self.assertIn("neighbors", out)
        self.assertIn("ops_monitors", out)

    def test_status_payload_source_precedence_is_stable(self):
        modem = ModemInput.safe_defaults()
        modem.radio_connected = True
        modem.primary_band = 20
        modem.ca_active = True
        modem.ca_band_signature = "B3+B20"
        modem.pci = 321
        modem.earfcn = 6400
        modem.enb_id = 999
        modem.sinr_db = 6.5
        modem.rsrp_dbm = -101.0
        modem.rsrq_db = -12.0
        modem.rssi_dbm = -75.0
        modem.mcs_dl = 7
        modem.mcs_ul = 5
        modem.mcs_ul_supported = False
        modem.cqi = 6
        modem.qam_dl_primary = 6
        modem.qam_dl_secondary = 0
        modem.qam_ul = 0
        modem.qam_source = "inferred"
        modem.qam_confidence = 0.6
        modem.rx_bytes_per_sec = 2_000_000
        modem.tx_bytes_per_sec = 500_000
        modem.carriers = []
        modem.neighbors = []

        computed = ComputedSignals()
        computed.rtt_ms = 52.0
        computed.rtt_sliding_avg = 60.0
        computed.cq_cell = 0.42

        modem.provider_name = "MODEM_PROVIDER"
        modem.provider_plmn = "22005"
        modem.network_type = "LTE"
        modem.dl_bandwidth = "20MHz"
        modem.ul_bandwidth = "10MHz"

        base_status = {
            "provider_name": "BASE_PROVIDER",
            "provider_plmn": "99999",
            "mcs_ul_supported": False,
            # Deliberately wrong values for modem/computed-owned fields:
            "band": 1,
            "sinr_db": -3.0,
            "rtt_ms": 999.0,
            # Canonical configured-pool owner keys should pass through unchanged:
            "configured_band_pool": [3, 20],
            "configured_band_pool_signature": "B3+B20",
        }
        constraints = type("K", (), {"provider_name": "CONSTRAINT_PROVIDER", "provider_plmn": "22005"})()

        out = build_status_payload(
            base_status=base_status,
            accepted=_accepted_status_input(modem, computed, base_status),
            constraints=constraints,
            state_name="DEGRADED",
            decision=None,
            historical=None,
            memory=None,
            ops_monitors=None,
        )

        # The accepted same-generation Modem Live identity publication wins
        # over uncoordinated raw-object and constraint copies.
        self.assertEqual(out["provider_name"], "BASE_PROVIDER")
        self.assertEqual(out["provider"], "BASE_PROVIDER")
        self.assertEqual(out["provider_plmn"], "99999")
        self.assertEqual(out["plmn"], "99999")

        # Modem/computed-owned fields must override base_status.
        self.assertEqual(out["band"], 20)
        self.assertAlmostEqual(out["sinr_db"], 6.5)
        self.assertAlmostEqual(out["rtt_ms"], 52.0)
        self.assertEqual(out["state"], "DEGRADED")
        self.assertFalse(out["mcs_ul_supported"])
        self.assertEqual(out["qam_dl_primary"], 6)

        # GUI-specific configured-pool passthrough fields must remain available.
        self.assertEqual(out["configured_band_pool_signature"], "B3+B20")
        self.assertEqual(out["network_type"], "LTE")
        self.assertEqual(out["dl_bandwidth"], "20MHz")
        self.assertEqual(out["ul_bandwidth"], "10MHz")

    def test_b535333_profile_suppresses_unsupported_qam_export(self):
        modem = ModemInput.safe_defaults()
        modem.radio_connected = True
        modem.primary_band = 3
        modem.ca_active = True
        modem.ca_band_signature = "B1+B3"
        modem.pci = 255
        modem.earfcn = 1650
        modem.enb_id = 77
        modem.sinr_db = 8.0
        modem.rsrp_dbm = -92.0
        modem.rsrq_db = -9.5
        modem.rssi_dbm = -67.0
        modem.mcs_dl = 0
        modem.mcs_ul = 0
        modem.mcs_dl0 = 0
        modem.mcs_dl1 = 0
        modem.cqi = 0
        modem.ri = 1
        modem.qam_dl_primary = 2
        modem.qam_dl_secondary = 2
        modem.qam_ul = 2
        modem.qam_source = "inferred"
        modem.qam_confidence = 0.7
        modem.rx_bytes_per_sec = 0.0
        modem.tx_bytes_per_sec = 0.0
        modem.carriers = []
        modem.neighbors = [type("N", (), {"pci": 447, "band": 3, "rsrp_dbm": -999.0, "rsrq_db": -999.0})()]
        modem.vendor = "Huawei"
        modem.model = "B535-333"
        modem.firmware = "11.0.5.51(H470SP6C9831)"

        computed = ComputedSignals()
        computed.rtt_ms = 35.0
        computed.rtt_sliding_avg = 40.0
        computed.cq_cell = 0.6

        base_status = {
            "backend": "b535",
            "vendor": "Huawei",
            "model": "B535-333",
            "firmware": "11.0.5.51(H470SP6C9831)",
            "qam_dl0": "QPSK",
            "qam_dl1": "QPSK",
        }
        out = build_status_payload(
            base_status=base_status,
            accepted=_accepted_status_input(modem, computed, base_status),
            constraints=None,
            state_name="WORKING",
            decision=None,
            historical=None,
            memory=None,
            ops_monitors=None,
        )

        self.assertEqual(out["qam_source"], "unknown")
        self.assertEqual(out["qam_confidence"], 0.0)
        self.assertEqual(out["qam_dl_primary"], 0)
        self.assertEqual(out["qam_dl_secondary"], 0)
        self.assertEqual(out["qam_ul"], "")
        self.assertEqual(out.get("qam_dl0"), "")
        self.assertEqual(out.get("qam_dl1"), "")


if __name__ == "__main__":
    unittest.main()
