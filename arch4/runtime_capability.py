from __future__ import annotations

from typing import Any, Mapping

from arch4.coerce import safe_bool
from arch4.modem.live_lane_contract import global_live_lane_purpose_map


_ACTION_CAPABILITY_KEYS = {
    "radio_mode": "radio_mode",
    "band_select": "band_selection",
    "band_lock": "band_lock",
    "cell_lock": "cell_lock",
    "restart": "restart",
    "pband": "pband",
    "nr_band_lock": "nr_band_lock",
    "wifi_control": "wifi_toggle",
    "network_settings": "network_settings",
    "mobile_data": "mobile_data",
}

_READ_CAPABILITY_KEYS = (
    "traffic_presence",
    "traffic_volume",
    "ue_capability",
    "neighbors",
    "ca_details",
    "plmn",
    "band_priority",
)

_ESTIMATED_PROVENANCE = {"community", "inferred"}
_SUPPORTED_PROVENANCE = {"observed", "documented"}
_RF_EVIDENCE_ACTIONS = {"band_select", "band_lock", "cell_lock", "pband", "nr_band_lock", "radio_mode"}
_SESSION_EVIDENCE_ACTIONS = {"restart", "wifi_control", "network_settings", "mobile_data"}
_CANONICAL_SURFACE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("provider_identity", ("provider_name", "provider_plmn", "plmn")),
    ("radio_state", ("radio_connected", "radio_state_text")),
    ("serving_band", ("serving_band", "serving_band_signature")),
    (
        "selected_band_pool",
        (
            "selected_band_set",
            "selected_band_set_signature",
            "configured_band_pool",
            "configured_band_pool_signature",
        ),
    ),
    (
        "active_band_set",
        ("active_band_set", "active_band_set_signature"),
    ),
    (
        "ca_summary",
        (
            "ca_signature",
            "ca_active",
            "ca_raw_state",
            "ca_realtime_state",
            "ca_stat_pct",
            "ca_stat_scope_key",
            "ca_realtime_ld_state",
            "ca_realtime_ld_valid",
            "ca_stat_ld_pct",
            "ca_stat_ld_valid",
            "ca_ld_change_per_hour",
        ),
    ),
    ("cell_identity", ("pci", "earfcn_dl", "earfcn_ul", "cell_id", "enb_id")),
    ("rf_quality", ("sinr_db", "rsrp_dbm", "rsrq_db", "rssi_dbm")),
    ("traffic", ("rx_bytes_per_sec", "tx_bytes_per_sec", "traffic_presence", "traffic_volume")),
)

def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _field_state_from_trust(trust: str) -> str:
    norm = _lower(trust)
    if norm == "confirmed":
        return "supported"
    if norm in {"expected", "synthetic"}:
        return "estimated"
    if norm == "unsupported":
        return "unsupported"
    if norm == "non_useful":
        # Field is computed but not meaningful on this exact lane.
        # Consumers should treat it like unsupported for display purposes
        # while forensic/telemetry paths continue recording it.
        return "non_useful"
    return "unknown"


