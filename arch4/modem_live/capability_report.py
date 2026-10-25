"""Publish deterministic Modem Live capability data."""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Mapping, Optional

from arch4.interfaces import Arch4Config, Constraints
from arch4.lte_mcs import canonical_qam_label
from arch4.modem.backend_contracts import BackendIoContract
from arch4.modem.catalog import (
    describe_backend_support,
    describe_runtime_profile,
    describe_runtime_profile_by_id,
    get_backend_action_semantics,
    get_backend_entry,
)
from arch4.modem.live_translation import (
    apply_live_alias_translation,
    apply_scheduler_field_trust,
    build_modem_live_capture_surface,
    scheduler_field_trust as modem_live_scheduler_field_trust,
)
from arch4.modem.recognition import recognize_black_box
from arch4.modem_live.runtime_identity_projection import (
    build_resolved_identity_card_section,
)
from arch4.modem_live.truth_matrix import ValidatedTruthMatrixReader
from arch4.operator_profiles import (
    family_label,
    family_support_text,
    infer_backend_family,
)
from arch4.runtime_capability import build_capability_contract
from arch4.runtime_identity import (
    build_runtime_identity,
    coerce_provider_name_text,
    coerce_provider_plmn_text,
    is_placeholder_identity_text,
    runtime_identity_from_capabilities_payload,
)
from arch4.runtime_identity_card import resolve_resolved_identity_card
from arch4.runtime_onboarding import build_onboarding_status
from arch4.runtime_operator_contract import (
    LIVE_CAPTURE_ALIASES,
    LIVE_CAPTURE_LIST_FIELDS,
    LIVE_CAPTURE_SCALAR_FIELDS,
)
from arch4.runtime_support_tier import (
    build_support_tier,
    publish_support_authority_fields,
)
from arch4.signal_processing.policy_thresholds import policy_threshold_authority

log = logging.getLogger(__name__)


def capability_report_from_matrix(
    matrix: ValidatedTruthMatrixReader | Mapping[str, Any],
) -> Dict[str, Any]:
    """Return the capability payload already accepted by Matrix.

    Reporting only reads Matrix. It must not contact a backend, merge a profile,
    infer identity, or fill a missing field.
    """

    reader = (
        matrix
        if isinstance(matrix, ValidatedTruthMatrixReader)
        else ValidatedTruthMatrixReader(matrix)
    )
    cell = reader.cell("capability", "runtime_capabilities")
    if not cell.available:
        return {}
    if not isinstance(cell.value, dict):
        raise TypeError("matrix_runtime_capabilities_mapping_required")
    return dict(cell.value)

class CapabilityReportSnapshot:
    """Read-only modem data prepared by Modem Live for reports.

    This object has no transport. Reporting code may read it but must not call
    backend drivers.
    """

    def __init__(
        self,
        *,
        info: Optional[Dict[str, Any]] = None,
        status: Optional[Dict[str, Any]] = None,
        live_capture: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._info = dict(info or {})
        self._status = dict(status or {})
        snapshot = dict(live_capture or {})
        for key, value in snapshot.items():
            setattr(self, key, value)

    def get_modem_info(self) -> Dict[str, Any]:
        return dict(self._info)

    def get_last_status(self) -> Dict[str, Any]:
        return dict(self._status)

    def get_report_info(self) -> Dict[str, Any]:
        return dict(self._info)

    def get_report_status(self) -> Dict[str, Any]:
        return dict(self._status)

def _is_backend_driver_surface(surface: Any) -> bool:
    module = str(getattr(type(surface), "__module__", "") or "")
    return module.startswith("arch4.modem.") and not isinstance(surface, CapabilityReportSnapshot)

def _report_surface_dict(surface: Any, *method_names: str) -> Dict[str, Any]:
    """Read a report snapshot without opening backend transport.

    Runtime passes ``CapabilityReportSnapshot`` here. Test doubles may expose
    the same methods. Real backend drivers are rejected so reports cannot become
    another modem access path.
    """
    if surface is None or _is_backend_driver_surface(surface):
        return {}
    for method_name in method_names:
        fn = getattr(surface, method_name, None)
        if not callable(fn):
            continue
        try:
            data = fn() or {}
        except Exception as exc:
            log.debug("report snapshot %s suppressed: %s", method_name, exc)
            continue
        if isinstance(data, dict):
            return dict(data)
    return {}

def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}

