"""Standalone NGUI visual and interaction demo."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import sys
from types import MappingProxyType

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .control_availability import (
    PHYSICAL_CONTROLS,
    WORKFLOW_CONTROL,
)
from .adapters.owner_register import adapt_owner_register
from .chart_model import (
    DEFAULT_CHART_WINDOW_SECONDS,
    ChartWindowRequest,
    NGUIChartHistory,
    NGUIChartRegister,
)
from .edit_register import NGUIEditRegister
from .input_registers import (
    InputRegisterSnapshot,
    owner_input_batch_fingerprint,
)
from .interactions import NGUIInteractionRegister
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from .presentation import NGUIPresentationAuthority
from .registry import NGUI_INPUT_SOURCE_REGISTRY
from .renderer import NGUIWindow
from .types import (
    FieldKind,
    PageId,
    SourceOwner,
)


def _demo_control_availability(*, enabled: bool) -> dict:
    reason = "" if enabled else "Connect to a confirmed live modem first."
    return {
        control: {
            "enabled": bool(enabled),
            "reason": reason,
        }
        for control in (*PHYSICAL_CONTROLS, WORKFLOW_CONTROL)
    }


def _demo_band_context_evidence() -> dict[str, object]:
    dimensions = {
        "performance_capacity": {"state": "available", "text": "18.2", "grade": 2},
        "performance_latency": {"state": "available", "text": "42.0", "grade": 1},
        "performance_jitter": {"state": "available", "text": "5.0", "grade": 1},
        "performance_communication_quality": {"state": "available", "text": "0.78", "grade": 1},
        "performance_internet_availability": {"state": "available", "text": "100", "grade": 0},
        "performance_overall_outcome": {"state": "available", "text": "+2.0", "grade": 2},
    }

    def column(
        role: str,
        label: str,
        maturity: str,
        *,
        graded: bool = False,
    ) -> dict[str, object]:
        return {
            "role": role,
            "label": label,
            "target": "B3 / B1+B3+B20",
            "scope_key": "demo-b818-b3-cell-2050",
            "cell_id": 2050,
            "pci": 153,
            "earfcn": 1651,
            "nr_band": None,
            "nr_cell_id": None,
            "nr_pci": None,
            "nr_arfcn": None,
            "evidence_count": 3,
            "opened_at": 1786546800.0,
            "due_at": 1786547400.0,
            "closed_at": 1786547402.0 if graded else 0.0,
            "maturity": maturity,
            "reason": "demo owner evidence",
            "dimensions": {
                key: {
                    **value,
                    "grade": value["grade"] if graded else None,
                }
                for key, value in dimensions.items()
            },
        }

    timeline = [column("current", "CURRENT · 12 AUG 14:10–14:20", "collecting")]
    recent = column(
        "recent_1",
        "12 AUG 14:00–14:10",
        "completed",
        graded=True,
    )
    recent["dimensions"] = {
        **recent["dimensions"],
        "performance_capacity": {
            "state": "available",
            "text": "18.2",
            "grade": 2,
        },
        "performance_latency": {
            "state": "available",
            "text": "42.0",
            "grade": 1,
        },
        "performance_jitter": {
            "state": "available",
            "text": "5.0",
            "grade": 1,
        },
        "performance_communication_quality": {
            "state": "available",
            "text": "0.78",
            "grade": 1,
        },
        "performance_internet_availability": {
            "state": "available",
            "text": "100",
            "grade": 0,
        },
    }
    return {
        "schema": "NewHorizonBandContextPresentation.v14",
        "current_timeline": timeline,
        "recent_assessments": [recent],
        "history_note": "1 completed 10-minute window · oldest to newest.",
    }


def _demo_intelligence_impact(*, populated: bool) -> dict[str, object]:
    return {
        "schema": "NewHorizonIntelligenceImpact.v3",
        "window_started_at": 1785337802.0 if populated else 0.0,
        "generated_at": 1786547402.0 if populated else 1.0,
        "counts": {
            "improved": 8 if populated else 0,
            "neutral": 3 if populated else 0,
            "worse": 1 if populated else 0,
            "not_proven": 2 if populated else 0,
        },
        "forecast_direction": {
            "correct": 9 if populated else 0,
            "assessed": 12 if populated else 0,
            "accuracy_pct": 75.0 if populated else None,
        },
        "recent_outcomes": [],
        "positive_contexts": [],
        "negative_contexts": [],
    }


def _sample_value(field_id: str, kind: FieldKind, *, visual_profile: str = "live"):
    gib = 1024 ** 3
    tib = 1024 ** 4
    if visual_profile == "startup":
        startup_overrides = {
            "profile.available_models": [],
            "profile.models_by_manufacturer": {},
            "profile.detected_model": "---",
            "profile.selected_profile": "---",
            "profile.runtime_mode": "---",
            "profile.license_status": "",
            "profile.login_status": "",
            "device.health_status": "---",
            "device.persistence_status": "---",
            "device.modem_status": True,
            "radio.header_badge": "4G",
            "radio.network_type": "LTE (4G)",
            "device.access_type": "N/A",
            "device.wifi_24ghz_enabled": None,
            "device.wifi_5ghz_enabled": None,
            "device.wifi_6ghz_enabled": None,
            "event.recent": "",
            "intelligence.forecast": {
                "schema": "NewHorizonForecastPresentation.v12",
                "state": "collecting",
                "comparison": "Collecting evidence",
                "meaning": (
                    "No comparable completed assessment for this selected "
                    "option yet."
                ),
                "metrics": {
                    "capacity_points": None,
                    "estimated_aq": None,
                    "latency_ms": None,
                    "jitter_ms": None,
                    "communication_quality": None,
                    "internet_availability_pct": None,
                },
                "evidence_count": 0,
                "confidence_pct": 0.0,
                "accuracy_pct": None,
                "accuracy_assessments": 0,
                "candidates": [],
            },
            "intelligence.impact": _demo_intelligence_impact(populated=False),
            "intelligence.learning_cycle_history": {
                "schema": "NewHorizonLearningCycleHistory.v12",
                "rows": [],
            },
            "intelligence.referee_summary": {
                "schema": "NewHorizonRefereeSummary.v6",
                "current": "Collecting current 10-minute window",
                "latest_completed": None,
                "history_note": "No completed Referee assessment yet.",
            },
            "action.control_availability": _demo_control_availability(enabled=False),
        }
        for capability in (
            "capability.pband_control",
            "capability.ca_pool_control",
            "capability.band_lock",
            "capability.cell_lock",
            "capability.restart",
            "capability.max_tcp_payload",
            "capability.ttl_ip",
            "capability.keepalive_enabled",
            "capability.dl_256qam",
            "capability.pci_lock",
            "capability.ul_64qam",
            "capability.keepalive",
            "capability.ue_category",
            "capability.ue_info",
            "capability.traffic_volume",
            "capability.ca_details",
            "capability.neighbors",
            "capability.plmn_read",
            "capability.band_priority",
        ):
            startup_overrides[capability] = "PROBING"
        if field_id in startup_overrides:
            return startup_overrides[field_id]
        if kind is FieldKind.LIST:
            return []
        if kind is FieldKind.MAPPING:
            return {}
        return None

    overrides = {
        "profile.available_models": ["Huawei B818-263", "Huawei B535-333 HiLink", "MikroTik Chateau LTE12"],
        "profile.models_by_manufacturer": {
            "Huawei": ["Huawei B818-263", "Huawei B535-333 HiLink"],
            "MikroTik": ["MikroTik Chateau LTE12"],
        },
        "profile.detected_model": "Huawei B818-263",
        "profile.license_status": "Licensed",
        "device.health_status": "OK",
        "device.persistence_status": "OK",
        "device.modem_status": True,
        "runtime.state": "WORKING",
        "connection.user_link_state": "Connected",
        "connection.quality_pct": 66.0,
        "connection.led_level": 4,
        "radio.network_type": "LTE (4G)",
        "radio.pband": 3,
        "radio.ca_pool": [1, 3, 7, 20],
        "radio.active_bands": [1, 3, 7, 20],
        "radio.frequency_set": [1, 3, 7, 20],
        "radio.dl_bandwidth_mhz": 20.0,
        "radio.ul_bandwidth_mhz": 20.0,
        "radio.dl_frequency_mhz": 1842.5,
        "radio.ul_frequency_mhz": 1747.5,
        "radio.pci": 153,
        "radio.earfcn": 1795,
        "radio.cell_id": 232706,
        "radio.enb_id": 909,
        "radio.rrc": "Connected",
        "radio.transmission_mode": "TM[4] (CL-MIMO)",
        "provider.name": "A1 Serbia",
        "provider.plmn": "22001",
        "device.model": "B818-263",
        "device.serial": "VNNDW21224003358",
        "device.imei": "867193048169697",
        "device.imsi": "220052782517944",
        "device.firmware": "11.0.1.1(H220SP1C11)",
        "device.webui": "11.0.1.1(W2SP2C11)",
        "device.access_type": "Huawei HTTP API",
        "device.lan_mac": "00:11:22:33:44:55",
        "device.wan_ip": "10.122.162.77",
        "device.antenna": "External",
        "radio.tac": 112,
        "rf.sinr_db": 7.0,
        "rf.rsrp_dbm": -71.0,
        "rf.rsrq_db": -10.0,
        "rf.rssi_dbm": -51.0,
        "rf.tx_power": "13 dBm",
        "scheduler.cqi0": 12,
        "scheduler.cqi1": 10,
        "scheduler.mcs_dl0": 18,
        "scheduler.mcs_dl1": 12,
        "scheduler.mcs_ul": 9,
        "scheduler.me0": 6,
        "scheduler.me1": 4,
        "scheduler.me_ul": 4,
        "scheduler.qam_dl0": "64QAM",
        "scheduler.qam_dl1": "16QAM",
        "scheduler.qam_ul": "16QAM",
        "nr.mode": None,
        "nr.band": None,
        "nr.pci": None,
        "nr.arfcn": None,
        "nr.cell_id": None,
        "nr.bandwidth_mhz": None,
        "nr.sinr_db": None,
        "nr.rsrp_dbm": None,
        "nr.rsrq_db": None,
        "quality.rtt_ms": 21.0,
        "quality.congestion_pct": 24.0,
        "quality.jitter_trimmed_ms": 1.0,
        "tuning.congestion_rsrq_weight": 50.0,
        "tuning.congestion_rtt_weight": 50.0,
        "tuning.jitter_scale_ms": 5.0,
        "tuning.jitter_gain": 3.0,
        "quality.ca_ld_pct": 100.0,
        "quality.cq_pct": 66.0,
        "traffic.dl_mbps": 4.2,
        "traffic.ul_mbps": 0.8,
        "traffic.budget_day_used_bytes": int(1.57 * gib),
        "traffic.budget_month_used_bytes": 291 * gib,
        "traffic.budget_total_used_bytes": int(2.10 * tib),
        "traffic.budget_left_bytes": 209 * gib,
        "traffic.budget_cycle_day": 1,
        "traffic.budget_limit_bytes": 500 * gib,
        "traffic.state_detail": "unknown",
        "traffic.invalid_reason": None,
        "capability.session_lte_status": "proven",
        "nr.ri": None, "nr.cqi": None, "nr.mcs_dl": None, "nr.mcs_ul": None,
        "nr.qam_dl": None, "nr.qam_ul": None, "nr.num_rbs": None,
        "nr.tx_power_dbm": None, "nr.ul_bandwidth_mhz": None,
        "nr.dl_frequency_mhz": None, "nr.ul_frequency_mhz": None,
        "capability.session_lte_bands": [1, 3, 7, 20],
        "capability.session_capability_comparison": "match",
        "capability.session_capability_id": "session-band:demo",
        "capability.sms_supported": True,
        "capability.pband_control": "YES",
        "capability.ca_pool_control": "YES",
        "capability.band_lock": "YES",
        "capability.cell_lock": "YES",
        "capability.restart": "YES",
        "capability.max_tcp_payload": "OK",
        "capability.ttl_ip": "OK",
        "capability.keepalive_enabled": "OK",
        "capability.dl_256qam": "OK",
        "capability.pci_lock": "OK",
        "capability.ul_64qam": "OK",
        "capability.keepalive": "OK",
        "capability.ue_category": "19",
        "capability.ue_info": "OK",
        "capability.traffic_volume": "OK",
        "capability.ca_details": "OK",
        "capability.neighbors": "OK",
        "capability.plmn_read": "OK",
        "capability.band_priority": "OK",
        "site_report.selected_window": "1d",
        "site_report.status": "Idle",
        "site_report.progress_pct": 0.0,
        "event.conclusion": "Connection is stable. Monitoring only.",
        "event.recent": (
            "15:58:04 Site report generated for 1d.\n"
            "15:58:06 Band sequence completed."
        ),
        "event.decision_summary_context": {
            "mode": "hold_for_more_evidence",
            "operating_stance": "observe_monitor",
            "prediction_confidence_label": "medium",
            "recent_action_count": 2,
            "sample_count": 8,
            "profile_confidence": 0.6,
        },
        "antenna_guide.active_action": "",
        "antenna_guide.actions_enabled": True,
        "antenna_guide.score_pct": 74.0,
        "antenna_guide.confidence": "medium",
        "antenna_guide.trend": "stable",
        "antenna_guide.hint": "Move slowly. Finish Device Check first.",
        "antenna_guide.monitor_state": "Idle",
        "command.antenna_guide_status": "---",
        "antenna_guide.contributors": {"RF": 64, "RI": 72, "CA": 100, "LAT": 60},
        "antenna_guide.meter_pct": 74.0,
        "antenna_guide.current_check": "decision ready",
        "antenna_guide.saved_setup": "C25@v2",
        "antenna_guide.new_result": "C29",
        "antenna_guide.saved_setup_ready": True,
        "antenna_guide.action": "auto decision",
        "antenna_guide.check_status": "checking result",
        "antenna_guide.check_progress": 100.0,
        "antenna_guide.next_step": "auto decision",
        "antenna_guide.check_trust": "estimated",
        "antenna_guide.summary": "Some readings improved, but not enough.",
        "smartness.status": "active",
        "smartness.confidence": "medium",
        "smartness.evidence": 297380,
        "smartness.candidates": 1,
        "smartness.slots_window": 137,
        "smartness.blockers": [],
        "smartness.prediction_posture": "normal",
        "smartness.prior": "weighted_historical_seed",
        "smartness.brain_state": "WORKING",
        "smartness.mode": "prediction_available",
        "smartness.intent": "observe",
        "smartness.budget": "available",
        "smartness.selected_action": "none",
        "smartness.bouncer": "idle",
        "smartness.coverage": "covered",
        "smartness.gate_review": "Learning gates checked: completed.",
        "command.status": "Ready",
        "manual.status": "ready",
        "manual.message": "Ready",
        "manual.action_pending": False,
        "manual.action_sequence": 0,
        "manual.core_received_ms": None,
        "manual.modem_work_started_ms": None,
        "manual.pband_command_sent_ms": None,
        "manual.ca_pool_command_sent_ms": None,
        "manual.target_first_verified_ms": None,
        "manual.next_chart_sample_ms": None,
        "manual.target_sample_sequence": None,
        "manual.chart_sample_sequence": None,
        "action.control_availability": _demo_control_availability(enabled=True),
        "manual.chart_updated": False,
        "manual.lifecycle": {
            "schema_version": "ngui_action_lifecycle_projection.v3",
            "owner": "ngui.action_lifecycle_projection",
            "source_authority": "arch4.command_transactions.CommandAuthority",
            "status": "ready",
        },
        "manual.command_received_timing": "---",
        "manual.internet_pause_timing": "---",
        "command.manual_transaction": {},
        "command.ping_status": "Downstream health monitor is off.",
        "command.sms_message": (
            "+38164123456: maintenance complete [2026-07-14 08:41:03]"
        ),
        "command.band_sequence_status": "Band sequence requests are idle.",
        "command.band_schedule_status": "Band scheduling requests are idle.",
        "history.import_status": "Imported history loaded",
        "history.workflow_status": (
            "Background history complete: all historical continuity consumers acknowledged"
        ),
        "history.prior_source": "V5.84",
        "history.predictive_window": 14,
        "history.fact_count": 18,
        "history.observation_count": 7,
        "history.brain_influence": "weighted_historical_seed",
        "history.matched_tier": ["matched prior"],
        "history.channel": "mainline",
        "history.auto_import_status": "imported",
        "intelligence.history_continuity": (
            "NATIVE CONTINUITY · 18 prior evidence · 2 checkpoints"
        ),
        "intelligence.status_messages": (
            "10:10:01 Brain started and is observing the live connection."
        ),
        "intelligence.forecast": {
            "schema": "NewHorizonForecastPresentation.v12",
            "state": "predicted",
            "comparison": "Similar-time comparison",
            "meaning": (
                "Same selected option, radio context, day type and load, "
                "observed within +/-2 clock hours."
            ),
            "metrics": {
                "capacity_points": 11.5,
                "estimated_aq": 1.8,
                "latency_ms": 70.0,
                "jitter_ms": 25.2,
                "communication_quality": 0.75,
                "internet_availability_pct": 100.0,
            },
            "evidence_count": 1,
            "confidence_pct": 7.0,
            "accuracy_pct": 72.0,
            "accuracy_assessments": 3,
            "candidates": [
                {
                    "band": "B3", "ca_pool": "B1+B3+B20", "cell_id": "2050", "pci": "255",
                    "earfcn": "1795",
                    "nr": "---",
                    "role": "selected",
                    "state": "predicted",
                    "comparison": "Similar-time comparison",
                    "meaning": (
                        "Same selected option, radio context, day type and load, "
                        "observed within +/-2 clock hours."
                    ),
                    "metrics": {
                        "capacity_points": 11.5,
                        "estimated_aq": 1.8,
                        "latency_ms": 70.0,
                        "jitter_ms": 25.2,
                        "communication_quality": 0.75,
                        "internet_availability_pct": 100.0,
                    },
                    "evidence_count": 1,
                    "confidence_pct": 7.0,
                    "accuracy_pct": 72.0,
                    "accuracy_assessments": 3,
                },
                {
                    "band": "B20", "ca_pool": "B1+B3+B20", "cell_id": "2060", "pci": "195",
                    "earfcn": "---",
                    "nr": "---",
                    "role": "option",
                    "state": "collecting",
                    "comparison": "Collecting evidence",
                    "meaning": "No comparable completed assessment yet.",
                    "metrics": {
                        "capacity_points": None,
                        "estimated_aq": None,
                        "latency_ms": None,
                        "jitter_ms": None,
                        "communication_quality": None,
                        "internet_availability_pct": None,
                    },
                    "evidence_count": 0,
                    "confidence_pct": 0.0,
                    "accuracy_pct": None,
                    "accuracy_assessments": 0,
                },
            ],
        },
        "intelligence.impact": _demo_intelligence_impact(populated=True),
        "intelligence.learning_cycle_history": {
            "schema": "NewHorizonLearningCycleHistory.v12",
            "rows": [
                {
                    "assessment_id": "demo-learning-cycle-1",
                    "decided_at": 1786547400.0,
                    "completed_at": 1786548000.0,
                    "before_context": "B1/B1+B3+B20/2050/255",
                    "before_nr": "---",
                    "after_context": "B3/B1+B3+B20/2080/126",
                    "after_nr": "---",
                    "why": "Completed evidence predicts improvement.",
                    "expected": "Expected AQ: +1.0",
                    "actual_grade": "AQ +2.7 · Change +0.8",
                    "grade_parts": [{"text": "AQ +2.7", "outcome": "improved"}, {"text": "Change +0.8", "outcome": "improved"}],
                    "impact_result": None,
                    "result_detail": (
                        "ECI 30.0 · Lat 32 ms · Jit 5.0 ms · CQ 0.82 · AQ +2.7"
                    ),
                    "result_parts": [
                        {"text": text, "outcome": "improved"}
                        for text in ("ECI 30.0", "Lat 32 ms", "Jit 5.0 ms", "CQ 0.82", "AQ +2.7")
                    ],
                    "learning_effect": "Positive B3 evidence added.",
                    "next_consequence": "Supports B3 in matching contexts.",
                }
            ],
        },
        "intelligence.referee_summary": {
            "schema": "NewHorizonRefereeSummary.v6",
            "current": "Collecting current 10-minute window",
            "latest_completed": {
                "window": "12 AUG 14:00–14:10",
                "target": "B1 / B1+B3+B20",
                "identity": "Cell 2080 / PCI 126 / EARFCN 375",
                "service_text": "+2.0",
                "service_grade": 2,
                "communication_text": "0.74",
                "communication_grade": 0,
            },
            "history_note": "1 completed 10-minute window · oldest to newest.",
        },
        "intelligence.band_context_evidence": _demo_band_context_evidence(),
            "intelligence.control_status_message": "BRAIN ACTIVATED",
        "learning.status": "observing",
        "learning.evidence": 1348,
        "learning.contexts": 109,
        "learning.warning_rate": 0.0,
        "learning.observe_rate": 0.0,
        "learning.direct_action": "not needed",
        "learning.confidence_pct": 100.0,
        "learning.candidates": 3,
        "learning.blocked_updates": 0,
        "learning.rejected_rate": 0.0,
        "learning.blockers": [],
        "learning.reason": "current anchor already best",
    }
    if field_id in overrides:
        return overrides[field_id]
    if kind is FieldKind.BOOL:
        return True
    if kind is FieldKind.FLOAT:
        return 1.0
    if kind is FieldKind.INTEGER:
        return 1
    if kind is FieldKind.LIST:
        return []
    if kind is FieldKind.MAPPING:
        return {}
    return "Available"


_DEMO_GENERATIONS = 12


def build_demo_front_register(*, visual_profile: str = "live"):
    chart_register = NGUIChartRegister()
    history = NGUIChartHistory()
    history.bind_runtime_namespace("ngui-demo-device", "ngui-demo")
    chart_register.bind_runtime_namespace("ngui-demo-device", "ngui-demo")
    grouped = defaultdict(dict)
    for source in NGUI_INPUT_SOURCE_REGISTRY.values():
        grouped[source.owner][source.source_id] = _sample_value(
            source.source_id,
            source.kind,
            visual_profile=visual_profile,
        )
    if visual_profile == "startup":
        grouped[SourceOwner.MODEM_MATRIX] = {
            source_id: value
            for source_id, value in grouped[SourceOwner.MODEM_MATRIX].items()
            if value not in (None, [], {})
        }

    for sequence in range(1, _DEMO_GENERATIONS + 1):
        timestamp = 100.0 + sequence * 30.0
        namespace = RuntimeGenerationNamespace(
            session_scope_key="ngui-demo-device",
            source_epoch="ngui-demo",
            lifecycle_generation=1,
        )
        if visual_profile != "startup":
            matrix_values = grouped[SourceOwner.MODEM_MATRIX]
            matrix_values["traffic.dl_mbps"] = (
                1.0 + (sequence % 20) * 0.35
            )
            matrix_values["traffic.ul_mbps"] = (
                0.3 + (sequence % 7) * 0.1
            )
            matrix_values["rf.sinr_db"] = (
                5.0 + (sequence % 8) * 0.5
            )
            matrix_values["rf.rsrq_db"] = (
                -11.0 + (sequence % 5) * 0.3
            )
        batches = tuple(
            adapt_owner_register(
                owner=owner,
                sequence=sequence,
                published_at=timestamp,
                values=values,
                sampled_at={
                    source_id: timestamp for source_id in values
                },
                sample_sequences={
                    source_id: sequence for source_id in values
                },
            )
            for owner, values in grouped.items()
            if values
        )
        # This demo is not a live Input owner. Build one frozen observation
        # without installing it in the input bank or calling the coordinator.
        input_values = {
            item.source_id: item
            for batch in batches
            for item in batch.values
        }
        owner_sequences = {
            batch.owner: batch.sequence for batch in batches
        }
        inputs = InputRegisterSnapshot(
            generation=sequence,
            published_at=timestamp,
            values=MappingProxyType(input_values),
            owner_sequences=MappingProxyType(owner_sequences),
            owner_fingerprints=MappingProxyType(
                {
                    batch.owner: owner_input_batch_fingerprint(batch)
                    for batch in batches
                }
            ),
            source_owner_sequences=MappingProxyType(
                {
                    item.source_id: batch.sequence
                    for batch in batches
                    for item in batch.values
                }
            ),
            runtime_session_scope_key=namespace.session_scope_key,
            runtime_source_epoch=namespace.source_epoch,
            accepted_source_sequence=sequence,
            accepted_source_sampled_at=timestamp,
            matrix_identity=MatrixGenerationIdentity(
                namespace=namespace,
                matrix_sequence=sequence,
                matrix_revision=f"{sequence:064x}",
            ),
        )
        frame = NGUIPresentationAuthority(
            chart_register=chart_register
        ).publish(inputs, now=timestamp)
        history.append_front_register(frame)

    final_timestamp = 100.0 + _DEMO_GENERATIONS * 30.0
    request = ChartWindowRequest(
        1,
        DEFAULT_CHART_WINDOW_SECONDS,
        final_timestamp,
        "ngui-demo-device",
        "ngui-demo",
        source_sequence=_DEMO_GENERATIONS,
        source_append_ts=final_timestamp,
    )
    chart_register.expect(request)
    chart_register.publish(
        history.build_snapshot(request)
    )
    authority = NGUIPresentationAuthority(chart_register=chart_register)
    authority.publish(inputs, now=final_timestamp + 0.1)
    return authority.front_register


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--page",
        choices=[page.value for page in PageId if page not in {PageId.HEADER, PageId.FOOTER, PageId.ENTRY}],
        default="graphs",
    )
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    front = build_demo_front_register(visual_profile="live")
    edits = NGUIEditRegister()
    edits.reconcile(
        front.snapshot().pages[PageId.DEVELOPER_CONTROL].fields,
    )
    window = NGUIWindow(
        front,
        NGUIInteractionRegister(),
        edits,
        report_render_error=lambda message: print(message, file=sys.stderr),
    )
    window.show_connected(PageId(args.page))
    window.show()
    if args.screenshot is not None:
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)

        def save_and_close() -> None:
            window.repaint()
            app.processEvents()
            window.grab().save(str(args.screenshot))
            window.close()
            app.quit()

        QTimer.singleShot(1500, save_and_close)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
