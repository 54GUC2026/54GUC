"""arch4/tests/test_incident.py
Merged incident-tracker and incident-recovery coverage.
"""
import tempfile
import time
import unittest
from types import SimpleNamespace

from arch4.brain.aggressiveness import AggressivenessEngine
from arch4.brain.decision import DecisionEngine as _ProductionDecisionEngine
from arch4.brain.feed_registers import BrainFeedRegisterBank
from arch4.brain.historical import HistoricalJournal
from arch4.brain.memory_action_history_owner import record_action_history
from arch4.brain.runtime_incident_tracker import RuntimeIncidentTracker
from arch4.brain.memory import Memory
from arch4.tests.controller_posture_test_support import TestControllerPostureAuthority as _ProductionStateMachine
from arch4.interfaces import (
    ActionRecord,
    Arch4Config, CarrierInfo, ComputedSignals, Constraints,
    LeverAction, LeverType, ModemInput, SystemState,
)
from arch4.policy.incident_action_policy import should_hold_action_for_incident
from arch4.tests.smart_loop_test_helpers import publish_smart_loop_radio_command_for_test
from arch4.tests.controller_generation_factory import cellular_test_inputs
from arch4.tests.decision_owner_test_support import with_decision_owner_ports


def _decide(decision, modem, computed):
    intelligence, derived = cellular_test_inputs(modem, computed)
    return decision.decide(intelligence, derived)


def _publish_smart_loop_for_inputs(decision, modem, computed, **publication):
    intelligence, derived = cellular_test_inputs(modem, computed)
    publish_smart_loop_radio_command_for_test(
        decision,
        accepted_frame=intelligence,
        **publication,
    )
    return intelligence, derived


def _assess(assessor, *, modem, computed, **kwargs):
    intelligence, derived = cellular_test_inputs(modem, computed)
    return assessor.assess(
        modem=intelligence,
        computed=derived,
        **kwargs,
    )


class TestControllerPostureAuthority(_ProductionStateMachine):
    """Explicit raw-fixture adapter for direct TestControllerPostureAuthority unit tests."""

    def _rf_collapse_entry_ready(self, modem, computed):
        intelligence, derived = cellular_test_inputs(modem, computed)
        return super()._rf_collapse_entry_ready(intelligence, derived)


