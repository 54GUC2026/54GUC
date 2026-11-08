"""Compact Brain decision receipt projection.

This module owns the small, cross-surface receipt used to correlate Brain
decisions, no-action holds, telemetry rows, and current-state/support surfaces.
It is intentionally much smaller than the full decision context.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict


def object_to_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        return {}
    try:
        payload = to_dict()
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def compact_decision_trace_surface(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    out: Dict[str, Any] = {}
    for key in (
        "decision_trace_id",
        "source",
        "state",
        "proposed",
        "final",
        "passed",
        "blocked",
        "tsar_verdict",
        "tsar_reason",
        "tsar_summary",
        "primary_blocker_gate",
        "primary_blocker_reason",
        "also_blocked_by",
    ):
        item = value.get(key)
        if item not in (None, "", [], {}):
            out[key] = item
    steps = value.get("steps")
    if isinstance(steps, list) and steps:
        compact_steps: list[Dict[str, Any]] = []
        for step in steps[:8]:
            if not isinstance(step, Mapping):
                continue
            row = {
                key: step.get(key)
                for key in (
                    "gate",
                    "result",
                    "reason",
                    "changed",
                    "skipped",
                    "remaining_s",
                    "skip_count",
                    "skip_limit",
                )
                if step.get(key) not in (None, "", [], {})
            }
            for key, item in step.items():
                if key in row or item in (None, "", [], {}):
                    continue
                if isinstance(item, (str, int, float, bool)):
                    row[str(key)] = item
            if row:
                compact_steps.append(row)
        if compact_steps:
            out["steps"] = compact_steps
            out["steps_truncated"] = len(steps) > len(compact_steps)
    return out


def compact_brain_decision_surface(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    out: Dict[str, Any] = {}
    trace = compact_decision_trace_surface(value.get("decision_trace"))
    if trace:
        out["decision_trace"] = trace
        out["decision_receipt"] = {
            "schema_version": "brain_decision_receipt.v1",
            "decision_trace_id": str(trace.get("decision_trace_id", "") or ""),
            "source": str(trace.get("source", "") or ""),
            "final": str(trace.get("final", "") or ""),
            "verdict": str(trace.get("tsar_verdict", "") or ""),
            "gate": str(trace.get("primary_blocker_gate", "") or ""),
            "reason": str(trace.get("primary_blocker_reason", "") or ""),
        }
        for source_key, target_key in (
            ("decision_trace_id", "decision_trace_id"),
            ("source", "decision_trace_source"),
            ("final", "decision_trace_final"),
            ("tsar_verdict", "decision_trace_verdict"),
            ("primary_blocker_gate", "decision_trace_gate"),
            ("primary_blocker_reason", "decision_trace_reason"),
        ):
            item = trace.get(source_key)
            if item not in (None, "", [], {}):
                out[target_key] = item
    context = value.get("decision_context")
    if isinstance(context, Mapping):
        compact_context = {
            key: context.get(key)
            for key in (
                "decision_trace_id",
                "state",
                "warning_class",
                "warning_source",
                "warning_mechanism",
                "warning_reason",
                "incident_class",
                "incident_next_step",
                "health_stage",
                "health_action_hint",
            )
            if context.get(key) not in (None, "", [], {})
        }
        if compact_context:
            out["decision_context"] = compact_context
    result = value.get("decision_result")
    if isinstance(result, Mapping):
        compact_result = {
            key: result.get(key)
            for key in (
                "decision_trace_id",
                "selected",
                "final",
                "verdict",
                "action_lever",
                "action_reason",
                "blocked",
                "passed",
            )
            if result.get(key) not in (None, "", [], {})
        }
        if compact_result:
            out["decision_result"] = compact_result
    return out


def compact_brain_current_state_payload(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    out: Dict[str, Any] = {}
    learning = value.get("learning")
    if isinstance(learning, Mapping) and learning:
        out["learning"] = dict(learning)
    out.update(compact_brain_decision_surface(value))
    return out


def promote_compact_decision_receipt(target: Dict[str, Any], brain: Mapping[str, Any]) -> None:
    if not isinstance(target, dict) or not isinstance(brain, Mapping):
        return
    for key in (
        "decision_trace_id",
        "decision_trace_source",
        "decision_trace_final",
        "decision_trace_verdict",
        "decision_trace_gate",
        "decision_trace_reason",
    ):
        value = brain.get(key)
        if value not in (None, "", [], {}):
            target[key] = value