def _copy_identity_projection(
    target: Dict[str, Any],
    source: Dict[str, Any],
    *,
    authoritative: bool = False,
) -> None:
    if not isinstance(target, dict) or not isinstance(source, dict):
        return
    for src_key, dst_key in (
        ("vendor", "vendor"),
        ("model", "model"),
        ("router_model", "model"),
        ("device_model", "model"),
        ("firmware", "firmware"),
        ("software_version", "software_version"),
        ("webui", "webui"),
        ("config_version", "config_version"),
        ("serial", "serial"),
        ("imei", "imei"),
        ("imsi", "imsi"),
        ("hardware_version", "hardware_version"),
        ("lan_mac", "lan_mac"),
        ("wan_ip", "wan_ip"),
        ("provider", "provider"),
        ("provider_name", "provider_name"),
        ("provider_plmn", "provider_plmn"),
        ("plmn", "plmn"),
        ("os_name", "os_name"),
        ("os_version", "os_version"),
    ):
        if src_key in {"provider", "provider_name"}:
            value = coerce_provider_name_text(source.get(src_key, ""))
        elif src_key in {"provider_plmn", "plmn"}:
            value = coerce_provider_plmn_text(source.get(src_key, ""))
        else:
            value = str(source.get(src_key, "") or "").strip()
        existing = target.get(dst_key)
        if value and (authoritative or is_placeholder_identity_text(existing)):
            target[dst_key] = value

def _build_report_runtime_identity(data: Dict[str, Any], *, backend: str) -> Dict[str, Any]:
    """Map report identity data to the normalized identity fields."""
    identity = data.get("identity") if isinstance(data.get("identity"), dict) else {}
    provenance = data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
    profile_name = str(
        data.get("profile_name")
        or data.get("profile")
        or provenance.get("profile_name")
        or ""
    )
    runtime_identity = build_runtime_identity(
        backend=backend,
        backend_family=str(data.get("backend_family") or provenance.get("backend_family") or ""),
        vendor=str(identity.get("vendor") or ""),
        model=str(identity.get("model") or ""),
        firmware=str(identity.get("firmware") or ""),
        provider_name=str(identity.get("provider_name") or identity.get("provider") or ""),
        provider_plmn=str(identity.get("provider_plmn") or identity.get("plmn") or ""),
        runtime_dir=str(data.get("runtime_dir") or provenance.get("runtime_dir") or ""),
        memory_dir=str(data.get("memory_dir") or provenance.get("memory_dir") or ""),
        instance_id=str(data.get("instance_id") or provenance.get("instance_id") or ""),
        profile_name=profile_name,
    ).to_dict()
    for key in (
        "target",
        "iface",
        "iface_id",
        "sim_id",
        "serial",
        "imei",
        "imsi",
        "hardware_version",
        "software_version",
        "webui",
        "config_version",
        "lan_mac",
        "wan_ip",
        "antenna",
        "antenna_mode",
        "antenna_mode_source",
        "os_name",
        "os_version",
    ):
        value = str(identity.get(key) or data.get(key) or "")
        if value:
            runtime_identity[key] = value
    return runtime_identity

def parser_mode_for_backend(backend: str) -> str:
    """Return the transport description for a backend.

    Use catalog transport values instead of hardcoded protocol names. Vendor
    names stay at the backend boundary.
    """
    from arch4.modem.catalog import get_backend_entry
    entry = get_backend_entry(backend)
    if entry is None:
        return "unknown"
    return "+".join(entry.transports) if entry.transports else "unknown"

def _truthy_profile_capability(value: Any) -> bool:
    if isinstance(value, str):
        text = value.strip().lower()
        return text in {"1", "true", "yes", "enabled", "available", "ok", "cat12_implied"}
    return bool(value)

def _constraint_static_payload(constraints: Any) -> Dict[str, Any]:
    """Return only static facts supported by evidence.

    A default ``Constraints(False)`` means "not proven yet", not "unsupported".
    False is exported only when startup or live evidence observed that field.
    """
    if constraints is None:
        return {}
    evidence_keys = getattr(constraints, "_capability_static_evidence_keys", None)
    allowed = {str(key) for key in evidence_keys} if evidence_keys is not None else None
    out: Dict[str, Any] = {}
    for key in (
        "ttl_ipv4",
        "ttl_ipv6",
        "mss_ipv4",
        "mss_ipv6",
        "keepalive",
        "dl_256qam",
        "ul_64qam",
        "pci_lock_available",
    ):
        if not hasattr(constraints, key):
            continue
        value = bool(getattr(constraints, key, False))
        if allowed is not None:
            if key in allowed:
                out[key] = value
        elif value:
            out[key] = value
    try:
        ue_category = int(getattr(constraints, "ue_category", 0) or 0)
    except Exception:
        ue_category = 0
    if allowed is not None:
        if "ue_category" in allowed:
            out["ue_category"] = ue_category
    elif ue_category > 0:
        out["ue_category"] = ue_category
    return out

