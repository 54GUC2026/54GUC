"""LearningAuthority runtime event and visible action-description owner."""

from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

import time
from typing import Any, Dict, Optional

from arch4.core.bouncer import BouncerVerdict
from arch4.brain.referee import RefereeVerdict
from arch4.brain.tick_contract import DecisionTrace
from arch4.interfaces import LeverAction, LeverType
from arch4.policy.incident_action_policy import incident_class_name, incident_next_step

OWNER = "arch4.brain.learning_authority_event_owner"


def _safe_dict_snapshot(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    try:
        return dict(value)
    except RuntimeError:
        return {}


class LearningAuthorityEventOwner:
    """Own LearningAuthority runtime events and display action summaries."""

    def __init__(self) -> None:
        self._runtime_events: list[dict] = []
        self._last_reasoning: Dict[str, Any] = {}
        self._pre_reasoning_by_trace: Dict[str, Dict[str, Any]] = {}
        self._last_action_description: str = ""
        self._last_passive_intent_signature: Optional[tuple] = None
        self._last_passive_outcome_signature: Optional[tuple] = None
        self._last_intent_verdict: Optional[BouncerVerdict] = None

    def ingest_decision_trace(
        self,
        trace: Optional[Dict[str, Any] | DecisionTrace],
        *,
        decision_trace_id: str = "",
        action: Optional[LeverAction] = None,
        incident_assessment: Optional[Dict[str, Any]] = None,
    ) -> None:
        if isinstance(trace, DecisionTrace):
            trace = trace.to_dict()
        if not isinstance(trace, dict):
            return
        tid = str(decision_trace_id or trace.get("decision_trace_id") or "").strip()
        incident = _incident_record(incident_assessment)
        record = {
            "decision_trace_id": tid,
            "source": str(trace.get("source", "") or ""),
            "state": str(trace.get("state", "") or ""),
            "proposed": str(trace.get("proposed", "") or ""),
            "final": str(trace.get("final", "") or ""),
            "selected_brief": str(
                trace.get("final", "") or trace.get("proposed", "") or ""
            ),
            "passed": int(trace.get("passed", 0) or 0),
            "blocked": int(trace.get("blocked", 0) or 0),
            "selected_lever": action.lever.name if action else "",
            "selected_intent": (action.intent or "") if action else "",
            "selected_objective_id": (action.objective_id or "") if action else "",
            "selected_escape_probe": bool((action.params or {}).get("escape_probe", False)) if action else False,
            "selected_escape_probe_id": str((action.params or {}).get("escape_probe_id", "") or "") if action else "",
            "ts": float(time.time()),
        }
        record.update(incident)
        self._last_reasoning = record
        if tid:
            self._pre_reasoning_by_trace[tid] = dict(record)
        if action is not None:
            self._emit_runtime_event("learning_pre_action_reasoning", **record)

    def observe_intent(
        self,
        *,
        verdict: BouncerVerdict,
        action: Optional[LeverAction],
        decision_trace_id: str,
        incident_assessment: Optional[Dict[str, Any]],
    ) -> None:
        incident = _incident_record(incident_assessment)
        payload = {
            "decision_trace_id": str(decision_trace_id or ""),
            "disposition": verdict.disposition.value,
            "level": str(verdict.level or ""),
            "code": str(verdict.code or ""),
            "message": str(verdict.message or ""),
            "details": dict(verdict.details or {}),
            "selected_escape_probe": bool((action.params or {}).get("escape_probe", False)) if action else False,
            "selected_escape_probe_id": str((action.params or {}).get("escape_probe_id", "") or "") if action else "",
            **incident,
        }
        if action is not None:
            self._last_passive_intent_signature = None
        else:
            self._last_passive_intent_signature = _passive_event_signature(payload)
        self._last_intent_verdict = verdict

    def last_intent_verdict(self) -> Optional[BouncerVerdict]:
        return self._last_intent_verdict

    def observe_outcome(
        self,
        *,
        verdict: RefereeVerdict,
        action: Optional[LeverAction],
        action_suppressed: bool,
        modem_before: Optional[CellularIntelligenceGeneration],
        modem_after: Optional[CellularIntelligenceGeneration],
        decision_trace_id: str,
        incident_assessment: Optional[Dict[str, Any]],
        decision_trace: Optional[Dict[str, Any]],
    ) -> None:
        if action is not None and not action_suppressed:
            self._last_action_description = _build_action_description(
                action, verdict, modem_before, modem_after,
            )

        tid = str(decision_trace_id or "")
        pre = self._pre_reasoning_by_trace.pop(tid, None) if tid else None
        incident = _incident_record(incident_assessment)
        payload = {
            "decision_trace_id": tid,
            "level": str(verdict.level or ""),
            "code": str(verdict.code or ""),
            "message": str(verdict.message or ""),
            "details": dict(verdict.details or {}),
            "linked_pre_reasoning": bool(pre),
            "selected_escape_probe": bool((action.params or {}).get("escape_probe", False)) if action else False,
            "selected_escape_probe_id": str((action.params or {}).get("escape_probe_id", "") or "") if action else "",
            **incident,
        }
        if action is not None:
            self._last_passive_outcome_signature = None
            self._emit_runtime_event("referee_outcome", **payload)
            return

        trace_source = str((decision_trace or {}).get("source", "") or "").strip().lower()
        incident_class = str(incident.get("incident_class", "") or "").strip().lower()
        meaningful_passive = bool(pre) or trace_source == "constraint_vector" or incident_class not in {"", "healthy"}
        if meaningful_passive:
            signature = _passive_event_signature(payload)
            if signature != self._last_passive_outcome_signature:
                self._last_passive_outcome_signature = signature
                self._emit_runtime_event("referee_outcome", **payload)

    def runtime_status(self) -> Dict[str, Any]:
        return {
            "last_reasoning": _safe_dict_snapshot(self._last_reasoning),
            "last_action_description": str(self._last_action_description or ""),
        }

    def drain_runtime_events(self) -> list[dict]:
        out = list(self._runtime_events)
        self._runtime_events.clear()
        return out

    def _emit_runtime_event(self, kind: str, **payload: Any) -> None:
        self._runtime_events.append({"kind": kind, **payload})


def _build_action_description(
    action: LeverAction,
    verdict: BouncerVerdict | RefereeVerdict,
    modem_before: Optional[CellularIntelligenceGeneration],
    modem_after: Optional[CellularIntelligenceGeneration],
) -> str:
    lever = action.lever
    outcome_code = str(verdict.code or "")
    suffix = ""
    if outcome_code == "post_action_regression":
        suffix = " RF worse"
    elif outcome_code == "band_action_no_effect":
        suffix = " no effect"
    elif outcome_code == "radio_lost_after_action":
        suffix = " radio lost"
    elif outcome_code == "action_failed":
        suffix = " failed"

    if lever in {LeverType.BAND_SELECTION, LeverType.BAND_LOCK}:
        band_before = int(getattr(modem_before.cellular, 'primary_band', 0) or 0)
        band_after = int(getattr(modem_after.cellular, 'primary_band', 0) or 0)
        prefix = "BAND LOCK" if lever == LeverType.BAND_LOCK else "BAND"
        if band_before and band_after:
            return f"{prefix} B{band_before}\u2192B{band_after}{suffix}"
        if band_after:
            return f"{prefix} \u2192B{band_after}{suffix}"
        return f"{prefix}{suffix}"

    if lever == LeverType.CELL_LOCK:
        pci_before = int(getattr(modem_before.cellular, 'pci', 0) or 0)
        pci_after = int(getattr(modem_after.cellular, 'pci', 0) or 0)
        if pci_before and pci_after:
            return f"CELL PCI{pci_before}\u2192PCI{pci_after}{suffix}"
        return f"CELL{suffix}"

    if lever == LeverType.RESTART:
        return f"RESTART{suffix}"

    if lever == LeverType.NR_BAND_LOCK:
        return f"NR BAND{suffix}"

    return f"{lever.name}{suffix}"


def _passive_event_signature(payload: Dict[str, Any]) -> tuple:
    details = payload.get("details", {})
    if not isinstance(details, dict):
        details = {}
    return (
        str(payload.get("level", "") or ""),
        str(payload.get("code", "") or ""),
        str(payload.get("message", "") or ""),
        tuple(sorted((str(k), repr(v)) for k, v in details.items())),
        str(payload.get("incident_class", "") or ""),
        str(payload.get("incident_next_step", "") or ""),
        bool(payload.get("incident_self_induced", False)),
        bool(payload.get("incident_recovery_candidate", False)),
        bool(payload.get("linked_pre_reasoning", False)),
        bool(payload.get("selected_escape_probe", False)),
        str(payload.get("selected_escape_probe_id", "") or ""),
    )


def _incident_record(incident_assessment: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    assessment = incident_assessment or {}
    return {
        "incident_class": incident_class_name(assessment),
        "incident_next_step": incident_next_step(assessment),
        "incident_self_induced": bool(assessment.get("self_induced", False)),
        "incident_recovery_candidate": bool(assessment.get("recovery_candidate", False)),
    }


__all__ = ["OWNER", "LearningAuthorityEventOwner"]