class DecisionEngine(_ProductionDecisionEngine):
    """Explicit raw-fixture adapter for direct decision-helper unit tests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **with_decision_owner_ports(kwargs))

    def _apply_action_availability(self, *args, **kwargs):
        typed = dict(kwargs)
        modem = typed.get("modem")
        if isinstance(modem, ModemInput):
            typed["modem"], _ = cellular_test_inputs(modem, ComputedSignals())
        return super()._apply_action_availability(*args, **typed)


# ── Incident assessor ───────────────────────────────────────────

def _IncidentAssessor_make_modem(
    *,
    radio_connected: bool = True,
    ca_active: bool = True,
    ca_signature: str = "B1+B3+B20",
    selected_band_set=(1, 3, 20),
    sinr_db: float = 15.0,
    rsrp_dbm: float = -85.0,
    rsrq_db: float = -9.0,
    rx_bytes_per_sec: float = 120_000.0,
    tx_bytes_per_sec: float = 10_000.0,
    traffic_valid: bool = True,
    packet_session_connected=None,
    connection_status_code: str = "",
) -> ModemInput:
    return ModemInput(
        sinr_db=sinr_db,
        rsrp_dbm=rsrp_dbm,
        rsrq_db=rsrq_db,
        rssi_dbm=-60.0,
        mcs_dl=12,
        mcs_ul=0,
        cqi=9,
        ri=2,
        carriers=[CarrierInfo(3, 1300, 123, 20.0, sinr_db, rsrp_dbm, 1, True)],
        ca_active=ca_active,
        ca_band_signature=ca_signature,
        primary_band=3,
        pci=123,
        earfcn=1300,
        enb_id=77,
        radio_connected=radio_connected,
        session_uptime_s=300.0,
        rx_bytes_per_sec=rx_bytes_per_sec,
        tx_bytes_per_sec=tx_bytes_per_sec,
        traffic_valid=traffic_valid,
        traffic_state_detail="packet_session_invalid" if traffic_valid is False else "",
        traffic_invalid_reason=f"connection_status_{connection_status_code}" if connection_status_code else "",
        packet_session_connected=packet_session_connected,
        connection_status_code=connection_status_code,
        selected_band_set=list(selected_band_set or []),
        timestamp=time.time(),
    )


def _IncidentAssessor_make_computed(
    *,
    rtt: float = 45.0,
    lq: float = 0.8,
    pressure: str = "normal",
    probe_ok: bool = True,
    alarm: bool = False,
    cart_ld: int = 1,
    cart_ld_valid: bool = True,
) -> ComputedSignals:
    return ComputedSignals(
        rtt_ms=rtt,
        rtt_ema=rtt,
        rtt_sliding_avg=rtt,
        rtt_baseline=40.0,
        rtt_elevated=(pressure == "busy"),
        rtt_alarm=alarm,
        rtt_probe_ok=probe_ok,
        rtt_pressure_level=pressure,
        cq_cell=lq,
        cq_vector_a_valid=True,
        cart_ld=int(cart_ld),
        cart_ld_valid=bool(cart_ld_valid),
    )


def _append_scoped_action(memory: Memory, record: ActionRecord) -> ActionRecord:
    """Admit a legacy-shaped fixture through the canonical history owner."""

    before_raw = _IncidentAssessor_make_modem(
        ca_active="+" in str(record.ca_signature or ""),
        ca_signature=str(record.ca_signature or "B3"),
        sinr_db=float(record.sinr_db or 0.0),
        rsrq_db=float(record.rsrq_db or -9.0),
        rx_bytes_per_sec=max(
            0.0, float(record.throughput_before_mbps or 0.0) * 125_000.0
        ),
    )
    after_raw = _IncidentAssessor_make_modem(
        ca_active="+" in str(record.ca_signature_after or ""),
        ca_signature=str(record.ca_signature_after or record.ca_signature or "B3"),
        sinr_db=float(record.sinr_after or record.sinr_db or 0.0),
        rsrq_db=float(record.rsrq_db or -9.0),
        rx_bytes_per_sec=max(
            0.0, float(record.throughput_after_mbps or 0.0) * 125_000.0
        ),
    )
    before_computed = _IncidentAssessor_make_computed(
        rtt=float(record.rtt_ms or record.rtt_sliding_avg or 0.0),
    )
    after_computed = _IncidentAssessor_make_computed(
        rtt=float(record.rtt_ms or record.rtt_sliding_avg or 0.0),
    )
    before, before_derived = cellular_test_inputs(before_raw, before_computed)
    after, after_derived = cellular_test_inputs(after_raw, after_computed)
    admitted = record_action_history(
        memory,
        modem_before=before,
        computed_before=before_derived,
        lever=record.lever,
        proposal_action_origin="fault_recovery",
        proof_observed_at=float(record.timestamp),
        params=dict(record.action_params or {}),
        success=bool(record.success),
        modem_after=after,
        computed_after=after_derived,
        learning_gate={
            "learning_allowed": True,
            "gate_input_complete": True,
        },
    )
    for field_name in record.__dataclass_fields__:
        if field_name in {
            "baseline_evidence_scope",
            "evidence_scope",
            "objective_metadata",
        }:
            continue
        setattr(admitted, field_name, getattr(record, field_name))
    return admitted


class TestRuntimeIncidentTracker(unittest.TestCase):
    def _assessor(self):
        cfg = Arch4Config(min_action_interval_s=0.0)
        td = tempfile.TemporaryDirectory()
        mem = Memory(cfg, memory_dir=td.name)
        hist = HistoricalJournal(cfg)
        assessor = RuntimeIncidentTracker(cfg, mem, hist)
        self.addCleanup(td.cleanup)
        return assessor, mem, hist

    def test_non_hard_degraded_window_stays_healthy(self):
        assessor, _, _ = self._assessor()

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=True, sinr_db=16.0, rsrp_dbm=-86.0),
            computed=_IncidentAssessor_make_computed(rtt=42.0, lq=0.72, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "degraded_persist", "overall_ok": False, "action_hint": "escalation_candidate"},
            now=time.time(),
        )

        self.assertEqual(out["incident_class"], "healthy")
        self.assertFalse(out["suppress_exploration"])
        self.assertEqual(out["recommended_next_step"], "none")

    def test_classifies_post_action_instability_from_recent_mutation(self):
        assessor, mem, _ = self._assessor()
        now = time.time()
        _append_scoped_action(mem,
            ActionRecord(
                timestamp=now - 20.0,
                sinr_db=15.0,
                rsrq_db=-9.0,
                mcs_dl=12,
                ca_signature="B1+B3+B20",
                rtt_sliding_avg=45.0,
                rtt_ms=45.0,
                lever=LeverType.BAND_SELECTION,
                action_params={"bands": [1, 3]},
                success=True,
                throughput_before_mbps=10.0,
                throughput_after_mbps=20.0,
                sinr_after=15.0,
                ca_signature_after="B1+B3+B20",
                improvement=10.0,
            )
        )

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=True),
            computed=_IncidentAssessor_make_computed(rtt=45.0, lq=0.75, pressure="probe_missing", probe_ok=False),
            state=SystemState.WORKING,
            health_status={"stage": "warning", "overall_ok": True, "action_hint": "observe"},
            now=now,
        )

        self.assertEqual(out["incident_class"], "post_action_instability")
        self.assertTrue(out["self_induced"])
        self.assertEqual(out["recommended_next_step"], "settle_backoff")

    def test_staged_roi_record_does_not_count_as_recent_modem_mutation(self):
        assessor, mem, _ = self._assessor()
        now = time.time()
        _append_scoped_action(mem,
            ActionRecord(
                timestamp=now - 20.0,
                sinr_db=15.0,
                rsrq_db=-9.0,
                mcs_dl=12,
                ca_signature="B1+B3+B20",
                rtt_sliding_avg=45.0,
                rtt_ms=45.0,
                lever=LeverType.BAND_SELECTION,
                action_params={"bands": [1, 3], "_roi_parent_trace_id": "trace-physical"},
                success=False,
                throughput_before_mbps=10.0,
                throughput_after_mbps=2.0,
                sinr_after=8.0,
                ca_signature_after="B1+B3",
                improvement=-8.0,
                outcome_label="bad",
                referee_outcome_code="staged_roi_negative",
                outcome_verdict_stage="staged_roi",
                execution_status="staged_roi",
                roi_parent_trace_id="trace-physical",
            )
        )

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=True),
            computed=_IncidentAssessor_make_computed(rtt=45.0, lq=0.75, pressure="probe_missing", probe_ok=False),
            state=SystemState.WORKING,
            health_status={"stage": "warning", "overall_ok": True, "action_hint": "observe"},
            now=now,
        )

        self.assertNotEqual(out["incident_class"], "post_action_instability")
        self.assertFalse(out["self_induced"])

    def test_recent_successful_mutation_defers_post_action_instability_during_settle_ping_blip(self):
        assessor, mem, _ = self._assessor()
        now = time.time()
        _append_scoped_action(mem,
            ActionRecord(
                timestamp=now - 10.0,
                sinr_db=4.0,
                rsrq_db=-10.0,
                mcs_dl=12,
                ca_signature="B1+B3+B20",
                rtt_sliding_avg=35.0,
                rtt_ms=35.0,
                lever=LeverType.PBAND,
                action_params={"pband": 20, "ca_pool": [1, 3, 20]},
                success=True,
                throughput_before_mbps=10.0,
                throughput_after_mbps=20.0,
                sinr_after=13.0,
                ca_signature_after="B1+B3+B20",
                improvement=10.0,
                outcome_label="good",
            )
        )

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=True,
                sinr_db=13.0,
                rsrp_dbm=-55.0,
                rsrq_db=-10.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=35.0, lq=0.80, pressure="probe_missing", probe_ok=False),
            state=SystemState.WORKING,
            health_status={
                "stage": "degraded_hold",
                "overall_ok": False,
                "action_hint": "observe",
                "ping_ok": False,
                "ping_fail_streak": 3,
            },
            now=now,
        )

        self.assertNotEqual(out["incident_class"], "post_action_instability")

    def test_recent_successful_mutation_still_flags_post_action_instability_after_settle_window(self):
        assessor, mem, _ = self._assessor()
        now = time.time()
        _append_scoped_action(mem,
            ActionRecord(
                timestamp=now - 45.0,
                sinr_db=4.0,
                rsrq_db=-10.0,
                mcs_dl=12,
                ca_signature="B1+B3+B20",
                rtt_sliding_avg=35.0,
                rtt_ms=35.0,
                lever=LeverType.PBAND,
                action_params={"pband": 20, "ca_pool": [1, 3, 20]},
                success=True,
                throughput_before_mbps=10.0,
                throughput_after_mbps=20.0,
                sinr_after=13.0,
                ca_signature_after="B1+B3+B20",
                improvement=10.0,
                outcome_label="good",
            )
        )

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=True,
                sinr_db=13.0,
                rsrp_dbm=-55.0,
                rsrq_db=-10.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=35.0, lq=0.80, pressure="probe_missing", probe_ok=False),
            state=SystemState.WORKING,
            health_status={
                "stage": "degraded_hold",
                "overall_ok": False,
                "action_hint": "observe",
                "ping_ok": False,
                "ping_fail_streak": 3,
            },
            now=now,
        )

        self.assertEqual(out["incident_class"], "post_action_instability")

    def test_planned_radio_quiet_suppresses_post_action_instability(self):
        assessor, mem, _ = self._assessor()
        now = time.time()
        _append_scoped_action(mem,
            ActionRecord(
                timestamp=now - 20.0,
                sinr_db=15.0,
                rsrq_db=-9.0,
                mcs_dl=12,
                ca_signature="B1+B3+B20",
                rtt_sliding_avg=45.0,
                rtt_ms=45.0,
                lever=LeverType.PBAND,
                action_params={"pband": 20, "ca_pool": [1, 3, 20]},
                success=True,
                throughput_before_mbps=10.0,
                throughput_after_mbps=20.0,
                sinr_after=15.0,
                ca_signature_after="B1+B3+B20",
                improvement=10.0,
            )
        )

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=True),
            computed=_IncidentAssessor_make_computed(rtt=45.0, lq=0.75, pressure="probe_missing", probe_ok=False),
            state=SystemState.WORKING,
            health_status={"stage": "warning", "overall_ok": True, "action_hint": "observe"},
            radio_mutation_window={
                "active": True,
                "reason": "planned_radio_quiet",
                "reasons": ["planned_radio_quiet", "mutating_action_lock"],
                "remaining_s": 42,
                "workflow_id": "pband_plus_ca",
                "workflow_active": True,
                "sequence_step": 1,
                "sequence_total": 2,
            },
            now=now,
        )

        self.assertEqual(out["incident_class"], "healthy")
        self.assertFalse(out["suppress_exploration"])
        self.assertTrue(out["planned_radio_quiet_active"])
        self.assertEqual(out["radio_mutation_workflow_id"], "pband_plus_ca")
        self.assertEqual(out["radio_mutation_sequence_step"], 1)
        self.assertEqual(out["radio_mutation_sequence_total"], 2)
        self.assertEqual(out["root_cause"]["radio_mutation_workflow_id"], "pband_plus_ca")
        self.assertEqual(assessor.drain_runtime_events(), [])

    def test_classifies_radio_ca_loss(self):
        assessor, _, hist = self._assessor()
        hour = time.localtime().tm_hour
        hist.hours[hour].samples = 20
        hist.hours[hour].avg_rtt = 85.0
        hist.hours[(hour + 1) % 24].samples = 20
        hist.hours[(hour + 1) % 24].avg_rtt = 110.0
        hist.band_perf[hour]["B1+B3+B20"] = (300.0, 5)
        assessor.note_calibration_handoff(
            {
                "allow_prediction_input": True,
                "reason": "saved_setup_active",
            }
        )

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=False, ca_signature="B3", rx_bytes_per_sec=500_000.0),
            computed=_IncidentAssessor_make_computed(rtt=90.0, lq=0.4, pressure="busy", cart_ld=0),
            state=SystemState.WORKING,
            health_status={"stage": "degraded_persist", "overall_ok": False, "action_hint": "escalation_candidate"},
            now=time.time(),
        )

        self.assertEqual(out["incident_class"], "radio_ca_loss")
        self.assertEqual(out["recommended_next_step"], "recover_ca")
        self.assertTrue(out["recovery_candidate"])

    def test_usable_ca_loss_under_load_stays_observation_before_persistence_gate(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B3",
                selected_band_set=(1, 3, 20),
                sinr_db=12.0,
                rsrp_dbm=-84.0,
                rsrq_db=-8.0,
                rx_bytes_per_sec=500_000.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=42.0, lq=0.78, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
            ca_lost_filtered=True,
        )

        self.assertEqual(out["incident_class"], "ca_capacity_reduced_observe")
        self.assertFalse(out["operator_incident"])
        self.assertFalse(out["suppress_exploration"])
        self.assertFalse(out["recovery_candidate"])
        self.assertEqual(assessor.drain_runtime_events(), [])

    def test_usable_ca_loss_under_load_stays_observation_after_persistence_gate(self):
        cfg = Arch4Config(min_action_interval_s=0.0, ca_lost_operator_min_s=60.0)
        td = tempfile.TemporaryDirectory()
        mem = Memory(cfg, memory_dir=td.name)
        hist = HistoricalJournal(cfg)
        assessor = RuntimeIncidentTracker(cfg, mem, hist)
        self.addCleanup(td.cleanup)
        now = time.time()

        kwargs = dict(
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B3",
                selected_band_set=(1, 3, 20),
                sinr_db=12.0,
                rsrp_dbm=-84.0,
                rsrq_db=-8.0,
                rx_bytes_per_sec=500_000.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=42.0, lq=0.78, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            ca_lost_filtered=True,
        )
        first = _assess(assessor, now=now, **kwargs)
        second = _assess(assessor, now=now + 61.0, **kwargs)

        self.assertEqual(first["incident_class"], "ca_capacity_reduced_observe")
        self.assertEqual(second["incident_class"], "ca_capacity_reduced_observe")
        self.assertFalse(second["operator_incident"])
        self.assertGreaterEqual(second["ca_loss_observation_age_s"], 60.0)
        self.assertEqual(assessor.drain_runtime_events(), [])

    def test_ca_loss_impacted_reuses_episode_through_observe_state(self):
        assessor, _, _ = self._assessor()
        now = time.time()
        ca_missing = dict(
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B3",
                selected_band_set=(3, 20),
                rx_bytes_per_sec=500_000.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=42.0, lq=0.78, pressure="normal"),
            state=SystemState.WORKING,
            ca_lost_filtered=True,
        )

        impacted = _assess(assessor,
            **ca_missing,
            health_status={"stage": "degraded_persist", "overall_ok": False, "action_hint": "escalation_candidate"},
            now=now,
        )
        observe = _assess(assessor,
            **ca_missing,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now + 10.0,
        )
        impacted_again = _assess(assessor,
            **ca_missing,
            health_status={"stage": "degraded_persist", "overall_ok": False, "action_hint": "escalation_candidate"},
            now=now + 20.0,
        )

        events = assessor.drain_runtime_events()
        self.assertEqual(impacted["incident_class"], "radio_ca_loss")
        self.assertEqual(observe["incident_class"], "ca_capacity_reduced_observe")
        self.assertTrue(observe["incident_state_active"])
        self.assertEqual(impacted["episode_id"], observe["episode_id"])
        self.assertEqual(impacted["episode_id"], impacted_again["episode_id"])
        self.assertEqual(sum(1 for e in events if e["kind"] == "incident_started"), 1)
        self.assertEqual(sum(1 for e in events if e["kind"] == "incident_resolved"), 0)

    def test_tracks_incident_episode_events(self):
        assessor, _, _ = self._assessor()
        now = time.time()
        _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=False, ca_signature="B3", rx_bytes_per_sec=500_000.0),
            computed=_IncidentAssessor_make_computed(rtt=90.0, lq=0.40, pressure="busy", cart_ld=0),
            state=SystemState.WORKING,
            health_status={"stage": "degraded_persist", "overall_ok": False, "action_hint": "escalation_candidate"},
            now=now,
        )
        _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=True),
            computed=_IncidentAssessor_make_computed(rtt=45.0, lq=0.75, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now + 10.0,
        )
        resolved = _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=True),
            computed=_IncidentAssessor_make_computed(rtt=45.0, lq=0.75, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now + 45.0,
        )
        events = assessor.drain_runtime_events()
        kinds = [e["kind"] for e in events]
        self.assertIn("incident_started", kinds)
        self.assertIn("incident_resolved", kinds)
        self.assertEqual(resolved["incident_class"], "healthy")
        self.assertEqual(resolved["episode_id"], "")

    def test_startup_bootstrap_grace_observes_data_path_degrade_without_operator_incident(self):
        assessor, _, _ = self._assessor()
        now = time.time()
        assessor.arm_startup_bootstrap_grace(now=now)

        _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=True,
                ca_signature="B3+B20",
                selected_band_set=(1, 3, 20),
                sinr_db=16.0,
                rsrp_dbm=-86.0,
                rsrq_db=-9.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=260.0, lq=0.74, pressure="normal"),
            state=SystemState.WORKING,
            health_status={
                "stage": "degraded_hold",
                "overall_ok": False,
                "action_hint": "observe",
                "ping_ok": False,
                "ping_fail_streak": 1,
            },
            now=now + 5.0,
        )
        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=True,
                ca_signature="B3+B20",
                selected_band_set=(1, 3, 20),
                sinr_db=16.0,
                rsrp_dbm=-86.0,
                rsrq_db=-9.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=260.0, lq=0.74, pressure="normal"),
            state=SystemState.WORKING,
            health_status={
                "stage": "degraded_hold",
                "overall_ok": False,
                "action_hint": "observe",
                "ping_ok": False,
                "ping_fail_streak": 1,
            },
            now=now + 26.0,
        )

        self.assertEqual(out["incident_class"], "startup_bootstrap_observe")
        self.assertFalse(out["operator_incident"])
        self.assertFalse(out["suppress_exploration"])
        self.assertFalse(out["recovery_candidate"])
        self.assertTrue(out["startup_bootstrap_grace_active"])

    def test_startup_bootstrap_grace_expiry_allows_data_path_incident(self):
        cfg = Arch4Config(min_action_interval_s=0.0)
        cfg.startup_incident_grace_s = 10.0
        td = tempfile.TemporaryDirectory()
        mem = Memory(cfg, memory_dir=td.name)
        hist = HistoricalJournal(cfg)
        assessor = RuntimeIncidentTracker(cfg, mem, hist)
        self.addCleanup(td.cleanup)
        now = time.time()
        assessor.arm_startup_bootstrap_grace(now=now)

        _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=True,
                ca_signature="B3+B20",
                selected_band_set=(1, 3, 20),
                sinr_db=16.0,
                rsrp_dbm=-86.0,
                rsrq_db=-9.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=260.0, lq=0.74, pressure="normal"),
            state=SystemState.WORKING,
            health_status={
                "stage": "degraded_persist",
                "overall_ok": False,
                "action_hint": "observe",
                "ping_ok": False,
                "ping_fail_streak": 3,
            },
            now=now + 1.0,
        )
        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=True,
                ca_signature="B3+B20",
                selected_band_set=(1, 3, 20),
                sinr_db=16.0,
                rsrp_dbm=-86.0,
                rsrq_db=-9.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=260.0, lq=0.74, pressure="normal"),
            state=SystemState.WORKING,
            health_status={
                "stage": "degraded_persist",
                "overall_ok": False,
                "action_hint": "observe",
                "ping_ok": False,
                "ping_fail_streak": 3,
            },
            now=now + 23.0,
        )

        self.assertEqual(out["incident_class"], "data_path_degraded")
        self.assertTrue(out["operator_incident"])

    def test_startup_bootstrap_grace_observes_connection_lost(self):
        assessor, _, _ = self._assessor()
        now = time.time()
        assessor.arm_startup_bootstrap_grace(now=now)

        out = assessor.note_connection_lost(now=now + 2.0)

        self.assertEqual(out["incident_class"], "startup_bootstrap_observe")
        self.assertFalse(out["operator_incident"])
        self.assertFalse(out["recovery_candidate"])

    def test_rf_watch_does_not_open_critical_episode_without_bad_rf_proof(self):
        assessor, _, _ = self._assessor()
        now = time.time()
        kwargs = dict(
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B3",
                selected_band_set=(3,),
                sinr_db=-2.0,
                rsrp_dbm=-108.0,
                rsrq_db=-15.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=28.0, lq=0.72, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
        )

        _assess(assessor, now=now, **kwargs)
        out = _assess(assessor, now=now + 3.0, **kwargs)

        self.assertEqual(out["incident_class"], "rf_quality_watch")
        self.assertEqual(out["severity"], "low")
        self.assertFalse(out["operator_incident"])
        self.assertFalse(out["recovery_candidate"])
        self.assertFalse(out["suppress_exploration"])
        self.assertEqual(assessor.drain_runtime_events(), [])

    def test_rf_collapse_clear_flicker_keeps_one_episode_until_hold_expires(self):
        assessor, _, _ = self._assessor()
        now = time.time()
        bad = dict(
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B3",
                selected_band_set=(3,),
                sinr_db=-7.0,
                rsrp_dbm=-112.0,
                rsrq_db=-15.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=28.0, lq=0.25, pressure="normal"),
            state=SystemState.BAD_RF,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
        )

        _assess(assessor, now=now, **bad)
        first = _assess(assessor, now=now + 3.0, **bad)
        clear = _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=True),
            computed=_IncidentAssessor_make_computed(rtt=42.0, lq=0.75, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now + 10.0,
        )
        restored = _assess(assessor, now=now + 16.0, **bad)

        events = assessor.drain_runtime_events()
        self.assertEqual(first["incident_class"], "rf_collapse")
        self.assertEqual(first["episode_id"], clear["episode_id"])
        self.assertEqual(first["episode_id"], restored["episode_id"])
        self.assertTrue(clear["incident_clear_pending"])
        self.assertEqual(sum(1 for e in events if e["kind"] == "incident_started"), 1)
        self.assertEqual(sum(1 for e in events if e["kind"] == "incident_resolved"), 0)

    def test_radio_ca_loss_clear_flicker_keeps_one_episode_until_hold_expires(self):
        assessor, _, _ = self._assessor()
        now = time.time()
        first = _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=False, ca_signature="B3", rx_bytes_per_sec=500_000.0),
            computed=_IncidentAssessor_make_computed(rtt=90.0, lq=0.40, pressure="busy", cart_ld=0),
            state=SystemState.WORKING,
            health_status={"stage": "degraded_persist", "overall_ok": False, "action_hint": "escalation_candidate"},
            now=now,
        )
        short_clear = _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=True),
            computed=_IncidentAssessor_make_computed(rtt=45.0, lq=0.75, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now + 5.0,
        )
        restored = _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=False, ca_signature="B3", rx_bytes_per_sec=500_000.0),
            computed=_IncidentAssessor_make_computed(rtt=90.0, lq=0.40, pressure="busy", cart_ld=0),
            state=SystemState.WORKING,
            health_status={"stage": "degraded_persist", "overall_ok": False, "action_hint": "escalation_candidate"},
            now=now + 12.0,
        )

        events = assessor.drain_runtime_events()
        self.assertEqual(first["episode_id"], short_clear["episode_id"])
        self.assertEqual(first["episode_id"], restored["episode_id"])
        self.assertEqual(sum(1 for e in events if e["kind"] == "incident_started"), 1)
        self.assertEqual(sum(1 for e in events if e["kind"] == "incident_resolved"), 0)

    def test_critical_incident_splits_when_class_changes(self):
        assessor, mem, _ = self._assessor()
        now = time.time()

        _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B3",
                selected_band_set=(3,),
                sinr_db=-7.0,
                rsrp_dbm=-112.0,
                rsrq_db=-15.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=28.0, lq=0.72, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )
        first = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B3",
                selected_band_set=(3,),
                sinr_db=-7.0,
                rsrp_dbm=-112.0,
                rsrq_db=-15.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=28.0, lq=0.72, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now + 3.0,
        )
        _append_scoped_action(mem,
            ActionRecord(
                timestamp=now + 4.0,
                sinr_db=15.0,
                rsrq_db=-9.0,
                mcs_dl=12,
                ca_signature="B3",
                rtt_sliding_avg=45.0,
                rtt_ms=45.0,
                lever=LeverType.BAND_SELECTION,
                action_params={"bands": [3]},
                success=True,
                throughput_before_mbps=10.0,
                throughput_after_mbps=20.0,
                sinr_after=15.0,
                ca_signature_after="B3",
                improvement=10.0,
            )
        )
        second = _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=True, sinr_db=14.0, rsrp_dbm=-86.0, rsrq_db=-6.0),
            computed=_IncidentAssessor_make_computed(rtt=42.0, lq=0.72, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "degraded_persist", "overall_ok": False, "action_hint": "escalation_candidate"},
            now=now + 13.0,
        )

        events = assessor.drain_runtime_events()
        kinds = [e["kind"] for e in events]
        self.assertEqual(first["incident_class"], "rf_collapse")
        self.assertEqual(second["incident_class"], "post_action_instability")
        self.assertIn("incident_started", kinds)
        self.assertIn("incident_resolved", kinds)
        self.assertEqual(sum(1 for e in events if e["kind"] == "incident_started"), 2)
        self.assertNotEqual(first["episode_id"], second["episode_id"])

    def test_connection_lost_replaces_active_critical_episode(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B3",
                selected_band_set=(3,),
                sinr_db=-7.0,
                rsrp_dbm=-112.0,
                rsrq_db=-15.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=28.0, lq=0.72, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )
        first = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B3",
                selected_band_set=(3,),
                sinr_db=-7.0,
                rsrp_dbm=-112.0,
                rsrq_db=-15.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=28.0, lq=0.72, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now + 3.0,
        )
        second = assessor.note_connection_lost(now=now + 18.0)

        events = assessor.drain_runtime_events()
        self.assertEqual(first["incident_class"], "rf_collapse")
        self.assertEqual(second["incident_class"], "radio_disconnected")
        self.assertEqual(second["health_stage"], "connection_lost")
        self.assertEqual(sum(1 for e in events if e["kind"] == "incident_started"), 2)
        self.assertEqual(sum(1 for e in events if e["kind"] == "incident_resolved"), 1)
        self.assertNotEqual(first["episode_id"], second["episode_id"])

    def test_incident_started_event_carries_root_cause_summary(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=False, ca_signature="B3", sinr_db=14.0, rx_bytes_per_sec=500_000.0),
            computed=_IncidentAssessor_make_computed(rtt=95.0, lq=0.4, pressure="busy", cart_ld=0),
            state=SystemState.WORKING,
            health_status={"stage": "degraded_persist", "overall_ok": False, "action_hint": "escalation_candidate"},
            now=now,
        )

        events = assessor.drain_runtime_events()
        started = next(e for e in events if e["kind"] == "incident_started")
        self.assertIn("root_cause", started)
        self.assertIsInstance(started["root_cause"], dict)
        self.assertIn("probable_cause", started["root_cause"])
        self.assertEqual(started["root_cause"]["primary_band"], 3)
        self.assertEqual(started["root_cause"]["selected_band_set_signature"], "B1+B3+B20")
        self.assertEqual(started["root_cause"]["active_band_set_signature"], "B3")
        self.assertEqual(started["root_cause"]["ca_signature"], "B3")

    def test_transient_sinr_floor_breach_uses_window_not_single_sample(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        first = _assess(assessor,
            modem=_IncidentAssessor_make_modem(sinr_db=6.0, rsrp_dbm=-82.0, rsrq_db=-5.0, rx_bytes_per_sec=0.0),
            computed=_IncidentAssessor_make_computed(rtt=30.0, lq=0.72, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )
        second = _assess(assessor,
            modem=_IncidentAssessor_make_modem(sinr_db=3.0, rsrp_dbm=-82.0, rsrq_db=-5.0, rx_bytes_per_sec=0.0),
            computed=_IncidentAssessor_make_computed(rtt=30.0, lq=0.72, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now + 3.0,
        )

        self.assertEqual(first["incident_class"], "healthy")
        self.assertEqual(second["incident_class"], "healthy")

    def test_single_rf_metric_breaches_do_not_classify_rf_collapse(self):
        cases = [
            {
                "name": "weak_rsrp_only",
                "modem": {
                    "ca_active": True,
                    "ca_signature": "B1+B3+B20",
                    "selected_band_set": (1, 3, 20),
                    "sinr_db": 12.0,
                    "rsrp_dbm": -111.0,
                    "rsrq_db": -9.0,
                    "rx_bytes_per_sec": 80_000.0,
                },
                "computed": {"rtt": 28.0, "lq": 0.78, "pressure": "normal"},
                "repeat": False,
            },
            {
                "name": "low_sinr_only",
                "modem": {
                    "ca_active": True,
                    "ca_signature": "B1+B3+B20",
                    "selected_band_set": (1, 3, 20),
                    "sinr_db": -1.0,
                    "rsrp_dbm": -90.0,
                    "rsrq_db": -9.0,
                    "rx_bytes_per_sec": 80_000.0,
                },
                "computed": {"rtt": 28.0, "lq": 0.78, "pressure": "normal"},
                "repeat": True,
            },
            {
                "name": "weak_rsrq_only",
                "modem": {
                    "ca_active": True,
                    "ca_signature": "B1+B3+B20",
                    "selected_band_set": (1, 3, 20),
                    "sinr_db": 12.0,
                    "rsrp_dbm": -90.0,
                    "rsrq_db": -15.0,
                    "rx_bytes_per_sec": 80_000.0,
                },
                "computed": {"rtt": 28.0, "lq": 0.78, "pressure": "normal"},
                "repeat": False,
            },
            {
                "name": "weak_cq_only",
                "modem": {
                    "ca_active": True,
                    "ca_signature": "B1+B3+B20",
                    "selected_band_set": (1, 3, 20),
                    "sinr_db": 12.0,
                    "rsrp_dbm": -90.0,
                    "rsrq_db": -9.0,
                    "rx_bytes_per_sec": 80_000.0,
                },
                "computed": {"rtt": 28.0, "lq": 0.40, "pressure": "normal"},
                "repeat": False,
            },
        ]

        for case in cases:
            with self.subTest(case["name"]):
                assessor, _, _ = self._assessor()
                now = time.time()
                if case["repeat"]:
                    _assess(assessor,
                        modem=_IncidentAssessor_make_modem(**case["modem"]),
                        computed=_IncidentAssessor_make_computed(**case["computed"]),
                        state=SystemState.WORKING,
                        health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
                        now=now,
                    )
                    now += 3.0
                out = _assess(assessor,
                    modem=_IncidentAssessor_make_modem(**case["modem"]),
                    computed=_IncidentAssessor_make_computed(**case["computed"]),
                    state=SystemState.WORKING,
                    health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
                    now=now,
                )

                self.assertEqual(out["incident_class"], "healthy")
                self.assertFalse(out["suppress_exploration"])

    def test_good_sinr_and_rsrp_blocks_startup_rf_collapse_even_if_cq_is_early_low(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="",
                selected_band_set=(1, 3, 7, 8, 20, 28),
                sinr_db=10.0,
                rsrp_dbm=-93.0,
                rsrq_db=-14.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=62.0, lq=0.33, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )

        self.assertEqual(out["incident_class"], "healthy")
        self.assertFalse(out["suppress_exploration"])

    def test_usable_sinr_and_rsrp_blocks_weak_rsrq_cq_only_rf_collapse(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="",
                selected_band_set=(1, 3, 7, 8, 20, 28),
                sinr_db=8.0,
                rsrp_dbm=-93.0,
                rsrq_db=-15.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=24.0, lq=0.42, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )

        self.assertEqual(out["incident_class"], "healthy")
        self.assertFalse(out["suppress_exploration"])

    def test_ca_capacity_with_good_outcome_blocks_weak_rsrp_rsrq_rf_collapse(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=True,
                ca_signature="B1+B3+B20",
                selected_band_set=(1, 3, 20),
                sinr_db=8.0,
                rsrp_dbm=-112.0,
                rsrq_db=-14.0,
                rx_bytes_per_sec=80_000.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=38.0, lq=0.52, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )

        self.assertEqual(out["incident_class"], "healthy")
        self.assertFalse(out["suppress_exploration"])

    def test_latency_keepalive_path_is_not_rf_collapse_when_rf_is_usable(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=True,
                ca_signature="B1+B3+B20",
                selected_band_set=(1, 3, 20),
                sinr_db=8.0,
                rsrp_dbm=-112.0,
                rsrq_db=-12.0,
                rx_bytes_per_sec=60_000.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=220.0, lq=0.55, pressure="busy"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )

        self.assertEqual(out["incident_class"], "latency_keepalive_path")
        self.assertFalse(out["suppress_exploration"])
        self.assertFalse(out["recovery_candidate"])

    def test_soft_observations_do_not_create_incident_start_resolve_noise(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        for offset_s in (0.0, 121.0):
            out = _assess(assessor,
                modem=_IncidentAssessor_make_modem(
                    ca_active=True,
                    ca_signature="B1+B3+B20",
                    selected_band_set=(1, 3, 20),
                    sinr_db=8.0,
                    rsrp_dbm=-112.0,
                    rsrq_db=-12.0,
                    rx_bytes_per_sec=60_000.0,
                ),
                computed=_IncidentAssessor_make_computed(rtt=220.0, lq=0.55, pressure="busy"),
                state=SystemState.WORKING,
                health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
                now=now + offset_s,
            )

        events = assessor.drain_runtime_events()
        self.assertEqual(out["incident_class"], "latency_keepalive_path")
        self.assertFalse(out["operator_incident"])
        self.assertEqual(out["episode_id"], "")
        self.assertEqual(sum(1 for e in events if e["kind"] == "incident_started"), 0)
        self.assertEqual(sum(1 for e in events if e["kind"] == "incident_resolved"), 0)
        self.assertEqual(sum(1 for e in events if e["kind"] == "incident_observation_summary"), 1)

    def test_latency_spike_with_low_sinr_but_strong_serving_rf_is_not_rf_collapse(self):
        cfg = Arch4Config(min_action_interval_s=0.0, emergency_sinr_floor=3.0)
        td = tempfile.TemporaryDirectory()
        mem = Memory(cfg, memory_dir=td.name)
        hist = HistoricalJournal(cfg)
        assessor = RuntimeIncidentTracker(cfg, mem, hist)
        self.addCleanup(td.cleanup)
        now = time.time()

        modem = _IncidentAssessor_make_modem(
            ca_active=True,
            ca_signature="B1+CA",
            selected_band_set=(1, 3, 7, 20, 28),
            sinr_db=1.0,
            rsrp_dbm=-76.0,
            rsrq_db=-10.0,
            rx_bytes_per_sec=80_000.0,
        )
        computed = _IncidentAssessor_make_computed(rtt=313.0, lq=0.76, pressure="busy")

        _assess(assessor,
            modem=modem,
            computed=computed,
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )
        out = _assess(assessor,
            modem=modem,
            computed=computed,
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now + 3.0,
        )

        self.assertEqual(out["incident_class"], "latency_keepalive_path")
        self.assertFalse(out["suppress_exploration"])
        self.assertFalse(out["recovery_candidate"])

    def test_rf_truth_blocks_bad_rf_when_b818_has_strong_rsrp_and_ca(self):
        cfg = Arch4Config(min_action_interval_s=0.0, emergency_sinr_floor=3.0)
        td = tempfile.TemporaryDirectory()
        mem = Memory(cfg, memory_dir=td.name)
        hist = HistoricalJournal(cfg)
        assessor = RuntimeIncidentTracker(cfg, mem, hist)
        self.addCleanup(td.cleanup)
        now = time.time()
        modem = _IncidentAssessor_make_modem(
            ca_active=True,
            ca_signature="B1+B3+B20",
            selected_band_set=(1, 3, 20),
            sinr_db=1.0,
            rsrp_dbm=-77.0,
            rsrq_db=-10.0,
            rx_bytes_per_sec=80_000.0,
        )
        computed = _IncidentAssessor_make_computed(rtt=203.0, lq=0.76, pressure="busy")

        _assess(assessor,
            modem=modem,
            computed=computed,
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )
        out = _assess(assessor,
            modem=modem,
            computed=computed,
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now + 3.0,
        )

        self.assertNotEqual(out["incident_class"], "rf_collapse")
        self.assertTrue(out["root_cause"]["rf_truth_verdict"] in {"usable", "good"})
        self.assertTrue(out["brain_health"]["evidence"]["rf_truth_verdict"] in {"usable", "good"})
        self.assertFalse(out["suppress_exploration"])

    def test_state_machine_rf_entry_uses_shared_truth_with_strong_serving_rf(self):
        cfg = Arch4Config(min_action_interval_s=0.0)
        td = tempfile.TemporaryDirectory()
        mem = Memory(cfg, memory_dir=td.name)
        self.addCleanup(td.cleanup)
        sm = TestControllerPostureAuthority(cfg, mem)

        modem = _IncidentAssessor_make_modem(
            ca_active=True,
            ca_signature="B1+B3+B20",
            selected_band_set=(1, 3, 20),
            sinr_db=1.0,
            rsrp_dbm=-77.0,
            rsrq_db=-10.0,
            rx_bytes_per_sec=80_000.0,
        )
        computed = _IncidentAssessor_make_computed(rtt=203.0, lq=0.76, pressure="busy")

        self.assertFalse(sm._rf_collapse_entry_ready(modem, computed))

    def test_degraded_ping_path_with_usable_rf_is_not_named_rf_collapse(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=True,
                ca_signature="B3+B20",
                selected_band_set=(1, 3, 20),
                sinr_db=18.0,
                rsrp_dbm=-83.0,
                rsrq_db=-9.0,
                rx_bytes_per_sec=80_000.0,
            ),
            computed=_IncidentAssessor_make_computed(
                rtt=910.0,
                lq=0.76,
                pressure="busy",
                probe_ok=False,
            ),
            state=SystemState.WORKING,
            health_status={
                "stage": "degraded_persist",
                "overall_ok": False,
                "action_hint": "escalation_candidate",
                "ping_ok": False,
                "ping_fail_streak": 4,
                "recovery_candidate": True,
            },
            now=now,
        )

        self.assertNotEqual(out["incident_class"], "data_path_degraded")
        self.assertFalse(out["operator_incident"])

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=True,
                ca_signature="B3+B20",
                selected_band_set=(1, 3, 20),
                sinr_db=18.0,
                rsrp_dbm=-83.0,
                rsrq_db=-9.0,
                rx_bytes_per_sec=0.0,
                tx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(
                rtt=910.0,
                lq=0.76,
                pressure="busy",
                probe_ok=False,
            ),
            state=SystemState.WORKING,
            health_status={
                "stage": "degraded_persist",
                "overall_ok": False,
                "action_hint": "escalation_candidate",
                "ping_ok": False,
                "ping_fail_streak": 4,
                "recovery_candidate": True,
            },
            now=now + 21.0,
        )

        self.assertEqual(out["incident_class"], "data_path_degraded")
        self.assertNotEqual(out["root_cause"]["probable_cause"], "rf_degradation")
        self.assertTrue(out["suppress_exploration"])
        self.assertFalse(out["recovery_candidate"])

    def test_degraded_data_path_takes_priority_over_ca_loss_when_rf_is_usable(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B3",
                selected_band_set=(1, 3, 20),
                sinr_db=17.0,
                rsrp_dbm=-105.0,
                rsrq_db=-7.0,
                rx_bytes_per_sec=500_000.0,
            ),
            computed=_IncidentAssessor_make_computed(
                rtt=910.0,
                lq=0.70,
                pressure="busy",
                probe_ok=False,
            ),
            state=SystemState.WORKING,
            health_status={
                "stage": "degraded_persist",
                "overall_ok": False,
                "action_hint": "escalation_candidate",
                "ping_ok": False,
                "ping_fail_streak": 4,
                "recovery_candidate": True,
            },
            now=now,
        )
        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B3",
                selected_band_set=(1, 3, 20),
                sinr_db=17.0,
                rsrp_dbm=-105.0,
                rsrq_db=-7.0,
                rx_bytes_per_sec=0.0,
                tx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(
                rtt=910.0,
                lq=0.70,
                pressure="busy",
                probe_ok=False,
            ),
            state=SystemState.WORKING,
            health_status={
                "stage": "degraded_persist",
                "overall_ok": False,
                "action_hint": "escalation_candidate",
                "ping_ok": False,
                "ping_fail_streak": 4,
                "recovery_candidate": True,
            },
            now=now + 21.0,
        )

        self.assertEqual(out["incident_class"], "data_path_degraded")
        self.assertEqual(out["recommended_next_step"], "hold_radio_changes")
        self.assertFalse(out["recovery_candidate"])

    def test_packet_session_invalid_is_data_path_not_idle_zero(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        pending = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                sinr_db=16.0,
                rsrp_dbm=-82.0,
                rsrq_db=-8.0,
                rx_bytes_per_sec=0.0,
                traffic_valid=False,
                packet_session_connected=False,
                connection_status_code="902",
            ),
            computed=_IncidentAssessor_make_computed(rtt=40.0, lq=0.8, probe_ok=True),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True},
            now=now,
        )
        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                sinr_db=16.0,
                rsrp_dbm=-82.0,
                rsrq_db=-8.0,
                rx_bytes_per_sec=0.0,
                traffic_valid=False,
                packet_session_connected=False,
                connection_status_code="902",
            ),
            computed=_IncidentAssessor_make_computed(rtt=40.0, lq=0.8, probe_ok=True),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True},
            now=now + 21.0,
        )

        self.assertNotEqual(pending["incident_class"], "data_path_degraded")
        self.assertEqual(out["incident_class"], "data_path_degraded")
        self.assertEqual(out["incident_subtype"], "packet_session_invalid")
        self.assertEqual(out["root_cause"]["probable_cause"], "modem_packet_session_invalid")
        self.assertEqual(out["root_cause"]["connection_status_code"], "902")
        self.assertFalse(out["root_cause"]["traffic_valid"])
        self.assertTrue(out["suppress_exploration"])

    def test_traffic_sample_invalid_requires_persistent_confirmation_before_operator_incident(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        pending = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                sinr_db=16.0,
                rsrp_dbm=-82.0,
                rsrq_db=-8.0,
                rx_bytes_per_sec=0.0,
                traffic_valid=False,
                packet_session_connected=True,
            ),
            computed=_IncidentAssessor_make_computed(rtt=40.0, lq=0.8, probe_ok=True),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True},
            now=now,
        )
        confirmed = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                sinr_db=16.0,
                rsrp_dbm=-82.0,
                rsrq_db=-8.0,
                rx_bytes_per_sec=0.0,
                traffic_valid=False,
                packet_session_connected=True,
            ),
            computed=_IncidentAssessor_make_computed(rtt=40.0, lq=0.8, probe_ok=True),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True},
            now=now + 21.0,
        )

        self.assertNotEqual(pending["incident_class"], "data_path_degraded")
        self.assertFalse(pending["operator_incident"])
        self.assertEqual(confirmed["incident_class"], "data_path_degraded")
        self.assertEqual(confirmed["incident_subtype"], "traffic_sample_invalid")

    def test_zero_traffic_with_path_failure_can_flag_downstream_collision_suspect(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                sinr_db=16.0,
                rsrp_dbm=-82.0,
                rsrq_db=-8.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=500.0, lq=0.8, probe_ok=False),
            state=SystemState.WORKING,
            health_status={
                "stage": "degraded_persist",
                "overall_ok": False,
                "ping_ok": False,
                "ping_fail_streak": 4,
            },
            now=now,
        )

        self.assertNotEqual(out["incident_class"], "data_path_degraded")
        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                sinr_db=16.0,
                rsrp_dbm=-82.0,
                rsrq_db=-8.0,
                rx_bytes_per_sec=0.0,
                tx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=500.0, lq=0.8, probe_ok=False),
            state=SystemState.WORKING,
            health_status={
                "stage": "degraded_persist",
                "overall_ok": False,
                "ping_ok": False,
                "ping_fail_streak": 4,
            },
            now=now + 21.0,
        )

        self.assertEqual(out["incident_class"], "data_path_degraded")
        self.assertEqual(out["root_cause"]["probable_cause"], "downstream_topology_or_collision")
        self.assertTrue(out["root_cause"]["downstream_collision_suspected"])

    def test_loaded_probe_failure_does_not_confirm_data_path_while_traffic_is_active(self):
        assessor, _, _ = self._assessor()
        now = time.time()
        health = {
            "stage": "degraded_persist",
            "overall_ok": False,
            "action_hint": "escalation_candidate",
            "ping_ok": False,
            "ping_fail_streak": 4,
        }

        first = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=True,
                ca_signature="B3+B20",
                selected_band_set=(1, 3, 20),
                sinr_db=18.0,
                rsrp_dbm=-83.0,
                rsrq_db=-9.0,
                rx_bytes_per_sec=1_500_000.0,
                tx_bytes_per_sec=600_000.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=650.0, lq=0.76, pressure="busy", probe_ok=False),
            state=SystemState.WORKING,
            health_status=health,
            now=now,
        )
        second = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=True,
                ca_signature="B3+B20",
                selected_band_set=(1, 3, 20),
                sinr_db=18.0,
                rsrp_dbm=-83.0,
                rsrq_db=-9.0,
                rx_bytes_per_sec=1_500_000.0,
                tx_bytes_per_sec=600_000.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=650.0, lq=0.76, pressure="busy", probe_ok=False),
            state=SystemState.WORKING,
            health_status=health,
            now=now + 25.0,
        )

        self.assertNotEqual(first["incident_class"], "data_path_degraded")
        self.assertNotEqual(second["incident_class"], "data_path_degraded")
        self.assertFalse(first["operator_incident"])
        self.assertFalse(second["operator_incident"])

    def test_idle_ca_absence_stays_healthy_instead_of_rf_collapse(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B3",
                selected_band_set=(1, 3, 20),
                sinr_db=8.0,
                rsrp_dbm=-112.0,
                rsrq_db=-12.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=42.0, lq=0.55, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )

        self.assertEqual(out["incident_class"], "healthy")
        self.assertFalse(out["operator_incident"])
        self.assertFalse(out["suppress_exploration"])
        self.assertFalse(out["recovery_candidate"])

    def test_idle_ca_absence_with_low_sinr_but_good_rsrp_rsrq_stays_healthy(self):
        cfg = Arch4Config(min_action_interval_s=0.0, emergency_sinr_floor=3.0)
        td = tempfile.TemporaryDirectory()
        mem = Memory(cfg, memory_dir=td.name)
        hist = HistoricalJournal(cfg)
        assessor = RuntimeIncidentTracker(cfg, mem, hist)
        self.addCleanup(td.cleanup)
        now = time.time()

        modem = _IncidentAssessor_make_modem(
            ca_active=False,
            ca_signature="B1",
            selected_band_set=(1, 3, 7, 20, 28),
            sinr_db=1.0,
            rsrp_dbm=-76.0,
            rsrq_db=-10.0,
            rx_bytes_per_sec=0.0,
        )
        computed = _IncidentAssessor_make_computed(rtt=38.0, lq=0.72, pressure="normal")

        _assess(assessor,
            modem=modem,
            computed=computed,
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )
        out = _assess(assessor,
            modem=modem,
            computed=computed,
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now + 3.0,
        )

        self.assertEqual(out["incident_class"], "healthy")
        self.assertFalse(out["operator_incident"])
        self.assertFalse(out["suppress_exploration"])
        self.assertFalse(out["recovery_candidate"])

    def test_sustained_low_sinr_without_other_bad_evidence_does_not_classify_rf_collapse(self):
        cfg = Arch4Config(min_action_interval_s=0.0, emergency_sinr_floor=0.0)
        td = tempfile.TemporaryDirectory()
        mem = Memory(cfg, memory_dir=td.name)
        hist = HistoricalJournal(cfg)
        assessor = RuntimeIncidentTracker(cfg, mem, hist)
        self.addCleanup(td.cleanup)
        now = time.time()

        _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B1",
                selected_band_set=(1,),
                sinr_db=-1.0,
                rsrp_dbm=-100.0,
                rsrq_db=-13.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=28.0, lq=0.72, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )
        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B1",
                selected_band_set=(1,),
                sinr_db=-1.0,
                rsrp_dbm=-100.0,
                rsrq_db=-13.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=28.0, lq=0.72, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now + 3.0,
        )

        self.assertNotEqual(out["incident_class"], "rf_collapse")
        self.assertFalse(out["suppress_exploration"])

    def test_low_sinr_ca_capacity_lane_does_not_classify_rf_collapse(self):
        cfg = Arch4Config(min_action_interval_s=0.0, emergency_sinr_floor=3.0)
        td = tempfile.TemporaryDirectory()
        mem = Memory(cfg, memory_dir=td.name)
        hist = HistoricalJournal(cfg)
        assessor = RuntimeIncidentTracker(cfg, mem, hist)
        self.addCleanup(td.cleanup)
        now = time.time()

        modem = _IncidentAssessor_make_modem(
            ca_active=True,
            ca_signature="B1+CA",
            selected_band_set=(1, 3, 7, 20, 28),
            sinr_db=1.0,
            rsrp_dbm=-76.0,
            rsrq_db=-10.0,
            rx_bytes_per_sec=80_000.0,
        )
        computed = _IncidentAssessor_make_computed(rtt=32.0, lq=0.78, pressure="normal")

        _assess(assessor,
            modem=modem,
            computed=computed,
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )
        out = _assess(assessor,
            modem=modem,
            computed=computed,
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now + 3.0,
        )

        self.assertEqual(out["incident_class"], "healthy")
        self.assertFalse(out["suppress_exploration"])
        self.assertEqual(out["brain_health"]["status"], "healthy")
        self.assertIn("no confirmed incident", out["brain_health"]["user_message"])

    def test_brain_health_broadcast_investigates_low_confidence_observation(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        modem = _IncidentAssessor_make_modem(
            ca_active=False,
            ca_signature="B1",
            selected_band_set=(1, 3, 7, 20, 28),
            sinr_db=12.0,
            rsrp_dbm=-92.0,
            rsrq_db=-10.0,
            rx_bytes_per_sec=500_000.0,
        )
        computed = _IncidentAssessor_make_computed(rtt=40.0, lq=0.70, pressure="normal", cart_ld=0)

        out = _assess(assessor,
            modem=modem,
            computed=computed,
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )

        self.assertEqual(out["incident_class"], "ca_capacity_reduced_observe")
        self.assertFalse(out["operator_incident"])
        self.assertEqual(out["brain_health"]["status"], "investigating")
        self.assertIn("collecting evidence", out["brain_health"]["user_message"])

    def test_brain_health_broadcast_confirms_hard_rf_collapse(self):
        cfg = Arch4Config(min_action_interval_s=0.0, emergency_sinr_floor=0.0)
        td = tempfile.TemporaryDirectory()
        mem = Memory(cfg, memory_dir=td.name)
        hist = HistoricalJournal(cfg)
        assessor = RuntimeIncidentTracker(cfg, mem, hist)
        self.addCleanup(td.cleanup)
        now = time.time()

        _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B3",
                selected_band_set=(3,),
                sinr_db=-7.0,
                rsrp_dbm=-112.0,
                rsrq_db=-15.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=35.0, lq=0.25, pressure="normal"),
            state=SystemState.BAD_RF,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )
        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B3",
                selected_band_set=(3,),
                sinr_db=-7.0,
                rsrp_dbm=-112.0,
                rsrq_db=-15.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=35.0, lq=0.25, pressure="normal"),
            state=SystemState.BAD_RF,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now + 3.0,
        )

        self.assertEqual(out["incident_class"], "rf_collapse")
        self.assertEqual(out["brain_health"]["status"], "confirmed")
        self.assertIn("RF collapse confirmed", out["brain_health"]["user_message"])

    def test_rf_collapse_keeps_ownership_when_spike_detector_also_fires(self):
        cfg = Arch4Config(min_action_interval_s=0.0, emergency_sinr_floor=0.0)
        td = tempfile.TemporaryDirectory()
        mem = Memory(cfg, memory_dir=td.name)
        hist = HistoricalJournal(cfg)
        assessor = RuntimeIncidentTracker(cfg, mem, hist)
        self.addCleanup(td.cleanup)
        now = time.time()

        _assess(assessor,
            modem=_IncidentAssessor_make_modem(sinr_db=12.0, rsrp_dbm=-82.0, rsrq_db=-7.0, rx_bytes_per_sec=0.0),
            computed=_IncidentAssessor_make_computed(rtt=28.0, lq=0.72, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now,
        )
        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(
                ca_active=False,
                ca_signature="B3",
                selected_band_set=(3,),
                sinr_db=-12.0,
                rsrp_dbm=-110.0,
                rsrq_db=-15.0,
                rx_bytes_per_sec=0.0,
            ),
            computed=_IncidentAssessor_make_computed(rtt=28.0, lq=0.72, pressure="normal"),
            state=SystemState.WORKING,
            health_status={"stage": "healthy", "overall_ok": True, "action_hint": "none"},
            now=now + 3.0,
        )

        self.assertEqual(out["incident_class"], "rf_collapse")

    def test_ca_observe_does_not_escalate_without_health_impact(self):
        assessor, _, _ = self._assessor()
        now = time.time()

        _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=False, ca_signature="B3", sinr_db=15.0, rx_bytes_per_sec=500_000.0),
            computed=_IncidentAssessor_make_computed(rtt=90.0, lq=0.45, pressure="busy", cart_ld=0),
            state=SystemState.WORKING,
            health_status={"stage": "warning", "overall_ok": True, "action_hint": "observe"},
            now=now,
        )
        out = _assess(assessor,
            modem=_IncidentAssessor_make_modem(ca_active=False, ca_signature="B3", sinr_db=6.0, rx_bytes_per_sec=500_000.0),
            computed=_IncidentAssessor_make_computed(rtt=90.0, lq=0.45, pressure="busy", cart_ld=0),
            state=SystemState.WORKING,
            health_status={"stage": "warning", "overall_ok": True, "action_hint": "observe"},
            now=now + 3.0,
        )

        self.assertEqual(out["incident_class"], "ca_capacity_reduced_observe")
        self.assertFalse(out["operator_incident"])


# ── Incident recovery policy ────────────────────────────────────

def _IncidentRecoveryPolicy_make_modem(
    *,
    bands=(3,),
    ca_signature="B3",
    ca_active=False,
    rx_bytes_per_sec=2_000_000.0,
    tx_bytes_per_sec=200_000.0,
    sinr_db=14.0,
) -> ModemInput:
    carriers = [
        CarrierInfo(
            band=int(b),
            earfcn=1200 + i,
            pci=100,
            bandwidth_mhz=20.0,
            sinr_db=sinr_db,
            rsrp_dbm=-88.0,
            state=1 if i else 0,
            is_pcell=(i == 0),
        )
        for i, b in enumerate(bands)
    ]
    primary = int(bands[0])
    return ModemInput(
        sinr_db=sinr_db,
        rsrp_dbm=-88.0,
        rsrq_db=-9.0,
        rssi_dbm=-68.0,
        mcs_dl=18,
        mcs_ul=10,
        cqi=9,
        ri=2,
        carriers=carriers,
        ca_active=bool(ca_active),
        ca_band_signature=ca_signature,
        primary_band=primary,
        pci=100,
        earfcn=1200,
        enb_id=1,
        radio_connected=True,
        session_uptime_s=300.0,
        rx_bytes_per_sec=rx_bytes_per_sec,
        tx_bytes_per_sec=tx_bytes_per_sec,
        timestamp=time.time(),
    )


def _IncidentRecoveryPolicy_make_computed(*, rtt: float = 70.0) -> ComputedSignals:
    return ComputedSignals(
        rtt_ms=rtt,
        rtt_ema=rtt,
        rtt_sliding_avg=rtt,
        rtt_baseline=45.0,
        rtt_elevated=False,
        rtt_alarm=False,
        cq_cell=0.62,
    )


class TestIncidentRecoveryPolicy(unittest.TestCase):
    def _decision(self, *, state: SystemState = SystemState.DEGRADED, backend: str = "b818") -> DecisionEngine:
        cfg = Arch4Config(
            min_action_interval_s=0.0,
            autonomous_connect_settle_s=0.0,
            radio_change_settle_s=0.0,
            modem_backend=backend,
        )
        td = tempfile.TemporaryDirectory()
        mem = Memory(cfg, memory_dir=td.name)
        sm = TestControllerPostureAuthority(cfg, mem)
        sm.private_state_for_test.state = state
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
        decision.note_health_status({"stage": "healthy", "action_hint": "none"})
        return decision

    def test_post_action_instability_holds_degraded_ladder(self):
        decision = self._decision()
        decision.note_incident_assessment(
            {
                "incident_class": "post_action_instability",
                "recommended_next_step": "settle_backoff",
                "suppress_exploration": True,
                "self_induced": True,
            }
        )

        action = _decide(decision, _IncidentRecoveryPolicy_make_modem(), _IncidentRecoveryPolicy_make_computed())

        self.assertIsNone(action)
        self.assertEqual(decision.posture_authority.private_state_for_test.degraded_actions_tried, 0)

    def test_radio_ca_loss_prefers_restore_ca_before_generic_degraded_recovery(self):
        decision = self._decision()
        _append_scoped_action(decision.memory,
            ActionRecord(
                timestamp=time.time() - 120.0,
                sinr_db=15.0,
                rsrq_db=-9.0,
                mcs_dl=18,
                ca_signature="B3",
                rtt_sliding_avg=60.0,
                rtt_ms=60.0,
                lever=LeverType.BAND_SELECTION,
                action_params={"bands": [1, 3, 20]},
                success=True,
                throughput_before_mbps=20.0,
                throughput_after_mbps=35.0,
                sinr_after=14.0,
                ca_signature_after="B1+B3+B20",
                improvement=15.0,
            )
        )
        decision.note_incident_assessment(
            {
                "incident_class": "radio_ca_loss",
                "recommended_next_step": "recover_ca",
                "recovery_candidate": True,
            }
        )
        intelligence, derived = _publish_smart_loop_for_inputs(
            decision,
            _IncidentRecoveryPolicy_make_modem(
                bands=(3,), ca_signature="B3", ca_active=False
            ),
            _IncidentRecoveryPolicy_make_computed(),
            target_band=3,
            target_bands=(1, 3, 20),
            reason_code="restore_ca_candidate",
            availability_intent="restore_ca",
        )

        action = decision.decide(intelligence, derived)

        self.assertIsNotNone(action)
        self.assertEqual(action.intent, "smart_loop_candidate")
        self.assertEqual(action.params.get("ca_pool"), [1, 3, 20])
        self.assertEqual(decision.posture_authority.private_state_for_test.degraded_actions_tried, 0)

    def test_mikrotik_radio_ca_loss_does_not_restore_ca_with_band_write(self):
        decision = self._decision(backend="mikrotik")
        _append_scoped_action(decision.memory,
            ActionRecord(
                timestamp=time.time() - 120.0,
                sinr_db=15.0,
                rsrq_db=-9.0,
                mcs_dl=18,
                ca_signature="B3",
                rtt_sliding_avg=60.0,
                rtt_ms=60.0,
                lever=LeverType.BAND_SELECTION,
                action_params={"bands": [1, 3, 20]},
                success=True,
                throughput_before_mbps=20.0,
                throughput_after_mbps=35.0,
                sinr_after=14.0,
                ca_signature_after="B1+B3+B20",
                improvement=15.0,
            )
        )
        decision.note_incident_assessment(
            {
                "incident_class": "radio_ca_loss",
                "recommended_next_step": "recover_ca",
                "recovery_candidate": True,
            }
        )

        action = _decide(decision,
            _IncidentRecoveryPolicy_make_modem(bands=(3,), ca_signature="B3", ca_active=False),
            _IncidentRecoveryPolicy_make_computed(),
        )

        self.assertIsNone(action)
        self.assertEqual(decision.posture_authority.private_state_for_test.degraded_actions_tried, 0)
        events = decision.drain_runtime_events()
        self.assertTrue(any(e.get("kind") == "smart_loop_strategy_unavailable" for e in events))

    def test_post_action_instability_holds_congested_mitigation(self):
        decision = self._decision(state=SystemState.CONGESTED)
        decision.note_incident_assessment(
            {
                "incident_class": "post_action_instability",
                "recommended_next_step": "settle_backoff",
                "suppress_exploration": True,
                "self_induced": True,
            }
        )

        action = _decide(decision, _IncidentRecoveryPolicy_make_modem(), _IncidentRecoveryPolicy_make_computed())

        self.assertIsNone(action)
        self.assertFalse(decision.posture_authority.private_state_for_test.congested_action_taken)

    def test_immediate_recovery_incident_holds_keepalive_traffic_presence(self):
        held = should_hold_action_for_incident(
            assessment={
                "incident_class": "rf_collapse",
                "recommended_next_step": "emergency_recovery",
                "severity": "critical",
                "suppress_exploration": True,
                "recovery_candidate": True,
            },
            action=LeverAction(
                lever=LeverType.TRAFFIC_PRESENCE,
                params={"keepalive_interval_s": 5},
                reason="Idle keepalive: RTT EMA 208ms suggests RRC release",
            ),
        )

        self.assertTrue(held)

    def test_immediate_recovery_incident_holds_cell_optimization_hook(self):
        held = should_hold_action_for_incident(
            assessment={
                "incident_class": "rf_collapse",
                "recommended_next_step": "emergency_recovery",
                "severity": "critical",
                "suppress_exploration": True,
                "recovery_candidate": True,
            },
            action=LeverAction(
                lever=LeverType.BAND_SELECTION,
                intent="cell_optimization_hook",
                params={"bands": [3]},
                reason="Cell opt toward cached better PCI",
            ),
        )

        self.assertTrue(held)

    def test_critical_incident_holds_generic_working_explore_lane(self):
        decision = self._decision(state=SystemState.WORKING)
        decision.note_incident_assessment(
            {
                "incident_class": "rf_collapse",
                "recommended_next_step": "emergency_recovery",
                "severity": "critical",
                "suppress_exploration": True,
                "recovery_candidate": True,
            }
        )
        decision.historical.get_best_bandset_for_hour = lambda *args, **kwargs: None
        decision.historical.has_data = lambda hour: True
        decision.historical.what_worked = lambda hour: [20]
        decision.historical.get_avg_throughput = lambda hour, sig: 50.0
        decision.historical.is_cliff_ahead = lambda *args, **kwargs: False
        intelligence, derived = _publish_smart_loop_for_inputs(
            decision,
            _IncidentRecoveryPolicy_make_modem(
                bands=(3,),
                ca_signature="B3",
                ca_active=True,
                rx_bytes_per_sec=1_000_000.0,
            ),
            _IncidentRecoveryPolicy_make_computed(rtt=200.0),
            target_band=20,
            target_bands=(3, 20),
            reason_code="explore_candidate",
            availability_intent="explore",
        )

        action = decision.decide(intelligence, derived)

        self.assertIsNone(action)
        events = decision.drain_runtime_events()
        blocked = [e for e in events if e.get("kind") == "action_availability_blocked"]
        self.assertTrue(blocked)
        self.assertEqual(blocked[0].get("reason_code"), "incident_hold")

    def test_post_fault_cooldown_holds_generic_working_explore_lane(self):
        decision = self._decision(state=SystemState.WORKING)
        decision.note_incident_assessment(
            {
                "incident_class": "healthy",
                "recommended_next_step": "none",
                "severity": "info",
                "suppress_exploration": False,
            }
        )
        decision.posture_authority.private_state_for_test.post_fault_class = "BAD_RF"
        decision.posture_authority.private_state_for_test.post_fault_until = time.time() + 120.0
        decision.historical.get_best_bandset_for_hour = lambda *args, **kwargs: None
        decision.historical.has_data = lambda hour: True
        decision.historical.what_worked = lambda hour: [20]
        decision.historical.get_avg_throughput = lambda hour, sig: 50.0
        decision.historical.is_cliff_ahead = lambda *args, **kwargs: False
        intelligence, derived = _publish_smart_loop_for_inputs(
            decision,
            _IncidentRecoveryPolicy_make_modem(
                bands=(3,),
                ca_signature="B3",
                ca_active=True,
                rx_bytes_per_sec=1_000_000.0,
            ),
            _IncidentRecoveryPolicy_make_computed(rtt=200.0),
            target_band=20,
            target_bands=(3, 20),
            reason_code="explore_candidate",
            availability_intent="explore",
        )

        action = decision.decide(intelligence, derived)

        self.assertIsNone(action)
        events = decision.drain_runtime_events()
        blocked = [e for e in events if e.get("kind") == "action_availability_blocked"]
        self.assertTrue(blocked)
        self.assertEqual(blocked[0].get("reason_code"), "post_fault_cooldown")

    def test_auto_action_cooldown_blocks_fault_recovery_inside_brain_trace(self):
        decision = self._decision(state=SystemState.BAD_RF)
        decision.posture_authority.private_state_for_test.emergency_class = "BAD_RF"
        decision.note_incident_assessment(
            {
                "incident_class": "rf_collapse",
                "recommended_next_step": "emergency_recovery",
                "severity": "critical",
                "suppress_exploration": False,
                "recovery_candidate": True,
            }
        )
        decision.note_constraint_vector(
            {
                "supports_active_control": True,
                "auto_action_lockout_active": True,
                "auto_action_lockout_remaining_s": 32,
                "incident_assessment": {
                    "incident_class": "rf_collapse",
                    "recommended_next_step": "emergency_recovery",
                    "severity": "critical",
                    "suppress_exploration": False,
                    "recovery_candidate": True,
                },
            }
        )

        modem = _IncidentRecoveryPolicy_make_modem(
            bands=(3,),
            ca_signature="B3",
            ca_active=False,
            sinr_db=-5.0,
            rx_bytes_per_sec=0.0,
            tx_bytes_per_sec=0.0,
        )
        action = LeverAction(
            lever=LeverType.PBAND,
            params={"pband": 20, "ca_pool": [1, 3, 20]},
            reason="BAD RF recovery",
            intent="fault_monitoring",
        )

        guarded = decision._apply_action_availability(
            action=action,
            modem=modem,
            state=SystemState.BAD_RF,
            now=time.time(),
            source="test_action_cooldown",
            check_objective=True,
            emit_trace=True,
        )

        self.assertIsNone(guarded)
        trace = decision.get_last_decision_trace()
        self.assertIn(trace.get("final"), (None, "none"))
        self.assertTrue(
            any(
                step.get("gate") == "action_availability"
                and step.get("reason_code") == "action_cooldown"
                for step in trace.get("steps", [])
            )
        )

    def test_escape_probe_bypasses_post_fault_cooldown_when_starved(self):
        decision = self._decision(state=SystemState.WORKING)
        decision.note_incident_assessment(
            {
                "incident_class": "healthy",
                "recommended_next_step": "none",
                "severity": "info",
                "suppress_exploration": False,
            }
        )
        decision.posture_authority.private_state_for_test.post_fault_class = "BAD_RF"
        decision.posture_authority.private_state_for_test.post_fault_until = time.time() + 120.0
        decision.aggressiveness._current_posture = SimpleNamespace(
            quality_confidence=0.25,
            posture_modifier=0.55,
        )
        now = time.time()
        decision._exploration_suppression_events = (
            [(now - 60.0, "post_fault_cooldown", "explore_band_probe")] * 40
            + [(now - 60.0, "rf_cliff_cooldown", "explore_band_probe")] * 20
        )
        intelligence, derived = _publish_smart_loop_for_inputs(
            decision,
            _IncidentRecoveryPolicy_make_modem(
                bands=(3,),
                ca_signature="B3",
                ca_active=True,
                rx_bytes_per_sec=0.0,
                tx_bytes_per_sec=0.0,
            ),
            _IncidentRecoveryPolicy_make_computed(rtt=45.0),
            target_band=20,
            target_bands=(3, 20),
            reason_code="explore_candidate",
            availability_intent="explore",
        )

        action = decision.decide(intelligence, derived)

        events = decision.drain_runtime_events()
        self.assertIsNone(action)
        blocked = [e for e in events if e.get("kind") == "action_availability_blocked"]
        self.assertTrue(blocked)
        self.assertEqual(blocked[0].get("reason_code"), "post_fault_cooldown")

    def test_escape_probe_never_bypasses_incident_exploration_hold(self):
        decision = self._decision(state=SystemState.WORKING)
        decision.note_incident_assessment(
            {
                "incident_class": "post_action_instability",
                "recommended_next_step": "settle_backoff",
                "severity": "warning",
                "suppress_exploration": True,
            }
        )
        decision.aggressiveness._current_posture = SimpleNamespace(
            quality_confidence=0.25,
            posture_modifier=0.55,
        )
        now = time.time()
        decision._exploration_suppression_events = (
            [(now - 60.0, "post_fault_cooldown", "explore_band_probe")] * 40
            + [(now - 60.0, "rf_cliff_cooldown", "explore_band_probe")] * 20
        )
        intelligence, derived = _publish_smart_loop_for_inputs(
            decision,
            _IncidentRecoveryPolicy_make_modem(
                bands=(3,),
                ca_signature="B3",
                ca_active=True,
                rx_bytes_per_sec=0.0,
                tx_bytes_per_sec=0.0,
            ),
            _IncidentRecoveryPolicy_make_computed(rtt=45.0),
            target_band=20,
            target_bands=(3, 20),
            reason_code="explore_candidate",
            availability_intent="explore",
        )

        action = decision.decide(intelligence, derived)

        self.assertIsNone(action)
        events = decision.drain_runtime_events()
        blocked = [e for e in events if e.get("kind") == "action_availability_blocked"]
        self.assertTrue(blocked)
        self.assertEqual(blocked[0].get("reason_code"), "incident_hold")

    def test_escape_probe_rf_cliff_requires_stable_sinr_ticks(self):
        decision = self._decision(state=SystemState.WORKING)
        decision.note_incident_assessment(
            {
                "incident_class": "healthy",
                "recommended_next_step": "none",
                "severity": "info",
                "suppress_exploration": False,
            }
        )
        decision.posture_authority.private_state_for_test.rf_cliff_event_at = time.time()
        decision.posture_authority.private_state_for_test.rf_cliff_event_reason = "test_rf_cliff"
        decision.aggressiveness._current_posture = SimpleNamespace(
            quality_confidence=0.25,
            posture_modifier=0.55,
        )
        now = time.time()
        decision._exploration_suppression_events = (
            [(now - 60.0, "post_fault_cooldown", "explore_band_probe")] * 40
            + [(now - 60.0, "rf_cliff_cooldown", "explore_band_probe")] * 20
        )
        intelligence, derived = _publish_smart_loop_for_inputs(
            decision,
            _IncidentRecoveryPolicy_make_modem(
                bands=(3,),
                ca_signature="B3",
                ca_active=True,
                sinr_db=8.0,
                rx_bytes_per_sec=0.0,
                tx_bytes_per_sec=0.0,
            ),
            _IncidentRecoveryPolicy_make_computed(rtt=45.0),
            target_band=20,
            target_bands=(3, 20),
            reason_code="explore_candidate",
            availability_intent="explore",
        )

        first = decision.decide(intelligence, derived)
        self.assertIsNone(first)

        second = decision.decide(intelligence, derived)
        events = decision.drain_runtime_events()
        self.assertIsNone(second)
        blocked = [e for e in events if e.get("kind") == "action_availability_blocked"]
        self.assertTrue(blocked)
        self.assertEqual(blocked[0].get("reason_code"), "rf_cliff_cooldown")

    def test_radio_ca_loss_prefers_restore_ca_before_generic_congested_recovery(self):
        decision = self._decision(state=SystemState.CONGESTED)
        _append_scoped_action(decision.memory,
            ActionRecord(
                timestamp=time.time() - 120.0,
                sinr_db=15.0,
                rsrq_db=-9.0,
                mcs_dl=18,
                ca_signature="B3",
                rtt_sliding_avg=60.0,
                rtt_ms=60.0,
                lever=LeverType.BAND_SELECTION,
                action_params={"bands": [1, 3, 20]},
                success=True,
                throughput_before_mbps=20.0,
                throughput_after_mbps=35.0,
                sinr_after=14.0,
                ca_signature_after="B1+B3+B20",
                improvement=15.0,
            )
        )
        decision.note_incident_assessment(
            {
                "incident_class": "radio_ca_loss",
                "recommended_next_step": "recover_ca",
                "recovery_candidate": True,
            }
        )
        intelligence, derived = _publish_smart_loop_for_inputs(
            decision,
            _IncidentRecoveryPolicy_make_modem(
                bands=(3,), ca_signature="B3", ca_active=False
            ),
            _IncidentRecoveryPolicy_make_computed(),
            target_band=3,
            target_bands=(1, 3, 20),
            reason_code="restore_ca_candidate",
            availability_intent="restore_ca",
        )

        action = decision.decide(intelligence, derived)

        self.assertIsNotNone(action)
        self.assertEqual(action.intent, "smart_loop_candidate")
        self.assertEqual(action.params.get("ca_pool"), [1, 3, 20])
        self.assertFalse(decision.posture_authority.private_state_for_test.congested_action_taken)

    def test_post_action_instability_holds_congestion_storm_recovery(self):
        decision = self._decision(state=SystemState.E_CONGESTION_STORM)
        decision.posture_authority.private_state_for_test.emergency_class = "E_CONGESTION_STORM"
        decision.posture_authority.private_state_for_test.emergency_timeout_at = time.time() - 1.0
        decision.note_incident_assessment(
            {
                "incident_class": "post_action_instability",
                "recommended_next_step": "settle_backoff",
                "suppress_exploration": True,
                "self_induced": True,
            }
        )

        action = _decide(decision, _IncidentRecoveryPolicy_make_modem(), _IncidentRecoveryPolicy_make_computed())

        self.assertIsNone(action)
        events = decision.drain_runtime_events()
        hold = [e for e in events if e.get("kind") == "incident_recovery_hold"]
        self.assertTrue(hold)
        self.assertEqual(hold[0].get("state"), "E_CONGESTION_STORM")

    def test_radio_ca_loss_prefers_restore_ca_before_congestion_storm_timeout_restart(self):
        decision = self._decision(state=SystemState.E_CONGESTION_STORM)
        decision.posture_authority.private_state_for_test.emergency_class = "E_CONGESTION_STORM"
        decision.posture_authority.private_state_for_test.emergency_timeout_at = time.time() - 1.0
        _append_scoped_action(decision.memory,
            ActionRecord(
                timestamp=time.time() - 120.0,
                sinr_db=15.0,
                rsrq_db=-9.0,
                mcs_dl=18,
                ca_signature="B3",
                rtt_sliding_avg=60.0,
                rtt_ms=60.0,
                lever=LeverType.BAND_SELECTION,
                action_params={"bands": [1, 3, 20]},
                success=True,
                throughput_before_mbps=20.0,
                throughput_after_mbps=35.0,
                sinr_after=14.0,
                ca_signature_after="B1+B3+B20",
                improvement=15.0,
            )
        )
        decision.note_incident_assessment(
            {
                "incident_class": "radio_ca_loss",
                "recommended_next_step": "recover_ca",
                "recovery_candidate": True,
            }
        )

        action = _decide(decision, _IncidentRecoveryPolicy_make_modem(bands=(3,), ca_signature="B3", ca_active=False), _IncidentRecoveryPolicy_make_computed())

        self.assertIsNotNone(action)
        self.assertEqual(action.intent, "restore_ca")
        self.assertEqual(action.params.get("bands"), [1, 3, 20])
        self.assertEqual(int(decision.posture_authority.private_state_for_test.emergency_recovery_step or 0), 1)



if __name__ == "__main__":
    import unittest
    unittest.main()
