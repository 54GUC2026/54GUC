"""Intelligence-only adapters for optional decision-context evidence."""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from arch4.action_admission import selected_action_context_from_action
from arch4.interfaces import LeverAction
from arch4.runtime_sidecar_health import format_runtime_iso_utc

from .evidence_recorder import intelligence_evidence_recorder_if_attached


log = logging.getLogger(__name__)


def _report_evidence_fault(engine: Any, *, source: str, exc: Exception) -> None:
    """Report an optional Intelligence-evidence failure without owning Core."""

    log.warning("Intelligence evidence failed at %s: %s: %s", source, type(exc).__name__, exc)
    diagnostics = getattr(engine, "diagnostics", None)
    publish = getattr(diagnostics, "log_event", None)
    if callable(publish):
        try:
            publish(
                "intelligence_evidence_failed",
                source=source,
                error_type=type(exc).__name__,
                detail=str(exc),
            )
        except Exception as report_exc:
            log.warning(
                "Intelligence evidence failure report failed at %s: %s: %s",
                source,
                type(report_exc).__name__,
                report_exc,
            )


def record_connection_lost_context(
    engine: Any,
    *,
    action: Optional[LeverAction],
    action_suppressed: bool,
) -> None:
    recorder = intelligence_evidence_recorder_if_attached(engine)
    if recorder is None:
        return
    selected_context = (
        selected_action_context_from_action(action) if action is not None else None
    )
    try:
        now = time.time()
        accepted = getattr(engine, "_last_accepted_controller_generation", None)
        accepted_generation = getattr(
            getattr(getattr(accepted, "intelligence", None), "generation", None),
            "generation",
            0,
        )
        snapshot = {
            "ts": now,
            "iso": format_runtime_iso_utc(now),
            "tick": int(accepted_generation or 0),
            "state": "TELEMETRY_UNAVAILABLE",
            "time_in_state_s": 0.0,
            "radio_state_text": "Telemetry unavailable",
            "serving_band_signature": "",
            "band_set_signature": "",
            "ca_state_text": "SINGLE BAND ACTIVE",
            "pci": 0,
            "earfcn": 0,
            "sinr_db": 0.0,
            "rsrp_dbm": 0.0,
            "rsrq_db": 0.0,
            "rssi_dbm": 0.0,
            "cqi": 0,
            "ri": 0,
            "mcs_dl": 0,
            "mcs_ul": 0,
            "qm_dl0": 0,
            "qm_dl1": 0,
            "qm_ul": 0,
            "tbs_dl0": 0,
            "tbs_dl1": 0,
            "tbs_ul": 0,
            "tx_power": 0.0,
            "dl_mbps": 0.0,
            "ul_mbps": 0.0,
            "has_traffic": False,
            "rtt_ms": 0.0,
            "rtt_sliding_avg": 0.0,
            "rtt_pressure_level": "probe_missing",
            "cq_cell": 0.0,
            "cq_cell_valid": False,
            "health_stage": "telemetry_unavailable",
            "health_action_hint": "wait_reconnect",
            "incident_class": "modem_snapshot_missed",
            "incident_next_step": "wait_reconnect",
            "decision_trace_id": "",
            "decision_trace_source": "telemetry_unavailable",
            "decision_trace_final": "none",
            "bouncer_intent": "",
            "bouncer_outcome": "",
            "action_lever": str(
                getattr(action.lever, "name", "none") if action else "none"
            ),
            "action_reason": str(getattr(action, "reason", "") or ""),
            "action_suppressed": bool(action_suppressed),
            "action_origin": (
                selected_context.origin.kind.value if selected_context else ""
            ),
            "operator_initiated": (
                selected_context.operator_initiated if selected_context else False
            ),
        }
        recorder.observe_decision_context(snapshot)
    except Exception as exc:
        _report_evidence_fault(
            engine,
            source="record_connection_lost_context",
            exc=exc,
        )


def record_modem_read_missed_context(
    engine: Any,
    *,
    action: Optional[LeverAction],
    action_suppressed: bool,
) -> None:
    record_connection_lost_context(
        engine,
        action=action,
        action_suppressed=action_suppressed,
    )


__all__ = (
    "record_connection_lost_context",
    "record_modem_read_missed_context",
)