def _apply_profile_static_truth(data: Dict[str, Any], profile: Dict[str, Any]) -> None:
    """Publish profile router-check facts into capability data."""
    static = data.get("static")
    if not isinstance(static, dict):
        static = {}
        data["static"] = static
    evidence_raw = data.get("capability_static_evidence_keys")
    if isinstance(evidence_raw, (list, tuple, set, frozenset)):
        evidence_keys = {str(key) for key in evidence_raw if str(key or "").strip()}
    elif isinstance(evidence_raw, dict):
        evidence_keys = {str(key) for key in evidence_raw if str(key or "").strip()}
    else:
        evidence_keys = set()

    def _set_profile_fact(key: str, value: Any) -> None:
        # Safe live evidence wins. The profile fills a gap only when this session
        # did not read the static fact.
        if key in static and key in evidence_keys:
            return
        static[key] = value

    profile_static = profile.get("static_config")
    if isinstance(profile_static, dict):
        for key, value in profile_static.items():
            if str(key or "").strip():
                _set_profile_fact(str(key), "unsupported" if value == "unsupported" else bool(value))

    profile_ue = profile.get("ue_capability")
    if not isinstance(profile_ue, dict):
        return
    for key in ("dl_256qam", "ul_64qam"):
        if key in profile_ue:
            _set_profile_fact(key, _truthy_profile_capability(profile_ue.get(key)))
    if "ue_category" in profile_ue:
        try:
            _set_profile_fact("ue_category", int(str(profile_ue.get("ue_category") or "0")))
        except Exception:
            _set_profile_fact("ue_category", 0)

def _repair_capability_contract(data: Dict[str, Any], extra: Dict[str, Any]) -> None:
    alerts: list[str] = []
    live_evidence = data.get("live_capability_evidence")
    if not isinstance(live_evidence, list):
        live_evidence = []
        data["live_capability_evidence"] = live_evidence

    def _mark_live_evidence(key: str) -> None:
        text = str(key or "").strip()
        if text and text not in live_evidence:
            live_evidence.append(text)

    static = data.get("static") if isinstance(data.get("static"), dict) else {}
    if not isinstance(static, dict):
        static = {}
    semantics = data.get("backend_action_semantics") if isinstance(data.get("backend_action_semantics"), dict) else {}
    has_cell_lock_control = (
        str(semantics.get("cell_lock_control_mode") or "none") != "none"
        or str(semantics.get("cell_control_mode") or "none") != "none"
    )
    if not has_cell_lock_control:
        data["cell_lock"] = False
        if "pci_lock_available" in static:
            static["pci_lock_available"] = False

    if int(static.get("ue_category", 0) or 0) > 0 or bool(static.get("dl_256qam")) or bool(static.get("ul_64qam")):
        _mark_live_evidence("ue_capability")
        if not bool(data.get("ue_capability")):
            data["ue_capability"] = True
            alerts.append("ue_capability_flag_mismatch")

    if has_cell_lock_control and bool(static.get("pci_lock_available")):
        _mark_live_evidence("cell_lock")
        if not bool(data.get("cell_lock")):
            data["cell_lock"] = True
            alerts.append("cell_lock_flag_mismatch")

    def _has_exact_component_identity(row: Any) -> bool:
        if not isinstance(row, dict):
            return False
        try:
            band = int(row.get("band", 0) or 0)
            earfcn = int(row.get("earfcn", 0) or 0)
            pci = int(row.get("pci", -1) if row.get("pci") is not None else -1)
        except (TypeError, ValueError):
            return False
        return band > 0 and earfcn > 0 and pci >= 0

    carrier_details = extra.get("carrier_details")
    if isinstance(carrier_details, list) and any(
        _has_exact_component_identity(row)
        for row in carrier_details
    ):
        _mark_live_evidence("ca_details")
        if not bool(data.get("ca_details")):
            data["ca_details"] = True
            alerts.append("ca_details_flag_mismatch")

    neighbors = extra.get("neighbors")
    if isinstance(neighbors, list) and neighbors:
        _mark_live_evidence("neighbors")
        if not bool(data.get("neighbors")):
            data["neighbors"] = True
            alerts.append("neighbors_flag_mismatch")

    runtime_identity = data.get("runtime_identity") if isinstance(data.get("runtime_identity"), dict) else {}
    plmn = str(runtime_identity.get("provider_plmn", "") or runtime_identity.get("plmn", "") or "")
    provider = str(runtime_identity.get("provider_name", "") or runtime_identity.get("provider", "") or "")
    if plmn or provider:
        _mark_live_evidence("plmn")
        if not bool(data.get("plmn")):
            data["plmn"] = True
            alerts.append("plmn_flag_mismatch")

    contract = data.get("capability_contract") if isinstance(data.get("capability_contract"), dict) else {}
    canonical_surface = contract.get("canonical_surface") if isinstance(contract.get("canonical_surface"), dict) else {}
    if canonical_surface:
        if canonical_surface.get("field_conflicts"):
            alerts.append("canonical_surface_field_conflict")
        if canonical_surface.get("missing_groups"):
            alerts.append("canonical_surface_missing_group")
        if canonical_surface.get("degraded_groups"):
            alerts.append("canonical_surface_degraded_group")

    if alerts:
        parser = data.get("parser")
        if not isinstance(parser, dict):
            parser = {}
            data["parser"] = parser
        parser["contract_alerts"] = sorted(set(str(a) for a in alerts))

