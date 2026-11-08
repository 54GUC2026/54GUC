"""
Black-box modem/router recognition scaffold.

Phase 1A should classify the attached box before RF/bootstrap calibration
starts, so family reuse can be attempted safely.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List

from arch4.modem.catalog import (
    FAMILY_UNKNOWN,
    family_probe_order,
    family_reuse_policy,
    get_backend_entry,
    get_backend_family,
    match_candidate_router_profile,
)


_PROBE_STEP_PURPOSES: Dict[str, str] = {
    "http_root": "verify a reachable web surface exists before deeper probing",
    "webui_session_token": "confirm the web UI session/token flow used by the router family",
    "webui_login_state": "detect whether a passive or authenticated web session is available",
    "identity_endpoint": "read vendor/model/firmware identity safely",
    "router_identity": "read router identity from the primary management surface",
    "serving_cell_endpoint": "verify serving-cell and RF reads on the family web surface",
    "serving_cell_snapshot": "verify serving-cell and RF reads from the generic path",
    "lte_monitor_snapshot": "verify RouterOS LTE monitor read coverage",
    "at_servingcell_read": "verify modem serving-cell reads through AT access",
    "traffic_stats_endpoint": "verify traffic/session counters on the family web surface",
    "traffic_counter_read": "verify traffic/session counters from the generic path",
    "modemmanager_presence": "verify ModemManager is present and usable",
}

_PROBE_STEP_OVERLAP: Dict[str, List[str]] = {
    "webui_session_token": ["auth_login"],
    "webui_login_state": ["auth_login", "safe_capability_detection"],
    "routeros_api_login": ["auth_login"],
    "identity_endpoint": ["identity_model_fw"],
    "router_identity": ["identity_model_fw"],
    "serving_cell_endpoint": ["serving_cell_rf_read"],
    "serving_cell_snapshot": ["serving_cell_rf_read"],
    "lte_monitor_snapshot": ["serving_cell_rf_read"],
    "at_servingcell_read": ["serving_cell_rf_read"],
    "traffic_stats_endpoint": ["traffic_session_read"],
    "traffic_counter_read": ["traffic_session_read"],
}


@dataclass(frozen=True)
class BlackBoxRecognitionResult:
    backend_hint: str
    family_hint: str
    vendor: str
    model: str
    firmware: str
    confidence: float
    implemented_backend_match: bool
    profile_match: str
    reuse_backend_hint: str
    reuse_status: str
    overlay_strategy: str
    overlay_risk: str
    conservative_capability_only: bool
    transports: List[str]
    probe_order: List[str]
    probe_plan: List[Dict[str, Any]]
    required_overlap: List[str]
    overlap_confirmed: List[str]
    overlap_projected: List[str]
    overlap_missing: List[str]
    minimum_overlap_score: int
    minimum_overlap_total: int
    recommended_next_step: str
    notes: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _identity_confidence(vendor: str, model: str, firmware: str) -> float:
    score = 0.0
    if vendor:
        score += 0.25
    if model:
        score += 0.45
    if firmware:
        score += 0.20
    return min(1.0, score)


def _dedupe(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _projected_overlap(
    *,
    family: str,
    reuse_backend_hint: str,
    implemented_match: bool,
    vendor: str,
    model: str,
    firmware: str,
    transports: List[str],
    required_overlap: List[str],
) -> tuple[List[str], List[str], List[str]]:
    if implemented_match:
        return list(required_overlap), [], []

    confirmed: List[str] = []
    projected: List[str] = []

    if vendor or model or firmware:
        confirmed.append("identity_model_fw")
    if transports:
        confirmed.append("auth_login")

    if family != FAMILY_UNKNOWN:
        projected.extend(["serving_cell_rf_read", "traffic_session_read", "safe_capability_detection"])
    if reuse_backend_hint:
        projected.append("safe_capability_detection")

    confirmed = [item for item in _dedupe(confirmed) if item in required_overlap]
    projected = [item for item in _dedupe(projected) if item in required_overlap and item not in confirmed]
    missing = [item for item in required_overlap if item not in confirmed and item not in projected]
    return confirmed, projected, missing


def _build_probe_plan(
    probe_order: List[str],
    *,
    overlap_confirmed: List[str],
    overlap_projected: List[str],
) -> List[Dict[str, Any]]:
    confirmed = set(overlap_confirmed)
    projected = set(overlap_projected)
    out: List[Dict[str, Any]] = []
    for step in probe_order:
        overlap_keys = list(_PROBE_STEP_OVERLAP.get(step, []))
        status = "probe_required"
        if overlap_keys and all(key in confirmed for key in overlap_keys):
            status = "already_confirmed"
        elif overlap_keys and any(key in projected for key in overlap_keys):
            status = "preferred_next_probe"
        out.append(
            {
                "step": str(step),
                "purpose": str(_PROBE_STEP_PURPOSES.get(step, "verify this probe path for safe family reuse")),
                "overlap": overlap_keys,
                "status": status,
            }
        )
    return out


def recognize_black_box(identity: Dict[str, Any] | None = None, backend_hint: str = "") -> Dict[str, Any]:
    ident = identity if isinstance(identity, dict) else {}
    backend = str(backend_hint or ident.get("backend", "") or "").strip().lower()
    vendor = str(ident.get("vendor", "") or ident.get("manufacturer", "") or "").strip()
    model = str(ident.get("model", "") or "").strip()
    firmware = str(ident.get("firmware", "") or ident.get("software_version", "") or "").strip()

    notes: List[str] = []
    reuse_backend_hint = ""
    confidence = _identity_confidence(vendor, model, firmware)
    profile_match = ""
    overlay_strategy = ""
    overlay_risk = "high"
    reuse_status = "unknown_black_box"
    conservative_capability_only = True

    implemented = get_backend_entry(backend)
    if implemented:
        family = get_backend_family(backend)
        confidence = 1.0
        reuse_backend_hint = backend
        profile_match = implemented.display_name
        overlay_strategy = "implemented_backend_runtime_match"
        overlay_risk = "low"
        reuse_status = "implemented_backend_match"
        conservative_capability_only = False
        notes.append("runtime already reports an implemented backend")
        transports = list(implemented.transports)
        implemented_match = True
    else:
        matched = match_candidate_router_profile(vendor, model)
        if matched:
            family = matched.family
            reuse_backend_hint = matched.reuse_backend_hint
            profile_match = matched.model
            overlay_strategy = matched.overlay_strategy
            overlay_risk = matched.overlay_risk
            confidence = 0.75 if reuse_backend_hint else 0.55
            notes.append(matched.notes)
            notes.append("family reuse should be preferred before creating a new backend")
            transports = list(matched.transports)
            reuse_status = (
                "family_reuse_pending_probe"
                if reuse_backend_hint
                else "family_probe_first"
            )
        else:
            family = FAMILY_UNKNOWN
            confidence = 0.20 if (vendor or model) else 0.0
            transports = []
            overlay_strategy = "unknown_family_probe_first"
            notes.append("insufficient identity data for family-level reuse")
            reuse_status = "unknown_black_box"
        implemented_match = False

    policy = family_reuse_policy(family)
    if family != FAMILY_UNKNOWN and not overlay_strategy:
        overlay_strategy = "family_probe_before_overlay_decision"
    required_overlap = list(policy.get("required_overlap", []) or [])
    overlap_confirmed, overlap_projected, overlap_missing = _projected_overlap(
        family=family,
        reuse_backend_hint=reuse_backend_hint,
        implemented_match=bool(implemented_match),
        vendor=vendor,
        model=model,
        firmware=firmware,
        transports=transports,
        required_overlap=required_overlap,
    )
    minimum_overlap_score = len(overlap_confirmed)
    minimum_overlap_total = len(required_overlap)
    if implemented_match:
        recommended_next_step = "ready_for_rf_bootstrap_calibration"
    elif reuse_backend_hint:
        recommended_next_step = "verify_family_overlap_before_enabling_control"
    elif family != FAMILY_UNKNOWN:
        recommended_next_step = "probe_black_box_family_surface"
    else:
        recommended_next_step = "collect_identity_and_transport_fingerprint"
    probe_order = list(policy.get("probe_order", []) or family_probe_order(family))
    result = BlackBoxRecognitionResult(
        backend_hint=backend,
        family_hint=family,
        vendor=vendor,
        model=model,
        firmware=firmware,
        confidence=float(confidence),
        implemented_backend_match=bool(implemented_match),
        profile_match=str(profile_match),
        reuse_backend_hint=str(reuse_backend_hint),
        reuse_status=str(reuse_status),
        overlay_strategy=str(overlay_strategy or policy.get("overlay_default_risk", "") or ""),
        overlay_risk=str(overlay_risk or policy.get("overlay_default_risk", "high")),
        conservative_capability_only=bool(conservative_capability_only),
        transports=transports,
        probe_order=probe_order,
        probe_plan=_build_probe_plan(
            probe_order,
            overlap_confirmed=overlap_confirmed,
            overlap_projected=overlap_projected,
        ),
        required_overlap=required_overlap,
        overlap_confirmed=overlap_confirmed,
        overlap_projected=overlap_projected,
        overlap_missing=overlap_missing,
        minimum_overlap_score=int(minimum_overlap_score),
        minimum_overlap_total=int(minimum_overlap_total),
        recommended_next_step=str(recommended_next_step),
        notes=notes,
    )
    out = result.to_dict()
    out["reuse_policy"] = policy
    out["phase_1a"] = {
        "name": "device_black_box_recognition",
        "goal": "identify family, profile, transports, and safe capability envelope before RF/bootstrap calibration",
        "required_overlap": list(required_overlap),
        "minimum_overlap_score": int(minimum_overlap_score),
        "minimum_overlap_total": int(minimum_overlap_total),
        "recommended_next_step": str(recommended_next_step),
    }
    return out
