"""Canonical runtime incident RCA evidence contract.

RuntimeIncidentTracker owns incident detection and episode lifecycle.  This
module owns the root-cause payload shape and missing-evidence discipline that
GUI, telemetry, support bundles, and learning consumers may read from
``incident_assessment.root_cause``.
"""

from __future__ import annotations

from typing import Any, Mapping


RCA_SCHEMA_VERSION = "runtime_incident_rca_v1"
RCA_OWNER = "brain.incident_rca"


_CAUSE_BY_INCIDENT_CLASS = {
    "healthy": "none",
    "radio_disconnected": "radio_disconnected",
    "rf_collapse": "rf_degradation",
    "rf_event_spike": "rf_degradation",
    "rf_quality_watch": "rf_degradation_unproven",
    "data_path_degraded": "wan_or_downstream_path",
    "latency_keepalive_path": "latency_or_keepalive",
    "ca_capacity_reduced_observe": "ca_capacity_reduced_but_service_usable",
    "post_action_instability": "self_induced_instability",
    "radio_ca_loss": "ca_loss_under_load",
    "startup_bootstrap_observe": "startup_bootstrap_observe",
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _has(live: Mapping[str, Any], key: str) -> bool:
    return key in live and live.get(key) is not None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _missing_live_evidence(incident_class: str, live: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    if incident_class == "healthy":
        return missing
    if not live:
        if incident_class == "radio_disconnected":
            return missing
        return ["live_snapshot"]

    if incident_class in {"rf_collapse", "rf_event_spike", "rf_quality_watch"}:
        if not _has(live, "radio_connected"):
            missing.append("radio_connected")
        rf_truth_present = bool(_text(live.get("rf_truth_verdict")))
        if not rf_truth_present:
            for key in ("sinr_db", "rsrp_dbm", "rsrq_db"):
                if not _has(live, key):
                    missing.append(key)
    elif incident_class == "data_path_degraded":
        if not _has(live, "radio_connected"):
            missing.append("radio_connected")
        if not _has(live, "traffic_valid"):
            missing.append("traffic_valid")
        has_packet = _has(live, "packet_session_connected")
        has_probe = _has(live, "ping_fail_streak") or _has(live, "rtt_ms")
        if not has_packet and not has_probe:
            missing.append("packet_session_or_probe_evidence")
    elif incident_class in {"radio_ca_loss", "ca_capacity_reduced_observe"}:
        if not _has(live, "primary_band"):
            missing.append("primary_band")
        if not _text(live.get("ca_signature")):
            missing.append("ca_signature")
        if not _has(live, "traffic_valid"):
            missing.append("traffic_valid")
    elif incident_class == "latency_keepalive_path":
        if not _has(live, "rtt_ms"):
            missing.append("rtt_ms")
        if not _has(live, "radio_connected"):
            missing.append("radio_connected")

    return missing


def build_runtime_incident_root_cause(
    *,
    incident_class: str,
    live: Mapping[str, Any] | None = None,
    assessment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the only supported runtime incident root-cause payload.

    Missing evidence remains explicit.  Consumers may display or export the
    probable cause, but they must use ``evidence_state`` and
    ``missing_evidence`` before treating that cause as proven.
    """

    incident = _text(incident_class).lower() or "healthy"
    live_map = _mapping(live)
    assessment_map = _mapping(assessment)
    missing = _missing_live_evidence(incident, live_map)

    probable_cause = _CAUSE_BY_INCIDENT_CLASS.get(incident, "unknown")
    if missing and not live_map and incident not in {"healthy", "radio_disconnected"}:
        probable_cause = "unknown"

    summary: dict[str, Any] = {
        "schema_version": RCA_SCHEMA_VERSION,
        "owner": RCA_OWNER,
        "incident_class": incident,
        "incident_subtype": _text(assessment_map.get("incident_subtype")),
        "probable_cause": probable_cause,
        "evidence_state": "missing" if missing and not live_map else ("partial" if missing else "complete"),
        "missing_evidence": list(dict.fromkeys(missing)),
        "cause_confidence": "supported" if not missing else "inferred_with_missing_evidence",
        "confidence": _float(assessment_map.get("confidence")),
        "load_biased_window": False,
        "calibration_note": "",
        "rsrq_gap": 0.0,
        "rsrq_gap_valid": False,
    }

    if live_map:
        summary.update(
            {
                "primary_band": _int(live_map.get("primary_band")),
                "selected_band_set_signature": _text(live_map.get("selected_band_set_signature")),
                "active_band_set_signature": _text(live_map.get("active_band_set_signature")),
                "ca_signature": _text(live_map.get("ca_signature")),
                "rf_truth_verdict": _text(live_map.get("rf_truth_verdict")),
                "rf_truth_reason": _text(live_map.get("rf_truth_reason")),
                "traffic_valid": bool(live_map.get("traffic_valid", True)),
                "traffic_state": _text(live_map.get("traffic_state")),
                "traffic_state_detail": _text(live_map.get("traffic_state_detail")),
                "traffic_invalid_reason": _text(live_map.get("traffic_invalid_reason")),
                "packet_session_connected": live_map.get("packet_session_connected"),
                "connection_status_code": _text(live_map.get("connection_status_code")),
                "downstream_collision_suspected": bool(
                    live_map.get("downstream_collision_suspected", False)
                ),
                "radio_mutation_workflow_id": _text(live_map.get("radio_mutation_workflow_id")),
                "radio_mutation_sequence_step": _int(live_map.get("radio_mutation_sequence_step")),
                "radio_mutation_sequence_total": _int(live_map.get("radio_mutation_sequence_total")),
            }
        )
        if incident == "data_path_degraded":
            if live_map.get("packet_session_connected") is False or live_map.get("traffic_valid") is False:
                summary["probable_cause"] = "modem_packet_session_invalid"
            elif bool(live_map.get("downstream_collision_suspected", False)):
                summary["probable_cause"] = "downstream_topology_or_collision"

    return summary