def build_capabilities_report(
    *,
    cfg: Arch4Config,
    capabilities: Any,
    constraints: Constraints | None,
    modem: Any,
    deployed_bands: Iterable[int],
    effective_policy: Dict[str, Any],
    live_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    backend = str(cfg.modem_backend).lower()
    backend_entry = get_backend_entry(backend)
    _conn_resolver = getattr(cfg, "connection_defaults_for_backend", None)
    host, user = _conn_resolver(backend) if callable(_conn_resolver) else ("", "")
    _transport_resolver = getattr(cfg, "connection_transport_for_backend", None)
    transport = str(_transport_resolver(backend) or "unknown") if callable(_transport_resolver) else "unknown"
    if transport == "unknown" and backend_entry and backend_entry.transports:
        transport = " + ".join(backend_entry.transports)
    # Generic catalog capabilities help select a profile. They are not runtime
    # capability data. Only ``capability_contract`` below is exported to Matrix.
    support = {
        key: value
        for key, value in describe_backend_support(backend).items()
        if key != "capabilities"
    }
    backend_family = infer_backend_family(backend)
    backend_semantics = get_backend_action_semantics(backend)
    has_cell_lock_control = (
        str(getattr(backend_semantics, "cell_lock_control_mode", "none") or "none") != "none"
        or str(getattr(backend_semantics, "cell_control_mode", "none") or "none") != "none"
    )
    io_contract = BackendIoContract()
    data: Dict[str, Any] = {
        "backend": backend,
        "band_select": bool(capabilities and capabilities.supports_band_selection),
        "band_lock": bool(capabilities and getattr(capabilities, "supports_band_lock", False)),
        "cell_lock": bool(capabilities and capabilities.supports_cell_lock and has_cell_lock_control),
        "traffic_presence": bool(capabilities and capabilities.supports_traffic_presence),
        "traffic_presence_owner": str(getattr(capabilities, "traffic_presence_owner", "runtime_54guc") or "runtime_54guc"),
        "traffic_volume": bool(capabilities and capabilities.supports_traffic_volume),
        "ue_capability": bool(capabilities and capabilities.supports_ue_capability),
        "restart": bool(capabilities and capabilities.supports_restart),
        "neighbors": bool(capabilities and capabilities.supports_neighbors),
        "sms": bool(capabilities and getattr(capabilities, "supports_sms", False)),
        "ca_details": bool(capabilities and capabilities.supports_ca_details),
        "band_priority": bool(capabilities and capabilities.supports_band_priority),
        "plmn": bool(capabilities and capabilities.supports_plmn),
        "pband": bool(capabilities and getattr(capabilities, "supports_pband", False)),
        "nr_band_lock": bool(getattr(capabilities, "supports_nr_band_lock", False)),
        "wifi_control": bool(getattr(capabilities, "supports_wifi_control", False)),
        "network_settings": bool(getattr(capabilities, "supports_network_settings", False)),
        "mobile_data": bool(getattr(capabilities, "supports_mobile_data", False)),
        "radio_mode": bool(getattr(capabilities, "supports_radio_mode", False)),
        "backend_io_contract": io_contract.as_dict(),
        "backend_feature_contract": {
            "version": 1,
            "unsupported_distinct_from_empty": True,
            "failed_distinct_from_empty": True,
            "result_states": [
                "ok",
                "empty_valid",
                "timeout",
                "auth_failed",
                "transport_error",
                "malformed_response",
                "unsupported",
                "stale_cache",
                "failed",
            ],
        },
        "backend_action_semantics": {
            "band_control_mode": str(getattr(backend_semantics, "band_control_mode", "none") or "none"),
            "pband_control_mode": str(getattr(backend_semantics, "pband_control_mode", "none") or "none"),
            "ca_pool_control_mode": str(getattr(backend_semantics, "ca_pool_control_mode", "none") or "none"),
            "cell_control_mode": str(getattr(backend_semantics, "cell_control_mode", "none") or "none"),
            "cell_lock_control_mode": str(getattr(backend_semantics, "cell_lock_control_mode", "none") or "none"),
            "restart_mode": str(getattr(backend_semantics, "restart_mode", "none") or "none"),
            "traffic_presence_mode": str(getattr(backend_semantics, "traffic_presence_mode", "none") or "none"),
            "traffic_visibility": str(getattr(backend_semantics, "traffic_visibility", "none") or "none"),
            "ca_visibility": str(getattr(backend_semantics, "ca_visibility", "none") or "none"),
            "safety_grade": str(getattr(backend_semantics, "safety_grade", "unknown") or "unknown"),
        },
        "static": _constraint_static_payload(constraints),
        "identity": {
            # This identity input is only for the report. ``runtime_identity``
            # separately exports modem and session identity.
            "provider": coerce_provider_name_text(getattr(constraints, "provider_name", "")),
            "plmn": coerce_provider_plmn_text(getattr(constraints, "provider_plmn", "")),
            "target": str(host or ""),
            "user": str(user or ""),
            "iface": str(transport or "unknown"),
            "iface_id": "",
            "vendor": str((backend_entry.vendor if backend_entry else "") or ""),
            "deployed_bands": ",".join(
                f"B{b}" for b in sorted({int(b) for b in deployed_bands or [] if int(b) > 0})
            ),
            "model": "",
            "firmware": "",
            "serial": "",
            "imei": "",
            "imsi": "",
            "hardware_version": "",
            "software_version": "",
            "webui": "",
            "config_version": "",
            "lan_mac": "",
            "wan_ip": "",
            "antenna": "",
            "os_name": "",
            "os_version": "",
        },
        "signal_contract": {
            "sinr_scale": float(getattr(cfg, "signal_sinr_scale", 1.0) or 1.0),
            "rsrp_scale": float(getattr(cfg, "signal_rsrp_scale", 1.0) or 1.0),
            "rsrq_scale": float(getattr(cfg, "signal_rsrq_scale", 1.0) or 1.0),
            "rssi_scale": float(getattr(cfg, "signal_rssi_scale", 1.0) or 1.0),
            "warn": bool(getattr(cfg, "signal_contract_warn", True)),
            "units": {
                "sinr": "dB",
                "rsrp": "dBm",
                "rsrq": "dB",
                "rssi": "dBm",
            },
        },
        "parser": {
            "mode": parser_mode_for_backend(backend),
            "normalization_applied": True,
        },
        "support": {**support, "family": backend_family, "family_label": family_label(backend_family), "family_support_text": family_support_text(backend_family)},
        "signal_provenance": {
            "rsrq_source": "reported",
            "cqi_source": "reported",
        },
        "decision_constraints": policy_threshold_authority(cfg),
        "effective_policy": effective_policy,
        "live_capture": build_modem_live_capture_surface(
            modem=modem,
            capture_scope="startup_probe",
            capture_source="modem_snapshot",
        ),
    }
    if constraints is not None:
        for key in ("antenna_external", "nr5g_mode_5g_only"):
            if hasattr(constraints, key):
                data["static"][key] = getattr(constraints, key)
        if hasattr(constraints, "antenna_external"):
            data["static"]["antenna_switch_api_supported"] = bool(getattr(constraints, "antenna_external"))
        evidence_keys = getattr(constraints, "_capability_static_evidence_keys", None)
        if evidence_keys is not None:
            allowed = {str(key) for key in evidence_keys}
            data["capability_static_evidence_keys"] = sorted(allowed)
            data["static"] = {
                key: value
                for key, value in data["static"].items()
                if key in allowed
            }
    def _merge_live_capture(raw: Any, *, source_name: str, parser_field_key: str) -> None:
        if not isinstance(raw, dict):
            return
        data["parser"][parser_field_key] = sorted(str(k) for k in raw.keys())
        live_capture = data.get("live_capture")
        if not isinstance(live_capture, dict):
            live_capture = {}
            data["live_capture"] = live_capture
        for field in LIVE_CAPTURE_SCALAR_FIELDS:
            keys = LIVE_CAPTURE_ALIASES.get(field, (field,))
            value = None
            for key in keys:
                candidate = raw.get(key)
                if candidate not in (None, "", [], {}):
                    value = candidate
                    break
            if value not in (None, "", [], {}):
                live_capture[field] = value
        for field in LIVE_CAPTURE_LIST_FIELDS:
            value = raw.get(field)
            if isinstance(value, list):
                live_capture[field] = list(value)
        neighbors = raw.get("neighbors")
        if isinstance(neighbors, list):
            live_capture["neighbors"] = [n for n in neighbors if isinstance(n, dict)]
        live_capture["capture_source"] = str(source_name)
        if source_name == "live_status":
            live_capture["capture_scope"] = "live_sample"
        apply_live_alias_translation(live_capture, raw)

    try:
        if backend_family == "routeros_at_like":
            info = _report_surface_dict(modem, "get_report_status", "get_last_status")
        else:
            info = _report_surface_dict(modem, "get_report_info", "get_modem_info")
        if isinstance(info, dict):
            # This identity view is only for the report. ``runtime_identity``
            # exports modem and session identity.
            _copy_identity_projection(data["identity"], info)
    except Exception as exc:
        log.debug("unknown: suppressed %s", exc)
        pass
    try:
        extra = _report_surface_dict(modem, "get_report_status", "get_last_status")
        extra = extra or {}
        if isinstance(extra, dict):
            status_resolved_card = _mapping(extra.get("resolved_identity_card"))
            status_identity_display = _mapping(extra.get("runtime_identity_display"))
            status_runtime_identity = _mapping(extra.get("runtime_identity"))
            _copy_identity_projection(
                data["identity"],
                status_resolved_card,
                authoritative=bool(status_resolved_card.get("exact_identity")),
            )
            _copy_identity_projection(
                data["identity"],
                status_identity_display,
                authoritative=bool(status_identity_display),
            )
            _copy_identity_projection(data["identity"], status_runtime_identity)
            _copy_identity_projection(data["identity"], extra)
        modem_trans_mode = str(getattr(modem, "trans_mode", "") or "")
        modem_rrc = str(getattr(modem, "rrc", "") or "")
        modem_antenna = str(getattr(modem, "antenna_mode", getattr(modem, "antenna", "")) or "")
        if modem_trans_mode:
            data["parser"]["backend_mode"] = modem_trans_mode
        elif modem_rrc:
            data["parser"]["backend_mode"] = modem_rrc
        if modem_antenna:
            data["identity"]["antenna"] = modem_antenna
        if isinstance(extra, dict):
            _merge_live_capture(extra, source_name="backend_status", parser_field_key="status_fields")
            if not data["parser"].get("backend_mode") and "trans_mode" in extra and extra.get("trans_mode"):
                data["parser"]["backend_mode"] = str(extra.get("trans_mode"))
            elif not data["parser"].get("backend_mode") and "rrc" in extra and extra.get("rrc"):
                data["parser"]["backend_mode"] = str(extra.get("rrc"))
            antenna = str(extra.get("antenna_mode", extra.get("antenna", "")) or "")
            if antenna and not data["identity"].get("antenna"):
                data["identity"]["antenna"] = antenna
        allowed = getattr(modem, "configured_band_pool", None)
        if not isinstance(allowed, list) or not allowed:
            allowed = extra.get("configured_band_pool") if isinstance(extra, dict) else None
        if isinstance(allowed, list) and allowed:
            norm = []
            for b in allowed:
                try:
                    bi = int(b)
                except (TypeError, ValueError):
                    continue
                if bi > 0:
                    norm.append(bi)
            if norm:
                data["identity"]["deployed_bands"] = ",".join(f"B{b}" for b in sorted(set(norm)))
    except Exception:
        extra = {}
    try:
        if isinstance(live_snapshot, dict) and live_snapshot:
            _merge_live_capture(
                live_snapshot,
                source_name="live_status",
                parser_field_key="live_status_fields",
            )
            live_access_profile = live_snapshot.get("modem_live_access_profile")
            if isinstance(live_access_profile, dict):
                data["modem_live_access_profile"] = dict(live_access_profile)
            for live_identity_source in (
                _mapping(live_snapshot.get("resolved_identity_card")),
                _mapping(live_snapshot.get("runtime_identity_display")),
                _mapping(live_snapshot.get("runtime_identity")),
                _mapping(live_snapshot.get("_canonical_status")),
                _mapping(live_snapshot.get("_base_status")),
            ):
                if live_identity_source:
                    _copy_identity_projection(
                        data["identity"],
                        live_identity_source,
                        authoritative=True,
                    )
            _copy_identity_projection(data["identity"], live_snapshot)
    except Exception as exc:
        log.debug("merge_live_capture live snapshot suppressed: %s", exc)
    try:
        # Keep ``runtime_identity`` as the complete report identity. Smaller live
        # and status snapshots are inputs, not replacement identity sources.
        data["runtime_identity"] = _build_report_runtime_identity(data, backend=backend)
    except Exception as exc:
        log.debug("runtime identity owner projection suppressed: %s", exc)
        data["runtime_identity"] = {}
    runtime_identity_preview: Dict[str, Any] = {}
    try:
        runtime_identity_preview = runtime_identity_from_capabilities_payload(data)
    except Exception as exc:
        log.debug("runtime identity preview suppressed: %s", exc)
        runtime_identity_preview = {}
    try:
        live_resolved_card_hint = (
            live_snapshot.get("resolved_identity_card")
            if isinstance(live_snapshot, dict) and isinstance(live_snapshot.get("resolved_identity_card"), dict)
            else {}
        )
        status_resolved_card_hint = _mapping(extra.get("resolved_identity_card")) if isinstance(extra, dict) else {}
        data_resolved_card_hint = _mapping(data.get("resolved_identity_card"))
        runtime_identity_resolved_profile_id = ""
        runtime_identity_data = data.get("runtime_identity")
        if isinstance(runtime_identity_data, dict):
            runtime_identity_resolved_profile_id = str(runtime_identity_data.get("resolved_profile_id") or "").strip()
        resolved_profile_id = ""
        for card in (live_resolved_card_hint, status_resolved_card_hint, data_resolved_card_hint):
            if bool(card.get("exact_identity")) and str(card.get("profile_id") or "").strip():
                resolved_profile_id = str(card.get("profile_id") or "").strip()
                break
        if not resolved_profile_id and runtime_identity_resolved_profile_id:
            resolved_profile_id = runtime_identity_resolved_profile_id
        if resolved_profile_id:
            profile = describe_runtime_profile_by_id(resolved_profile_id)
            if not profile:
                raise RuntimeError(
                    "modem_live_capability_exact_profile_unknown"
                )
        else:
            profile = describe_runtime_profile(
                backend=backend,
                vendor=runtime_identity_preview.get("vendor", ""),
                model=runtime_identity_preview.get("model", ""),
                firmware=runtime_identity_preview.get("firmware", ""),
                provider_name=runtime_identity_preview.get("provider_name", ""),
                provider_plmn=runtime_identity_preview.get("provider_plmn", ""),
                os_name=runtime_identity_preview.get("os_name", ""),
                os_version=runtime_identity_preview.get("os_version", ""),
            )
        if profile:
            data["modem_profile"] = profile
            if not isinstance(data.get("signal_provenance"), dict):
                data["signal_provenance"] = {}
            # Derive signal source from the profile's unsupported-field list.
            unsupported = []
            fs = profile.get("field_support")
            if isinstance(fs, dict):
                unsupported = fs.get("unsupported", []) or []
            if "rsrq_db" in unsupported:
                data["signal_provenance"]["rsrq_source"] = "estimated"
            if "cqi" in unsupported:
                data["signal_provenance"]["cqi_source"] = "estimated"
            # CQI may be calculated by 54GUC. In that case it is estimated, not
            # reported by the modem.
            _synth_raw = fs.get("synthetic") if isinstance(fs, dict) else None
            _synth_keys = (
                list(_synth_raw.keys()) if isinstance(_synth_raw, dict)
                else list(_synth_raw) if isinstance(_synth_raw, (list, tuple))
                else []
            )
            if "cqi" in _synth_keys and data["signal_provenance"].get("cqi_source") != "estimated":
                data["signal_provenance"]["cqi_source"] = "estimated"
            live_capture = data.get("live_capture")
            if isinstance(live_capture, dict):
                def _live_source_provided(field: str) -> bool:
                    if (
                        field.startswith("qam_")
                        and live_capture.get(field) not in (None, "", [], {})
                        and str(live_capture.get("qam_source") or "").strip().lower() == "reported"
                    ):
                        return True
                    keys = set(LIVE_CAPTURE_ALIASES.get(field, (field,)))
                    keys.add(field)
                    for parser_key in ("live_status_fields", "status_fields"):
                        seen = data.get("parser", {}).get(parser_key, [])
                        if isinstance(seen, list) and any(str(k) in keys for k in seen):
                            return True
                    return False

                for field in ("qam_dl0", "qam_dl1", "qam_ul"):
                    live_capture[field] = canonical_qam_label(live_capture.get(field))
                if "qam_dl0" in unsupported and not _live_source_provided("qam_dl0"):
                    live_capture["qam_dl0"] = ""
                if "qam_dl1" in unsupported and not _live_source_provided("qam_dl1"):
                    live_capture["qam_dl1"] = ""
                if "qam_ul" in unsupported and not _live_source_provided("qam_ul"):
                    live_capture["qam_ul"] = ""
                resolved_exact = bool((data.get("resolved_identity_card") or {}).get("exact_identity"))
                profile_match_quality = str(profile.get("match_quality") or "").strip().lower()
                profile_revalidation_required = bool(
                    profile.get("firmware_revalidation_required")
                    or profile.get("backend_model_revalidation_required")
                )
                exact_profile_trust = bool(resolved_profile_id) or (
                    profile_match_quality == "exact_firmware" and not profile_revalidation_required
                )
                profile_trust = (
                    profile.get("field_trust")
                    if isinstance(profile.get("field_trust"), dict)
                    else {}
                )
                if resolved_exact or exact_profile_trust or profile_trust:
                    apply_scheduler_field_trust(
                        live_capture,
                        modem_live_scheduler_field_trust(raw_base_status=data, modem=modem),
                    )
            profile_caps = profile.get("capabilities")
            if isinstance(profile_caps, dict):
                mapping = {
                    "supports_band_selection": "band_select",
                    "supports_band_lock": "band_lock",
                    "supports_cell_lock": "cell_lock",
                    "supports_traffic_presence": "traffic_presence",
                    "supports_traffic_volume": "traffic_volume",
                    "supports_ue_capability": "ue_capability",
                    "supports_restart": "restart",
                    "supports_neighbors": "neighbors",
                    "supports_sms": "sms",
                    "supports_ca_details": "ca_details",
                    "supports_band_priority": "band_priority",
                    "supports_plmn": "plmn",
                    "supports_pband": "pband",
                    "supports_nr_band_lock": "nr_band_lock",
                    "supports_wifi_control": "wifi_control",
                    "supports_network_settings": "network_settings",
                    "supports_mobile_data": "mobile_data",
                    "supports_radio_mode": "radio_mode",
                }
                for src, dst in mapping.items():
                    if src in profile_caps:
                        data[dst] = bool(profile_caps[src])
            qualified_network_firmware = profile.get("network_settings_firmware")
            if qualified_network_firmware and runtime_identity_preview.get("firmware") not in qualified_network_firmware:
                data["network_settings"] = False
                data["mobile_data"] = False
            _apply_profile_static_truth(data, profile)
        else:
            data["modem_profile"] = {}
    except Exception as exc:
        log.debug("runtime profile resolution suppressed: %s", exc)
    try:
        runtime_identity_preview = runtime_identity_from_capabilities_payload(data)
        status_resolved_card = _mapping(extra.get("resolved_identity_card")) if isinstance(extra, dict) else {}
        status_identity_display = _mapping(extra.get("runtime_identity_display")) if isinstance(extra, dict) else {}
        status_identity_card_source = {
            "backend": backend,
            "vendor": status_identity_display.get("vendor", ""),
            "model": status_identity_display.get("model", ""),
            "firmware": status_identity_display.get("firmware", ""),
            "hardware_version": status_identity_display.get("hardware_version", ""),
            "software_version": status_identity_display.get("software_version", ""),
            "webui": status_identity_display.get("webui", ""),
            "config_version": status_identity_display.get("config_version", ""),
            "provider_name": status_identity_display.get("provider_name", ""),
            "provider_plmn": status_identity_display.get("provider_plmn", ""),
            "os_name": status_identity_display.get("os_name", ""),
            "os_version": status_identity_display.get("os_version", ""),
        }
        runtime_identity_card_source = {
            "backend": backend,
            "vendor": runtime_identity_preview.get("vendor", ""),
            "model": runtime_identity_preview.get("model", ""),
            "firmware": runtime_identity_preview.get("firmware", ""),
            "hardware_version": runtime_identity_preview.get("hardware_version", ""),
            "software_version": runtime_identity_preview.get("software_version", ""),
            "webui": runtime_identity_preview.get("webui", ""),
            "config_version": runtime_identity_preview.get("config_version", ""),
            "provider_name": runtime_identity_preview.get("provider_name", ""),
            "provider_plmn": runtime_identity_preview.get("provider_plmn", ""),
            "os_name": runtime_identity_preview.get("os_name", ""),
            "os_version": runtime_identity_preview.get("os_version", ""),
        }
        live_resolved_card = (
            live_snapshot.get("resolved_identity_card")
            if isinstance(live_snapshot, dict) and isinstance(live_snapshot.get("resolved_identity_card"), dict)
            else {}
        )
        data["resolved_identity_card"] = resolve_resolved_identity_card(
            live_resolved_card,
            status_resolved_card,
            build_resolved_identity_card_section(status_identity_card_source),
            build_resolved_identity_card_section(runtime_identity_card_source),
        )
    except Exception as exc:
        log.debug("resolved identity card export suppressed: %s", exc)
        data["resolved_identity_card"] = {}
    try:
        iface_id = getattr(modem, "_lte_id", "")
        if iface_id:
            data["identity"]["iface_id"] = str(iface_id)
    except Exception as exc:
        log.debug("unknown: suppressed %s", exc)
        pass
    try:
        identity = runtime_identity_preview
        data["recognition"] = recognize_black_box(
            identity if isinstance(identity, dict) else {},
            backend_hint=backend,
        )
    except Exception:
        data["recognition"] = {}
    try:
        data["runtime_identity"] = runtime_identity_from_capabilities_payload(data)
    except Exception as exc:
        log.debug("runtime identity export suppressed: %s", exc)
        data["runtime_identity"] = {}
    # Correct profile and backend flags from Modem Live evidence before building
    # the capability contract, onboarding state, or support tier. This prevents
    # stale GUI or report data from winning before the next accepted live sample.
    _repair_capability_contract(data, extra if isinstance(extra, dict) else {})
    try:
        data["capability_contract"] = build_capability_contract(data)
    except Exception as exc:
        log.debug("capability contract export suppressed: %s", exc)
        data["capability_contract"] = {}
    try:
        data["onboarding_status"] = build_onboarding_status(data)
    except Exception as exc:
        log.debug("onboarding status export suppressed: %s", exc)
        data["onboarding_status"] = {}
    try:
        data["support_tier"] = build_support_tier(data)
        publish_support_authority_fields(data)
    except Exception as exc:
        log.debug("support tier export suppressed: %s", exc)
        data["support_tier"] = {}
    live_capture = data.get("live_capture")
    if isinstance(live_capture, dict):
        for field in ("qam_dl0", "qam_dl1", "qam_ul"):
            live_capture[field] = canonical_qam_label(live_capture.get(field))
    _repair_capability_contract(data, extra if isinstance(extra, dict) else {})
    return data
