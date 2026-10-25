from __future__ import annotations

from typing import Any, Mapping

from arch4.coerce import safe_bool


_RF_ACTION_KEYS = frozenset(
    {"band_select", "band_lock", "cell_lock", "pband", "nr_band_lock"}
)
_SESSION_ACTION_KEYS = frozenset({"restart", "wifi_control"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _action_evidence_capture_state(
    *,
    payload: Mapping[str, Any],
    focus: str,
    rf_needed: bool,
    session_needed: bool,
) -> dict[str, Any]:
    data = payload if isinstance(payload, Mapping) else {}
    live = data.get("live_capture") if isinstance(data.get("live_capture"), Mapping) else {}
    parser = data.get("parser") if isinstance(data.get("parser"), Mapping) else {}

    radio_connected = safe_bool(live.get("radio_connected"), False)
    serving_band = _int(live.get("serving_band"))
    pci = _int(live.get("pci"))
    serving_band_signature = _text(live.get("serving_band_signature"))
    session_uptime_s = _float(live.get("session_uptime_s"))
    status_fields = parser.get("status_fields") if isinstance(parser.get("status_fields"), list) else []
    status_visible = bool(status_fields)

    rf_capture_ready = bool(radio_connected and (serving_band > 0 or serving_band_signature) and pci > 0)
    session_capture_ready = bool(status_visible or session_uptime_s > 0.0 or radio_connected)

    if focus == "rf_control":
        ready = rf_capture_ready
        missing = []
        if not radio_connected:
            missing.append("radio link")
        if serving_band <= 0 and not serving_band_signature:
            missing.append("serving band")
        if pci <= 0:
            missing.append("PCI")
        summary = (
            f"Current session already shows RF telemetry ({serving_band_signature or f'B{serving_band}'}, PCI {pci})."
            if ready
            else "Current session is still missing live RF telemetry: " + ", ".join(missing) + "."
        )
        return {
            "status": "ready" if ready else "pending",
            "ready": bool(ready),
            "summary": summary,
            "missing": missing,
        }

    if focus == "session_control":
        ready = session_capture_ready
        missing = []
        if not status_visible:
            missing.append("live modem status")
        if session_uptime_s <= 0.0 and not radio_connected:
            missing.append("active session/runtime state")
        summary = (
            "Current session already shows live modem status and session/runtime state for evidence export."
            if ready
            else "Current session is still missing session/runtime evidence: " + ", ".join(missing) + "."
        )
        return {
            "status": "ready" if ready else "pending",
            "ready": bool(ready),
            "summary": summary,
            "missing": missing,
        }

    if focus == "all_write_actions" or (rf_needed and session_needed):
        ready = rf_capture_ready and session_capture_ready
        missing = []
        if not rf_capture_ready:
            missing.append("RF telemetry")
        if not session_capture_ready:
            missing.append("session/runtime evidence")
        summary = (
            "Current session already shows both RF telemetry and session/runtime evidence for export."
            if ready
            else "Current session is still missing: " + ", ".join(missing) + "."
        )
        return {
            "status": "ready" if ready else "pending",
            "ready": bool(ready),
            "summary": summary,
            "missing": missing,
        }

    if focus == "evidence_level_unknown":
        ready = rf_capture_ready or session_capture_ready
        summary = (
            "Current session already shows live modem telemetry that can be exported for evidence review."
            if ready
            else "Current session still needs live modem telemetry before evidence export is useful."
        )
        missing = [] if ready else ["live modem telemetry"]
        return {
            "status": "ready" if ready else "pending",
            "ready": bool(ready),
            "summary": summary,
            "missing": missing,
        }

    return {
        "status": "",
        "ready": False,
        "summary": "",
        "missing": [],
    }


def _promotion_policy(
    *,
    identity_state: str,
    onboarding_state: str,
    evidence_status: str,
    calibration_refresh_required: bool,
    action_evidence_focus: str,
    action_evidence_capture_ready: bool,
    action_evidence_capture_missing: list[str],
) -> dict[str, Any]:
    focus = _lower(action_evidence_focus)
    state = _lower(onboarding_state)
    identity = _lower(identity_state)
    evidence = _lower(evidence_status)
    missing = [str(item) for item in (action_evidence_capture_missing or []) if str(item or "").strip()]

    if identity in {"unknown", "vendor_known_model_unknown"} or state in {"unknown_device", "profile_missing"}:
        return {
            "target_tier": 1,
            "target_label": "T1 Observed",
            "summary": "Promote this modem to T1 Observed by confirming identity and collecting a first compatibility evidence bundle.",
            "requirements": [
                "Confirm backend family and runtime owner",
                "Capture live modem telemetry in a normal session",
                "Export a compatibility evidence bundle for review",
            ],
            "tests": [
                "manual_first_connect",
                "runtime_identity_contract",
                "observe_first_unknown_device",
            ],
            "bundle_evidence": [
                "telemetry_production_*.jsonl",
                "telemetry_trace_matrix_*.jsonl",
                "events_*.jsonl",
                "capabilities.json",
                "ngui_page_truth.json and runtime_health.json",
            ],
        }

    if identity == "family_known_model_unknown" or state in {
        "family_known_model_unknown",
        "firmware_revalidation_required",
        "generic_family_support",
    } or evidence in {"collecting", "family_level_only", "unknown"}:
        requirements = [
            "Confirm exact model and firmware signature",
            "Capture action-family evidence in a live session",
            "Export a compatibility evidence bundle for review",
        ]
        tests = [
            "support_tier_gate",
            "capability_contract_gate",
            "firmware_downgrade_regression",
        ]
        if focus == "rf_control":
            requirements.insert(1, "Capture live RF telemetry with serving band and PCI visible")
            tests.append("rf_control_smoke")
        elif focus == "session_control":
            requirements.insert(1, "Capture normal session/runtime activity with live status visible")
            tests.append("session_control_smoke")
        if calibration_refresh_required:
            requirements.append("Refresh calibration after firmware evidence review")
        if missing:
            requirements.append("Still missing in this session: " + ", ".join(missing))
        return {
            "target_tier": 2,
            "target_label": "T2 Controlled",
            "summary": "Promote this modem to T2 Controlled by proving exact model/firmware behavior and validating safe write authority.",
            "requirements": requirements,
            "tests": tests,
            "bundle_evidence": [
                "telemetry_production_*.jsonl",
                "telemetry_trace_matrix_*.jsonl",
                "events_*.jsonl",
                "capabilities.json",
                "BUNDLE_SUMMARY.txt",
                "COMPATIBILITY_EVIDENCE.txt",
            ],
        }

    if state in {"model_specific_support", "exact_firmware_support", "ready"} and evidence == "sufficient":
        return {
            "target_tier": 3,
            "target_label": "T3 Optimized",
            "summary": "Promote this modem to T3 Optimized by keeping model-specific evidence stable and validating multiple supported write actions.",
            "requirements": [
                "Keep exact model/firmware evidence stable across normal runtime",
                "Verify at least two supported write actions remain gated correctly",
                "Keep support-bundle diagnostics aligned with runtime truth",
            ],
            "tests": [
                "support_tier_matrix",
                "capability_action_gating",
                "support_bundle_identity_contract",
            ],
            "bundle_evidence": [
                "capabilities.json",
                "ngui_page_truth.json and runtime_health.json",
                "BUNDLE_SUMMARY.txt",
            ],
        }

    return {
        "target_tier": 0,
        "target_label": "",
        "summary": "",
        "requirements": [],
        "tests": [],
        "bundle_evidence": [],
    }


def _action_evidence_summary(
    *,
    payload: Mapping[str, Any],
    evidence_status: str,
) -> dict[str, Any]:
    data = payload if isinstance(payload, Mapping) else {}
    contract = data.get("capability_contract") if isinstance(data.get("capability_contract"), Mapping) else {}
    actions = contract.get("actions") if isinstance(contract.get("actions"), Mapping) else {}
    if not isinstance(actions, Mapping) or not actions:
        return {
            "status": "",
            "summary": "",
            "unlock_level": "",
            "strip_hint": "",
            "rf_ready": False,
            "session_ready": False,
            "capture_hint": "",
            "bundle_prompt": "",
        }

    def _candidate(meta: Mapping[str, Any] | None) -> bool:
        if not isinstance(meta, Mapping):
            return False
        state = _lower(meta.get("state"))
        return state in {"supported", "estimated"} or safe_bool(meta.get("raw_enabled"), False)

    rf_candidates = {
        key: meta for key, meta in actions.items()
        if key in _RF_ACTION_KEYS and _candidate(meta if isinstance(meta, Mapping) else None)
    }
    session_candidates = {
        key: meta for key, meta in actions.items()
        if key in _SESSION_ACTION_KEYS and _candidate(meta if isinstance(meta, Mapping) else None)
    }
    rf_ready = any(safe_bool(meta.get("write_allowed"), False) for meta in rf_candidates.values())
    session_ready = any(safe_bool(meta.get("write_allowed"), False) for meta in session_candidates.values())
    rf_needed = bool(rf_candidates) and not bool(rf_ready)
    session_needed = bool(session_candidates) and not bool(session_ready)

    if evidence_status == "collecting":
        if rf_needed and session_needed:
            summary = (
                "54GUC is collecting exact model and firmware evidence while supported write actions remain available unless proven unsupported."
            )
            focus = "all_write_actions"
            next_step = "collect_exact_model_firmware_evidence"
            operator_action = (
                "Keep this exact modem and firmware connected during a normal live session, export a compatibility "
                "evidence bundle, and send it to 54GUC support so both session and RF control can be reviewed."
            )
            collection_target = "exact_model_firmware"
            capture_hint = (
                "Keep the modem in a normal live session with both radio telemetry and session state visible before exporting."
            )
            bundle_prompt = (
                "Export a compatibility evidence bundle after both RF telemetry and normal session activity have been observed."
            )
        elif rf_needed:
            summary = (
                "54GUC is collecting exact model and firmware evidence for RF control while supported RF actions remain available unless proven unsupported."
            )
            focus = "rf_control"
            next_step = "collect_exact_model_firmware_evidence_for_rf_control"
            operator_action = (
                "Keep this exact modem and firmware connected, export a compatibility evidence bundle while RF "
                "telemetry is visible, and send it to 54GUC support so RF control can be reviewed."
            )
            collection_target = "exact_model_firmware_rf"
            capture_hint = (
                "Keep RF telemetry visible with live band, PCI, and signal values on screen before exporting."
            )
            bundle_prompt = (
                "Export a compatibility evidence bundle once RF telemetry is visible and stable for this modem."
            )
        elif session_needed:
            summary = (
                "54GUC is collecting session-control evidence while supported session actions remain available unless proven unsupported."
            )
            focus = "session_control"
            next_step = "collect_session_control_evidence"
            operator_action = (
                "Keep this modem connected during a normal management session, export a compatibility evidence "
                "bundle, and send it to 54GUC support so session control can be reviewed."
            )
            collection_target = "session_control"
            capture_hint = (
                "Keep a normal management session active so session controls, events, and runtime state are visible before exporting."
            )
            bundle_prompt = (
                "Export a compatibility evidence bundle after normal session activity and runtime events have been captured."
            )
        else:
            summary = "54GUC is collecting compatibility evidence for this exact model and firmware."
            focus = ""
            next_step = "collect_exact_model_firmware_evidence"
            operator_action = (
                "Keep this exact modem and firmware connected during a normal live session, export a compatibility "
                "evidence bundle, and send it to 54GUC support for review."
            )
            collection_target = "exact_model_firmware"
            capture_hint = "Keep the modem connected during a normal live session before exporting."
            bundle_prompt = "Export a compatibility evidence bundle after normal live telemetry has been captured."
        capture = _action_evidence_capture_state(
            payload=data,
            focus=focus,
            rf_needed=rf_needed,
            session_needed=session_needed,
        )
        return {
            "status": "collecting",
            "summary": summary,
            "unlock_level": "exact_model_firmware_evidence",
            "strip_hint": "ACTION EVIDENCE COLLECTING",
            "rf_ready": bool(rf_ready),
            "session_ready": bool(session_ready),
            "focus": focus,
            "next_step": next_step,
            "operator_action": operator_action,
            "collection_target": collection_target,
            "capture_hint": capture_hint,
            "bundle_prompt": bundle_prompt,
            "capture_status": str(capture.get("status", "") or ""),
            "capture_ready": safe_bool(capture.get("ready"), False),
            "capture_summary": str(capture.get("summary", "") or ""),
            "capture_missing": list(capture.get("missing") or []),
        }
    capture = _action_evidence_capture_state(
        payload=data,
        focus="rf_control" if evidence_status == "family_level_only" and rf_candidates else "evidence_level_unknown" if evidence_status == "unknown" else "",
        rf_needed=rf_needed,
        session_needed=session_needed,
    )
    if evidence_status == "unknown":
        return {
            "status": "unknown",
            "summary": "54GUC has not determined the current compatibility evidence level yet.",
            "unlock_level": "known_evidence_level",
            "strip_hint": "ACTION EVIDENCE UNKNOWN",
            "rf_ready": bool(rf_ready),
            "session_ready": bool(session_ready),
            "focus": "evidence_level_unknown",
            "next_step": "collect_identity_and_compatibility_evidence",
            "operator_action": (
                "Keep this modem connected, export a compatibility evidence bundle, and send it to 54GUC support so the "
                "current identity and evidence level can be reviewed."
            ),
            "collection_target": "identity_and_compatibility",
            "capture_hint": "Keep the modem connected until identity and live telemetry stabilize before exporting.",
            "bundle_prompt": "Export a compatibility evidence bundle after identity and live telemetry are visible.",
            "capture_status": str(capture.get("status", "") or ""),
            "capture_ready": safe_bool(capture.get("ready"), False),
            "capture_summary": str(capture.get("summary", "") or ""),
            "capture_missing": list(capture.get("missing") or []),
        }
    if evidence_status == "family_level_only" and rf_candidates:
        summary = (
            "Session control is evidence-ready; exact model and firmware evidence is still being collected for RF control."
            if session_ready
            else "Exact model and firmware evidence is still being collected for RF control."
        )
        return {
            "status": "rf_exact_model_required",
            "summary": summary,
            "unlock_level": "exact_model_firmware_evidence",
            "strip_hint": "RF NEEDS EXACT MODEL/FW",
            "rf_ready": safe_bool(rf_ready, False),
            "session_ready": safe_bool(session_ready, False),
            "focus": "rf_control",
            "next_step": "collect_exact_model_firmware_evidence_for_rf_control",
            "operator_action": (
                "Keep this exact modem and firmware connected, export a compatibility evidence bundle while RF "
                "telemetry is visible, and send it to 54GUC support so RF control can be reviewed."
            ),
            "collection_target": "exact_model_firmware_rf",
            "capture_hint": "Keep RF telemetry visible with live band, PCI, and signal values on screen before exporting.",
            "bundle_prompt": "Export a compatibility evidence bundle once RF telemetry is visible and stable for this modem.",
            "capture_status": str(capture.get("status", "") or ""),
            "capture_ready": safe_bool(capture.get("ready"), False),
            "capture_summary": str(capture.get("summary", "") or ""),
            "capture_missing": list(capture.get("missing") or []),
        }
    if rf_ready or session_ready:
        return {
            "status": "sufficient",
            "summary": "Supported write actions have enough compatibility evidence for normal operation.",
            "unlock_level": "none",
            "strip_hint": "",
            "rf_ready": bool(rf_ready),
            "session_ready": bool(session_ready),
            "focus": "",
            "next_step": "",
            "operator_action": "",
            "collection_target": "",
            "capture_hint": "",
            "bundle_prompt": "",
            "capture_status": "ready",
            "capture_ready": True,
            "capture_summary": "Current session already shows enough live evidence for normal operation.",
            "capture_missing": [],
        }
    return {
        "status": "",
        "summary": "",
        "unlock_level": "",
        "strip_hint": "",
        "rf_ready": bool(rf_ready),
        "session_ready": bool(session_ready),
        "focus": "",
        "next_step": "",
        "operator_action": "",
        "collection_target": "",
        "capture_hint": "",
        "bundle_prompt": "",
        "capture_status": "",
        "capture_ready": False,
        "capture_summary": "",
        "capture_missing": [],
    }


def build_onboarding_status(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    data = payload if isinstance(payload, Mapping) else {}
    identity = data.get("runtime_identity") if isinstance(data.get("runtime_identity"), Mapping) else {}
    resolved_card = (
        data.get("resolved_identity_card")
        if isinstance(data.get("resolved_identity_card"), Mapping)
        else {}
    )
    identity_state = _lower(identity.get("identity_state"))
    effective_profile = resolved_card
    match_quality = _lower(effective_profile.get("match_quality"))
    firmware_revalidation_required = safe_bool(effective_profile.get("firmware_revalidation_required"), False)
    identity_confidence = max(0.0, min(1.0, _float(identity.get("identity_confidence"))))
    exact_identity = safe_bool(resolved_card.get("exact_identity", False), False)
    provisional_reason = _lower(resolved_card.get("provisional_reason"))

    state = "ready"
    next_step = "normal_operation"
    write_downgraded = False
    collect_evidence = False
    calibration_refresh_required = False
    if exact_identity or match_quality == "exact_firmware":
        state = "exact_firmware_support"
        next_step = "normal_operation"
    elif identity_state in {"unknown", "vendor_known_model_unknown"}:
        state = "unknown_device"
        next_step = "collect_compatibility_evidence"
        write_downgraded = True
        collect_evidence = True
    elif identity_state == "family_known_model_unknown":
        state = "family_known_model_unknown"
        next_step = "collect_compatibility_evidence"
        write_downgraded = True
        collect_evidence = True
    elif not effective_profile:
        state = "profile_missing"
        next_step = "collect_compatibility_evidence"
        write_downgraded = True
        collect_evidence = True
    elif firmware_revalidation_required:
        state = "firmware_revalidation_required"
        next_step = "collect_compatibility_evidence_and_refresh_calibration"
        write_downgraded = True
        collect_evidence = True
        calibration_refresh_required = True
    elif provisional_reason == "family_baseline" or match_quality == "family_baseline":
        state = "generic_family_support"
        next_step = "review_capability_truth"
    elif provisional_reason == "model_specific_needs_exact_firmware" or match_quality == "model_specific":
        state = "model_specific_support"
        next_step = "normal_operation"

    evidence_status = "unknown"
    evidence_summary = "Compatibility evidence has not been established yet."
    evidence_sufficient = False
    if collect_evidence:
        evidence_status = "collecting"
        evidence_summary = "Compatibility evidence is still being collected while supported write actions remain available unless proven unsupported."
    elif state == "generic_family_support":
        evidence_status = "family_level_only"
        evidence_summary = "Only family-level compatibility evidence is available so far."
    else:
        evidence_status = "sufficient"
        evidence_summary = "Enough compatibility evidence has been collected for normal operation."
        evidence_sufficient = True

    action_evidence = _action_evidence_summary(
        payload=data,
        evidence_status=evidence_status,
    )
    promotion = _promotion_policy(
        identity_state=identity_state,
        onboarding_state=state,
        evidence_status=evidence_status,
        calibration_refresh_required=calibration_refresh_required,
        action_evidence_focus=str(action_evidence.get("focus", "") or ""),
        action_evidence_capture_ready=safe_bool(action_evidence.get("capture_ready"), False),
        action_evidence_capture_missing=list(action_evidence.get("capture_missing") or []),
    )

    return {
        "state": state,
        "next_step": next_step,
        "write_downgraded": bool(write_downgraded),
        "collect_evidence": bool(collect_evidence),
        "calibration_refresh_required": bool(calibration_refresh_required),
        "match_quality": match_quality or "none",
        "profile_id": _text(effective_profile.get("profile_id")),
        "evidence_status": evidence_status,
        "evidence_summary": evidence_summary,
        "evidence_sufficient": bool(evidence_sufficient),
        "action_evidence_status": str(action_evidence.get("status", "") or ""),
        "action_evidence_summary": str(action_evidence.get("summary", "") or ""),
        "action_evidence_unlock_level": str(action_evidence.get("unlock_level", "") or ""),
        "action_evidence_strip_hint": str(action_evidence.get("strip_hint", "") or ""),
        "action_evidence_focus": str(action_evidence.get("focus", "") or ""),
        "action_evidence_next_step": str(action_evidence.get("next_step", "") or ""),
        "action_evidence_operator_action": str(action_evidence.get("operator_action", "") or ""),
        "action_evidence_collection_target": str(action_evidence.get("collection_target", "") or ""),
        "action_evidence_capture_hint": str(action_evidence.get("capture_hint", "") or ""),
        "action_evidence_bundle_prompt": str(action_evidence.get("bundle_prompt", "") or ""),
        "action_evidence_capture_status": str(action_evidence.get("capture_status", "") or ""),
        "action_evidence_capture_ready": safe_bool(action_evidence.get("capture_ready"), False),
        "action_evidence_capture_summary": str(action_evidence.get("capture_summary", "") or ""),
        "action_evidence_capture_missing": list(action_evidence.get("capture_missing") or []),
        "promotion_target_tier": int(promotion.get("target_tier", 0) or 0),
        "promotion_target_label": str(promotion.get("target_label", "") or ""),
        "promotion_summary": str(promotion.get("summary", "") or ""),
        "promotion_requirements": list(promotion.get("requirements") or []),
        "promotion_tests": list(promotion.get("tests") or []),
        "promotion_bundle_evidence": list(promotion.get("bundle_evidence") or []),
        "action_evidence_rf_ready": safe_bool(action_evidence.get("rf_ready"), False),
        "action_evidence_session_ready": safe_bool(action_evidence.get("session_ready"), False),
        "identity_confidence": identity_confidence,
        "identity_confidence_pct": int(round(identity_confidence * 100.0)),
    }
