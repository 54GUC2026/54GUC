from __future__ import annotations

from typing import Any, Dict

from arch4.operator_profiles import apply_runtime_event_texts, apply_runtime_status_aliases, apply_runtime_status_texts, build_runtime_event_records


def hilink_sequence_alias_fields(engine: Any) -> Dict[str, Any]:
    antithrash = getattr(engine, "_a1_antithrash", {}) or {}
    morning = getattr(engine, "_a1_morning", {}) or {}
    return {
        "a1_antithrash_hold_active": bool(float(antithrash.get("hold_until", 0.0) or 0.0) > 0.0),
        "a1_antithrash_freeze_bands": list(antithrash.get("freeze_bands", []) or []),
        "a1_morning_active": bool(morning.get("active", False)),
    }


def apply_family_status_aliases(status: Dict[str, Any]) -> Dict[str, Any]:
    return apply_runtime_status_texts(apply_runtime_status_aliases(status))


def apply_family_event_texts(event: Dict[str, Any]) -> Dict[str, Any]:
    return apply_runtime_event_texts(event)


def build_family_runtime_events(event: Dict[str, Any]) -> list[Dict[str, Any]]:
    return [apply_family_event_texts(dict(e)) for e in build_runtime_event_records(event)]