def _field_name_set(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        iterable = value.keys()
    elif isinstance(value, (list, tuple, set, frozenset)):
        iterable = value
    else:
        iterable = ()
    return {str(field).strip() for field in iterable if str(field).strip()}


def _field_support_conflicts(field_support: Mapping[str, Any]) -> list[str]:
    buckets = {
        "supported": _field_name_set(field_support.get("supported")),
        "unsupported": _field_name_set(field_support.get("unsupported")),
        "synthetic": _field_name_set(field_support.get("synthetic")),
    }
    unsupported = buckets["unsupported"]
    conflicts = [field for field in unsupported if field in buckets["supported"] or field in buckets["synthetic"]]
    return sorted(conflicts)


def _profile_capability_key_for_action(action_name: str) -> str:
    if action_name == "band_selection":
        return "supports_band_selection"
    if action_name == "wifi_toggle":
        return "supports_wifi_control"
    return f"supports_{action_name}"


def _canonical_surface_contract(
    *,
    fields: Mapping[str, Any],
    field_conflicts: list[str],
    backend: str,
    backend_family: str,
    profile_id: str,
    identity_state: str,
) -> dict[str, Any]:
    known_profile = bool(backend_family or profile_id or backend)
    groups: dict[str, Any] = {}
    missing_groups: list[str] = []
    degraded_groups: list[str] = []
    present_groups: list[str] = []

    for group_name, candidates in _CANONICAL_SURFACE_GROUPS:
        usable_fields: list[str] = []
        unsupported_fields: list[str] = []
        declared_fields: list[str] = []
        for field in candidates:
            meta = fields.get(field) if isinstance(fields.get(field), Mapping) else {}
            state = str(meta.get("state") or "").strip()
            if state:
                declared_fields.append(field)
            if state in {"supported", "estimated"}:
                usable_fields.append(field)
            elif state in {"unsupported", "non_useful"}:
                unsupported_fields.append(field)

        if usable_fields:
            state = "present"
            present_groups.append(group_name)
        elif unsupported_fields:
            state = "degraded"
            degraded_groups.append(group_name)
        else:
            state = "unknown"
            if known_profile:
                missing_groups.append(group_name)

        groups[group_name] = {
            "state": state,
            "accepted_fields": list(candidates),
            "declared_fields": declared_fields,
            "usable_fields": usable_fields,
            "unsupported_fields": unsupported_fields,
        }

    return {
        "version": 1,
        "authority": "runtime_capability_contract",
        "known_profile": bool(known_profile),
        "identity_state": identity_state,
        "backend": backend,
        "backend_family": backend_family,
        "profile_id": profile_id,
        "groups": groups,
        "present_groups": present_groups,
        "missing_groups": missing_groups,
        "degraded_groups": degraded_groups,
        "field_conflicts": field_conflicts,
        "complete": bool(known_profile and not missing_groups and not degraded_groups and not field_conflicts),
    }


def _feature_state(*, enabled: bool | None, provenance: str, profile_mentions: bool) -> str:
    if enabled is None:
        return "unknown"
    if not enabled:
        return "unsupported"
    if profile_mentions and provenance in _ESTIMATED_PROVENANCE:
        return "estimated"
    return "supported"


def _action_state(*, enabled: bool | None, overlay_trust: str, provenance: str, profile_mentions: bool) -> tuple[str, bool]:
    trust = _lower(overlay_trust)
    if trust == "supported":
        return "supported", True
    if trust == "degraded":
        return "estimated", True
    if trust == "unsupported":
        return "unsupported", False
    if enabled is True:
        return "supported", True
    if enabled is False:
        return "unsupported", False
    if profile_mentions:
        return "supported", True
    state = _feature_state(enabled=enabled, provenance=provenance, profile_mentions=profile_mentions)
    return state, state == "supported"


def _action_evidence_gate(*, action_key: str, onboarding: Mapping[str, Any], base_write_allowed: bool) -> tuple[bool, str]:
    if not safe_bool(base_write_allowed, False):
        return False, ""
    return True, ""


def _action_enabled_from_payload(*, report: Mapping[str, Any], key: str, profile_cap_key: str) -> bool | None:
    data = report if isinstance(report, Mapping) else {}
    profile_caps = data.get("modem_profile", {}).get("capabilities", {}) if isinstance(data.get("modem_profile", {}), Mapping) else {}
    if not isinstance(profile_caps, Mapping):
        profile_caps = {}

    if key == "pband":
        if "pband" in data:
            return _bool_or_none(data.get("pband"))
        if "supports_pband" in profile_caps:
            return _bool_or_none(profile_caps.get("supports_pband"))
        return None

    enabled = _bool_or_none(data.get(key))
    if enabled is None and profile_cap_key in profile_caps:
        return _bool_or_none(profile_caps.get(profile_cap_key))
    return enabled


def _backend_io_lane_contract(
    *,
    backend: str,
    backend_family: str,
    profile_id: str = "",
    model: str = "",
    firmware: str = "",
    vendor: str = "",
    backend_io_contract: Mapping[str, Any],
    modem_live_access_profile: Mapping[str, Any],
) -> dict[str, Any]:
    from arch4.operator_profiles import infer_backend_family
    family = backend_family or infer_backend_family(backend)
    is_unknown_family = family == "unknown"
    serial_required = safe_bool(backend_io_contract.get("io_serial_required", True), True)
    startup_parallel_allowed = safe_bool(backend_io_contract.get("startup_parallel_allowed", False), False)
    runtime_action_requires_io_lock = safe_bool(backend_io_contract.get("runtime_action_requires_io_lock", True), True)
    access_profile = dict(modem_live_access_profile or {})
    lane_groups = {
        str(lane): tuple(groups or ())
        for lane, groups in dict(access_profile.get("lane_groups") or {}).items()
    }
    lane_purposes = global_live_lane_purpose_map()
    export_lane_policy = lane_purposes.get("export", {})
    fast_groups = list(lane_groups.get("fast_live") or [])
    maintenance_groups = list(lane_groups.get("slow_live") or [])
    static_capability_groups = list(lane_groups.get("boot_static") or [])
    event_write_groups = list(lane_groups.get("event_write") or [])
    export_groups = list(lane_groups.get("export") or [])
    write_state = "blocked" if is_unknown_family else "supported" if event_write_groups else "unknown"
    write_direct_allowed = bool(event_write_groups and not is_unknown_family)

    return {
        "version": 1,
        "authority": "runtime_capability_contract",
        "backend": backend,
        "backend_family": family,
        "access_profile": access_profile,
        "access_profile_source": (
            "validated_modem_live_copy" if access_profile else "missing_fail_closed"
        ),
        "global_lanes": lane_purposes,
        "io_serial_required": serial_required,
        "startup_parallel_allowed": startup_parallel_allowed,
        "runtime_action_requires_io_lock": runtime_action_requires_io_lock,
        "fast_read": {
            "state": "supported" if fast_groups else "unknown",
            "lane": "protected_fast_read",
            "global_lane": "fast_live",
            "groups": fast_groups,
            "excludes": ["boot_static", "slow_live", "event_write", "export"] if fast_groups else [],
            "io_lock_required": serial_required,
        },
        "maintenance_read": {
            "state": "supported" if maintenance_groups else "unknown",
            "lane": "slow_maintenance_read",
            "global_lane": "slow_live",
            "groups": maintenance_groups,
            "excludes": [],
            "io_lock_required": serial_required,
        },
        "static_capability_read": {
            "state": "supported" if static_capability_groups else "unknown",
            "lane": "boot_static_capability",
            "global_lane": "boot_static",
            "groups": static_capability_groups,
            "refresh_policy": "identity_or_operator_refresh_only",
            "refresh_after_same_identity_reset": False,
            "io_lock_required": serial_required,
        },
        "write_action": {
            "state": write_state,
            "lane": "write_action",
            "global_lane": "event_write",
            "groups": event_write_groups,
            "io_lock_required": runtime_action_requires_io_lock,
            "capability_gate_required": True,
            "direct_modem_writes_allowed": write_direct_allowed,
            "discovery_role": "probe_only_no_write" if is_unknown_family else "",
        },
        "support_export": {
            "state": "supported" if export_groups else "unknown",
            "lane": "support_export",
            "global_lane": "export",
            "groups": export_groups,
            "io_lock_required": False,
            "contacts_modem": safe_bool(export_lane_policy.get("contacts_modem", False), False),
            "snapshot_only": safe_bool(export_lane_policy.get("snapshot_only", True), True),
            "allowed_inputs": [
                "runtime_registers",
                "current_state",
                "append_only_journal",
                "runtime_logs",
                "support_snapshots",
            ],
        },
    }


def build_capability_contract(report: Mapping[str, Any] | None) -> dict[str, Any]:
    data = report if isinstance(report, Mapping) else {}
    modem_profile = data.get("modem_profile") if isinstance(data.get("modem_profile"), Mapping) else {}
    resolved_card = data.get("resolved_identity_card") if isinstance(data.get("resolved_identity_card"), Mapping) else {}
    profile_caps = modem_profile.get("capabilities") if isinstance(modem_profile.get("capabilities"), Mapping) else {}
    action_support = modem_profile.get("action_support") if isinstance(modem_profile.get("action_support"), Mapping) else {}
    field_support = modem_profile.get("field_support") if isinstance(modem_profile.get("field_support"), Mapping) else {}
    identity = data.get("runtime_identity") if isinstance(data.get("runtime_identity"), Mapping) else {}
    onboarding = data.get("onboarding_status") if isinstance(data.get("onboarding_status"), Mapping) else {}
    provenance = _lower(resolved_card.get("profile_provenance") or modem_profile.get("provenance"))
    live_capability_evidence = {
        _lower(item)
        for item in data.get("live_capability_evidence", [])
        if _text(item)
    } if isinstance(data.get("live_capability_evidence"), (list, tuple, set, frozenset)) else set()

    actions: dict[str, Any] = {}
    for key, action_name in _ACTION_CAPABILITY_KEYS.items():
        enabled = _action_enabled_from_payload(report=data, key=key, profile_cap_key=_profile_capability_key_for_action(action_name))
        profile_cap_key = _profile_capability_key_for_action(action_name)
        overlay_trust = _text(action_support.get(action_name))
        profile_mentions = profile_cap_key in profile_caps
        state, write_allowed = _action_state(
            enabled=enabled,
            overlay_trust=overlay_trust,
            provenance=provenance,
            profile_mentions=bool(profile_mentions),
        )
        evidence_allowed, evidence_reason = _action_evidence_gate(
            action_key=key,
            onboarding=onboarding,
            base_write_allowed=write_allowed,
        )
        actions[key] = {
            "state": state,
            "write_allowed": (
                safe_bool(write_allowed, False)
                and safe_bool(evidence_allowed, False)
            ),
            "raw_enabled": bool(enabled) if enabled is not None else False,
            "overlay_trust": _lower(overlay_trust) or "untested",
            "action_family": "rf_control" if key in _RF_EVIDENCE_ACTIONS else "session_control" if key in _SESSION_EVIDENCE_ACTIONS else "other",
            "evidence_allowed": bool(evidence_allowed),
            "evidence_reason": str(evidence_reason or ""),
            "source": (
                f"overlay_{_lower(overlay_trust)}"
                if _lower(overlay_trust) in {"supported", "degraded", "unsupported"}
                else "backend_capability"
                if enabled is not None
                else "unknown"
            ),
        }

    reads: dict[str, Any] = {}
    for key in _READ_CAPABILITY_KEYS:
        enabled = _bool_or_none(data.get(key))
        profile_key = f"supports_{key}"
        if bool(enabled) and key in live_capability_evidence:
            state = "supported"
            source = "live_capability_evidence"
        else:
            state = _feature_state(
                enabled=enabled,
                provenance=provenance,
                profile_mentions=profile_key in profile_caps,
            )
            source = "profile_capability" if profile_key in profile_caps else "backend_capability" if enabled is not None else "unknown"
        reads[key] = {
            "state": state,
            "raw_enabled": bool(enabled) if enabled is not None else False,
            "source": source,
        }

    fields: dict[str, Any] = {}
    unsupported_fields: list[str] = []
    known_field_states: dict[str, str] = {}
    field_conflicts = _field_support_conflicts(field_support)
    for field in field_support.get("supported") or []:
        known_field_states[str(field)] = "supported" if provenance in _SUPPORTED_PROVENANCE else "estimated"
    for field in field_support.get("unsupported") or []:
        known_field_states[str(field)] = "unsupported"
    synthetic = field_support.get("synthetic")
    if isinstance(synthetic, Mapping):
        for field in synthetic.keys():
            known_field_states[str(field)] = "estimated"
    trust_map = modem_profile.get("field_trust")
    if isinstance(trust_map, Mapping):
        for field, trust in trust_map.items():
            known_field_states[str(field)] = _field_state_from_trust(str(trust))
    for field, state in sorted(known_field_states.items()):
        fields[field] = {"state": state}
        if state in {"unsupported", "non_useful"}:
            unsupported_fields.append(field)

    backend = _text(
        resolved_card.get("backend")
        or data.get("backend")
        or identity.get("backend")
        or ""
    )
    backend_family = _text(
        resolved_card.get("family")
        or data.get("backend_family")
        or identity.get("backend_family")
        or (data.get("support") or {}).get("family")
    )
    if not backend_family and backend:
        from arch4.operator_profiles import infer_backend_family
        backend_family = infer_backend_family(backend)
    profile_id = _text(resolved_card.get("profile_id") or modem_profile.get("profile_id") or "")
    model = _text(resolved_card.get("model") or identity.get("model") or data.get("model") or "")
    firmware = _text(resolved_card.get("firmware") or identity.get("firmware") or data.get("firmware") or "")
    vendor = _text(resolved_card.get("vendor") or identity.get("vendor") or data.get("vendor") or "")
    identity_state = _text(identity.get("identity_state") or "")
    backend_io_contract = dict(data.get("backend_io_contract")) if isinstance(data.get("backend_io_contract"), Mapping) else {}
    modem_live_access_profile = (
        dict(data.get("modem_live_access_profile") or {})
        if isinstance(data.get("modem_live_access_profile"), Mapping)
        else {}
    )
    backend_io_lane_contract = _backend_io_lane_contract(
        backend=backend,
        backend_family=backend_family,
        profile_id=profile_id,
        model=model,
        firmware=firmware,
        vendor=vendor,
        backend_io_contract=backend_io_contract,
        modem_live_access_profile=modem_live_access_profile,
    )

    for meta in actions.values():
        meta["io_lane"] = "write_action"
        meta["io_lock_required"] = bool(backend_io_lane_contract["write_action"]["io_lock_required"])
        meta["capability_gate_required"] = True
        meta["serialization_required"] = bool(backend_io_lane_contract["io_serial_required"])
        meta["direct_modem_writes_allowed"] = bool(
            backend_io_lane_contract["write_action"].get("direct_modem_writes_allowed", False)
        )

    return {
        "version": 1,
        "backend_io_contract": backend_io_contract,
        "backend_io_lane_contract": backend_io_lane_contract,
        "backend_feature_contract": dict(data.get("backend_feature_contract"))
        if isinstance(data.get("backend_feature_contract"), Mapping)
        else {},
        "identity_state": identity_state,
        "backend": backend,
        "backend_family": backend_family,
        "profile_id": profile_id,
        "profile_provenance": provenance,
        "resolved_identity_exact": safe_bool(resolved_card.get("exact_identity", False), False),
        "resolved_identity_provisional": safe_bool(resolved_card.get("provisional", False), False),
        "resolved_identity_provisional_reason": _text(resolved_card.get("provisional_reason") or ""),
        "actions": actions,
        "reads": reads,
        "fields": fields,
        "unsupported_fields": unsupported_fields,
        "field_conflicts": field_conflicts,
        "canonical_surface": _canonical_surface_contract(
            fields=fields,
            field_conflicts=field_conflicts,
            backend=backend,
            backend_family=backend_family,
            profile_id=profile_id,
            identity_state=identity_state,
        ),
        "write_allowed_actions": sorted(key for key, meta in actions.items() if safe_bool(meta.get("write_allowed"), False)),
    }


def capability_contract_action_enabled(payload: Mapping[str, Any] | None, key: str) -> bool | None:
    meta = capability_contract_action_meta(payload, key)
    if not isinstance(meta, Mapping):
        return None
    return safe_bool(meta.get("write_allowed", False), False)


def capability_contract_action_meta(payload: Mapping[str, Any] | None, key: str) -> Mapping[str, Any] | None:
    data = payload if isinstance(payload, Mapping) else {}
    contract = data.get("capability_contract") if isinstance(data.get("capability_contract"), Mapping) else {}
    actions = contract.get("actions") if isinstance(contract.get("actions"), Mapping) else {}
    meta = actions.get(str(key or "").strip().lower()) if isinstance(actions, Mapping) else None
    if not isinstance(meta, Mapping):
        return None
    return meta


def capability_contract_read_enabled(payload: Mapping[str, Any] | None, key: str) -> bool | None:
    meta = capability_contract_read_meta(payload, key)
    if not isinstance(meta, Mapping):
        return None
    state = _lower(meta.get("state"))
    if state in {"supported", "estimated"}:
        return True
    if state in {"unsupported", "non_useful"}:
        return False
    return None


def capability_contract_read_meta(payload: Mapping[str, Any] | None, key: str) -> Mapping[str, Any] | None:
    data = payload if isinstance(payload, Mapping) else {}
    contract = data.get("capability_contract") if isinstance(data.get("capability_contract"), Mapping) else {}
    reads = contract.get("reads") if isinstance(contract.get("reads"), Mapping) else {}
    meta = reads.get(str(key or "").strip().lower()) if isinstance(reads, Mapping) else None
    if not isinstance(meta, Mapping):
        return None
    return meta


def capability_contract_unsupported_fields(payload: Mapping[str, Any] | None) -> frozenset[str]:
    data = payload if isinstance(payload, Mapping) else {}
    contract = data.get("capability_contract") if isinstance(data.get("capability_contract"), Mapping) else {}
    unsupported = contract.get("unsupported_fields")
    if isinstance(unsupported, (list, tuple, set, frozenset)):
        return frozenset(str(field) for field in unsupported if field)
    return frozenset()
