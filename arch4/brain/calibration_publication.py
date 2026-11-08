"""Calibration publication sanitizers."""

from __future__ import annotations

from typing import Any, Dict, Mapping


CALIBRATION_RUNTIME_POLICY_OWNER = "arch4.brain.calibration_publication"
CALIBRATION_RUNTIME_POLICY_SCHEMA = "calibration_profile_override_publication.v1"


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def default_provider_time_behavior_publication() -> Dict[str, Any]:
    return {
        "owner": "self_calibration",
        "published": False,
        "current_source": "operator_defaults",
        "status": "not_published",
        "provider_name": "",
        "provider_plmn": "",
        "scope": {
            "provider_names": [],
            "provider_plmns": [],
            "applies_to_all_modems": False,
            "backend_hint": "",
            "family_hint": "",
        },
        "schedule_window": {
            "timezone": "",
            "start_local_hour": 0,
            "end_local_hour": 0,
        },
        "trigger_basis": [],
        "action_plan": [],
        "confidence": 0.0,
        "rule_id": "",
        "workflow_phase": "",
        "updated_ts": 0.0,
        "note": (
            "Provider/time-of-day behavior still falls back to operator defaults "
            "until self-calibration publishes learned rules."
        ),
    }


def sanitize_provider_time_behavior_publication(raw: Dict[str, Any]) -> Dict[str, Any]:
    default = default_provider_time_behavior_publication()
    data = dict(raw or {})
    scope = data.get("scope") if isinstance(data.get("scope"), dict) else {}
    schedule_window = (
        data.get("schedule_window")
        if isinstance(data.get("schedule_window"), dict)
        else {}
    )
    published = bool(data.get("published", False))
    current_source_default = "self_calibration" if published else "operator_defaults"
    status_default = "published" if published else "not_published"
    note_default = "" if published else str(default["note"])
    provider_names = [
        str(v) for v in list(scope.get("provider_names", []) or []) if str(v or "").strip()
    ][:8]
    provider_plmns = [
        str(v) for v in list(scope.get("provider_plmns", []) or []) if str(v or "").strip()
    ][:8]
    trigger_basis = [
        str(v) for v in list(data.get("trigger_basis", []) or []) if str(v or "").strip()
    ][:12]
    action_plan = [
        str(v) for v in list(data.get("action_plan", []) or []) if str(v or "").strip()
    ][:12]
    provider_name = str(data.get("provider_name", "") or "")
    provider_plmn = str(data.get("provider_plmn", "") or "")
    if not provider_name and provider_names:
        provider_name = provider_names[0]
    if not provider_plmn and provider_plmns:
        provider_plmn = provider_plmns[0]
    return {
        "owner": "self_calibration",
        "published": published,
        "current_source": str(data.get("current_source", current_source_default) or current_source_default),
        "status": str(data.get("status", status_default) or status_default),
        "provider_name": provider_name,
        "provider_plmn": provider_plmn,
        "scope": {
            "provider_names": provider_names,
            "provider_plmns": provider_plmns,
            "applies_to_all_modems": bool(scope.get("applies_to_all_modems", False)),
            "backend_hint": str(scope.get("backend_hint", "") or ""),
            "family_hint": str(scope.get("family_hint", "") or ""),
        },
        "schedule_window": {
            "timezone": str(schedule_window.get("timezone", "") or ""),
            "start_local_hour": int(schedule_window.get("start_local_hour", 0) or 0),
            "end_local_hour": int(schedule_window.get("end_local_hour", 0) or 0),
        },
        "trigger_basis": trigger_basis,
        "action_plan": action_plan,
        "confidence": float(data.get("confidence", 0.0) or 0.0),
        "rule_id": str(data.get("rule_id", "") or ""),
        "workflow_phase": str(data.get("workflow_phase", "") or ""),
        "updated_ts": float(data.get("updated_ts", 0.0) or 0.0),
        "note": str(data.get("note", note_default) or note_default),
    }


def build_runtime_policy_publication(
    *,
    authority: Mapping[str, Any],
    workflow: Mapping[str, Any],
    active_config_overrides: Mapping[str, Any],
    active_config_override_rows: list[Any],
) -> Dict[str, Any]:
    authority_map = _mapping(authority)
    workflow_map = _mapping(workflow)
    active_overrides = _mapping(active_config_overrides)
    override_rows = list(active_config_override_rows or [])
    return {
        "schema": CALIBRATION_RUNTIME_POLICY_SCHEMA,
        "owner": CALIBRATION_RUNTIME_POLICY_OWNER,
        "source": "calibration_surface",
        "published": bool(authority_map or workflow_map or active_overrides),
        "status": "accepted" if active_overrides else "inactive",
        "parameter_group": "calibration_profile_overrides",
        "parameter_id": "calibration.active_profile_overrides",
        "mode": "active_profile_overrides" if active_overrides else "static_defaults",
        "runtime_influence": "active_profile_overrides" if active_overrides else "none",
        "active_profile_id": str(authority_map.get("active_profile_id", "") or "").strip(),
        "active_profile_version": int(authority_map.get("active_profile_version", 0) or 0),
        "active_reference_marker": str(authority_map.get("active_reference_marker", "") or "").strip(),
        "active_config_override_count": int(authority_map.get("active_config_override_count", 0) or 0),
        "active_config_override_rows": override_rows,
        "active_config_overrides": active_overrides,
        "evidence": {
            "source": "calibration_authority_active_profile",
            "workflow_phase": str(workflow_map.get("phase", "") or ""),
            "candidate_profile_id": str(authority_map.get("candidate_profile_id", "") or "").strip(),
        },
        "confidence": float(authority_map.get("candidate_primary_metric_confidence", 0.0) or 0.0),
        "scope": {
            "active_profile_id": str(authority_map.get("active_profile_id", "") or "").strip(),
            "active_profile_version": int(authority_map.get("active_profile_version", 0) or 0),
            "active_reference_marker": str(authority_map.get("active_reference_marker", "") or "").strip(),
        },
        "effective_window": {
            "starts_ts": float(authority_map.get("active_profile_approved_ts", 0.0) or 0.0),
            "expires_ts": float(authority_map.get("active_profile_expires_ts", 0.0) or 0.0),
        },
        "rollback": {
            "allowed": True,
            "target": "static_policy_defaults",
            "trigger": "profile_rejected_or_scope_mismatch",
        },
        "safety_floor": {
            "owner": "policy_profile",
            "cannot_be_overridden_silently": True,
        },
    }
