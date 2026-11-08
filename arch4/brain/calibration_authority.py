"""
arch4/brain/calibration_authority.py

Central authority for calibration profiles.
Owns candidate/active lifecycle and versioned profile publish.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import copy
from functools import wraps
from inspect import isfunction
import time
import threading
from pathlib import Path
from typing import Any, Dict, Optional, List
import logging

log = logging.getLogger("arch4.brain.calibration_authority")

BACKGROUND_ANALYSIS_MIN_INTERVAL_S = 30.0
AUTHORITY_RAW_SAMPLE_KEEP_LAST = 800
AUTHORITY_SAMPLE_JOURNAL_COMPACT_AT = 2000
AUTHORITY_SAMPLE_JOURNAL_KEEP_LAST = 800

from arch4.interfaces import Arch4Config
from arch4.brain.calibration_workflow import (
    PHASE_ANCHOR_SURVEY,
    PHASE_BACKGROUND,
    PHASE_BOOTSTRAP,
    PHASE_COMPARATIVE,
)
from arch4.brain.calibration_external_knowledge import (
    build_external_knowledge_advisory,
    load_external_knowledge_file,
    merge_external_knowledge_advisories,
    sanitize_external_knowledge,
)
from arch4.brain.calibration_active_profile import (
    active_profile_from_state,
    build_active_metadata,
    build_calibration_profile,
)
from arch4.brain.calibration_collection_attempt import (
    build_collection_attempt_record,
    build_collection_attempt_status_fields,
)
from arch4.brain.calibration_monitoring import (
    build_background_monitoring_event,
    build_background_monitoring_recommendation,
    build_calibration_analysis_done_event,
)
from arch4.brain.calibration_candidate_lifecycle import (
    activate_candidate_profile,
    cancel_candidate_profile,
    detach_candidate_profile,
    mark_candidate_analysis_state,
    reject_candidate_profile,
    time_out_candidate_profile,
)
from arch4.brain.calibration_session import (
    DEFAULT_COLLECTION_TIMEOUT_S,
    DEFAULT_FIRST_SETUP_TIMEOUT_S,
    build_candidate_lineage,
    build_collection_session,
    normalize_workflow_mode,
)
from arch4.brain.calibration_decision_log import (
    build_analysis_report_journal_entry,
    build_candidate_decision_sample,
    build_context_baseline_seeded_history_entry,
    build_context_baseline_seeded_journal_entry,
    build_external_knowledge_import_journal_entry,
    build_raw_sample_journal_entry,
    build_recent_decision_history_entry,
)
from arch4.brain.calibration_review import (
    build_bootstrap_playbook,
    build_comparison_rows,
    build_gate_rows,
    build_operator_report,
    build_review_badge,
    build_session_outcome_report,
    build_summary_highlights,
)
from arch4.brain.calibration_scoring import (
    assess_bootstrap_primary_metrics,
    compare_candidate_to_reference,
    metric_avg,
    safe_float,
    score_bootstrap_baseline,
    summary_scalar,
    summarize_samples,
)
from arch4.brain.calibration_contract import (
    build_calibration_proposal,
    calibration_observation_error,
    calibration_proposal_error,
    evidence_scopes_match,
    observation_sample,
)
from arch4.brain.memory_calibration_store import (
    init_calibration_db,
    insert_calibration_observation,
)
from arch4.coerce import safe_bool
from arch4.brain.calibration_context import (
    context_baseline_storage_key,
    context_key_from_calibration_status,
    context_key_from_sample,
    context_key_from_summary,
    split_context_key,
)
from arch4.memory_store import CalibrationStore
from arch4.runtime_identity import (
    runtime_identity_from_capabilities_payload,
    build_runtime_session_scope,
    runtime_session_scope_key,
    runtime_session_scope_matches,
)
from arch4.brain.calibration_profile import (
    BACKGROUND_CANDIDATE_TRIGGER_HITS,
    BACKGROUND_TRIGGER_FAMILY_RULES,
    RECENT_SESSION_HISTORY_LIMIT,
)
from arch4.core_contracts.configuration_keys import CALIBRATION_PROFILE_KEYS
from arch4.brain.calibration_publication import (
    build_runtime_policy_publication,
    default_provider_time_behavior_publication,
    sanitize_provider_time_behavior_publication,
)
from arch4.brain.calibration_fact_confidence import (
    calibration_fact_confidence,
    fact_confidence_level,
)


_CAPABILITY_KEYS = (
    "band_select",
    "cell_lock",
    "traffic_presence",
    "traffic_volume",
    "ue_capability",
    "restart",
    "neighbors",
    "ca_details",
    "band_priority",
    "plmn",
)

BACKGROUND_ANALYSIS_WINDOW_MAX_SAMPLES = 300
DEFAULT_CALIBRATION_CHECKPOINT_INTERVAL_S = 300.0
DEFAULT_CALIBRATION_CHECKPOINT_MAX_DIRTY_EVENTS = 100


def _identity_provider_name(identity: Dict[str, Any]) -> str:
    return str(
        identity.get("provider_name", identity.get("provider", ""))
        or ""
    )


def _identity_provider_plmn(identity: Dict[str, Any]) -> str:
    return str(
        identity.get("provider_plmn", identity.get("plmn", ""))
        or ""
    )


def sanitize_recognition_context(raw: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(raw or {})
    overlap_confirmed = [
        str(v) for v in list(data.get("overlap_confirmed", []) or []) if str(v or "").strip()
    ][:8]
    overlap_projected = [
        str(v) for v in list(data.get("overlap_projected", []) or []) if str(v or "").strip()
    ][:8]
    overlap_missing = [
        str(v) for v in list(data.get("overlap_missing", []) or []) if str(v or "").strip()
    ][:8]
    probe_order = [
        str(v) for v in list(data.get("probe_order", []) or []) if str(v or "").strip()
    ][:8]
    return {
        "family_hint": str(data.get("family_hint", "") or ""),
        "profile_match": str(data.get("profile_match", "") or ""),
        "reuse_backend_hint": str(data.get("reuse_backend_hint", "") or ""),
        "reuse_status": str(data.get("reuse_status", "") or ""),
        "overlay_strategy": str(data.get("overlay_strategy", "") or ""),
        "overlay_risk": str(data.get("overlay_risk", "") or ""),
        "confidence": float(data.get("confidence", 0.0) or 0.0),
        "implemented_backend_match": bool(data.get("implemented_backend_match", False)),
        "conservative_capability_only": bool(data.get("conservative_capability_only", True)),
        "minimum_overlap_score": int(data.get("minimum_overlap_score", 0) or 0),
        "minimum_overlap_total": int(data.get("minimum_overlap_total", 0) or 0),
        "overlap_confirmed": overlap_confirmed,
        "overlap_projected": overlap_projected,
        "overlap_missing": overlap_missing,
        "probe_order": probe_order,
        "recommended_next_step": str(data.get("recommended_next_step", "") or ""),
    }


@dataclass
class CalibrationWorkflowStatus:
    phase: str = PHASE_BOOTSTRAP
    phase_reason: str = "runtime_context_incomplete"
    objective: str = ""
    next_step: str = ""
    mode_source: str = "inferred"
    runtime_context_ready: bool = False
    router_identity_ready: bool = False
    capability_probe_ready: bool = False
    recognition_ready: bool = False
    reference_profile_ready: bool = False
    baseline_ready: bool = False
    needs_calibration: bool = False
    collection_enabled: bool = True
    background_trigger_recommended: bool = False
    active_profile_id: str = ""
    active_profile_version: int = 0
    candidate_profile_id: str = ""
    candidate_workflow_mode: str = ""
    backend: str = ""
    target: str = ""
    provider: str = ""
    plmn: str = ""
    model: str = ""
    firmware: str = ""
    recognition_family: str = ""
    recognition_profile: str = ""
    recognition_reuse_backend: str = ""
    recognition_status: str = ""
    recognition_overlay_risk: str = ""
    recognition_next_step: str = ""
    capability_flags: Dict[str, bool] = field(default_factory=dict)
    static_flags: Dict[str, bool] = field(default_factory=dict)
    ts: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def sanitize_runtime_context(raw: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(raw or {})
    runtime_identity = (
        data.get("runtime_identity")
        if isinstance(data.get("runtime_identity"), dict)
        else {}
    )
    if not runtime_identity:
        runtime_identity = runtime_identity_from_capabilities_payload(data)
    identity = {
        "provider_name": _identity_provider_name(runtime_identity),
        "provider_plmn": _identity_provider_plmn(runtime_identity),
        "provider": _identity_provider_name(runtime_identity),
        "plmn": _identity_provider_plmn(runtime_identity),
        "target": str(runtime_identity.get("target", "") or ""),
        "iface": str(runtime_identity.get("iface", "") or ""),
        "iface_id": str(runtime_identity.get("iface_id", "") or ""),
        "vendor": str(runtime_identity.get("vendor", "") or ""),
        "model": str(runtime_identity.get("model", "") or ""),
        "firmware": str(runtime_identity.get("firmware", "") or ""),
        "serial": str(runtime_identity.get("serial", "") or ""),
        "imei": str(runtime_identity.get("imei", "") or ""),
        "imsi": str(runtime_identity.get("imsi", "") or ""),
        "sim_id": str(runtime_identity.get("sim_id", "") or ""),
    }
    static = data.get("static") if isinstance(data.get("static"), dict) else {}
    recognition = (
        data.get("recognition")
        if isinstance(data.get("recognition"), dict)
        else {}
    )
    out = {
        "backend": str(runtime_identity.get("backend", data.get("backend", "")) or ""),
        "runtime_identity": dict(runtime_identity),
        # Compatibility-only projection for older stored context readers.
        "identity": identity,
        "static": {str(k): bool(v) for k, v in static.items()},
        "recognition": sanitize_recognition_context(recognition),
    }
    for key in _CAPABILITY_KEYS:
        out[key] = bool(data.get(key, False))
    return out


def _default_provider_time_behavior_publication() -> Dict[str, Any]:
    return default_provider_time_behavior_publication()


def assess_calibration_workflow(
    *,
    state: Dict[str, Any],
    calibration_status: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    st = dict(state or {})
    runtime_context = sanitize_runtime_context(
        st.get("runtime_context") if isinstance(st.get("runtime_context"), dict) else {}
    )
    runtime_identity = (
        runtime_context.get("runtime_identity")
        if isinstance(runtime_context.get("runtime_identity"), dict)
        else {}
    )
    static = runtime_context.get("static", {})
    recognition = runtime_context.get("recognition", {})
    calibration = dict(calibration_status or {})

    active_profile_id = str(st.get("active_profile_id", "") or "")
    active_profile_version = int(st.get("active_profile_version", 0) or 0)
    candidate_profile_id = str(st.get("candidate_profile_id", "") or "")
    profiles = st.get("profiles", {}) if isinstance(st.get("profiles"), dict) else {}
    candidate_profile = profiles.get(candidate_profile_id, {}) if candidate_profile_id else {}
    candidate_workflow_mode = normalize_workflow_mode(
        candidate_profile.get("workflow_mode", "") if isinstance(candidate_profile, dict) else "",
        active_profile_id=active_profile_id,
        active_profile_version=active_profile_version,
    )

    capability_flags = {key: bool(runtime_context.get(key, False)) for key in _CAPABILITY_KEYS}
    router_identity_ready = bool(
        str(runtime_identity.get("target", "") or "")
        or str(runtime_identity.get("model", "") or "")
        or str(runtime_identity.get("firmware", "") or "")
    )
    capability_probe_ready = any(capability_flags.values()) or any(bool(v) for v in static.values())
    recognition_ready = bool(
        str(recognition.get("family_hint", "") or "")
        or bool(recognition.get("implemented_backend_match", False))
    )
    runtime_context_ready = router_identity_ready and capability_probe_ready
    active_profile_ready = active_profile_id not in {"", "C0"} and active_profile_version > 1
    active_reference_scope_match = bool(calibration.get("active_reference_scope_match", active_profile_ready))
    active_profile_ready = bool(active_profile_ready and active_reference_scope_match)
    baseline_ready = bool(calibration.get("baseline_ready", False))
    reference_profile_ready = bool(active_profile_ready or baseline_ready)
    needs_calibration = bool(calibration.get("needs_calibration", False))
    collection_enabled = bool(st.get("collection_enabled", True))

    status = CalibrationWorkflowStatus(
        phase=PHASE_BOOTSTRAP,
        phase_reason="runtime_context_incomplete",
        objective="Fingerprint the attached router and discover the calibration surface.",
        next_step="probe_router_identity_and_capabilities",
        runtime_context_ready=runtime_context_ready,
        router_identity_ready=router_identity_ready,
        capability_probe_ready=capability_probe_ready,
        recognition_ready=recognition_ready,
        reference_profile_ready=reference_profile_ready,
        baseline_ready=baseline_ready,
        needs_calibration=needs_calibration,
        collection_enabled=collection_enabled,
        active_profile_id=active_profile_id,
        active_profile_version=active_profile_version,
        candidate_profile_id=candidate_profile_id,
        candidate_workflow_mode=candidate_workflow_mode if candidate_profile_id else "",
        backend=str(runtime_context.get("backend", "") or ""),
        target=str(runtime_identity.get("target", "") or ""),
        provider=_identity_provider_name(runtime_identity),
        plmn=_identity_provider_plmn(runtime_identity),
        model=str(runtime_identity.get("model", "") or ""),
        firmware=str(runtime_identity.get("firmware", "") or ""),
        recognition_family=str(recognition.get("family_hint", "") or ""),
        recognition_profile=str(recognition.get("profile_match", "") or ""),
        recognition_reuse_backend=str(recognition.get("reuse_backend_hint", "") or ""),
        recognition_status=str(recognition.get("reuse_status", "") or ""),
        recognition_overlay_risk=str(recognition.get("overlay_risk", "") or ""),
        recognition_next_step=str(recognition.get("recommended_next_step", "") or ""),
        capability_flags=capability_flags,
        static_flags={str(k): bool(v) for k, v in static.items()},
        ts=float(time.time()),
    )

    if not runtime_context_ready:
        if recognition_ready and str(recognition.get("recommended_next_step", "") or ""):
            status.next_step = str(recognition.get("recommended_next_step", "") or "")
            status.phase_reason = "phase_1a_black_box_probe_incomplete"
            status.objective = "Fingerprint the attached router and verify minimum family overlap before RF calibration."
        return status.to_dict()

    if candidate_profile_id:
        if candidate_workflow_mode == PHASE_BOOTSTRAP:
            status.phase = PHASE_BOOTSTRAP
            status.phase_reason = "bootstrap_candidate_open"
            status.objective = "Build the first saved same-cell baseline from live RF evidence."
            status.next_step = "collect_bootstrap_rf_baseline"
            return status.to_dict()
        if candidate_workflow_mode == PHASE_COMPARATIVE:
            status.phase = PHASE_COMPARATIVE
            status.phase_reason = "comparative_candidate_open"
            status.objective = "Compare current same-cell RF evidence against the saved baseline."
            status.next_step = "collect_comparative_window"
            return status.to_dict()
        if candidate_workflow_mode == PHASE_ANCHOR_SURVEY:
            status.phase = PHASE_ANCHOR_SURVEY
            status.phase_reason = "anchor_survey_candidate_open"
            status.objective = "Test single anchor bands one by one before trusting a CA compare result."
            status.next_step = "collect_anchor_survey_window"
            return status.to_dict()
        if candidate_workflow_mode == PHASE_BACKGROUND:
            status.phase = PHASE_BACKGROUND
            status.phase_reason = "background_candidate_open"
            status.objective = "Collect a clean same-cell check and refresh the saved setup if it is better."
            status.next_step = "collect_background_candidate_window"
            status.background_trigger_recommended = True
            return status.to_dict()

    if not active_profile_ready:
        status.phase = PHASE_BOOTSTRAP
        status.phase_reason = "no_approved_profile_yet"
        status.objective = "Start setup and save this cell."
        status.next_step = "collect_bootstrap_rf_baseline"
        return status.to_dict()

    status.phase = PHASE_BACKGROUND
    status.phase_reason = "baseline_context_present"
    status.objective = "Watch this cell and refresh the saved setup when it clearly improves."
    status.next_step = "observe_same_cell_baseline"
    status.background_trigger_recommended = bool(collection_enabled and needs_calibration)
    return status.to_dict()


def latest_history_entry(entries: Any, *, kind: str = "") -> Dict[str, Any]:
    if not isinstance(entries, list):
        return {}
    target_kind = str(kind or "").strip()
    for row in reversed(entries):
        if not isinstance(row, dict):
            continue
        if target_kind and str(row.get("kind", "") or "") != target_kind:
            continue
        return copy.deepcopy(row)
    return {}


def build_runtime_status(
    *,
    active_metadata: Dict[str, Any],
    active_profile: Dict[str, Any],
    state: Dict[str, Any],
    reference_summary: Dict[str, Any],
    reference_meta: Dict[str, Any] | None = None,
    session_summarizer,
    runtime_context: Dict[str, Any],
    external_knowledge: Dict[str, Any],
    active_config_overrides: Dict[str, Any] | None = None,
    active_profile_tuning_proposals: list[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    meta = dict(active_metadata or {})
    active_history = list(state.get("recent_session_history", []) or [])
    latest_decision = latest_history_entry(active_history, kind="decision")
    candidate_id = str(meta.get("candidate_profile_id", "") or "")
    profiles = state.get("profiles", {}) if isinstance(state.get("profiles"), dict) else {}
    candidate = profiles.get(candidate_id, {}) if candidate_id else {}
    analysis = candidate.get("analysis_report") if isinstance(candidate.get("analysis_report"), dict) else {}
    raw = candidate.get("raw_samples") if isinstance(candidate.get("raw_samples"), list) else []
    session = candidate.get("collection_session") if isinstance(candidate.get("collection_session"), dict) else {}
    lineage = candidate.get("lineage") if isinstance(candidate.get("lineage"), dict) else {}
    session_report = session_summarizer(session, sample_count=len(raw)) if candidate_id else {}
    operator_report = analysis.get("operator_report") if isinstance(analysis.get("operator_report"), dict) else {}
    session_outcome = (
        analysis.get("session_outcome_report")
        if isinstance(analysis.get("session_outcome_report"), dict)
        else {}
    )
    comparison = analysis.get("comparison") if isinstance(analysis.get("comparison"), dict) else {}
    baseline_quality = (
        analysis.get("baseline_quality")
        if isinstance(analysis.get("baseline_quality"), dict)
        else {}
    )
    bootstrap_primary = (
        analysis.get("bootstrap_primary_metrics")
        if isinstance(analysis.get("bootstrap_primary_metrics"), dict)
        else {}
    )
    analysis_summary = analysis.get("sample_summary") if isinstance(analysis.get("sample_summary"), dict) else {}
    if analysis_summary and not baseline_quality:
        backend_name = str(runtime_context.get("backend", "") or "")
        baseline_quality = score_bootstrap_baseline(
            analysis_summary,
            backend=backend_name,
        )
    if analysis_summary and not bootstrap_primary:
        backend_name = str(runtime_context.get("backend", "") or "")
        bootstrap_primary = assess_bootstrap_primary_metrics(
            sample_summary=analysis_summary,
            backend=backend_name,
        )
    effective_reference = dict(reference_meta or {})
    effective_reference_profile_id = str(
        effective_reference.get("profile_id", lineage.get("reference_profile_id", "")) or ""
    )
    effective_reference_profile_version = int(
        effective_reference.get("profile_version", lineage.get("reference_profile_version", 0)) or 0
    )
    effective_reference_marker = str(
        effective_reference.get("marker", lineage.get("reference_marker", "")) or ""
    )
    active_context_key = str(effective_reference.get("context_key", "") or "")
    active_context_band = int(
        effective_reference.get("band", 0)
        or summary_scalar(reference_summary, "dominant_context_band")
        or 0
    )
    active_context_cell = int(
        effective_reference.get("cell", 0)
        or summary_scalar(reference_summary, "dominant_context_cell")
        or 0
    )
    active_context_label = (
        f"B{active_context_band} / PCI {active_context_cell}"
        if active_context_band > 0 and active_context_cell > 0
        else ""
    )
    active_context_sample_count = int(reference_summary.get("sample_count", 0) or 0)
    meta["candidate_state"] = str(candidate.get("state", "") or "")
    meta["candidate_workflow_mode"] = str(candidate.get("workflow_mode", "") or "")
    meta["candidate_recommendation"] = str(analysis.get("recommendation", "") or "")
    meta["candidate_verdict"] = str(analysis.get("verdict", "") or "")
    meta["candidate_approval_ready"] = bool(analysis.get("approval_ready", False))
    meta["candidate_sample_count"] = int(len(raw))
    candidate_session_state = str(session_report.get("state", "") or "")
    meta["candidate_session_state"] = candidate_session_state
    meta["candidate_session_progress_pct"] = int(session_report.get("progress_pct", 0) or 0)
    meta["candidate_session_sample_count"] = int(session_report.get("sample_count", 0) or 0)
    meta["candidate_session_min_samples"] = int(session_report.get("min_samples", 0) or 0)
    meta["candidate_session_target_samples"] = int(session_report.get("target_samples", 0) or 0)
    meta["candidate_session_duration_s"] = float(session_report.get("duration_s", 0.0) or 0.0)
    meta["candidate_session_min_duration_s"] = float(session_report.get("min_duration_s", 0.0) or 0.0)
    meta["candidate_session_target_duration_s"] = float(session_report.get("target_duration_s", 0.0) or 0.0)
    meta["candidate_session_note"] = str(session_report.get("operator_note", "") or "")
    last_attempt = state.get("last_collection_attempt") if isinstance(state.get("last_collection_attempt"), dict) else {}
    meta.update(build_collection_attempt_status_fields(last_attempt))
    zero_sample_stale_s = 0.0
    if candidate_id and int(len(raw)) <= 0:
        zero_sample_stale_s = max(
            0.0,
            float(time.time()) - float(session_report.get("started_ts", 0.0) or 0.0),
        )
    meta["candidate_zero_sample_stale_s"] = float(zero_sample_stale_s)
    meta["candidate_lineage_reason"] = str(lineage.get("opened_reason", "") or "")
    meta["candidate_reference_profile_id"] = effective_reference_profile_id
    meta["candidate_reference_profile_version"] = effective_reference_profile_version
    meta["candidate_reference_marker"] = effective_reference_marker
    meta["active_context_key"] = active_context_key
    meta["active_context_band"] = active_context_band
    meta["active_context_cell"] = active_context_cell
    meta["active_context_label"] = active_context_label
    meta["active_context_sample_count"] = active_context_sample_count
    meta["active_context_sinr_avg"] = float(metric_avg(reference_summary, "sinr_db") or 0.0)
    meta["active_context_rsrp_avg"] = float(metric_avg(reference_summary, "rsrp_db") or 0.0)
    meta["active_context_ri_avg"] = float(metric_avg(reference_summary, "ri") or 0.0)
    meta["active_context_ca_avg"] = float(
        reference_summary.get("ca_persistence_ratio", reference_summary.get("ca_avg", 0.0)) or 0.0
    )
    meta["active_context_updated_ts"] = float(effective_reference.get("updated_ts", 0.0) or 0.0)
    meta["active_context_approved_by"] = str(
        effective_reference.get("approved_by", active_profile.get("approved_by", "")) or ""
    )
    meta["active_reference_scope_key"] = str(effective_reference.get("scope_key", "") or "")
    meta["active_reference_scope_match"] = bool(effective_reference.get("scope_match", False))
    meta["active_reference_scope"] = dict(
        effective_reference.get("scope") if isinstance(effective_reference.get("scope"), dict) else {}
    )
    meta["context_baseline_count"] = int(
        len(state.get("context_baselines", {})) if isinstance(state.get("context_baselines"), dict) else 0
    )
    meta["baseline_memory_kind"] = "per_context"
    meta["candidate_operator_headline"] = str(operator_report.get("headline", "") or "")
    meta["candidate_operator_summary"] = str(
        operator_report.get("summary", "") or session_report.get("operator_note", "") or ""
    )
    meta["candidate_operator_next_step"] = str(operator_report.get("next_step", "") or "")
    meta["candidate_operator_digest"] = str(session_outcome.get("operator_digest", "") or "")
    evidence_debt = (
        session_outcome.get("evidence_debt")
        if isinstance(session_outcome, dict)
        else {}
    )
    if not isinstance(evidence_debt, dict):
        evidence_debt = {}
    meta["candidate_evidence_debt_headline"] = str(evidence_debt.get("headline", "") or "")
    meta["candidate_evidence_debt_summary"] = str(evidence_debt.get("summary", "") or "")
    meta["candidate_evidence_debt_items"] = list(evidence_debt.get("items", []) or [])
    meta["candidate_session_id"] = str(session_report.get("session_id", "") or "")
    meta["candidate_blocking_gates"] = list(
        (session_outcome.get("blocking_gates", []) if isinstance(session_outcome, dict) else []) or []
    )
    meta["candidate_decision_ready"] = bool(session_report.get("ready", False))
    analysis_source_count = int(
        analysis.get("source_raw_sample_count", analysis.get("sample_count", 0)) or 0
    )
    workflow_mode = str(candidate.get("workflow_mode", "") or "")
    if workflow_mode == PHASE_BACKGROUND:
        target_samples = max(1, int(session.get("target_samples", 24) or 24))
        stale_sample_delta = max(0, int(len(raw)) - int(analysis_source_count))
        candidate_analysis_stale = bool(
            candidate_id
            and isinstance(analysis, dict)
            and analysis_source_count > 0
            and stale_sample_delta >= target_samples
        )
    else:
        candidate_analysis_stale = bool(
            candidate_id
            and isinstance(analysis, dict)
            and analysis_source_count != int(len(raw))
        )
    meta["candidate_analysis_stale"] = bool(
        candidate_analysis_stale
    )
    meta["candidate_analysis_evidence_grade"] = str(session_outcome.get("evidence_grade", "") or "")
    meta["candidate_analysis_weighted_score"] = float(comparison.get("weighted_score", 0.0) or 0.0)
    meta["candidate_analysis_weighted_threshold"] = float(comparison.get("weighted_threshold", 0.0) or 0.0)
    meta["candidate_analysis_stability_penalty"] = float(comparison.get("stability_penalty", 0.0) or 0.0)
    meta["candidate_analysis_context_ready"] = bool(comparison.get("context_ready", True))
    meta["candidate_analysis_context_reason"] = str(comparison.get("context_reason", "") or "")
    candidate_context_band = int(summary_scalar(analysis_summary, "dominant_context_band") or 0)
    candidate_context_cell = int(summary_scalar(analysis_summary, "dominant_context_cell") or 0)
    meta["candidate_context_band"] = candidate_context_band
    meta["candidate_context_cell"] = candidate_context_cell
    meta["candidate_context_key"] = (
        f"{candidate_context_band}|{candidate_context_cell}"
        if candidate_context_band > 0 and candidate_context_cell > 0
        else ""
    )
    meta["candidate_context_label"] = (
        f"B{candidate_context_band} / PCI {candidate_context_cell}"
        if candidate_context_band > 0 and candidate_context_cell > 0
        else ""
    )
    meta["candidate_analysis_ca_persistence_ratio"] = float(
        comparison.get("candidate_ca_persistence_ratio", 0.0) or 0.0
    )
    meta["candidate_offpeak_confirmation_required"] = bool(comparison.get("offpeak_confirmation_required", False))
    meta["candidate_offpeak_confirmation_reason"] = str(comparison.get("offpeak_confirmation_reason", "") or "")
    meta["candidate_offpeak_ready"] = bool(comparison.get("offpeak_ready", False))
    meta["candidate_network_load_biased_ratio"] = float(
        comparison.get("candidate_network_load_biased_ratio", 0.0) or 0.0
    )
    meta["candidate_analysis_baseline_quality_pct"] = int(baseline_quality.get("quality_pct", 0) or 0)
    meta["candidate_primary_metrics_ready"] = bool(bootstrap_primary.get("primary_metrics_ready", False))
    meta["candidate_primary_metric_count"] = int(bootstrap_primary.get("primary_metric_count", 0) or 0)
    meta["candidate_primary_metric_min_required"] = int(
        bootstrap_primary.get("primary_metric_min_required", 0) or 0
    )
    meta["candidate_primary_metric_confidence"] = float(
        bootstrap_primary.get("primary_metric_confidence", 0.0) or 0.0
    )
    meta["candidate_primary_metrics_available"] = list(
        bootstrap_primary.get("primary_metrics_available", []) or []
    )
    meta["candidate_primary_metrics_missing"] = list(
        bootstrap_primary.get("primary_metrics_missing", []) or []
    )
    meta["candidate_primary_metrics_headline"] = str(bootstrap_primary.get("headline", "") or "")
    meta["candidate_primary_metrics_summary"] = str(bootstrap_primary.get("summary", "") or "")
    meta["candidate_comparison_highlights"] = list(
        (session_outcome.get("comparison_highlights", []) if isinstance(session_outcome, dict) else []) or []
    )
    meta["candidate_comparison_regressions"] = list(
        (session_outcome.get("comparison_regressions", []) if isinstance(session_outcome, dict) else []) or []
    )
    meta["candidate_strengths"] = list(
        (session_outcome.get("strengths", []) if isinstance(session_outcome, dict) else []) or []
    )
    meta["candidate_weaknesses"] = list(
        (session_outcome.get("weaknesses", []) if isinstance(session_outcome, dict) else []) or []
    )
    meta["candidate_decision_basis"] = list(
        (session_outcome.get("decision_basis", []) if isinstance(session_outcome, dict) else []) or []
    )
    if candidate_id:
        review_badge = build_review_badge(
            workflow_mode=str(candidate.get("workflow_mode", "") or ""),
            decision_ready=bool(session_report.get("ready", False)),
            verdict=str(analysis.get("verdict", "") or ""),
            recommendation=str(analysis.get("recommendation", "") or ""),
            approval_ready=bool(analysis.get("approval_ready", False)),
            analysis_stale=bool(meta["candidate_analysis_stale"]),
        )
        meta["candidate_review_badge"] = str(review_badge.get("label", "") or "")
        meta["candidate_review_tone"] = str(review_badge.get("tone", "") or "")
        meta["candidate_review_reason"] = str(review_badge.get("reason", "") or "")
    else:
        meta["candidate_review_badge"] = ""
        meta["candidate_review_tone"] = ""
        meta["candidate_review_reason"] = ""
    active_profile_id = str(meta.get("active_profile_id", "") or "")
    active_profile_version = int(meta.get("active_profile_version", 0) or 0)
    meta["active_reference_marker"] = (
        f"{active_profile_id}@v{active_profile_version}" if active_profile_id else ""
    )
    meta["active_profile_approved_by"] = str(active_profile.get("approved_by", "") or "")
    meta["active_profile_updated_ts"] = float(active_profile.get("updated_ts", 0.0) or 0.0)
    meta["active_profile_state"] = str(active_profile.get("state", "") or "")
    meta["active_reference_summary_highlights"] = build_summary_highlights(
        reference_summary,
        metric_avg=metric_avg,
    )
    meta["last_decision_kind"] = str(latest_decision.get("decision", "") or "")
    meta["last_decision_headline"] = str(latest_decision.get("headline", "") or "")
    meta["last_decision_summary"] = str(latest_decision.get("summary", "") or "")
    meta["last_decision_review_badge"] = str(latest_decision.get("review_badge", "") or "")
    meta["last_decision_workflow_mode"] = str(latest_decision.get("workflow_mode", "") or "")
    meta["last_decision_candidate_id"] = str(latest_decision.get("candidate_id", "") or "")
    meta["last_decision_effect_headline"] = str(latest_decision.get("effect_headline", "") or "")
    meta["last_decision_effect_summary"] = str(latest_decision.get("effect_summary", "") or "")
    meta["last_decision_effect_highlights"] = list(latest_decision.get("effect_highlights", []) or [])
    meta["last_decision_effect_regressions"] = list(latest_decision.get("effect_regressions", []) or [])
    meta["last_decision_reference_marker"] = (
        f"{str(latest_decision.get('reference_profile_id', '') or '')}@v{int(latest_decision.get('reference_profile_version', 0) or 0)}"
        if str(latest_decision.get("reference_profile_id", "") or "")
        else ""
    )
    meta["last_decision_ts"] = float(latest_decision.get("ts", 0.0) or 0.0)
    meta["recent_session_history"] = list(state.get("recent_session_history", []) or [])
    meta["external_knowledge_advisory"] = build_external_knowledge_advisory(
        runtime_context=runtime_context,
        external_knowledge=external_knowledge,
    )
    meta["provider_time_behavior_publication"] = sanitize_provider_time_behavior_publication(
        state.get("provider_time_behavior_publication")
        if isinstance(state.get("provider_time_behavior_publication"), dict)
        else {}
    )
    meta["calibration_persistence"] = dict(
        state.get("calibration_persistence")
        if isinstance(state.get("calibration_persistence"), dict)
        else {}
    )
    playbook = build_bootstrap_playbook(
        backend=str(runtime_context.get("backend", "") or ""),
        recognition_family=str(
            (runtime_context.get("recognition", {}) if isinstance(runtime_context.get("recognition"), dict) else {}).get(
                "family_hint", ""
            )
            or ""
        ),
        capability_flags={
            "cell_lock": bool(runtime_context.get("cell_lock", False)),
            "neighbors": bool(runtime_context.get("neighbors", False)),
            "ca_details": bool(runtime_context.get("ca_details", False)),
        },
    )
    meta["bootstrap_playbook_headline"] = str(playbook.get("headline", "") or "")
    meta["bootstrap_playbook_summary"] = str(playbook.get("summary", "") or "")
    meta["bootstrap_playbook_operator_note"] = str(playbook.get("operator_note", "") or "")
    meta["bootstrap_playbook_primary_focus"] = list(playbook.get("primary_focus", []) or [])
    meta["bootstrap_playbook_secondary_focus"] = list(playbook.get("secondary_focus", []) or [])
    meta["bootstrap_playbook_limits"] = list(playbook.get("limits", []) or [])
    overrides = dict(active_config_overrides or {})
    meta["active_config_overrides"] = overrides
    meta["active_config_override_count"] = len(overrides)
    tuning_proposals = [
        copy.deepcopy(row)
        for row in list(active_profile_tuning_proposals or [])
        if isinstance(row, dict)
    ]
    tuning_proposals.sort(key=lambda row: str(row.get("parameter_id", "")))
    meta["active_profile_tuning_proposals"] = tuning_proposals
    meta["active_profile_tuning_proposal_count"] = len(tuning_proposals)
    override_rows: list[dict] = []
    for field_name, delta in overrides.items():
        startup_val = delta.get("startup")
        current_val = delta.get("current")
        try:
            if isinstance(startup_val, float) or isinstance(current_val, float):
                startup_str = f"{float(startup_val):.3g}"
                current_str = f"{float(current_val):.3g}"
                numeric_delta = float(current_val) - float(startup_val)
                sign = "+" if numeric_delta >= 0 else ""
                delta_str = f"{sign}{numeric_delta:.3g}"
            else:
                startup_str = str(startup_val)
                current_str = str(current_val)
                delta_str = ""
        except Exception:
            startup_str = str(startup_val)
            current_str = str(current_val)
            delta_str = ""
        override_rows.append({
            "field": str(field_name),
            "startup": startup_str,
            "current": current_str,
            "delta": delta_str,
        })
    override_rows.sort(key=lambda row: row["field"])
    meta["active_config_override_rows"] = override_rows
    return meta


def _to_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _baseline_context(authority: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    band = int(authority.get("active_context_band", state.get("baseline_context_band", 0)) or 0)
    cell = int(authority.get("active_context_cell", state.get("baseline_context_cell", 0)) or 0)
    context_key = str(authority.get("active_context_key", state.get("baseline_context_key", "")) or "").strip()
    label = str(authority.get("active_context_label", state.get("baseline_context_label", "")) or "").strip()
    if not context_key and band > 0 and cell > 0:
        context_key = f"{band}|{cell}"
    if not label and band > 0 and cell > 0:
        label = f"B{band} / PCI {cell}"
    return {
        "key": context_key,
        "label": label,
        "band": band,
        "cell": cell,
        "sample_count": int(authority.get("active_context_sample_count", 0) or 0),
        "approved_by": str(authority.get("active_context_approved_by", "") or "").strip(),
        "updated_ts": float(authority.get("active_context_updated_ts", 0.0) or 0.0),
        "sinr_avg": float(authority.get("active_context_sinr_avg", 0.0) or 0.0),
        "rsrp_avg": float(authority.get("active_context_rsrp_avg", 0.0) or 0.0),
        "ri_avg": float(authority.get("active_context_ri_avg", 0.0) or 0.0),
        "ca_avg": float(authority.get("active_context_ca_avg", 0.0) or 0.0),
        "memory_kind": str(authority.get("baseline_memory_kind", "per_context") or "per_context"),
        "memory_count": int(authority.get("context_baseline_count", 0) or 0),
        "marker": str(authority.get("active_reference_marker", "") or "").strip(),
    }


def _current_context(calibration: Dict[str, Any]) -> Dict[str, Any]:
    band = int(calibration.get("current_context_band", calibration.get("serving_band", calibration.get("band", 0))) or 0)
    cell = int(calibration.get("current_context_cell", calibration.get("pci", calibration.get("cell", 0))) or 0)
    context_key = str(calibration.get("current_context_key", "") or "").strip()
    label = str(calibration.get("current_context_label", "") or "").strip()
    if not context_key and band > 0 and cell > 0:
        context_key = f"{band}|{cell}"
    if not label and band > 0 and cell > 0:
        label = f"B{band} / PCI {cell}"
    elif not label and band > 0:
        label = f"B{band}"
    return {
        "present": bool(context_key or label),
        "key": context_key,
        "label": label,
        "band": band,
        "cell": cell,
    }


def _adaptive_threshold_scope(
    *,
    workflow: Dict[str, Any],
    baseline: Dict[str, Any],
    current_context: Dict[str, Any],
) -> Dict[str, Any]:
    provider_name = str(workflow.get("provider", "") or "").strip()
    provider_plmn = str(workflow.get("plmn", "") or "").strip()
    model = str(workflow.get("model", "") or "").strip()
    firmware = str(workflow.get("firmware", "") or "").strip()
    backend = str(workflow.get("backend", "") or "").strip()
    band = int(current_context.get("band", baseline.get("band", 0)) or 0)
    cell = int(current_context.get("cell", baseline.get("cell", 0)) or 0)
    context_key = str(current_context.get("key", baseline.get("key", "")) or "").strip()
    context_label = str(current_context.get("label", baseline.get("label", "")) or "").strip()

    if band > 0 and cell > 0:
        tier = "exact_cell_site"
    elif band > 0 and provider_plmn:
        tier = "provider_band"
    elif model and provider_plmn:
        tier = "modem_provider"
    elif model:
        tier = "modem_model"
    else:
        tier = "global_default"

    fallback_chain = [
        {
            "tier": "exact_cell_site",
            "available": bool(provider_plmn and band > 0 and cell > 0),
        },
        {
            "tier": "provider_band",
            "available": bool(provider_plmn and band > 0),
        },
        {
            "tier": "modem_provider",
            "available": bool(model and provider_plmn),
        },
        {
            "tier": "modem_model",
            "available": bool(model),
        },
        {
            "tier": "global_default",
            "available": True,
        },
    ]
    return {
        "tier": tier,
        "provider_name": provider_name,
        "provider_plmn": provider_plmn,
        "model": model,
        "firmware": firmware,
        "backend": backend,
        "band": band,
        "cell": cell,
        "context_key": context_key,
        "context_label": context_label,
        "fallback_chain": fallback_chain,
    }


def _adaptive_threshold_shadow(
    *,
    calibration: Dict[str, Any],
    authority: Dict[str, Any],
    workflow: Dict[str, Any],
    baseline: Dict[str, Any],
    current_context: Dict[str, Any],
) -> Dict[str, Any]:
    sample_count = int(calibration.get("sample_count", 0) or 0)
    baseline_ready = bool(calibration.get("baseline_ready", False))
    drift_hint_db = float(calibration.get("threshold_adjust_hint_db", 0.0) or 0.0)
    gap_set = float(calibration.get("gap_score_set_threshold", 0.0) or 0.0)
    gap_clear = float(calibration.get("gap_score_clear_threshold", 0.0) or 0.0)
    gap_score = float(calibration.get("gap_score", 0.0) or 0.0)
    active_overrides = _to_dict(authority.get("active_config_overrides"))

    evidence_confidence = float(authority.get("candidate_primary_metric_confidence", 0.0) or 0.0)
    if evidence_confidence <= 0.0:
        evidence_confidence = min(1.0, float(sample_count) / 72.0) if sample_count > 0 else 0.0
    if not baseline_ready:
        evidence_confidence *= 0.5
    evidence_confidence = max(0.0, min(1.0, evidence_confidence))

    scope = _adaptive_threshold_scope(
        workflow=workflow,
        baseline=baseline,
        current_context=current_context,
    )
    notes: List[str] = []
    if drift_hint_db > 0.0:
        notes.append("Baseline drift is suggesting threshold retuning, but runtime takeover remains disabled.")
    if active_overrides:
        notes.append("Active profile overrides remain the runtime truth; shadow rows are for comparison only.")
    if not baseline_ready:
        notes.append("Calibration baseline is not yet fully trusted, so shadow confidence is reduced.")

    published = bool(
        baseline_ready
        or drift_hint_db > 0.0
        or gap_set > 0.0
        or gap_clear > 0.0
        or active_overrides
    )
    recommendations = {
        "calibration_gap_set_threshold": {
            "value": float(gap_set),
            "source_field": "gap_score_set_threshold",
            "lane": "calibration_gap",
            "confidence": float(evidence_confidence),
            "advisory_only": True,
        },
        "calibration_gap_clear_threshold": {
            "value": float(gap_clear),
            "source_field": "gap_score_clear_threshold",
            "lane": "calibration_gap",
            "confidence": float(evidence_confidence),
            "advisory_only": True,
        },
        "threshold_adjustment_db": {
            "value": float(drift_hint_db),
            "source_field": "threshold_adjust_hint_db",
            "lane": "threshold_tuning",
            "confidence": float(evidence_confidence),
            "advisory_only": True,
        },
    }
    return {
        "owner": "calibration_authority",
        "published": bool(published),
        "mode": "shadow",
        "runtime_influence": "none",
        "promotion_ready": False,
        "scope_tier": str(scope.get("tier", "global_default") or "global_default"),
        "scope": {
            "provider_name": str(scope.get("provider_name", "") or ""),
            "provider_plmn": str(scope.get("provider_plmn", "") or ""),
            "model": str(scope.get("model", "") or ""),
            "firmware": str(scope.get("firmware", "") or ""),
            "backend": str(scope.get("backend", "") or ""),
            "band": int(scope.get("band", 0) or 0),
            "cell": int(scope.get("cell", 0) or 0),
            "context_key": str(scope.get("context_key", "") or ""),
            "context_label": str(scope.get("context_label", "") or ""),
        },
        "fallback_chain": list(scope.get("fallback_chain", []) or []),
        "confidence": float(evidence_confidence),
        "confidence_level": fact_confidence_level(evidence_confidence),
        "evidence": {
            "baseline_ready": bool(baseline_ready),
            "sample_count": int(sample_count),
            "primary_metric_confidence": float(authority.get("candidate_primary_metric_confidence", 0.0) or 0.0),
            "active_context_sample_count": int(authority.get("active_context_sample_count", 0) or 0),
            "current_gap_score": float(gap_score),
        },
        "recommendations": recommendations,
        "comparison": {
            "active_config_overrides": _to_dict(active_overrides),
        },
        "notes": notes,
    }


def _calibration_evidence_records(
    *,
    calibration: Dict[str, Any],
    authority: Dict[str, Any],
    workflow: Dict[str, Any],
    state: Dict[str, Any],
    handoff: Dict[str, Any],
    adaptive_threshold_shadow: Dict[str, Any],
) -> Dict[str, Any]:
    """Compact audit ledger for calibration/shadow recommendations.

    This deliberately stores derived evidence rows, not raw samples. Promotion
    tooling can inspect why a recommendation exists without growing history
    files or parsing GUI-oriented fields.
    """
    recommendations = _to_dict(adaptive_threshold_shadow.get("recommendations"))
    scope = _to_dict(adaptive_threshold_shadow.get("scope"))
    confidence = float(adaptive_threshold_shadow.get("confidence", 0.0) or 0.0)
    confidence_level = str(adaptive_threshold_shadow.get("confidence_level", "") or "")
    sample_count = int(calibration.get("sample_count", 0) or 0)
    active_context_sample_count = int(authority.get("active_context_sample_count", 0) or 0)
    primary_missing = list(authority.get("candidate_primary_metrics_missing", []) or [])
    network_load_biased = bool(calibration.get("network_load_biased", False))
    candidate_load_ratio = float(authority.get("candidate_network_load_biased_ratio", 0.0) or 0.0)
    active_overrides = _to_dict(authority.get("active_config_overrides"))
    runtime_truth = "active_profile_overrides" if active_overrides else "static_policy_defaults"

    blockers: List[str] = []
    if not bool(calibration.get("baseline_ready", False)):
        blockers.append("baseline_not_ready")
    if confidence < 0.70:
        blockers.append("confidence_below_usable")
    if network_load_biased:
        blockers.append("network_load_biased")
    if candidate_load_ratio >= 0.35:
        blockers.append("candidate_load_biased")
    if primary_missing:
        blockers.append("primary_metrics_missing")

    records: List[Dict[str, Any]] = []
    for name, raw_row in sorted(recommendations.items()):
        row = _to_dict(raw_row)
        lane = str(row.get("lane", "") or "")
        source_field = str(row.get("source_field", "") or "")
        record = {
            "record_id": f"{lane or 'calibration'}:{name}:{adaptive_threshold_shadow.get('scope_tier', 'global_default')}",
            "kind": "adaptive_threshold_shadow_evidence",
            "owner": "calibration_authority",
            "runtime_influence": "none",
            "scope_tier": str(adaptive_threshold_shadow.get("scope_tier", "global_default") or "global_default"),
            "scope": dict(scope),
            "lane": lane,
            "recommendation": {
                "name": str(name),
                "source_field": source_field,
                "candidate_value": float(row.get("value", 0.0) or 0.0),
                "advisory_only": bool(row.get("advisory_only", True)),
            },
            "active_runtime": {
                "truth_source": runtime_truth,
                "config_override_keys": sorted(str(k) for k in active_overrides.keys()),
            },
            "evidence": {
                "confidence": float(row.get("confidence", confidence) or confidence),
                "confidence_level": confidence_level,
                "sample_count": int(sample_count),
                "active_context_sample_count": int(active_context_sample_count),
                "baseline_ready": bool(calibration.get("baseline_ready", False)),
                "current_gap_score": float(calibration.get("gap_score", 0.0) or 0.0),
                "gap_score_smooth": float(calibration.get("gap_score_smooth", 0.0) or 0.0),
                "gap_score_volatility": float(calibration.get("gap_score_volatility", 0.0) or 0.0),
                "handoff_trust_level": str(handoff.get("trust_level", "") or ""),
                "workflow_phase": str(workflow.get("phase", "") or ""),
                "state": str(state.get("state", "") or ""),
                "freshness": str(state.get("freshness", "") or ""),
            },
            "contamination": {
                "network_load_biased": bool(network_load_biased),
                "candidate_network_load_biased_ratio": float(candidate_load_ratio),
                "primary_metrics_missing": list(primary_missing),
            },
            "promotion": {
                "eligible": False,
                "blockers": list(blockers),
                "reason": "shadow_only",
            },
        }
        records.append(record)

    return {
        "owner": "calibration_authority",
        "version": 1,
        "storage_model": "compact_derived_records",
        "raw_samples_persisted": False,
        "runtime_influence": "none",
        "record_count": len(records),
        "records": records,
        "promotion_gate": {
            "eligible": bool(not blockers and records),
            "blockers": list(blockers),
            "minimum_confidence": 0.70,
            "requires_clean_contamination": True,
        },
        "budget": {
            "max_records_per_surface": 3,
            "max_expected_bytes": 8192,
            "raw_history_dependency": "none",
        },
    }


def _score_setup_lane(*, state_name: str, freshness: str) -> float:
    state = str(state_name or "").strip().lower()
    fresh = str(freshness or "").strip().lower()
    if state == "stale" and fresh == "scope_mismatch":
        return 0.0
    if state == "active" and fresh == "fresh":
        return 1.0
    if state == "active":
        return 0.8
    if state == "stale":
        return 0.55
    if state in {"collecting", "provisional"}:
        return 0.45
    if state == "invalid":
        return 0.15
    return 0.0


def calibration_fact_confidence_from_handoff(handoff: Any) -> float:
    lane = _to_dict(handoff)
    if "calibration_fact_confidence" in lane:
        return max(0.0, min(1.0, float(lane.get("calibration_fact_confidence", 0.0) or 0.0)))
    return 0.0


def _score_feature_lane(*, baseline_ready: bool, value: float) -> float:
    if not baseline_ready:
        return 0.0
    return 1.0 if abs(float(value or 0.0)) > 0.0 else 0.25


def _pct(value: Any) -> float:
    try:
        numeric = float(value or 0.0)
    except (TypeError, ValueError):
        numeric = 0.0
    return max(0.0, min(100.0, numeric))


def _public_next_step_hint(raw: Any) -> str:
    key = str(raw or "").strip().lower()
    if key in {"", "none", "idle"}:
        return ""
    known = {
        "collect_bootstrap_rf_baseline": "Keep the antenna steady while 54GUC learns this position.",
        "auto_decision": "54GUC is checking whether the current setup is good enough.",
        "auto_apply": "54GUC has enough evidence to use this setup automatically.",
        "check_result": "Wait for the current check to finish before moving again.",
        "compare": "Move slowly, then let 54GUC compare the result.",
        "survey": "Let 54GUC compare available band positions before changing setup.",
        "start": "Start a short setup check before judging the antenna position.",
        "bootstrap": "Keep the antenna steady until the first setup check is complete.",
        "stop": "Stop the current antenna check.",
    }
    if key in known:
        return known[key]
    return str(raw or "").replace("_", " ").strip().capitalize()


def _public_monitor_state(
    *,
    state_name: str,
    workflow: Dict[str, Any],
    authority: Dict[str, Any],
) -> str:
    phase = str(workflow.get("phase", "") or "").strip().lower()
    candidate_mode = str(authority.get("candidate_workflow_mode", "") or "").strip().lower()
    if candidate_mode:
        return candidate_mode.replace("_", " ")
    if "bootstrap" in phase:
        return "setup"
    if "compar" in phase:
        return "checking"
    if "survey" in phase or "anchor" in phase:
        return "survey"
    if "background" in phase:
        return "monitoring"
    if state_name:
        return state_name.replace("_", " ")
    return "monitoring"


def _public_active_calibration_action(authority: Dict[str, Any]) -> str:
    """Map an open candidate session to the one active operator control."""

    candidate_id = str(authority.get("candidate_profile_id", "") or "").strip()
    if not candidate_id:
        return ""
    candidate_mode = str(
        authority.get("candidate_workflow_mode", "") or ""
    ).strip().lower()
    return {
        PHASE_BACKGROUND: "start",
        PHASE_BOOTSTRAP: "bootstrap",
        PHASE_COMPARATIVE: "compare",
        PHASE_ANCHOR_SURVEY: "survey",
    }.get(candidate_mode, "")


def _public_contributors(
    *,
    baseline_ready: bool,
    rf_lane: float,
    latency_lane: float,
    baseline: Dict[str, Any],
    fact_confidence: float,
) -> Dict[str, float]:
    ri_avg = safe_float(baseline.get("ri_avg"))
    ca_avg = safe_float(baseline.get("ca_avg"))
    if ri_avg > 0.0:
        ri_score = _pct((ri_avg / 2.0) * 100.0)
    else:
        ri_score = _pct(fact_confidence * 100.0 if baseline_ready else 0.0)
    if ca_avg > 0.0:
        ca_score = _pct((ca_avg / 3.0) * 100.0)
    else:
        ca_score = _pct(100.0 if baseline_ready else 0.0)
    return {
        "RF": round(_pct(rf_lane * 100.0), 1),
        "RI": round(ri_score, 1),
        "CA": round(ca_score, 1),
        "LAT": round(_pct(latency_lane * 100.0), 1),
    }


def _evidence_status(
    *,
    calibration_state: Dict[str, Any],
    authority: Dict[str, Any],
    calibration: Dict[str, Any],
) -> str:
    state_name = str(calibration_state.get("state", "") or "").strip().lower()
    freshness = str(calibration_state.get("freshness", "") or "").strip().lower()
    if state_name == "invalid":
        return "blocked"
    if state_name == "stale":
        return "stale"
    if freshness == "timed_out":
        return "timed_out"
    if freshness == "paused":
        return "paused"
    if bool(calibration_state.get("candidate_collection_stalled", False)):
        return "stalled"
    if str(authority.get("candidate_profile_id", "") or "").strip() and state_name in {
        "collecting",
        "provisional",
    }:
        return "collecting"
    if bool(calibration.get("baseline_ready", False)):
        return "sufficient"
    if bool(calibration.get("enabled", False)):
        return "pending"
    return "disabled"


def _plain_setup_summary(text: Any, fallback: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return fallback
    low = raw.lower()
    if "not safe for calibration evidence" in low or "current rf state is not safe" in low:
        return "Signal is unstable. Stop moving the antenna and check cable/placement."
    if "baseline drift" in low or "calibration drift" in low or "recalibration" in low:
        return "Saved setup should be checked again on this connection."
    if "timed out" in low:
        return "Setup check timed out before enough stable readings were collected."
    if "calibration evidence is being collected" in low or ("collect" in low and "evidence" in low):
        return "Collecting antenna readings. Hold position for stable quality."
    if "candidate is ready for review" in low or ("ready for review" in low and "candidate" in low):
        return "Review the measured position before moving again."
    if "calibration is active" in low or "guiding the current runtime thresholds" in low:
        return "Saved antenna setup is active."
    if "no approved reference is active yet" in low or "calibration context exists" in low:
        return "No saved antenna setup yet."
    return raw


def _recommended_action(
    *,
    state: str,
    workflow_phase: str,
    active_reference_marker: str,
    active_profile_id: str,
    candidate_decision_ready: bool,
) -> str:
    if candidate_decision_ready:
        return "auto_decide"
    if state == "active":
        return "none"
    if state == "invalid":
        return "wait"
    if state == "stale":
        return "compare"
    if workflow_phase == "bootstrap_initial":
        return "bootstrap"
    if workflow_phase == "comparative_recheck":
        return "compare"
    if workflow_phase == "background_self_calibration":
        return "start"
    if state == "collecting":
        return "bootstrap" if not (active_reference_marker or active_profile_id) else "compare"
    if not (active_reference_marker or active_profile_id):
        return "bootstrap"
    return "none"


def _calibration_baseline_state(
    *,
    state_name: str,
    freshness: str,
    baseline_ready: bool,
    candidate_profile_id: str,
    candidate_decision_ready: bool,
    candidate_terminal_timeout: bool,
) -> str:
    state = str(state_name or "").strip().lower()
    fresh = str(freshness or "").strip().lower()
    if baseline_ready and state == "active" and fresh in {"fresh", "watch"}:
        return "VERIFIED_BASELINE"
    if candidate_profile_id and not candidate_terminal_timeout:
        return "LEARNING_BASELINE"
    if state in {"collecting", "provisional"} and not candidate_terminal_timeout:
        return "LEARNING_BASELINE"
    if candidate_decision_ready and not candidate_terminal_timeout:
        return "LEARNING_BASELINE"
    return "NO_BASELINE"


def _calibration_delta_state(*, baseline_state: str, calibration: Dict[str, Any]) -> tuple[str, bool, Dict[str, float]]:
    if str(baseline_state or "") != "VERIFIED_BASELINE":
        return "UNKNOWN", False, {}
    gap_score = float(calibration.get("gap_score", 0.0) or 0.0)
    set_threshold = float(calibration.get("gap_score_set_threshold", 0.0) or 0.0)
    clear_threshold = float(calibration.get("gap_score_clear_threshold", 0.0) or 0.0)
    throughput_gap = float(calibration.get("throughput_gap_ratio", 0.0) or 0.0)
    rtt_gap = float(calibration.get("rtt_gap_ratio", 0.0) or 0.0)
    sinr_gap_db = float(calibration.get("sinr_gap_db", 0.0) or 0.0)
    threshold = set_threshold if set_threshold > 0.0 else 0.20
    same_threshold = clear_threshold if clear_threshold > 0.0 else min(0.08, threshold * 0.5)
    if gap_score >= threshold:
        state = "WORSE"
    elif gap_score <= same_threshold:
        state = "SAME"
    else:
        state = "SAME"
    return state, True, {
        "gap_score": float(gap_score),
        "gap_score_set_threshold": float(set_threshold),
        "gap_score_clear_threshold": float(clear_threshold),
        "throughput_gap_ratio": float(throughput_gap),
        "rtt_gap_ratio": float(rtt_gap),
        "sinr_gap_db": float(sinr_gap_db),
    }


def build_learning_calibration_handoff(
    *,
    calibration_state: Any,
    calibration: Any = None,
) -> Dict[str, Any]:
    state = _to_dict(calibration_state)
    calib = _to_dict(calibration)
    state_name = str(state.get("state", "") or "").strip().lower()
    freshness = str(state.get("freshness", "") or "").strip().lower()
    workflow_phase = str(state.get("workflow_phase", "") or "").strip()
    baseline_ready = bool(
        state.get(
            "baseline_ready",
            calib.get(
                "baseline_ready",
                state_name == "active" and freshness in {"fresh", "watch"},
            ),
        )
    )
    candidate_profile_id = str(state.get("candidate_profile_id", "") or "").strip()
    candidate_decision_ready = bool(state.get("candidate_decision_ready", False))
    candidate_terminal_timeout = bool(state.get("candidate_terminal_timeout", False))

    trust_level = "unknown"
    reason = "setup_unavailable"
    baseline = _baseline_context({}, state)
    baseline_state = _calibration_baseline_state(
        state_name=state_name,
        freshness=freshness,
        baseline_ready=baseline_ready,
        candidate_profile_id=candidate_profile_id,
        candidate_decision_ready=candidate_decision_ready,
        candidate_terminal_timeout=candidate_terminal_timeout,
    )
    delta_state, delta_valid, delta_values = _calibration_delta_state(
        baseline_state=baseline_state,
        calibration=calib,
    )
    terminal_state = ""
    if candidate_terminal_timeout or freshness in {"timed_out", "stalled", "paused"}:
        terminal_state = str(freshness or "terminal")

    if state_name == "active" and freshness == "fresh":
        trust_level = "trusted"
        reason = "saved_setup_active"
    elif state_name == "active":
        trust_level = "estimated"
        reason = "saved_setup_watch"
    elif state_name in {"provisional", "collecting"}:
        trust_level = "estimated"
        reason = "setup_evidence_collecting"
    elif state_name == "stale":
        if freshness == "scope_mismatch":
            trust_level = "unknown"
            reason = "saved_setup_context_mismatch"
        else:
            trust_level = "estimated"
            reason = "saved_setup_drifted"
    elif state_name == "invalid":
        trust_level = "unknown"
        reason = "setup_invalid"
    elif state_name == "disabled":
        trust_level = "unknown"
        if freshness == "timed_out":
            reason = "setup_collection_timed_out"
        elif freshness == "stalled":
            reason = "setup_collection_stalled"
        elif freshness == "paused":
            reason = "setup_collection_paused"
        else:
            reason = "setup_disabled"

    fact = calibration_fact_confidence(
        calibration_state=state,
        calibration=calib,
        state_name=state_name,
        freshness=freshness,
        trust_level=trust_level,
    )
    fact_confidence = fact.score

    return {
        "schema": "calibration_handoff.v2",
        "baseline_state": baseline_state,
        "baseline_valid": bool(baseline_state == "VERIFIED_BASELINE"),
        "baseline_learning": bool(baseline_state == "LEARNING_BASELINE"),
        "delta_state": delta_state,
        "delta_valid": bool(delta_valid),
        "delta_values": delta_values,
        "session_state": (
            "TERMINAL"
            if terminal_state
            else "RUNNING"
            if baseline_state == "LEARNING_BASELINE"
            else "READY"
            if baseline_state == "VERIFIED_BASELINE"
            else "ABSENT"
        ),
        "session_terminal_reason": terminal_state,
        "trust_level": trust_level,
        "reason": reason,
        "calibration_fact_confidence": float(fact_confidence),
        "calibration_fact_level": fact.level,
        "calibration_fact_basis": fact.basis,
        "calibration_fact_package": fact.package,
        "setup_state": state_name,
        "setup_freshness": freshness,
        "workflow_phase": workflow_phase,
        "baseline_context_key": str(state.get("baseline_context_key", baseline.get("key", "")) or "").strip(),
        "baseline_context_label": str(state.get("baseline_context_label", baseline.get("label", "")) or "").strip(),
        "baseline_context_band": int(state.get("baseline_context_band", baseline.get("band", 0)) or 0),
        "baseline_context_cell": int(state.get("baseline_context_cell", baseline.get("cell", 0)) or 0),
        "active_profile_id": str(state.get("active_profile_id", "") or "").strip(),
        "active_reference_marker": str(state.get("active_reference_marker", "") or "").strip(),
    }


def build_calibration_surface(
    *,
    calibration: Any,
    calibration_authority: Any,
    calibration_workflow: Any,
    calibration_state: Any,
    learning_calibration_handoff: Any,
) -> Dict[str, Any]:
    calib = _to_dict(calibration)
    authority = _to_dict(calibration_authority)
    workflow = _to_dict(calibration_workflow)
    state = _to_dict(calibration_state)
    handoff = _to_dict(learning_calibration_handoff)

    state_name = str(state.get("state", "") or "").strip().lower()
    freshness = str(state.get("freshness", "") or "").strip().lower()
    trust_level = str(handoff.get("trust_level", "") or "").strip().lower()
    baseline_ready = bool(calib.get("baseline_ready", False))
    baseline = _baseline_context(authority, state)

    throughput_baseline_mbps = float(calib.get("expected_dl_mbps", 0.0) or 0.0)
    latency_baseline_ms = float(calib.get("expected_rtt_ms", 0.0) or 0.0)
    sinr_baseline_db = float(calib.get("expected_sinr_db", 0.0) or 0.0)
    threshold_adjustment_db = float(calib.get("threshold_adjust_hint_db", 0.0) or 0.0)

    setup_trust_score = _score_setup_lane(state_name=state_name, freshness=freshness)
    fact_confidence = calibration_fact_confidence_from_handoff(handoff)

    evidence_status = _evidence_status(
        calibration_state=state,
        authority=authority,
        calibration=calib,
    )
    current_context = _current_context(calib)
    adaptive_threshold_shadow = _adaptive_threshold_shadow(
        calibration=calib,
        authority=authority,
        workflow=workflow,
        baseline=baseline,
        current_context=current_context,
    )
    calibration_evidence_records = _calibration_evidence_records(
        calibration=calib,
        authority=authority,
        workflow=workflow,
        state=state,
        handoff=handoff,
        adaptive_threshold_shadow=adaptive_threshold_shadow,
    )
    active_config_overrides = _to_dict(authority.get("active_config_overrides"))
    active_config_override_rows = list(authority.get("active_config_override_rows", []) or [])
    active_profile_tuning_proposals = [
        copy.deepcopy(row)
        for row in list(authority.get("active_profile_tuning_proposals", []) or [])
        if isinstance(row, dict)
    ]
    provider_time_behavior_publication = sanitize_provider_time_behavior_publication(
        authority.get("provider_time_behavior_publication")
        if isinstance(authority.get("provider_time_behavior_publication"), dict)
        else {}
    )
    candidate_publication = {
        "owner": "calibration_authority",
        "profile_id": str(authority.get("candidate_profile_id", "") or "").strip(),
        "present": bool(authority.get("candidate_profile_id")),
        "state": str(authority.get("candidate_state", "") or "").strip().lower(),
        "workflow_mode": str(authority.get("candidate_workflow_mode", "") or "").strip(),
        "session_progress_pct": int(authority.get("candidate_session_progress_pct", 0) or 0),
        "session_sample_count": int(authority.get("candidate_session_sample_count", 0) or 0),
        "session_min_samples": int(authority.get("candidate_session_min_samples", 0) or 0),
        "session_duration_s": float(authority.get("candidate_session_duration_s", 0.0) or 0.0),
        "session_min_duration_s": float(authority.get("candidate_session_min_duration_s", 0.0) or 0.0),
        "session_note": str(authority.get("candidate_session_note", "") or "").strip(),
        "approval_ready": bool(authority.get("candidate_approval_ready", False)),
        "decision_ready": bool(authority.get("candidate_decision_ready", False)),
        "collection_enabled": bool(
            authority.get("collection_enabled", workflow.get("collection_enabled", False))
        ),
        "collection_stalled": bool(state.get("candidate_collection_stalled", False)),
        "collection_paused": bool(state.get("candidate_collection_paused", False)),
        "terminal_timeout": bool(state.get("candidate_terminal_timeout", False)),
        "verdict": str(authority.get("candidate_verdict", "") or "").strip().lower(),
        "recommendation": str(authority.get("candidate_recommendation", "") or "").strip().lower(),
        "review_badge": str(authority.get("candidate_review_badge", "") or "").strip().lower(),
        "review_tone": str(authority.get("candidate_review_tone", "") or "").strip().lower(),
        "review_reason": str(authority.get("candidate_review_reason", "") or "").strip(),
        "operator_headline": str(authority.get("candidate_operator_headline", "") or "").strip(),
        "operator_digest": str(authority.get("candidate_operator_digest", "") or "").strip(),
        "operator_summary": str(authority.get("candidate_operator_summary", "") or "").strip(),
        "operator_next_step": str(authority.get("candidate_operator_next_step", "") or "").strip(),
        "analysis_context_ready": bool(authority.get("candidate_analysis_context_ready", True)),
        "analysis_context_reason": str(authority.get("candidate_analysis_context_reason", "") or "").strip(),
        "analysis_evidence_grade": str(authority.get("candidate_analysis_evidence_grade", "") or "").strip(),
        "analysis_weighted_score": float(authority.get("candidate_analysis_weighted_score", 0.0) or 0.0),
        "analysis_weighted_threshold": float(
            authority.get("candidate_analysis_weighted_threshold", 0.0) or 0.0
        ),
        "analysis_stability_penalty": float(
            authority.get("candidate_analysis_stability_penalty", 0.0) or 0.0
        ),
        "analysis_baseline_quality_pct": int(
            authority.get("candidate_analysis_baseline_quality_pct", 0) or 0
        ),
        "offpeak_confirmation_required": bool(
            authority.get("candidate_offpeak_confirmation_required", False)
        ),
        "offpeak_confirmation_reason": str(
            authority.get("candidate_offpeak_confirmation_reason", "") or ""
        ).strip(),
        "offpeak_ready": bool(authority.get("candidate_offpeak_ready", False)),
        "network_load_biased_ratio": float(
            authority.get("candidate_network_load_biased_ratio", 0.0) or 0.0
        ),
        "context_label": str(authority.get("candidate_context_label", "") or "").strip(),
        "context_key": str(authority.get("candidate_context_key", "") or "").strip(),
        "evidence_debt_summary": str(authority.get("candidate_evidence_debt_summary", "") or "").strip(),
        "evidence_debt_items": list(authority.get("candidate_evidence_debt_items", []) or []),
        "blocking_gates": list(authority.get("candidate_blocking_gates", []) or []),
        "comparison_highlights": list(authority.get("candidate_comparison_highlights", []) or []),
        "comparison_regressions": list(authority.get("candidate_comparison_regressions", []) or []),
        "strengths": list(authority.get("candidate_strengths", []) or []),
        "weaknesses": list(authority.get("candidate_weaknesses", []) or []),
        "decision_basis": list(authority.get("candidate_decision_basis", []) or []),
        "primary_metrics_ready": bool(authority.get("candidate_primary_metrics_ready", False)),
        "primary_metrics_available": list(authority.get("candidate_primary_metrics_available", []) or []),
        "primary_metrics_missing": list(authority.get("candidate_primary_metrics_missing", []) or []),
        "primary_metric_count": int(authority.get("candidate_primary_metric_count", 0) or 0),
        "primary_metric_min_required": int(authority.get("candidate_primary_metric_min_required", 0) or 0),
        "primary_metrics_headline": str(authority.get("candidate_primary_metrics_headline", "") or "").strip(),
        "primary_metrics_summary": str(authority.get("candidate_primary_metrics_summary", "") or "").strip(),
        "reference_marker": str(authority.get("candidate_reference_marker", "") or "").strip(),
        "active_reference_marker": str(authority.get("active_reference_marker", "") or "").strip(),
        "active_profile_approved_by": str(authority.get("active_profile_approved_by", "") or "").strip(),
        "active_reference_summary_highlights": list(
            authority.get("active_reference_summary_highlights", []) or []
        ),
        "recent_session_history": list(authority.get("recent_session_history", []) or []),
        "bootstrap_playbook_headline": str(authority.get("bootstrap_playbook_headline", "") or "").strip(),
        "bootstrap_playbook_summary": str(authority.get("bootstrap_playbook_summary", "") or "").strip(),
        "bootstrap_playbook_primary_focus": list(authority.get("bootstrap_playbook_primary_focus", []) or []),
        "bootstrap_playbook_secondary_focus": list(
            authority.get("bootstrap_playbook_secondary_focus", []) or []
        ),
        "bootstrap_playbook_limits": list(authority.get("bootstrap_playbook_limits", []) or []),
        "last_decision_kind": str(authority.get("last_decision_kind", "") or "").strip().lower(),
        "last_decision_headline": str(authority.get("last_decision_headline", "") or "").strip(),
        "last_decision_workflow_mode": str(
            authority.get("last_decision_workflow_mode", "") or ""
        ).strip(),
        "last_decision_review_badge": str(
            authority.get("last_decision_review_badge", "") or ""
        ).strip().lower(),
        "last_decision_effect_headline": str(
            authority.get("last_decision_effect_headline", "") or ""
        ).strip(),
        "last_decision_effect_summary": str(
            authority.get("last_decision_effect_summary", "") or ""
        ).strip(),
        "last_decision_effect_highlights": list(
            authority.get("last_decision_effect_highlights", []) or []
        ),
        "last_decision_effect_regressions": list(
            authority.get("last_decision_effect_regressions", []) or []
        ),
        "background_trigger_recommended": bool(
            workflow.get("background_trigger_recommended", False)
        ),
    }
    runtime_policy_publication = build_runtime_policy_publication(
        authority=authority,
        workflow=workflow,
        active_config_overrides=active_config_overrides,
        active_config_override_rows=active_config_override_rows,
    )
    throughput_lane_score = _score_feature_lane(
        baseline_ready=baseline_ready,
        value=throughput_baseline_mbps,
    )
    latency_lane_score = _score_feature_lane(
        baseline_ready=baseline_ready,
        value=latency_baseline_ms,
    )
    rf_lane_score = _score_feature_lane(
        baseline_ready=baseline_ready,
        value=sinr_baseline_db,
    )
    contributors = _public_contributors(
        baseline_ready=baseline_ready,
        rf_lane=rf_lane_score,
        latency_lane=latency_lane_score,
        baseline=baseline,
        fact_confidence=fact_confidence,
    )
    public_score_pct = _pct(
        (setup_trust_score * 25.0)
        + (fact_confidence * 35.0)
        + (rf_lane_score * 20.0)
        + (latency_lane_score * 10.0)
        + (min(contributors.get("RI", 0.0), contributors.get("CA", 0.0)) * 0.10)
    )
    sample_count = int(calib.get("sample_count", 0) or 0)
    progress_pct = _pct(
        authority.get("candidate_session_progress_pct")
        or state.get("candidate_session_progress_pct")
        or (sample_count / 72.0 * 100.0 if sample_count > 0 else public_score_pct)
    )
    recommended_action = str(state.get("recommended_action", "") or "").strip().lower()
    public_guidance = str(
        authority.get("candidate_operator_headline")
        or authority.get("candidate_operator_summary")
        or state.get("summary")
        or workflow.get("objective")
        or "Keep the antenna steady."
    ).strip()
    public_hint = str(
        authority.get("candidate_operator_next_step")
        or state.get("next_step")
        or workflow.get("next_step")
        or ""
    ).strip()
    return {
        "schema": "calibration_surface.v1",
        "owner": "calibration_authority",
        "state": state_name,
        "freshness": freshness,
        "trust_level": trust_level,
        "trust_reason": str(handoff.get("reason", "") or "").strip(),
        "guidance": public_guidance,
        "hint": _public_next_step_hint(public_hint),
        "active_action": _public_active_calibration_action(authority),
        "actions_enabled": bool(workflow.get("collection_enabled", authority.get("collection_enabled", True))),
        "score_pct": round(public_score_pct, 1),
        "meter_pct": round(progress_pct, 1),
        "confidence": fact_confidence_level(fact_confidence),
        "trend": str(
            handoff.get("delta_state", state.get("trend", "UNKNOWN"))
            or "UNKNOWN"
        ).strip(),
        "monitor_state": _public_monitor_state(
            state_name=state_name,
            workflow=workflow,
            authority=authority,
        ),
        "contributors": contributors,
        "setup": {
            "state": state_name,
            "freshness": freshness,
            "summary": str(state.get("summary", "") or "").strip(),
            "next_step": str(state.get("next_step", "") or "").strip(),
            "recommended_action": str(state.get("recommended_action", "") or "").strip().lower(),
        },
        "trust_scores": {
            "setup": float(setup_trust_score),
            "calibration_fact": float(fact_confidence),
            "learning_input": float(fact_confidence),
            "prediction_input": float(fact_confidence),
            "throughput_lane": float(throughput_lane_score),
            "latency_lane": float(latency_lane_score),
            "rf_lane": float(rf_lane_score),
        },
        "feature_aliases": {
            "throughput_baseline_mbps": {
                "value": float(throughput_baseline_mbps),
                "source_field": "expected_dl_mbps",
                "lane": "throughput",
                "trusted": bool(baseline_ready),
            },
            "latency_baseline_ms": {
                "value": float(latency_baseline_ms),
                "source_field": "expected_rtt_ms",
                "lane": "latency",
                "trusted": bool(baseline_ready),
            },
            "sinr_baseline_db": {
                "value": float(sinr_baseline_db),
                "source_field": "expected_sinr_db",
                "lane": "rf",
                "trusted": bool(baseline_ready),
            },
            "threshold_adjustment_db": {
                "value": float(threshold_adjustment_db),
                "source_field": "threshold_adjust_hint_db",
                "lane": "threshold_tuning",
                "trusted": bool(handoff.get("baseline_valid", False)),
            },
        },
        "local_baselines": {
            "baseline_ready": bool(baseline_ready),
            "sample_count": sample_count,
            "throughput_mbps": float(throughput_baseline_mbps),
            "latency_ms": float(latency_baseline_ms),
            "sinr_db": float(sinr_baseline_db),
            "context_key": str(baseline.get("key", "") or ""),
            "context_label": str(baseline.get("label", "") or ""),
            "context_band": int(baseline.get("band", 0) or 0),
            "context_cell": int(baseline.get("cell", 0) or 0),
            "context_sample_count": int(baseline.get("sample_count", 0) or 0),
            "context_approved_by": str(baseline.get("approved_by", "") or ""),
            "context_memory_kind": str(baseline.get("memory_kind", "per_context") or "per_context"),
            "context_memory_count": int(baseline.get("memory_count", 0) or 0),
            "rf_sinr_avg": float(baseline.get("sinr_avg", 0.0) or 0.0),
            "rf_rsrp_avg": float(baseline.get("rsrp_avg", 0.0) or 0.0),
            "ri_avg": float(baseline.get("ri_avg", 0.0) or 0.0),
            "ca_avg": float(baseline.get("ca_avg", 0.0) or 0.0),
            "active_profile_id": str(state.get("active_profile_id", "") or "").strip(),
            "active_profile_version": int(state.get("active_profile_version", 0) or 0),
            "active_reference_marker": str(state.get("active_reference_marker", "") or "").strip(),
        },
        "baseline_contract": {
            "state": str(handoff.get("baseline_state", "NO_BASELINE") or "NO_BASELINE"),
            "valid": bool(handoff.get("baseline_valid", False)),
            "learning": bool(handoff.get("baseline_learning", False)),
            "sample_count": int(calib.get("sample_count", 0) or 0),
            "context_key": str(baseline.get("key", "") or ""),
            "context_label": str(baseline.get("label", "") or ""),
        },
        "current_delta": {
            "state": str(handoff.get("delta_state", "UNKNOWN") or "UNKNOWN"),
            "valid": bool(handoff.get("delta_valid", False)),
            "values": _to_dict(handoff.get("delta_values")),
        },
        "baseline_context": {
            "present": bool(baseline.get("key") or baseline.get("label")),
            "key": str(baseline.get("key", "") or ""),
            "label": str(baseline.get("label", "") or ""),
            "band": int(baseline.get("band", 0) or 0),
            "cell": int(baseline.get("cell", 0) or 0),
            "sample_count": int(baseline.get("sample_count", 0) or 0),
            "approved_by": str(baseline.get("approved_by", "") or ""),
            "updated_ts": float(baseline.get("updated_ts", 0.0) or 0.0),
            "sinr_avg": float(baseline.get("sinr_avg", 0.0) or 0.0),
            "rsrp_avg": float(baseline.get("rsrp_avg", 0.0) or 0.0),
            "ri_avg": float(baseline.get("ri_avg", 0.0) or 0.0),
            "ca_avg": float(baseline.get("ca_avg", 0.0) or 0.0),
            "marker": str(baseline.get("marker", "") or ""),
            "memory_kind": str(baseline.get("memory_kind", "per_context") or "per_context"),
            "memory_count": int(baseline.get("memory_count", 0) or 0),
        },
        "current_context": current_context,
        "threshold_adjustments": {
            "hint_db": float(threshold_adjustment_db),
            "gap_score": float(calib.get("gap_score", 0.0) or 0.0),
            "gap_score_smooth": float(calib.get("gap_score_smooth", 0.0) or 0.0),
            "gap_score_volatility": float(calib.get("gap_score_volatility", 0.0) or 0.0),
            "set_threshold": float(calib.get("gap_score_set_threshold", 0.0) or 0.0),
            "clear_threshold": float(calib.get("gap_score_clear_threshold", 0.0) or 0.0),
            "active_config_overrides": active_config_overrides,
        },
        "profile_tuning_proposals": active_profile_tuning_proposals,
        "runtime_policy_publication": runtime_policy_publication,
        "provider_time_behavior_publication": provider_time_behavior_publication,
        "adaptive_threshold_shadow": adaptive_threshold_shadow,
        "calibration_evidence_records": calibration_evidence_records,
        "candidate": candidate_publication,
        "action_family_evidence": {
            "rf_control": {
                "status": evidence_status,
                "evidence_grade": str(authority.get("candidate_analysis_evidence_grade", "") or "").strip(),
                "required_gap_score": float(authority.get("required_gap_score", 0.0) or 0.0),
                "current_gap_score": float(calib.get("gap_score", 0.0) or 0.0),
                "baseline_quality_pct": int(authority.get("candidate_analysis_baseline_quality_pct", 0) or 0),
                "primary_metrics_ready": bool(authority.get("candidate_primary_metrics_ready", False)),
                "primary_metrics_confidence": float(
                    authority.get("candidate_primary_metric_confidence", 0.0) or 0.0
                ),
                "primary_metrics_missing": list(
                    authority.get("candidate_primary_metrics_missing", []) or []
                ),
                "summary": str(
                    authority.get("candidate_operator_summary")
                    or state.get("summary")
                    or ""
                ).strip(),
                "next_step": str(
                    authority.get("candidate_operator_next_step")
                    or state.get("next_step")
                    or ""
                ).strip(),
            }
        },
        "workflow": {
            "phase": str(state.get("workflow_phase", "") or workflow.get("phase", "") or "").strip(),
            "reason": str(state.get("workflow_reason", "") or workflow.get("phase_reason", "") or "").strip(),
            "recommended_action": str(state.get("recommended_action", "") or "").strip().lower(),
            "next_step": str(state.get("next_step", "") or "").strip(),
            "runtime_context_ready": bool(workflow.get("runtime_context_ready", False)),
            "capability_probe_ready": bool(workflow.get("capability_probe_ready", False)),
            "collection_enabled": bool(
                authority.get("collection_enabled", workflow.get("collection_enabled", False))
            ),
            "recognition_family": str(workflow.get("recognition_family", "") or "").strip(),
            "recognition_status": str(workflow.get("recognition_status", "") or "").strip(),
            "recognition_overlay_risk": str(workflow.get("recognition_overlay_risk", "") or "").strip(),
            "recognition_next_step": str(workflow.get("recognition_next_step", "") or "").strip(),
            "candidate_workflow_mode": str(authority.get("candidate_workflow_mode", "") or "").strip(),
            "background_trigger_recommended": bool(
                workflow.get("background_trigger_recommended", False)
            ),
        },
        "handoff": {
            "trust_level": str(handoff.get("trust_level", "") or "").strip().lower(),
            "reason": str(handoff.get("reason", "") or "").strip(),
            "baseline_state": str(handoff.get("baseline_state", "") or "").strip(),
            "baseline_valid": bool(handoff.get("baseline_valid", False)),
            "delta_state": str(handoff.get("delta_state", "") or "").strip(),
            "delta_valid": bool(handoff.get("delta_valid", False)),
            "session_state": str(handoff.get("session_state", "") or "").strip(),
            "session_terminal_reason": str(handoff.get("session_terminal_reason", "") or "").strip(),
            "runtime_identity_input": _to_dict(handoff.get("runtime_identity_input")),
            "calibration_fact_confidence": float(fact_confidence),
            "calibration_fact_level": str(handoff.get("calibration_fact_level", "") or "").strip().lower(),
            "calibration_fact_basis": _to_dict(handoff.get("calibration_fact_basis")),
            "calibration_fact_package": _to_dict(handoff.get("calibration_fact_package")),
            "setup_state": str(handoff.get("setup_state", "") or "").strip().lower(),
            "setup_freshness": str(handoff.get("setup_freshness", "") or "").strip().lower(),
        },
    }


def build_calibration_state(
    *,
    calibration: Any,
    calibration_authority: Any,
    calibration_workflow: Any,
) -> Dict[str, Any]:
    calib = _to_dict(calibration)
    authority = _to_dict(calibration_authority)
    workflow = _to_dict(calibration_workflow)

    enabled = bool(calib.get("enabled", False) or workflow.get("collection_enabled", False))
    baseline_ready = bool(calib.get("baseline_ready", False))
    needs_calibration = bool(calib.get("needs_calibration", False))
    drift_detected = bool(calib.get("baseline_drift_detected", False))
    recalibration_recommended = bool(calib.get("recalibration_recommended", False))
    fault_state_active = bool(calib.get("fault_state_active", False))
    active_reference_marker = str(authority.get("active_reference_marker", "") or "").strip()
    active_profile_id = str(authority.get("active_profile_id", "") or "").strip()
    active_profile_version = int(authority.get("active_profile_version", 0) or 0)
    if active_reference_marker and (not active_profile_id or active_profile_version <= 0):
        marker_profile, _, marker_version = active_reference_marker.partition("@")
        marker_profile = marker_profile.strip()
        if marker_profile and not active_profile_id:
            active_profile_id = marker_profile
        if marker_version.lower().startswith("v") and active_profile_version <= 0:
            try:
                active_profile_version = int(marker_version[1:] or 0)
            except ValueError:
                active_profile_version = 0
    active_reference_scope_match = bool(authority.get("active_reference_scope_match", True))
    baseline = _baseline_context(authority, {})
    has_saved_setup_reference = bool(
        baseline.get("key")
        or (
            active_reference_marker
            and active_profile_id not in {"", "C0"}
            and active_profile_version > 1
        )
        or (
            active_profile_id not in {"", "C0"}
            and active_profile_version > 1
        )
    )
    candidate_profile_id = str(authority.get("candidate_profile_id", "") or "").strip()
    candidate_decision_ready = bool(authority.get("candidate_decision_ready", False))
    candidate_session_progress_pct = int(authority.get("candidate_session_progress_pct", 0) or 0)
    candidate_sample_count = int(authority.get("candidate_sample_count", 0) or 0)
    candidate_session_sample_count = int(authority.get("candidate_session_sample_count", 0) or 0)
    candidate_zero_sample_stale_s = float(authority.get("candidate_zero_sample_stale_s", 0.0) or 0.0)
    candidate_session_state = str(authority.get("candidate_session_state", "") or "").strip().lower()
    last_decision_kind = str(authority.get("last_decision_kind", "") or "").strip().lower()
    last_decision_candidate_id = str(authority.get("last_decision_candidate_id", "") or "").strip()
    last_decision_badge = str(authority.get("last_decision_review_badge", "") or "").strip().lower()
    last_decision_summary = str(authority.get("last_decision_summary", "") or "").strip()
    workflow_phase = str(workflow.get("phase", "") or "").strip()
    collection_enabled = bool(
        authority.get("collection_enabled", workflow.get("collection_enabled", enabled))
    )
    candidate_collection_paused = bool(
        candidate_profile_id
        and not candidate_decision_ready
        and not collection_enabled
    )
    candidate_collection_stalled = bool(
        candidate_collection_paused
        and max(candidate_sample_count, candidate_session_sample_count) <= 0
        and candidate_zero_sample_stale_s >= float(DEFAULT_FIRST_SETUP_TIMEOUT_S)
    )
    candidate_terminal_timeout = bool(
        candidate_session_state == "timed_out"
        or (
            candidate_profile_id
            and last_decision_candidate_id == candidate_profile_id
            and last_decision_kind in {"failed_with_reason", "degraded_to_background"}
        )
        or (
            not candidate_profile_id
            and not (baseline.get("key") or active_reference_marker or active_profile_id)
            and workflow_phase == "bootstrap_initial"
            and (
                last_decision_badge == "timed out"
                or "timed out" in last_decision_summary.lower()
            )
        )
    )
    workflow_reason = str(workflow.get("phase_reason", "") or "").strip()
    next_step = str(
        authority.get("candidate_operator_next_step", "")
        or workflow.get("next_step", "")
        or ""
    ).strip()
    candidate_summary = str(
        authority.get("candidate_operator_summary", "")
        or authority.get("candidate_session_note", "")
        or ""
    ).strip()

    state = "disabled"
    freshness = "disabled"
    summary = "Setup help is currently off."

    if fault_state_active:
        state = "invalid"
        freshness = "invalid"
        summary = "Signal is unstable. Stop moving the antenna and check cable/placement."
    elif recalibration_recommended or drift_detected:
        state = "stale"
        freshness = "stale"
        summary = _plain_setup_summary(
            calib.get("baseline_drift_summary", "")
            or authority.get("candidate_operator_summary", "")
            or "Saved setup should be checked again on this connection.",
            "Saved setup should be checked again on this connection.",
        )
    elif candidate_terminal_timeout:
        state = "disabled"
        freshness = "timed_out"
        summary = _plain_setup_summary(
            last_decision_summary
            or authority.get("candidate_operator_summary", "")
            or authority.get("candidate_session_note", ""),
            "Setup check timed out.",
        )
    elif candidate_collection_stalled:
        state = "disabled"
        freshness = "stalled"
        summary = "Setup paused before readings. Hold antenna steady and start again."
    elif candidate_collection_paused:
        state = "disabled"
        freshness = "paused"
        summary = "Setup paused before the result. Keep position steady and resume."
    elif candidate_profile_id:
        if candidate_decision_ready:
            state = "provisional"
            freshness = "decision_pending"
            summary = _plain_setup_summary(candidate_summary, "54GUC is checking the result.")
            next_step = "auto_decide"
        else:
            state = "collecting"
            freshness = "collecting"
            summary = _plain_setup_summary(
                candidate_summary,
                "Collecting antenna readings. Hold position for stable quality.",
            )
    elif (
        not active_reference_scope_match
        and has_saved_setup_reference
    ):
        state = "stale"
        freshness = "scope_mismatch"
        summary = "Connection changed. Check this position again before judging antenna position."
    elif has_saved_setup_reference:
        state = "active"
        freshness = "fresh" if baseline_ready and not needs_calibration else "watch"
        summary = "Saved antenna setup is active."
    elif enabled and (workflow_phase or baseline_ready):
        state = "provisional"
        freshness = "pending"
        summary = "No saved antenna setup yet. Use a clear position and start setup."

    recommended_action = _recommended_action(
        state=state,
        workflow_phase=workflow_phase,
        active_reference_marker=active_reference_marker,
        active_profile_id=active_profile_id,
        candidate_decision_ready=candidate_decision_ready,
    )
    saved_setup_trust_level = (
        "trusted"
        if baseline_ready
        and active_reference_scope_match
        and has_saved_setup_reference
        and state in {"active", "provisional", "collecting"}
        else "estimated"
        if baseline_ready
        and has_saved_setup_reference
        else "none"
    )
    candidate_trust_level = (
        "decision_ready"
        if candidate_profile_id and candidate_decision_ready
        else "collecting"
        if candidate_profile_id
        else "none"
    )

    return {
        "state": state,
        "freshness": freshness,
        "enabled": enabled,
        "baseline_ready": baseline_ready,
        "saved_setup_trust_level": saved_setup_trust_level,
        "candidate_trust_level": candidate_trust_level,
        "active_reference_scope_match": active_reference_scope_match,
        "needs_calibration": needs_calibration,
        "baseline_drift_detected": drift_detected,
        "recalibration_recommended": recalibration_recommended,
        "fault_state_active": fault_state_active,
        "active_profile_id": active_profile_id,
        "active_profile_version": active_profile_version,
        "active_reference_marker": active_reference_marker,
        "baseline_context_key": str(baseline.get("key", "") or ""),
        "baseline_context_label": str(baseline.get("label", "") or ""),
        "baseline_context_band": int(baseline.get("band", 0) or 0),
        "baseline_context_cell": int(baseline.get("cell", 0) or 0),
        "baseline_context_sample_count": int(baseline.get("sample_count", 0) or 0),
        "baseline_context_approved_by": str(baseline.get("approved_by", "") or ""),
        "candidate_profile_id": candidate_profile_id,
        "candidate_decision_ready": candidate_decision_ready,
        "candidate_session_progress_pct": candidate_session_progress_pct,
        "candidate_sample_count": candidate_sample_count,
        "candidate_session_sample_count": candidate_session_sample_count,
        "candidate_primary_metric_confidence": float(
            authority.get("candidate_primary_metric_confidence", 0.0) or 0.0
        ),
        "candidate_network_load_biased_ratio": float(
            authority.get("candidate_network_load_biased_ratio", 0.0) or 0.0
        ),
        "candidate_session_state": candidate_session_state,
        "candidate_zero_sample_stale_s": candidate_zero_sample_stale_s,
        "candidate_terminal_timeout": candidate_terminal_timeout,
        "candidate_collection_paused": candidate_collection_paused,
        "candidate_collection_stalled": candidate_collection_stalled,
        "collection_enabled": collection_enabled,
        "workflow_phase": workflow_phase,
        "workflow_reason": workflow_reason,
        "next_step": next_step,
        "summary": summary,
        "recommended_action": recommended_action,
    }


def _synchronized_calibration_authority(cls):
    """Put every authority operation under the one re-entrant state lock."""

    for name, member in list(vars(cls).items()):
        if name == "__init__" or not isfunction(member):
            continue

        @wraps(member)
        def locked(self, *args, __member=member, **kwargs):
            with self._state_lock:
                return __member(self, *args, **kwargs)

        setattr(cls, name, locked)
    return cls


@_synchronized_calibration_authority
class CalibrationAuthority:
    """
    Versioned calibration source-of-truth.

    Lifecycle:
      collecting -> analyzed -> approved -> active
    """

    def __init__(
        self,
        cfg: Arch4Config,
        memory_dir: Path | str,
        *,
        checkpoint_state: Optional[Dict[str, Any]] = None,
        checkpoint_selection_error: str = "",
    ):
        self._state_lock = threading.RLock()
        self.cfg = cfg
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.store = CalibrationStore(self.memory_dir)
        self._calibration_db_path = self.memory_dir / "calibration.db"
        self._calibration_db_lock = threading.Lock()
        init_calibration_db(
            self._calibration_db_path,
            self._calibration_db_lock,
            enabled=bool(getattr(self.cfg, "calibration_db_enabled", True)),
        )
        self.path = self.store.authority_path
        self._runtime_events: List[Dict[str, Any]] = []
        self._background_signal_hits: int = 0
        self._checkpoint_interval_s = max(
            1.0,
            float(
                getattr(
                    self.cfg,
                    "calibration_checkpoint_interval_s",
                    DEFAULT_CALIBRATION_CHECKPOINT_INTERVAL_S,
                )
                or DEFAULT_CALIBRATION_CHECKPOINT_INTERVAL_S
            ),
        )
        self._checkpoint_max_dirty_events = max(
            1,
            int(
                getattr(
                    self.cfg,
                    "calibration_checkpoint_max_dirty_events",
                    DEFAULT_CALIBRATION_CHECKPOINT_MAX_DIRTY_EVENTS,
                )
                or DEFAULT_CALIBRATION_CHECKPOINT_MAX_DIRTY_EVENTS
            ),
        )
        self._dirty_delta_count = 0
        self._last_delta_reason = ""
        self._checkpoint_count = 0
        self._last_checkpoint_reason = ""
        self._checkpoint_reason_counts: Dict[str, int] = {}
        self._last_checkpoint_ts = 0.0
        self._state_mutation_epoch = int(
            dict(checkpoint_state or {}).get("calibration_state_epoch", 0) or 0
        )
        self._checkpoint_requested = False
        if isinstance(checkpoint_state, dict):
            persistence = checkpoint_state.get("calibration_persistence")
            if isinstance(persistence, dict):
                self._checkpoint_count = int(persistence.get("checkpoint_count", 0) or 0)
                self._last_checkpoint_ts = float(persistence.get("last_checkpoint_ts", 0.0) or 0.0)
                self._last_checkpoint_reason = str(persistence.get("last_checkpoint_reason", "") or "")
                counts = persistence.get("checkpoint_reason_counts")
                self._checkpoint_reason_counts = dict(counts) if isinstance(counts, dict) else {}
        self._state = self._load_or_init(
            checkpoint_state=checkpoint_state,
            checkpoint_selection_error=checkpoint_selection_error,
        )
        self._state.pop("active_config_generation", None)
        self._state.pop("pending_config_generation_ack", None)
        self._core_config_projection: Dict[str, Any] = {}

    def state_transaction(self):
        """Return the lock used by the Intelligence persistence owner."""

        return self._state_lock

    def refresh_thresholds(self) -> None:
        """ACK the active immutable config generation under the state lock."""

        self._checkpoint_interval_s = max(
            1.0,
            float(
                getattr(
                    self.cfg,
                    "calibration_checkpoint_interval_s",
                    DEFAULT_CALIBRATION_CHECKPOINT_INTERVAL_S,
                )
                or DEFAULT_CALIBRATION_CHECKPOINT_INTERVAL_S
            ),
        )
        self._checkpoint_max_dirty_events = max(
            1,
            int(
                getattr(
                    self.cfg,
                    "calibration_checkpoint_max_dirty_events",
                    DEFAULT_CALIBRATION_CHECKPOINT_MAX_DIRTY_EVENTS,
                )
                or DEFAULT_CALIBRATION_CHECKPOINT_MAX_DIRTY_EVENTS
            ),
        )

    def _default_values(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key in CALIBRATION_PROFILE_KEYS:
            out[key] = copy.deepcopy(getattr(self.cfg, key))
        return out

    @staticmethod
    def _extract_learning_gate(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        data = payload if isinstance(payload, dict) else {}
        gate = data.get("learning_gate") if isinstance(data.get("learning_gate"), dict) else {}
        if gate:
            return {
                "learning_allowed": safe_bool(gate.get("learning_allowed", True), True),
                "block_reason": str(gate.get("block_reason", "") or ""),
                "sample_stale": safe_bool(gate.get("sample_stale", False), False),
            }
        if any(key in data for key in ("learning_allowed", "learning_block_reason", "sample_stale")):
            return {
                "learning_allowed": safe_bool(data.get("learning_allowed", True), True),
                "block_reason": str(data.get("learning_block_reason", "") or ""),
                "sample_stale": safe_bool(data.get("sample_stale", False), False),
            }
        return {}

    _context_key_from_summary = staticmethod(context_key_from_summary)
    _context_key_from_calibration_status = staticmethod(context_key_from_calibration_status)
    _context_key_from_sample = staticmethod(context_key_from_sample)
    _split_context_key = staticmethod(split_context_key)

    def _context_baselines(self, target: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        st = target if isinstance(target, dict) else self._state
        raw = st.get("context_baselines")
        if not isinstance(raw, dict):
            raw = {}
            st["context_baselines"] = raw
        return raw

    def _build_context_baseline_entry(
        self,
        *,
        summary: Dict[str, Any],
        profile_id: str,
        profile_version: int,
        approved_by: str,
        runtime_context: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        context_key = self._context_key_from_summary(summary)
        band = int(summary_scalar(summary, "dominant_context_band") or 0)
        cell = int(summary_scalar(summary, "dominant_context_cell") or 0)
        scoped_context = sanitize_runtime_context(runtime_context if isinstance(runtime_context, dict) else {})
        scope = build_runtime_session_scope(scoped_context)
        return {
            "context_key": str(context_key),
            "band": int(band),
            "cell": int(cell),
            "profile_id": str(profile_id or ""),
            "profile_version": int(profile_version or 0),
            "approved_by": str(approved_by or ""),
            "updated_ts": float(now if now is not None else time.time()),
            "runtime_context": scoped_context,
            "scope": scope,
            "scope_key": runtime_session_scope_key(scoped_context),
            "summary": copy.deepcopy(summary if isinstance(summary, dict) else {}),
        }

    @staticmethod
    def _context_baseline_scope(entry: Optional[Dict[str, Any]]) -> Dict[str, str]:
        data = entry if isinstance(entry, dict) else {}
        scope = data.get("scope")
        if isinstance(scope, dict) and scope:
            return {str(k): str(v) for k, v in scope.items() if str(v or "")}
        runtime_context = data.get("runtime_context") if isinstance(data.get("runtime_context"), dict) else {}
        return build_runtime_session_scope(runtime_context)

    @staticmethod
    def _context_baseline_storage_key(
        context_key: str,
        *,
        runtime_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        scoped_context = sanitize_runtime_context(runtime_context if isinstance(runtime_context, dict) else {})
        return context_baseline_storage_key(context_key, runtime_context=scoped_context)

    def _scoped_context_baseline_item(
        self,
        context_key: str,
        *,
        runtime_context: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, Optional[Dict[str, Any]]]:
        normalized_context_key = str(context_key or "").strip()
        if not normalized_context_key:
            return "", None
        baselines = self._context_baselines()
        current_runtime_context = sanitize_runtime_context(
            runtime_context if isinstance(runtime_context, dict) else self.get_runtime_context()
        )
        direct_key = self._context_baseline_storage_key(
            normalized_context_key,
            runtime_context=current_runtime_context,
        )
        for storage_key in (direct_key, normalized_context_key):
            entry = baselines.get(storage_key)
            if isinstance(entry, dict) and runtime_session_scope_matches(entry, current_runtime_context):
                return str(storage_key), entry
        for storage_key, entry in baselines.items():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("context_key", "") or "") != normalized_context_key:
                continue
            if runtime_session_scope_matches(entry, current_runtime_context):
                return str(storage_key), entry
        return "", None

    def _scoped_context_baseline_entry(
        self,
        context_key: str,
        *,
        runtime_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        _storage_key, entry = self._scoped_context_baseline_item(
            context_key,
            runtime_context=runtime_context,
        )
        return entry

    @staticmethod
    def _merge_metric_rows(
        old_row: Optional[Dict[str, Any]],
        new_row: Optional[Dict[str, Any]],
        *,
        decay: float,
    ) -> Dict[str, float]:
        old = old_row if isinstance(old_row, dict) else {}
        new = new_row if isinstance(new_row, dict) else {}
        old_count = max(0.0, float(old.get("count", 0) or 0.0)) * float(decay)
        new_count = max(0.0, float(new.get("count", 0) or 0.0))
        old_avg = safe_float(old.get("avg"))
        new_avg = safe_float(new.get("avg"))
        total = old_count + new_count
        if total <= 0.0:
            return {"count": 0, "avg": 0.0, "min": 0.0, "max": 0.0}
        weighted = 0.0
        if old_avg is not None and old_count > 0.0:
            weighted += float(old_avg) * old_count
        if new_avg is not None and new_count > 0.0:
            weighted += float(new_avg) * new_count
        avg = weighted / total
        return {
            "count": int(round(total)),
            "avg": float(avg),
            "min": float(avg),
            "max": float(avg),
        }

    @staticmethod
    def _weighted_scalar(
        old_value: Any,
        new_value: Any,
        *,
        old_weight: float,
        new_weight: float,
    ) -> float:
        old_v = safe_float(old_value)
        new_v = safe_float(new_value)
        total = max(0.0, float(old_weight)) + max(0.0, float(new_weight))
        if total <= 0.0:
            return 0.0
        acc = 0.0
        if old_v is not None and old_weight > 0.0:
            acc += float(old_v) * float(old_weight)
        if new_v is not None and new_weight > 0.0:
            acc += float(new_v) * float(new_weight)
        return float(acc / total)

    def _merge_context_summary(
        self,
        old_summary: Optional[Dict[str, Any]],
        new_summary: Dict[str, Any],
        *,
        decay: float,
    ) -> Dict[str, Any]:
        if not isinstance(old_summary, dict) or int(old_summary.get("sample_count", 0) or 0) <= 0:
            return copy.deepcopy(new_summary if isinstance(new_summary, dict) else {})

        old_sample_count = max(0.0, float(old_summary.get("sample_count", 0) or 0.0)) * float(decay)
        new_sample_count = max(0.0, float(new_summary.get("sample_count", 0) or 0.0))
        old_connected = max(0.0, float(old_summary.get("connected_samples", 0) or 0.0)) * float(decay)
        new_connected = max(0.0, float(new_summary.get("connected_samples", 0) or 0.0))
        old_ca_active = max(0.0, float(old_summary.get("ca_active_samples", 0) or 0.0)) * float(decay)
        new_ca_active = max(0.0, float(new_summary.get("ca_active_samples", 0) or 0.0))

        merged = copy.deepcopy(new_summary if isinstance(new_summary, dict) else {})
        merged_metrics: Dict[str, Any] = {}
        old_metrics = old_summary.get("metrics") if isinstance(old_summary.get("metrics"), dict) else {}
        new_metrics = new_summary.get("metrics") if isinstance(new_summary.get("metrics"), dict) else {}
        for key in sorted(set(old_metrics.keys()) | set(new_metrics.keys())):
            merged_metrics[str(key)] = self._merge_metric_rows(
                old_metrics.get(key) if isinstance(old_metrics, dict) else None,
                new_metrics.get(key) if isinstance(new_metrics, dict) else None,
                decay=decay,
            )
        merged["metrics"] = merged_metrics
        merged["sample_count"] = int(round(old_sample_count + new_sample_count))
        merged["connected_samples"] = int(round(old_connected + new_connected))
        merged["ca_active_samples"] = int(round(old_ca_active + new_ca_active))
        merged["ca_persistence_ratio"] = (
            float(merged["ca_active_samples"]) / float(max(1, merged["connected_samples"]))
            if int(merged["connected_samples"]) > 0
            else 0.0
        )
        merged["ca_avg"] = float(merged.get("ca_persistence_ratio", 0.0) or 0.0)
        merged["network_load_biased_ratio"] = self._weighted_scalar(
            old_summary.get("network_load_biased_ratio", 0.0),
            new_summary.get("network_load_biased_ratio", 0.0),
            old_weight=old_sample_count,
            new_weight=new_sample_count,
        )
        merged["offpeak_ratio"] = self._weighted_scalar(
            old_summary.get("offpeak_ratio", 0.0),
            new_summary.get("offpeak_ratio", 0.0),
            old_weight=old_sample_count,
            new_weight=new_sample_count,
        )
        merged["ri_avg"] = metric_avg({"metrics": merged_metrics}, "ri")
        merged["dominant_context_band"] = int(summary_scalar(new_summary, "dominant_context_band") or 0)
        merged["dominant_context_cell"] = int(summary_scalar(new_summary, "dominant_context_cell") or 0)
        merged["dominant_context_ratio"] = 1.0
        merged["context_sample_count"] = int(round(old_sample_count + new_sample_count))
        merged["context_pure"] = True
        merged["dominant_anchor_band"] = int(summary_scalar(new_summary, "dominant_anchor_band") or 0)
        merged["dominant_anchor_ratio"] = 1.0 if int(merged["dominant_anchor_band"]) > 0 else 0.0
        merged["anchor_switches"] = 0
        merged["anchor_band_counts"] = (
            {int(merged["dominant_anchor_band"]): int(merged["sample_count"])}
            if int(merged["dominant_anchor_band"]) > 0 and int(merged["sample_count"]) > 0
            else {}
        )
        return merged

    def _store_context_baseline(
        self,
        *,
        summary: Dict[str, Any],
        profile_id: str,
        profile_version: int,
        approved_by: str,
        runtime_context: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> str:
        context_key = self._context_key_from_summary(summary)
        if not context_key:
            return ""
        baselines = self._context_baselines()
        current_runtime_context = sanitize_runtime_context(
            runtime_context if isinstance(runtime_context, dict) else self.get_runtime_context()
        )
        existing_storage_key, existing = self._scoped_context_baseline_item(
            context_key,
            runtime_context=current_runtime_context,
        )
        merged_summary = copy.deepcopy(summary if isinstance(summary, dict) else {})
        if isinstance(existing, dict):
            old_updated_ts = float(existing.get("updated_ts", now if now is not None else time.time()) or 0.0)
            now_ts = float(now if now is not None else time.time())
            age_days = max(0.0, (now_ts - old_updated_ts) / 86400.0)
            decay = 0.97 ** age_days
            old_summary = existing.get("summary") if isinstance(existing.get("summary"), dict) else {}
            merged_summary = self._merge_context_summary(
                old_summary if isinstance(old_summary, dict) else {},
                merged_summary,
                decay=decay,
            )
        storage_key = existing_storage_key or self._context_baseline_storage_key(
            context_key,
            runtime_context=current_runtime_context,
        )
        baselines[storage_key] = self._build_context_baseline_entry(
            summary=merged_summary,
            profile_id=profile_id,
            profile_version=profile_version,
            approved_by=approved_by,
            runtime_context=current_runtime_context,
            now=now,
        )
        return context_key

    def _maybe_seed_background_context_baseline(
        self,
        *,
        candidate_id: str,
        profile: Dict[str, Any],
        sample_summary: Dict[str, Any],
        session_report: Dict[str, Any],
        runtime_context_ready: bool,
        backend: str,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        if str(profile.get("workflow_mode", "") or "") != PHASE_BACKGROUND:
            return {"seeded": False, "reason": "not_background"}
        if not bool(session_report.get("ready", False)):
            return {"seeded": False, "reason": "window_not_ready"}
        if not bool(runtime_context_ready):
            return {"seeded": False, "reason": "runtime_context_not_ready"}
        if not bool(sample_summary.get("context_pure", False)):
            return {"seeded": False, "reason": "context_not_pure"}
        context_key = self._context_key_from_summary(sample_summary)
        if not context_key:
            return {"seeded": False, "reason": "missing_context_key"}

        runtime_context = self.get_runtime_context()
        if self._scoped_context_baseline_entry(context_key, runtime_context=runtime_context):
            return {"seeded": False, "reason": "context_baseline_present", "context_key": context_key}

        candidate_primary = assess_bootstrap_primary_metrics(
            sample_summary=sample_summary,
            backend=str(backend or ""),
        )
        if not bool(candidate_primary.get("primary_metrics_ready", False)):
            return {
                "seeded": False,
                "reason": "primary_metrics_not_ready",
                "context_key": context_key,
            }
        candidate_quality = score_bootstrap_baseline(
            sample_summary,
            backend=str(backend or ""),
        )
        if int(candidate_quality.get("metric_count", 0) or 0) <= 0:
            return {
                "seeded": False,
                "reason": "quality_metrics_not_ready",
                "context_key": context_key,
            }

        now_ts = float(now if now is not None else time.time())
        profile_version = int(profile.get("version", self._state.get("active_profile_version", 0)) or 0)
        seeded_context_key = self._store_context_baseline(
            summary=sample_summary,
            profile_id=str(candidate_id),
            profile_version=profile_version,
            approved_by="background_context_seed_auto",
            runtime_context=runtime_context,
            now=now_ts,
        )
        if not seeded_context_key:
            return {"seeded": False, "reason": "store_failed", "context_key": context_key}

        seed_report = {
            "seeded": True,
            "reason": "missing_same_context_baseline",
            "context_key": str(seeded_context_key),
            "profile_id": str(candidate_id),
            "profile_version": int(profile_version),
            "approved_by": "background_context_seed_auto",
            "sample_count": int(sample_summary.get("sample_count", 0) or 0),
            "quality_pct": int(candidate_quality.get("quality_pct", 0) or 0),
            "primary_metric_count": int(candidate_primary.get("primary_metric_count", 0) or 0),
            "primary_metrics_available": list(candidate_primary.get("primary_metrics_available", []) or []),
            "ts": float(now_ts),
        }
        self.store.append_sample(
            build_context_baseline_seeded_journal_entry(
                candidate_id=str(candidate_id),
                workflow_mode=PHASE_BACKGROUND,
                context_key=str(seeded_context_key),
                seed_report=seed_report,
                ts=float(now_ts),
            )
        )
        self._append_recent_session_history(
            build_context_baseline_seeded_history_entry(
                candidate_id=str(candidate_id),
                workflow_mode=PHASE_BACKGROUND,
                seed_report=seed_report,
                ts=float(now_ts),
            )
        )
        self._push_event(
            "calibration_context_baseline_seeded",
            candidate_id=str(candidate_id),
            workflow_mode=PHASE_BACKGROUND,
            context_key=str(seeded_context_key),
            reason="missing_same_context_baseline",
            sample_count=int(seed_report["sample_count"]),
            quality_pct=int(seed_report["quality_pct"]),
        )
        return seed_report

    def _reference_bundle(
        self,
        candidate_summary: Optional[Dict[str, Any]] = None,
        *,
        runtime_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        context_key = self._context_key_from_summary(candidate_summary)
        current_runtime_context = sanitize_runtime_context(
            runtime_context if isinstance(runtime_context, dict) else self.get_runtime_context()
        )
        if context_key:
            entry = self._scoped_context_baseline_entry(
                context_key,
                runtime_context=current_runtime_context,
            )
            if isinstance(entry, dict):
                summary = entry.get("summary")
                if isinstance(summary, dict) and int(summary.get("sample_count", 0) or 0) > 0:
                    profile_id = str(entry.get("profile_id", "") or "")
                    profile_version = int(entry.get("profile_version", 0) or 0)
                    return {
                        "summary": copy.deepcopy(summary),
                        "profile_id": profile_id,
                        "profile_version": profile_version,
                        "marker": (
                            f"{profile_id}@v{profile_version}"
                            if profile_id
                            else ""
                        ),
                        "context_key": str(context_key),
                        "band": int(entry.get("band", 0) or 0),
                        "cell": int(entry.get("cell", 0) or 0),
                        "approved_by": str(entry.get("approved_by", "") or ""),
                        "updated_ts": float(entry.get("updated_ts", 0.0) or 0.0),
                        "runtime_context": sanitize_runtime_context(
                            entry.get("runtime_context") if isinstance(entry.get("runtime_context"), dict) else {}
                        ),
                        "scope": self._context_baseline_scope(entry),
                        "scope_key": str(entry.get("scope_key", "") or ""),
                        "scope_match": True,
                    }
        active = self.get_active_profile()
        report = active.get("analysis_report") if isinstance(active.get("analysis_report"), dict) else {}
        summary: Dict[str, Any]
        if isinstance(report.get("sample_summary"), dict):
            summary = copy.deepcopy(report.get("sample_summary"))
        else:
            raw = active.get("raw_samples")
            if isinstance(raw, list):
                summary = summarize_samples([dict(v) for v in raw if isinstance(v, dict)])
            else:
                summary = {"sample_count": 0, "metrics": {}}
        profile_id = str(self._state.get("active_profile_id", "") or "")
        profile_version = int(self._state.get("active_profile_version", 0) or 0)
        active_runtime_context = sanitize_runtime_context(
            active.get("runtime_context") if isinstance(active.get("runtime_context"), dict) else {}
        )
        return {
            "summary": summary,
            "profile_id": profile_id,
            "profile_version": profile_version,
            "marker": (
                f"{profile_id}@v{profile_version}"
                if profile_id
                else ""
            ),
            "context_key": self._context_key_from_summary(summary),
            "band": int(summary_scalar(summary, "dominant_context_band") or 0),
            "cell": int(summary_scalar(summary, "dominant_context_cell") or 0),
            "approved_by": str(active.get("approved_by", "") or ""),
            "updated_ts": float(active.get("updated_ts", 0.0) or 0.0),
            "runtime_context": active_runtime_context,
            "scope": build_runtime_session_scope(active_runtime_context),
            "scope_key": runtime_session_scope_key(active_runtime_context),
            "scope_match": runtime_session_scope_matches(active_runtime_context, current_runtime_context),
        }

    def _sample_ts(self, sample: Dict[str, Any], now: Optional[float] = None) -> float:
        if isinstance(sample, dict):
            raw = safe_float(sample.get("ts"))
            if raw is not None:
                return float(raw)
        return float(now if now is not None else time.time())

    def _summarize_collection_session(
        self,
        session: Dict[str, Any],
        *,
        sample_count: int,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        now_ts = float(now if now is not None else time.time())
        st = dict(session or {})
        started_ts = float(st.get("started_ts", now_ts) or now_ts)
        last_sample_ts = float(st.get("last_sample_ts", 0.0) or 0.0)
        min_samples = max(1, int(st.get("min_samples", 1) or 1))
        target_samples = max(min_samples, int(st.get("target_samples", min_samples) or min_samples))
        min_duration_s = max(0.0, float(st.get("min_duration_s", 0.0) or 0.0))
        target_duration_s = max(min_duration_s, float(st.get("target_duration_s", min_duration_s) or min_duration_s))
        sample_interval_s = max(0.0, float(st.get("sample_interval_s", 0.0) or 0.0))
        recommended_hold_s = max(0.0, float(st.get("recommended_hold_s", 0.0) or 0.0))
        timeout_raw = st.get("timeout_s")
        if timeout_raw in (None, ""):
            mode = normalize_workflow_mode(
                str(st.get("workflow_mode", "") or ""),
                active_profile_id=str(self._state.get("active_profile_id", "") or ""),
                active_profile_version=int(self._state.get("active_profile_version", 0) or 0),
            )
            if mode == PHASE_BOOTSTRAP:
                timeout_s = float(
                    getattr(self.cfg, "calibration_first_setup_timeout_s", DEFAULT_FIRST_SETUP_TIMEOUT_S)
                    or DEFAULT_FIRST_SETUP_TIMEOUT_S
                )
            elif mode == PHASE_BACKGROUND:
                timeout_s = 0.0
            else:
                timeout_s = float(
                    getattr(self.cfg, "calibration_collection_timeout_s", DEFAULT_COLLECTION_TIMEOUT_S)
                    or DEFAULT_COLLECTION_TIMEOUT_S
                )
        else:
            timeout_s = max(0.0, float(timeout_raw or 0.0))
        effective_ts = max(now_ts, started_ts, last_sample_ts)
        if sample_count > 0 and last_sample_ts > 0.0:
            duration_s = max(0.0, last_sample_ts - started_ts)
        else:
            duration_s = max(0.0, effective_ts - started_ts)
        sample_progress = min(1.0, float(sample_count) / float(max(1, min_samples)))
        duration_progress = 1.0 if min_duration_s <= 0.0 else min(1.0, duration_s / min_duration_s)
        target_sample_progress = min(1.0, float(sample_count) / float(max(1, target_samples)))
        target_duration_progress = 1.0 if target_duration_s <= 0.0 else min(1.0, duration_s / target_duration_s)
        ready = bool(sample_count >= min_samples and duration_s >= min_duration_s)
        timed_out = bool(timeout_s > 0.0 and duration_s >= timeout_s and not ready)
        next_sample_due_in_s = 0.0
        if sample_count > 0 and sample_interval_s > 0.0 and last_sample_ts > 0.0:
            next_sample_due_in_s = max(0.0, sample_interval_s - max(0.0, effective_ts - last_sample_ts))

        workflow_mode = str(st.get("workflow_mode", "") or "")
        if timed_out:
            state = "timed_out"
        elif ready:
            state = "decision_ready"
        elif sample_count <= 0:
            state = "awaiting_first_sample"
        else:
            state = "collecting_window"
        if workflow_mode == PHASE_BOOTSTRAP:
            note = (
                "Setup check complete. 54GUC is checking the result."
                if ready
                else "Setup check timed out."
                if timed_out
                else "Keep the antenna steady."
            )
        elif workflow_mode == PHASE_ANCHOR_SURVEY:
            note = (
                "Anchor check complete. 54GUC is checking which band stayed strongest."
                if ready
                else "Hold each band position steady."
            )
        elif workflow_mode == PHASE_COMPARATIVE:
            note = (
                "Comparison complete. 54GUC is checking whether this position is better."
                if ready
                else "Hold the current antenna position."
            )
        elif workflow_mode == PHASE_BACKGROUND:
            note = (
                "Background check complete. Result is ready."
                if ready
                else "Still collecting signal quality."
            )
        else:
            note = "Calibration collection is active."

        return {
            "session_id": str(st.get("session_id", "") or ""),
            "workflow_mode": workflow_mode,
            "reason": str(st.get("reason", "") or ""),
            "started_ts": float(started_ts),
            "last_sample_ts": float(last_sample_ts),
            "state": str(state),
            "sample_count": int(sample_count),
            "min_samples": int(min_samples),
            "target_samples": int(target_samples),
            "sample_progress_pct": int(round(100.0 * sample_progress)),
            "target_sample_progress_pct": int(round(100.0 * target_sample_progress)),
            "duration_s": float(duration_s),
            "min_duration_s": float(min_duration_s),
            "target_duration_s": float(target_duration_s),
            "timeout_s": float(timeout_s),
            "timed_out": bool(timed_out),
            "duration_progress_pct": int(round(100.0 * duration_progress)),
            "target_duration_progress_pct": int(round(100.0 * target_duration_progress)),
            "progress_pct": int(round(100.0 * min(sample_progress, duration_progress))),
            "target_progress_pct": int(round(100.0 * min(target_sample_progress, target_duration_progress))),
            "sample_interval_s": float(sample_interval_s),
            "recommended_hold_s": float(recommended_hold_s),
            "ready": bool(ready),
            "next_sample_due_in_s": float(next_sample_due_in_s),
            "operator_note": str(note),
        }

    def _background_trigger_policy(
        self,
        calibration_status: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        calibration = dict(calibration_status or {})
        runtime_context = self.get_runtime_context()
        recognition = runtime_context.get("recognition") if isinstance(runtime_context.get("recognition"), dict) else {}
        backend = str(runtime_context.get("backend", "") or "").strip().lower()
        family = str(recognition.get("family_hint", "") or "").strip().lower()

        family_key = "default"
        if not family:
            from arch4.operator_profiles import infer_backend_family
            family = infer_backend_family(backend)
        if family in BACKGROUND_TRIGGER_FAMILY_RULES:
            family_key = family

        family_rule = BACKGROUND_TRIGGER_FAMILY_RULES.get(family_key, BACKGROUND_TRIGGER_FAMILY_RULES["default"])
        base_gap_threshold = max(0.0, float(getattr(self.cfg, "calibration_gap_threshold", 0.40) or 0.40))
        required_gap_score = float(base_gap_threshold + float(family_rule.get("gap_margin", 0.12) or 0.12))
        trigger_hits = int(family_rule.get("trigger_hits", BACKGROUND_CANDIDATE_TRIGGER_HITS) or BACKGROUND_CANDIDATE_TRIGGER_HITS)
        gap_score = float(calibration.get("gap_score", 0.0) or 0.0)
        baseline_ready = bool(calibration.get("baseline_ready", False))
        needs_calibration = bool(calibration.get("needs_calibration", False))
        health_ok = bool(calibration.get("health_ok", True))
        current_context_key = self._context_key_from_calibration_status(calibration)
        context_baseline_present = bool(
            self._scoped_context_baseline_entry(
                str(current_context_key),
                runtime_context=runtime_context,
            )
        )
        missing_context_baseline = bool(baseline_ready and current_context_key and not context_baseline_present)
        if missing_context_baseline:
            trigger_hits = 1

        reason = "ready"
        eligible = True
        if not baseline_ready:
            eligible = False
            reason = "baseline_not_ready"
        elif not health_ok:
            eligible = False
            reason = "background_hold_while_health_degraded"
        elif missing_context_baseline:
            eligible = True
            reason = "missing_context_baseline"
        elif not needs_calibration:
            eligible = False
            reason = "background_drift_not_present"
        elif gap_score < required_gap_score:
            eligible = False
            reason = "background_drift_too_weak_for_candidate_open"

        return {
            "backend": backend,
            "recognition_family": family,
            "family_rule_key": family_key,
            "headline": str(family_rule.get("headline", "") or ""),
            "trigger_hits": int(trigger_hits),
            "base_gap_threshold": float(base_gap_threshold),
            "required_gap_score": float(required_gap_score),
            "gap_score": float(gap_score),
            "baseline_ready": bool(baseline_ready),
            "needs_calibration": bool(needs_calibration),
            "health_ok": bool(health_ok),
            "current_context_key": str(current_context_key),
            "context_baseline_present": bool(context_baseline_present),
            "missing_context_baseline": bool(missing_context_baseline),
            "force_recommended": bool(missing_context_baseline),
            "eligible": bool(eligible),
            "reason": str(reason),
        }

    def _replay_raw_sample_deltas(self, state: Dict[str, Any]) -> int:
        profiles = state.get("profiles", {}) if isinstance(state.get("profiles"), dict) else {}
        if not profiles:
            return 0
        try:
            events = self.store.cm_samples_journal.read_all()
        except Exception as exc:
            log.debug("replay_calibration_sample_deltas: suppressed %s", exc)
            return 0
        replayed = 0
        seen_keys_by_candidate: Dict[str, set] = {}
        for event in events:
            if not isinstance(event, dict) or str(event.get("kind", "") or "") != "cm_raw_sample":
                continue
            candidate_id = str(event.get("candidate_id", "") or "")
            prof = profiles.get(candidate_id)
            if not isinstance(prof, dict):
                continue
            if str(prof.get("state", "") or "") not in {"collecting", "analyzed"}:
                continue
            sample = event.get("sample") if isinstance(event.get("sample"), dict) else {}
            if not sample:
                continue
            observation_id = str(sample.get("observation_id", "") or "")
            if observation_id:
                try:
                    self._archive_calibration_observation(
                        self._observation_from_sample(sample)
                    )
                except Exception as exc:
                    log.warning(
                        "Calibration observation archive replay failed id=%s: %s",
                        observation_id,
                        exc,
                    )
                receipts = state.setdefault("recent_observation_receipts", {})
                if isinstance(receipts, dict):
                    receipts[observation_id] = {
                        "accepted": True,
                        "reason": "replayed_observation",
                        "candidate_id": candidate_id,
                        "background_noted": True,
                    }
            raw_samples = prof.setdefault("raw_samples", [])
            if not isinstance(raw_samples, list):
                raw_samples = []
                prof["raw_samples"] = raw_samples
            if candidate_id not in seen_keys_by_candidate:
                seen_keys_by_candidate[candidate_id] = {
                    (
                        ("observation", str(row.get("observation_id", "") or ""))
                        if str(row.get("observation_id", "") or "")
                        else (
                            float(row.get("ts", 0.0) or 0.0),
                            str(row.get("marker", "") or ""),
                            str(row.get("serving_band", row.get("band", "")) or ""),
                            str(row.get("pci", row.get("cell_id", "")) or ""),
                        )
                    )
                    for row in raw_samples
                    if isinstance(row, dict)
                }
            sample_key = (
                ("observation", observation_id)
                if observation_id
                else (
                    float(event.get("ts", 0.0) or sample.get("ts", 0.0) or 0.0),
                    str(sample.get("marker", "") or ""),
                    str(sample.get("serving_band", sample.get("band", "")) or ""),
                    str(sample.get("pci", sample.get("cell_id", "")) or ""),
                )
            )
            if sample_key in seen_keys_by_candidate[candidate_id]:
                continue
            raw_samples.append(dict(sample))
            seen_keys_by_candidate[candidate_id].add(sample_key)
            if len(raw_samples) > AUTHORITY_RAW_SAMPLE_KEEP_LAST:
                del raw_samples[: len(raw_samples) - AUTHORITY_RAW_SAMPLE_KEEP_LAST]
            session = prof.get("collection_session") if isinstance(prof.get("collection_session"), dict) else {}
            if isinstance(session, dict):
                session["last_sample_ts"] = max(
                    float(session.get("last_sample_ts", 0.0) or 0.0),
                    float(event.get("ts", 0.0) or sample.get("ts", 0.0) or 0.0),
                )
            replayed += 1
        if replayed:
            state.setdefault("migration_notes", [])
            notes = state.get("migration_notes")
            if isinstance(notes, list):
                notes.append(
                    {
                        "ts": float(time.time()),
                        "kind": "replay_calibration_raw_sample_deltas",
                        "replayed": int(replayed),
                    }
                )
        return int(replayed)

    def _load_or_init(
        self,
        *,
        checkpoint_state: Optional[Dict[str, Any]] = None,
        checkpoint_selection_error: str = "",
    ) -> Dict[str, Any]:
        try:
            raw = (
                copy.deepcopy(checkpoint_state)
                if isinstance(checkpoint_state, dict)
                else None
            )
            if isinstance(raw, dict):
                raw["provider_time_behavior_publication"] = sanitize_provider_time_behavior_publication(
                    raw.get("provider_time_behavior_publication")
                    if isinstance(raw.get("provider_time_behavior_publication"), dict)
                    else {}
                )
                self._context_baselines(raw)
                active_profile_id = str(raw.get("active_profile_id", "") or "")
                active_profile_version = int(raw.get("active_profile_version", 0) or 0)
                if not self._context_baselines(raw) and active_profile_id not in {"", "C0"}:
                    profiles = raw.get("profiles", {}) if isinstance(raw.get("profiles"), dict) else {}
                    active = profiles.get(active_profile_id)
                    if isinstance(active, dict):
                        report = active.get("analysis_report") if isinstance(active.get("analysis_report"), dict) else {}
                        summary = report.get("sample_summary") if isinstance(report.get("sample_summary"), dict) else {}
                        if not isinstance(summary, dict) or int(summary.get("sample_count", 0) or 0) <= 0:
                            raw_samples = active.get("raw_samples")
                            if isinstance(raw_samples, list):
                                summary = summarize_samples([dict(v) for v in raw_samples if isinstance(v, dict)])
                        if isinstance(summary, dict) and int(summary.get("sample_count", 0) or 0) > 0:
                            self._store_context_baseline(
                                summary=summary,
                                profile_id=active_profile_id,
                                profile_version=active_profile_version,
                                approved_by=str(active.get("approved_by", "") or ""),
                                runtime_context=(
                                    active.get("runtime_context")
                                    if isinstance(active.get("runtime_context"), dict)
                                    else {}
                                ),
                                now=float(raw.get("last_updated_ts", time.time()) or time.time()),
                            )
                if not self._context_baselines(raw) and active_profile_id in {"", "C0"}:
                    profiles = raw.get("profiles", {}) if isinstance(raw.get("profiles"), dict) else {}
                    stale_candidate_id = str(raw.get("candidate_profile_id", "") or "")
                    stale_candidate = profiles.get(stale_candidate_id) if stale_candidate_id else None
                    stale_workflow = (
                        str(stale_candidate.get("workflow_mode", "") or "")
                        if isinstance(stale_candidate, dict)
                        else ""
                    )
                    stale_state = (
                        str(stale_candidate.get("state", "") or "")
                        if isinstance(stale_candidate, dict)
                        else ""
                    )
                    stale_session = (
                        stale_candidate.get("collection_session")
                        if isinstance(stale_candidate, dict) and isinstance(stale_candidate.get("collection_session"), dict)
                        else {}
                    )
                    has_live_collection = bool(
                        stale_state == "collecting"
                        and str(stale_session.get("session_id", "") or "")
                        and float(stale_session.get("started_ts", 0.0) or 0.0) > 0.0
                    )
                    should_reset_stale_candidate = bool(
                        stale_candidate_id
                        and stale_candidate_id != "C0"
                        and not has_live_collection
                    )
                    if should_reset_stale_candidate:
                        raw["candidate_profile_id"] = ""
                        raw["profile_counter"] = 0
                        raw["profiles"] = {"C0": profiles.get("C0", build_calibration_profile(
                            profile_id="C0",
                            version=1,
                            state="active",
                            values=self._default_values(),
                            now=float(raw.get("last_updated_ts", time.time()) or time.time()),
                            approved_by="bootstrap",
                        ))}
                        if stale_workflow:
                            raw.setdefault("migration_notes", [])
                            notes = raw.get("migration_notes")
                            if isinstance(notes, list):
                                notes.append(
                                    {
                                        "ts": float(time.time()),
                                        "kind": "reset_stale_calibration_candidate",
                                        "candidate_id": stale_candidate_id,
                                        "reason": "no_context_baseline_with_dummy_c0",
                                    }
                                )
                        self._save_state(raw)
                replayed = self._replay_raw_sample_deltas(raw)
                raw["calibration_persistence"] = self._persistence_status()
                if replayed:
                    self._save_state(raw, reason="replay_raw_sample_deltas")
                return raw
        except Exception as exc:
            log.error("selected calibration checkpoint rejected: %s", exc)
        now = time.time()
        active = build_calibration_profile(
            profile_id="C0",
            version=1,
            state="active",
            values=self._default_values(),
            now=now,
            approved_by="bootstrap",
        )
        st = {
            "active_profile_id": "C0",
            "active_profile_version": 1,
            "new_calibration_available": False,
            "candidate_profile_id": "",
            "collection_enabled": True,
            "profile_counter": 0,
            "runtime_context": {},
            "external_knowledge": {},
            "provider_time_behavior_publication": _default_provider_time_behavior_publication(),
            "recent_session_history": [],
            "context_baselines": {},
            "current_observation_scope": {},
            "recent_observation_receipts": {},
            "profiles": {"C0": active},
            "last_updated_ts": now,
        }
        if str(checkpoint_selection_error or ""):
            st["restart_degraded"] = {
                "reason": str(checkpoint_selection_error),
                "old_fixed_authority_reader": "deleted",
            }
        self._save_state(st)
        return st

    def _restore_persistence_meta(self) -> None:
        raise RuntimeError("fixed_calibration_meta_restart_reader_deleted")

    def _persistence_status(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        ts = float(now if now is not None else time.time())
        elapsed_s = max(0.0, ts - float(self._last_checkpoint_ts or 0.0)) if self._last_checkpoint_ts else 0.0
        return {
            "layout": "delta_journal_with_engine_atomic_checkpoint",
            "delta_journal": self.store.cm_samples_journal.filename,
            "checkpoint_snapshots": ["calibration_authority.json"],
            "checkpoint_owner": "arch4.engine_persistence",
            "checkpoint_interval_s": float(self._checkpoint_interval_s),
            "checkpoint_max_dirty_events": int(self._checkpoint_max_dirty_events),
            "dirty_delta_count": int(self._dirty_delta_count),
            "last_delta_reason": str(self._last_delta_reason or ""),
            "checkpoint_due": bool(
                self._dirty_delta_count > 0
                and (
                    self._dirty_delta_count >= self._checkpoint_max_dirty_events
                    or (
                        self._last_checkpoint_ts > 0.0
                        and elapsed_s >= self._checkpoint_interval_s
                    )
                )
            ),
            "checkpoint_count": int(self._checkpoint_count),
            "last_checkpoint_ts": float(self._last_checkpoint_ts or 0.0),
            "last_checkpoint_reason": str(self._last_checkpoint_reason or ""),
            "checkpoint_reason_counts": dict(self._checkpoint_reason_counts or {}),
        }

    def _checkpoint_due_reason(self, *, now: Optional[float] = None, force: bool = False) -> str:
        if force:
            return "force"
        if self._dirty_delta_count <= 0:
            return ""
        if self._dirty_delta_count >= self._checkpoint_max_dirty_events:
            return "dirty_delta_threshold"
        ts = float(now if now is not None else time.time())
        if self._last_checkpoint_ts > 0.0 and (ts - self._last_checkpoint_ts) >= self._checkpoint_interval_s:
            return "interval_elapsed"
        return ""

    def _mark_delta_dirty(self, *, reason: str, now: Optional[float] = None) -> None:
        self._dirty_delta_count += 1
        self._last_delta_reason = str(reason or "delta")
        if not isinstance(getattr(self, "_state", None), dict):
            return
        self._state["calibration_persistence"] = self._persistence_status(now=now)

    def _maybe_checkpoint_dirty(self, *, reason: str, now: Optional[float] = None) -> bool:
        due_reason = self._checkpoint_due_reason(now=now)
        if not due_reason:
            return False
        self._checkpoint_requested = True
        self._last_checkpoint_reason = f"requested:{reason}:{due_reason}"
        return True

    def _compact_sample_journal_if_needed(self) -> None:
        try:
            count = int(self.store.cm_samples_journal.count())
        except Exception as exc:
            log.debug("calibration sample journal count suppressed: %s", exc)
            return
        if count < AUTHORITY_SAMPLE_JOURNAL_COMPACT_AT:
            return
        try:
            stats = self.store.compact_samples(
                keep_last=AUTHORITY_SAMPLE_JOURNAL_KEEP_LAST,
                gap_threshold=0.4,
            )
        except Exception as exc:
            log.debug("calibration sample journal compaction suppressed: %s", exc)
            return
        try:
            self._state["last_cm_compaction"] = dict(stats)
        except Exception:
            pass

    def _authority_meta(self, target: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "active_profile_id": str(target.get("active_profile_id", "") or ""),
            "active_profile_version": int(target.get("active_profile_version", 0) or 0),
            "new_calibration_available": bool(target.get("new_calibration_available", False)),
            "candidate_profile_id": str(target.get("candidate_profile_id", "") or ""),
            "collection_enabled": bool(target.get("collection_enabled", True)),
            "profile_counter": int(target.get("profile_counter", 0) or 0),
            "runtime_context": sanitize_runtime_context(
                target.get("runtime_context") if isinstance(target.get("runtime_context"), dict) else {}
            ),
            "external_knowledge": sanitize_external_knowledge(
                target.get("external_knowledge") if isinstance(target.get("external_knowledge"), dict) else {}
            ),
            "provider_time_behavior_publication": sanitize_provider_time_behavior_publication(
                target.get("provider_time_behavior_publication")
                if isinstance(target.get("provider_time_behavior_publication"), dict)
                else {}
            ),
            "context_baseline_count": int(len(self._context_baselines(target))),
            "recent_session_history": list(target.get("recent_session_history", []) or []),
            "last_updated_ts": float(target.get("last_updated_ts", 0.0) or 0.0),
            "persistence": self._persistence_status(now=float(target.get("last_updated_ts", 0.0) or 0.0)),
        }

    def _save_state(self, st: Optional[Dict[str, Any]] = None, *, reason: str = "state_change") -> None:
        target = st if st is not None else self._state
        now = float(time.time())
        target["last_updated_ts"] = now
        self._state_mutation_epoch += 1
        self._dirty_delta_count += 1
        self._last_delta_reason = str(reason or "state_change")
        target["calibration_persistence"] = self._persistence_status(now=now)

    def save_state(self, *, reason: str = "manual_checkpoint") -> None:
        del reason
        raise RuntimeError("direct_calibration_checkpoint_writer_deleted")

    def checkpoint_due_reason(self, *, force: bool = False) -> str:
        return self._checkpoint_due_reason(force=force)

    def note_intelligence_checkpoint_committed(
        self,
        *,
        generation: str,
        reason: str,
    ) -> None:
        now = float(time.time())
        self._checkpoint_count += 1
        self._last_checkpoint_ts = now
        self._last_checkpoint_reason = str(reason or "intelligence_checkpoint")
        self._checkpoint_reason_counts[self._last_checkpoint_reason] = int(
            self._checkpoint_reason_counts.get(self._last_checkpoint_reason, 0) or 0
        ) + 1
        self._dirty_delta_count = 0
        self._last_delta_reason = ""
        self._checkpoint_requested = False
        self._last_intelligence_checkpoint_generation = str(generation or "")

    def checkpoint_payload(self) -> Dict[str, Any]:
        """Return current state for the Intelligence persistence owner."""

        payload = copy.deepcopy(self._state)
        payload["calibration_persistence"] = self._persistence_status()
        payload["calibration_state_epoch"] = int(self._state_mutation_epoch)
        return payload

    def _append_recent_session_history(self, entry: Dict[str, Any]) -> None:
        history = self._state.setdefault("recent_session_history", [])
        if not isinstance(history, list):
            history = []
            self._state["recent_session_history"] = history
        normalized = {
            "ts": float(entry.get("ts", time.time()) or time.time()),
            "kind": str(entry.get("kind", "") or ""),
            "candidate_id": str(entry.get("candidate_id", "") or ""),
            "session_id": str(entry.get("session_id", "") or ""),
            "workflow_mode": str(entry.get("workflow_mode", "") or ""),
            "reference_profile_id": str(entry.get("reference_profile_id", "") or ""),
            "reference_profile_version": int(entry.get("reference_profile_version", 0) or 0),
            "decision": str(entry.get("decision", "") or ""),
            "verdict": str(entry.get("verdict", "") or ""),
            "recommendation": str(entry.get("recommendation", "") or ""),
            "headline": str(entry.get("headline", "") or ""),
            "summary": str(entry.get("summary", "") or ""),
            "next_step": str(entry.get("next_step", "") or ""),
            "review_badge": str(entry.get("review_badge", "") or ""),
            "review_tone": str(entry.get("review_tone", "") or ""),
            "approval_ready": bool(entry.get("approval_ready", False)),
            "evidence_grade": str(entry.get("evidence_grade", "") or ""),
            "blocking_gates": list(entry.get("blocking_gates", []) or []),
            "sample_count": int(entry.get("sample_count", 0) or 0),
        }
        history.append(normalized)
        if len(history) > RECENT_SESSION_HISTORY_LIMIT:
            self._state["recent_session_history"] = history[-RECENT_SESSION_HISTORY_LIMIT:]

    def _push_event(self, kind: str, **data: Any) -> None:
        evt = {"kind": kind, "ts": float(time.time())}
        evt.update(data)
        self._runtime_events.append(evt)

    def drain_runtime_events(self) -> List[Dict[str, Any]]:
        out = list(self._runtime_events)
        self._runtime_events.clear()
        return out

    def get_active_profile(self) -> Dict[str, Any]:
        return active_profile_from_state(self._state)

    @staticmethod
    def _latest_history_entry(entries: Any, *, kind: str = "") -> Dict[str, Any]:
        return latest_history_entry(entries, kind=kind)

    def get_active_metadata(self) -> Dict[str, Any]:
        return build_active_metadata(self._state)

    def bind_config_generation(
        self,
        config: Any,
        *,
        generation_id: str,
        config_fingerprint: str,
    ) -> None:
        """Bind a read-only Core projection; never persist Core truth here."""

        if str(getattr(config, "generation_id", "") or "") != str(
            generation_id or ""
        ):
            raise RuntimeError("calibration_core_config_generation_mismatch")
        if str(getattr(config, "fingerprint", "") or "") != str(
            config_fingerprint or ""
        ):
            raise RuntimeError("calibration_core_config_fingerprint_mismatch")
        metadata = getattr(config, "metadata", None)
        to_mapping = getattr(metadata, "to_mapping", None)
        if not callable(to_mapping):
            raise TypeError("calibration_core_config_metadata_required")
        self.cfg = config
        self._core_config_projection = copy.deepcopy(dict(to_mapping() or {}))
        self.refresh_thresholds()

    def _record_collection_attempt(
        self,
        *,
        accepted: bool,
        reason: str,
        candidate_id: str = "",
        ts: Optional[float] = None,
        save: bool = False,
        **details: Any,
    ) -> None:
        payload = build_collection_attempt_record(
            accepted=bool(accepted),
            reason=str(reason or ""),
            candidate_id=str(candidate_id or ""),
            ts=ts,
            **details,
        )
        self._state["last_collection_attempt"] = payload
        if save:
            self._save_state()

    def _candidate_scope_matches_current(
        self,
        candidate_id: str,
        *,
        runtime_context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        profiles = self._state.get("profiles", {}) if isinstance(self._state.get("profiles"), dict) else {}
        prof = profiles.get(candidate_id)
        if not isinstance(prof, dict):
            return True
        current_context = sanitize_runtime_context(
            runtime_context if isinstance(runtime_context, dict) else self.get_runtime_context()
        )
        current_scope = build_runtime_session_scope(current_context)
        if not current_scope:
            return True
        candidate_context = sanitize_runtime_context(
            prof.get("runtime_context") if isinstance(prof.get("runtime_context"), dict) else {}
        )
        candidate_scope = build_runtime_session_scope(candidate_context)
        if not candidate_scope:
            return True
        return runtime_session_scope_matches(candidate_context, current_context)

    def _detach_candidate(
        self,
        candidate_id: str,
        *,
        reason: str,
        save: bool = True,
    ) -> None:
        profiles = self._state.get("profiles", {}) if isinstance(self._state.get("profiles"), dict) else {}
        prof = profiles.get(candidate_id)
        workflow_mode = ""
        lineage: Dict[str, Any] = {}
        report: Dict[str, Any] = {}
        raw_count = 0
        if isinstance(prof, dict):
            result = detach_candidate_profile(prof, reason=str(reason))
            workflow_mode = str(result.get("workflow_mode", "") or "")
            lineage = dict(result.get("lineage") or {})
            report = dict(result.get("report") or {})
            raw_count = int(result.get("raw_count", 0) or 0)
        if str(self._state.get("candidate_profile_id", "") or "") == str(candidate_id):
            self._state["candidate_profile_id"] = ""
        self.store.append_sample(
            build_candidate_decision_sample(
                candidate_id=candidate_id,
                decision="detached",
                reason=str(reason),
                workflow_mode=workflow_mode,
                lineage=lineage,
                report=report,
            )
        )
        self._append_recent_session_history(
            build_recent_decision_history_entry(
                candidate_id=candidate_id,
                decision="detached",
                workflow_mode=workflow_mode,
                lineage=lineage,
                report=report,
            )
        )
        if save:
            self._save_state()
        self._push_event(
            "calibration_candidate_detached",
            candidate_id=str(candidate_id),
            reason=str(reason),
            workflow_mode=workflow_mode,
            sample_count=raw_count,
        )

    def _enforce_candidate_scope(
        self,
        *,
        runtime_context: Optional[Dict[str, Any]] = None,
        reason: str = "runtime_scope_mismatch",
        save: bool = True,
    ) -> bool:
        candidate_id = str(self._state.get("candidate_profile_id", "") or "")
        if not candidate_id:
            return False
        if self._candidate_scope_matches_current(candidate_id, runtime_context=runtime_context):
            return False
        self._detach_candidate(candidate_id, reason=reason, save=save)
        return True

    def _enforce_candidate_time_limit(self, *, now: Optional[float] = None) -> bool:
        """Close stale setup candidates at the calibration authority boundary."""
        candidate_id = str(self._state.get("candidate_profile_id", "") or "")
        if not candidate_id:
            return False
        profiles = self._state.get("profiles", {}) if isinstance(self._state.get("profiles"), dict) else {}
        prof = profiles.get(candidate_id)
        if not isinstance(prof, dict):
            self._state["candidate_profile_id"] = ""
            self._save_state()
            return True
        if str(prof.get("state", "") or "") != "collecting":
            return False

        raw = prof.get("raw_samples") if isinstance(prof.get("raw_samples"), list) else []
        session = prof.get("collection_session") if isinstance(prof.get("collection_session"), dict) else {}
        report = self._summarize_collection_session(
            session,
            sample_count=len(raw),
            now=float(now if now is not None else time.time()),
        )
        if not bool(report.get("timed_out", False)):
            return False

        workflow_mode = str(prof.get("workflow_mode", "") or report.get("workflow_mode", "") or "")
        active_profile_id = str(self._state.get("active_profile_id", "") or "")
        active_profile_version = int(self._state.get("active_profile_version", 0) or 0)
        has_safe_baseline = bool(active_profile_id and active_profile_id != "C0" and active_profile_version > 1)
        decision = "degraded_to_background" if has_safe_baseline else "failed_with_reason"
        headline = (
            "Setup check moved to background"
            if has_safe_baseline
            else "Setup check timed out"
        )
        summary = (
            "Setup check timed out. 54GUC kept the last safe setup."
            if has_safe_baseline
            else "Setup check timed out. Hold position, then start setup again."
        )
        analysis = time_out_candidate_profile(
            prof,
            session_report=report,
            decision=decision,
            recommendation="background" if has_safe_baseline else "retry",
            headline=headline,
            summary=summary,
            next_step="background_watch" if has_safe_baseline else "retry_first_setup",
        )
        self._state["candidate_profile_id"] = ""
        self._append_recent_session_history(
            {
                "kind": "decision",
                "candidate_id": candidate_id,
                "session_id": str(report.get("session_id", "") or ""),
                "workflow_mode": workflow_mode,
                "decision": decision,
                "verdict": "timed_out",
                "recommendation": "background" if has_safe_baseline else "retry",
                "headline": headline,
                "summary": summary,
                "next_step": "background_watch" if has_safe_baseline else "retry_first_setup",
                "review_badge": "TIMED OUT",
                "review_tone": "warning",
                "approval_ready": False,
                "evidence_grade": "timeout",
                "blocking_gates": ["first_setup_timeout"],
                "sample_count": int(len(raw)),
            }
        )
        self._save_state()
        self._push_event(
            "calibration_collection_timed_out",
            candidate_id=candidate_id,
            workflow_mode=workflow_mode,
            decision=decision,
            sample_count=int(len(raw)),
            duration_s=float(report.get("duration_s", 0.0) or 0.0),
            timeout_s=float(report.get("timeout_s", 0.0) or 0.0),
        )
        return True

    def get_runtime_status(self) -> Dict[str, Any]:
        self._enforce_candidate_scope(save=True)
        self._enforce_candidate_time_limit()
        candidate_summary: Dict[str, Any] = {}
        candidate_id = str(self._state.get("candidate_profile_id", "") or "")
        profiles = self._state.get("profiles", {}) if isinstance(self._state.get("profiles"), dict) else {}
        candidate = profiles.get(candidate_id, {}) if candidate_id else {}
        raw = candidate.get("raw_samples") if isinstance(candidate.get("raw_samples"), list) else []
        if raw:
            if isinstance(candidate, dict) and str(candidate.get("workflow_mode", "") or "") == PHASE_BACKGROUND:
                analysis_raw, _analysis_window = self._analysis_samples_for_profile(candidate, list(raw))
                candidate_summary = summarize_samples(analysis_raw)
            else:
                candidate_summary = summarize_samples([dict(v) for v in raw if isinstance(v, dict)])
        runtime_context = self.get_runtime_context()
        reference_bundle = self._reference_bundle(
            candidate_summary if candidate_id else None,
            runtime_context=runtime_context,
        )
        status = build_runtime_status(
            active_metadata=self.get_active_metadata(),
            active_profile=self.get_active_profile(),
            state=self._state,
            reference_summary=reference_bundle.get("summary", {}),
            reference_meta=reference_bundle,
            session_summarizer=self._summarize_collection_session,
            runtime_context=runtime_context,
            external_knowledge=self.get_external_knowledge(),
            active_config_overrides=copy.deepcopy(
                dict(
                    self._core_config_projection.get(
                        "active_config_overrides", {}
                    )
                    or {}
                )
            ),
            active_profile_tuning_proposals=copy.deepcopy(
                list(
                    self._core_config_projection.get(
                        "profile_tuning_proposals", []
                    )
                    or []
                )
            ),
        )
        return status

    def update_runtime_context(self, context: Dict[str, Any]) -> bool:
        sanitized = sanitize_runtime_context(context if isinstance(context, dict) else {})
        current = sanitize_runtime_context(
            self._state.get("runtime_context") if isinstance(self._state.get("runtime_context"), dict) else {}
        )
        if sanitized == current:
            return False
        self._state["runtime_context"] = sanitized
        detached = self._enforce_candidate_scope(
            runtime_context=sanitized,
            reason="runtime_context_changed",
            save=False,
        )
        self._save_state()
        self._push_event("calibration_runtime_context_updated", backend=sanitized.get("backend", ""))
        if detached:
            self._push_event(
                "calibration_runtime_context_detached_candidate",
                backend=sanitized.get("backend", ""),
            )
        return True

    def get_runtime_context(self) -> Dict[str, Any]:
        return sanitize_runtime_context(
            self._state.get("runtime_context") if isinstance(self._state.get("runtime_context"), dict) else {}
        )

    def update_external_knowledge(self, knowledge: Dict[str, Any]) -> bool:
        sanitized = sanitize_external_knowledge(knowledge if isinstance(knowledge, dict) else {})
        current = sanitize_external_knowledge(
            self._state.get("external_knowledge") if isinstance(self._state.get("external_knowledge"), dict) else {}
        )
        if {
            key: value for key, value in sanitized.items() if key != "updated_ts"
        } == {key: value for key, value in current.items() if key != "updated_ts"}:
            return False
        self._state["external_knowledge"] = sanitized
        self._save_state()
        self._push_event(
            "calibration_external_knowledge_updated",
            source_count=int(build_external_knowledge_advisory(
                runtime_context=self.get_runtime_context(),
                external_knowledge=sanitized,
            ).get("source_count", 0) or 0),
        )
        return True

    def merge_external_knowledge_advisory(self, knowledge: Dict[str, Any]) -> bool:
        """Admit a low-confidence source without replacing stronger knowledge."""

        merged = merge_external_knowledge_advisories(
            self.get_external_knowledge(),
            knowledge if isinstance(knowledge, dict) else {},
        )
        return self.update_external_knowledge(merged)

    def get_external_knowledge(self) -> Dict[str, Any]:
        return sanitize_external_knowledge(
            self._state.get("external_knowledge") if isinstance(self._state.get("external_knowledge"), dict) else {}
        )

    def update_provider_time_behavior_publication(self, publication: Dict[str, Any]) -> bool:
        sanitized = sanitize_provider_time_behavior_publication(
            publication if isinstance(publication, dict) else {}
        )
        current = sanitize_provider_time_behavior_publication(
            self._state.get("provider_time_behavior_publication")
            if isinstance(self._state.get("provider_time_behavior_publication"), dict)
            else {}
        )
        if sanitized == current:
            return False
        self._state["provider_time_behavior_publication"] = sanitized
        self._save_state()
        self._push_event(
            "calibration_provider_time_behavior_updated",
            published=bool(sanitized.get("published", False)),
            current_source=str(sanitized.get("current_source", "") or ""),
            provider_name=str(sanitized.get("provider_name", "") or ""),
            provider_plmn=str(sanitized.get("provider_plmn", "") or ""),
        )
        return True

    def get_provider_time_behavior_publication(self) -> Dict[str, Any]:
        return sanitize_provider_time_behavior_publication(
            self._state.get("provider_time_behavior_publication")
            if isinstance(self._state.get("provider_time_behavior_publication"), dict)
            else {}
        )

    def import_external_knowledge_file(self, path: Path | str, *, source: str = "file_import") -> Dict[str, Any]:
        loaded = load_external_knowledge_file(path)
        if not bool(loaded.get("ok", False)):
            return loaded
        sanitized = sanitize_external_knowledge(
            loaded.get("knowledge") if isinstance(loaded.get("knowledge"), dict) else {}
        )
        changed = self.update_external_knowledge(sanitized)
        self.store.append_sample(
            build_external_knowledge_import_journal_entry(
                path=str(loaded.get("path", path) or path),
                source=str(source or "file_import"),
                changed=bool(changed),
                size_bytes=int(loaded.get("size_bytes", 0) or 0),
                provider_hint_present=bool(loaded.get("provider_hint_present", False)),
                device_hint_present=bool(loaded.get("device_hint_present", False)),
                site_hint_count=int(loaded.get("site_hint_count", 0) or 0),
                knowledge=sanitized,
            )
        )
        advisory = build_external_knowledge_advisory(
            runtime_context=self.get_runtime_context(),
            external_knowledge=sanitized,
        )
        self._push_event(
            "calibration_external_knowledge_imported",
            path=str(loaded.get("path", path) or path),
            changed=bool(changed),
            source_count=int(advisory.get("source_count", 0) or 0),
        )
        return {
            "ok": True,
            "path": str(loaded.get("path", path) or path),
            "changed": bool(changed),
            "size_bytes": int(loaded.get("size_bytes", 0) or 0),
            "provider_hint_present": bool(loaded.get("provider_hint_present", False)),
            "device_hint_present": bool(loaded.get("device_hint_present", False)),
            "site_hint_count": int(loaded.get("site_hint_count", 0) or 0),
            "source_count": int(advisory.get("source_count", 0) or 0),
            "advisory_summary": str(advisory.get("summary", "") or ""),
        }

    def get_workflow_status(self, calibration_status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return assess_calibration_workflow(
            state=self._state,
            calibration_status=calibration_status if isinstance(calibration_status, dict) else None,
        )

    def get_export_snapshot(
        self,
        *,
        calibration_status: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Return the canonical runtime calibration bundle.

        This is the single calibration export snapshot that runtime-status
        assembly should prefer instead of rebuilding authority/workflow/state/
        surface sections in multiple places.
        """
        authority_status = self.get_runtime_status()
        workflow_status = self.get_workflow_status(calibration_status)
        calibration_state = build_calibration_state(
            calibration=calibration_status,
            calibration_authority=authority_status,
            calibration_workflow=workflow_status,
        )
        handoff = build_learning_calibration_handoff(
            calibration_state=calibration_state,
        )
        calibration_surface = build_calibration_surface(
            calibration=calibration_status,
            calibration_authority=authority_status,
            calibration_workflow=workflow_status,
            calibration_state=calibration_state,
            learning_calibration_handoff=handoff,
        )
        return {
            "calibration_authority": authority_status,
            "calibration_workflow": workflow_status,
            "calibration_state": calibration_state,
            "learning_calibration_handoff": handoff,
            "calibration_surface": calibration_surface,
        }

    def has_new_calibration(self, last_seen_version: int) -> bool:
        cur = int(self._state.get("active_profile_version", 0) or 0)
        return cur > int(last_seen_version or 0)

    def acknowledge_active_profile(self, version: int) -> None:
        cur = int(self._state.get("active_profile_version", 0) or 0)
        if int(version or 0) >= cur and bool(self._state.get("new_calibration_available", False)):
            self._state["new_calibration_available"] = False
            self._save_state()
            self._push_event("calibration_profile_ack", version=cur)

    def control_start(self) -> None:
        self._background_signal_hits = 0
        self._state["collection_enabled"] = True
        self._save_state()
        self._push_event("calibration_runtime_control", command="start", enabled=True)

    def control_stop(self) -> None:
        self._background_signal_hits = 0
        self._state["collection_enabled"] = False
        self._save_state()
        self._push_event("calibration_runtime_control", command="stop", enabled=False)

    def control_restart(self) -> None:
        self._background_signal_hits = 0
        self._state["collection_enabled"] = True
        cid = str(self._state.get("candidate_profile_id", "") or "")
        if cid:
            profiles = self._state.get("profiles", {})
            if isinstance(profiles, dict):
                cancel_candidate_profile(profiles, candidate_id=cid)
        self._state["candidate_profile_id"] = ""
        self._save_state()
        self._push_event("calibration_runtime_control", command="restart", enabled=True)

    def start_collection(self, reason: str = "manual", workflow_mode: str = "") -> str:
        self._background_signal_hits = 0
        now = time.time()
        ctr = int(self._state.get("profile_counter", 0) or 0) + 1
        self._state["profile_counter"] = ctr
        candidate_id = f"C{ctr}"
        base_values = self.get_active_profile().get("values", self._default_values())
        mode = normalize_workflow_mode(
            workflow_mode,
            active_profile_id=str(self._state.get("active_profile_id", "") or ""),
            active_profile_version=int(self._state.get("active_profile_version", 0) or 0),
        )
        prof = build_calibration_profile(
            profile_id=candidate_id,
            version=int(self._state.get("active_profile_version", 1) or 1),
            state="collecting",
            values=base_values,
            now=now,
        )
        prof["workflow_mode"] = str(mode)
        prof["collection_session"] = build_collection_session(
            cfg=self.cfg,
            workflow_mode=str(mode),
            reason=str(reason),
            now=now,
            active_profile_id=str(self._state.get("active_profile_id", "") or ""),
            active_profile_version=int(self._state.get("active_profile_version", 0) or 0),
        )
        prof["runtime_context"] = self.get_runtime_context()
        prof["lineage"] = build_candidate_lineage(
            reason=str(reason),
            workflow_mode=str(mode),
            opened_ts=now,
            active_profile_id=str(self._state.get("active_profile_id", "") or ""),
            active_profile_version=int(self._state.get("active_profile_version", 0) or 0),
        )
        prof["analysis_report"] = {"reason": str(reason), "workflow_mode": str(mode)}
        prof["proposal_base_values"] = copy.deepcopy(base_values)
        prof["observation_scope"] = {}
        self._state.setdefault("profiles", {})[candidate_id] = prof
        self._state["candidate_profile_id"] = candidate_id
        self._save_state()
        self._push_event(
            "calibration_collection_started",
            candidate_id=candidate_id,
            reason=reason,
            workflow_mode=str(mode),
        )
        return candidate_id

    def _maybe_auto_refresh_candidate(
        self,
        candidate_id: str,
        *,
        now_ts: float,
    ) -> bool:
        profiles = self._state.get("profiles", {})
        prof = profiles.get(candidate_id)
        if not isinstance(prof, dict):
            return False
        workflow_mode = str(prof.get("workflow_mode", "") or "")
        if workflow_mode not in {PHASE_BOOTSTRAP, PHASE_COMPARATIVE, PHASE_BACKGROUND}:
            return False
        state = str(prof.get("state", "") or "")
        if state != "collecting" and not (workflow_mode == PHASE_BACKGROUND and state == "analyzed"):
            return False

        session = prof.get("collection_session", {}) if isinstance(prof.get("collection_session"), dict) else {}
        sample_count = len(list(prof.get("raw_samples", []) or []))
        session_report = self._summarize_collection_session(session, sample_count=sample_count, now=now_ts)

        active_profile_id = str(self._state.get("active_profile_id", "") or "")
        active_profile_version = int(self._state.get("active_profile_version", 0) or 0)
        bootstrap_reference_only = active_profile_id in {"", "C0"} or active_profile_version <= 1

        if workflow_mode == PHASE_BOOTSTRAP:
            if not (bootstrap_reference_only and bool(session_report.get("ready", False))):
                return False
            approver = "bootstrap_auto"
        elif workflow_mode == PHASE_COMPARATIVE:
            if not bool(session_report.get("ready", False)):
                return False
            approver = "comparative_auto"
        else:
            target_samples = int(session.get("target_samples", 0) or 0)
            started_ts = float(session.get("started_ts", 0.0) or 0.0)
            target_duration_s = float(session.get("target_duration_s", 0.0) or 0.0)
            elapsed = now_ts - started_ts
            runtime_review = prof.setdefault("runtime_review", {})
            if not isinstance(runtime_review, dict):
                runtime_review = {}
                prof["runtime_review"] = runtime_review
            last_analysis_ts = float(runtime_review.get("last_analysis_ts", 0.0) or 0.0)
            last_analysis_samples = int(runtime_review.get("last_analysis_samples", 0) or 0)
            if (
                last_analysis_ts > 0.0
                and now_ts - last_analysis_ts < BACKGROUND_ANALYSIS_MIN_INTERVAL_S
                and sample_count > last_analysis_samples
            ):
                return False
            if state == "analyzed":
                analysis = prof.get("analysis_report") if isinstance(prof.get("analysis_report"), dict) else {}
                last_source_count = int(
                    analysis.get("source_raw_sample_count", analysis.get("sample_count", 0)) or 0
                )
                if sample_count - last_source_count < max(1, target_samples):
                    return False
            if not (
                target_samples > 0
                and sample_count >= target_samples
                and target_duration_s > 0.0
                and elapsed >= target_duration_s
            ):
                return False
            approver = "background_auto"

        try:
            report = self.run_analysis(candidate_id)
            if workflow_mode == PHASE_BACKGROUND:
                runtime_review = prof.setdefault("runtime_review", {})
                if isinstance(runtime_review, dict):
                    runtime_review["last_analysis_ts"] = float(now_ts)
                    runtime_review["last_analysis_samples"] = int(sample_count)
            if bool(report.get("approval_ready", False)):
                self.approve_candidate(candidate_id, approver=approver)
                self._push_event(
                    "calibration_baseline_auto_refreshed",
                    candidate_id=candidate_id,
                    workflow_mode=workflow_mode,
                    approver=approver,
                    verdict=str(report.get("verdict", "") or ""),
                )
                return True
            if (
                workflow_mode == PHASE_BACKGROUND
                and bool(report.get("coverage_ok", False))
                and str(report.get("recommendation", "") or "").strip().lower() == "reject"
            ):
                verdict = str(report.get("verdict", "") or "")
                self.reject_candidate(candidate_id, "auto_reject_worse_than_reference")
                self._push_event(
                    "calibration_candidate_auto_rejected",
                    candidate_id=candidate_id,
                    workflow_mode=workflow_mode,
                    verdict=verdict,
                    reason="auto_reject_worse_than_reference",
                )
                return True
            if workflow_mode == PHASE_BACKGROUND:
                if bool(report.get("context_baseline_seeded", False)):
                    return False
                verdict = str(report.get("verdict", "") or "")
                monitoring = build_background_monitoring_recommendation(report)
                event = build_background_monitoring_event(
                    candidate_id=candidate_id,
                    profile=prof,
                    verdict=verdict,
                    monitoring=monitoring,
                    now_ts=now_ts,
                )
                if event:
                    kind = str(event.pop("kind", "calibration_monitoring_recommendation") or "")
                    self._push_event(kind, **event)
        except Exception as exc:
            log.debug("auto_refresh_candidate %s: suppressed %s", candidate_id, exc)
        return False

    def _archive_calibration_observation(self, observation: Dict[str, Any]) -> bool:
        return insert_calibration_observation(
            self._calibration_db_path,
            self._calibration_db_lock,
            enabled=bool(getattr(self.cfg, "calibration_db_enabled", True)),
            retention_days=int(getattr(self.cfg, "calibration_db_retention_days", 120) or 120),
            observation=dict(observation or {}),
        )

    @staticmethod
    def _observation_from_sample(sample: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(sample or {})
        return {
            "observation_id": str(payload.get("observation_id", "") or ""),
            "sample": payload,
            "calibration_status": dict(payload.get("calibration_status") or {})
            if isinstance(payload.get("calibration_status"), dict)
            else {},
            "evidence_scope": dict(payload.get("evidence_scope") or {})
            if isinstance(payload.get("evidence_scope"), dict)
            else {},
        }

    def _remember_observation_receipt(self, observation_id: str, receipt: Dict[str, Any]) -> None:
        receipts = self._state.setdefault("recent_observation_receipts", {})
        if not isinstance(receipts, dict):
            receipts = {}
            self._state["recent_observation_receipts"] = receipts
        receipts[str(observation_id)] = copy.deepcopy(dict(receipt or {}))
        while len(receipts) > 2048:
            receipts.pop(next(iter(receipts)))

    def consume_calibration_observation(self, observation: Dict[str, Any]) -> Dict[str, Any]:
        """The only production admission path into calibration evidence."""

        error = calibration_observation_error(observation)
        if error:
            return {"accepted": False, "reason": error, "background_noted": False}
        observation_id = str(observation.get("observation_id", "") or "")
        receipts = self._state.get("recent_observation_receipts")
        if isinstance(receipts, dict) and observation_id in receipts:
            prior = dict(receipts.get(observation_id) or {})
            prior["original_reason"] = str(prior.get("reason", "") or "")
            prior["reason"] = "observation_already_consumed"
            prior["idempotent"] = True
            return prior

        scope = dict(observation.get("evidence_scope") or {})
        self._state["current_observation_scope"] = copy.deepcopy(scope)
        candidate_id = str(self._state.get("candidate_profile_id", "") or "")
        profiles = self._state.get("profiles", {}) if isinstance(self._state.get("profiles"), dict) else {}
        prof = profiles.get(candidate_id) if candidate_id else None
        if isinstance(prof, dict):
            candidate_scope = prof.get("observation_scope") if isinstance(prof.get("observation_scope"), dict) else {}
            if candidate_scope and not evidence_scopes_match(candidate_scope, scope):
                self._detach_candidate(candidate_id, reason="calibration_evidence_scope_changed", save=True)
                receipt = {
                    "accepted": False,
                    "reason": "calibration_evidence_scope_changed",
                    "background_noted": False,
                    "candidate_id": candidate_id,
                }
                self._remember_observation_receipt(observation_id, receipt)
                # Candidate detachment was durable before the receipt existed.
                # Persist the rejection too so a crash/restart cannot admit the
                # same cross-scope observation into a newly opened candidate.
                self._save_state(reason="calibration_scope_rejection_receipt")
                return receipt

        sample = observation_sample(observation)
        calibration_status = (
            dict(observation.get("calibration_status") or {})
            if isinstance(observation.get("calibration_status"), dict)
            else {}
        )
        self.note_background_signal(calibration_status)
        candidate_id = str(self._state.get("candidate_profile_id", "") or "")
        profiles = self._state.get("profiles", {}) if isinstance(self._state.get("profiles"), dict) else {}
        prof = profiles.get(candidate_id) if candidate_id else None
        if isinstance(prof, dict):
            candidate_scope = prof.get("observation_scope") if isinstance(prof.get("observation_scope"), dict) else {}
            if not candidate_scope:
                prof["observation_scope"] = copy.deepcopy(scope)

        result = self.collect_runtime_sample(sample, now=float(sample.get("ts", 0.0) or 0.0) or None)
        receipt = dict(result or {})
        receipt["background_noted"] = True
        if bool(receipt.get("accepted", False)):
            self._archive_calibration_observation(dict(observation))
        self._remember_observation_receipt(observation_id, receipt)
        return receipt

    def collect_runtime_sample(self, sample: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
        """Internal candidate-window collector; production callers use the observation contract."""
        ts = self._sample_ts(sample if isinstance(sample, dict) else {}, now=now)
        self._enforce_candidate_scope(save=True)
        if not bool(self._state.get("collection_enabled", True)):
            self._record_collection_attempt(accepted=False, reason="collection_disabled", ts=ts, save=True)
            return {"accepted": False, "reason": "collection_disabled"}
        candidate_id = str(self._state.get("candidate_profile_id", "") or "")
        if not candidate_id:
            self._record_collection_attempt(accepted=False, reason="no_candidate", ts=ts, save=False)
            return {"accepted": False, "reason": "no_candidate"}
        profiles = self._state.get("profiles", {})
        prof = profiles.get(candidate_id)
        if not isinstance(prof, dict):
            self._record_collection_attempt(
                accepted=False,
                reason="candidate_not_found",
                candidate_id=candidate_id,
                ts=ts,
                save=True,
            )
            return {"accepted": False, "reason": "candidate_not_found", "candidate_id": candidate_id}
        if isinstance(sample, dict) and sample.get("sample_fresh") is False:
            self._record_collection_attempt(
                accepted=False,
                reason="sample_not_fresh",
                candidate_id=candidate_id,
                ts=ts,
                save=False,
            )
            return {
                "accepted": False,
                "reason": "sample_not_fresh",
                "candidate_id": candidate_id,
            }
        learning_gate = self._extract_learning_gate(sample)
        if learning_gate and not safe_bool(learning_gate.get("learning_allowed", True), True):
            block_reason = str(learning_gate.get("block_reason", "") or "learning_gate_blocked")
            self._record_collection_attempt(
                accepted=False,
                reason="learning_gate_blocked",
                candidate_id=candidate_id,
                ts=ts,
                learning_block_reason=block_reason,
                sample_stale=safe_bool(learning_gate.get("sample_stale", False), False),
                save=False,
            )
            return {
                "accepted": False,
                "reason": "learning_gate_blocked",
                "candidate_id": candidate_id,
                "learning_block_reason": block_reason,
                "sample_stale": safe_bool(learning_gate.get("sample_stale", False), False),
            }
        raw = prof.get("raw_samples") if isinstance(prof.get("raw_samples"), list) else []
        session = prof.get("collection_session") if isinstance(prof.get("collection_session"), dict) else {}
        session_report = self._summarize_collection_session(session, sample_count=len(raw), now=ts)
        if len(raw) > 0 and float(session_report.get("next_sample_due_in_s", 0.0) or 0.0) > 0.0:
            self._record_collection_attempt(
                accepted=False,
                reason="sample_interval_gate",
                candidate_id=candidate_id,
                ts=ts,
                next_sample_due_in_s=float(session_report.get("next_sample_due_in_s", 0.0) or 0.0),
                save=False,
            )
            return {
                "accepted": False,
                "reason": "sample_interval_gate",
                "candidate_id": candidate_id,
                "next_sample_due_in_s": float(session_report.get("next_sample_due_in_s", 0.0) or 0.0),
            }
        workflow_mode = str(prof.get("workflow_mode", "") or "")
        sample_count_before = len(raw)
        accepted = self.append_raw_sample(candidate_id, dict(sample or {}))
        restarted_for_context = False
        if (
            not accepted
            and workflow_mode == PHASE_BACKGROUND
            and str(prof.get("state", "") or "") == "superseded"
            and not str(self._state.get("candidate_profile_id", "") or "")
        ):
            candidate_id = self.start_collection(
                reason="background_context_changed",
                workflow_mode=PHASE_BACKGROUND,
            )
            profiles = self._state.get("profiles", {})
            prof = profiles.get(candidate_id) if isinstance(profiles, dict) else {}
            raw = prof.get("raw_samples") if isinstance(prof, dict) and isinstance(prof.get("raw_samples"), list) else []
            sample_count_before = len(raw)
            accepted = self.append_raw_sample(candidate_id, dict(sample or {}))
            restarted_for_context = bool(accepted)
        self._record_collection_attempt(
            accepted=bool(accepted),
            reason=(
                "accepted_after_context_window_switch"
                if restarted_for_context
                else ("accepted" if accepted else "rejected")
            ),
            candidate_id=candidate_id,
            ts=ts,
            sample_count_after=sample_count_before + (1 if accepted else 0),
            save=False,
        )
        updated = self.get_runtime_status()
        auto_refreshed = False

        if accepted:
            now_ts = float(now) if now is not None else time.time()
            auto_refreshed = self._maybe_auto_refresh_candidate(candidate_id, now_ts=now_ts)
            if auto_refreshed:
                updated = self.get_runtime_status()

        return {
            "accepted": bool(accepted),
            "candidate_id": candidate_id,
            "reason": (
                "accepted_after_context_window_switch"
                if restarted_for_context
                else ("accepted" if accepted else "rejected")
            ),
            "session_ready": bool(updated.get("candidate_decision_ready", False)),
            "session_progress_pct": int(updated.get("candidate_session_progress_pct", 0) or 0),
            "auto_refreshed": bool(auto_refreshed),
        }

    def note_background_signal(self, calibration_status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._enforce_candidate_scope(save=True)
        workflow = self.get_workflow_status(calibration_status)
        policy = self._background_trigger_policy(calibration_status)
        learning_gate = self._extract_learning_gate(calibration_status)
        if learning_gate and not safe_bool(learning_gate.get("learning_allowed", True), True):
            self._background_signal_hits = 0
            return {
                "opened": False,
                "hits": 0,
                "threshold": int(policy.get("trigger_hits", BACKGROUND_CANDIDATE_TRIGGER_HITS) or BACKGROUND_CANDIDATE_TRIGGER_HITS),
                "policy": policy,
                "reason": "learning_gate_blocked",
                "learning_block_reason": str(learning_gate.get("block_reason", "") or "learning_gate_blocked"),
                "sample_stale": safe_bool(learning_gate.get("sample_stale", False), False),
            }
        if workflow.get("phase") != "background_self_calibration":
            self._background_signal_hits = 0
            return {"opened": False, "hits": 0, "threshold": int(policy.get("trigger_hits", BACKGROUND_CANDIDATE_TRIGGER_HITS) or BACKGROUND_CANDIDATE_TRIGGER_HITS), "policy": policy}
        if not bool(self._state.get("collection_enabled", True)):
            self._background_signal_hits = 0
            return {"opened": False, "hits": 0, "threshold": int(policy.get("trigger_hits", BACKGROUND_CANDIDATE_TRIGGER_HITS) or BACKGROUND_CANDIDATE_TRIGGER_HITS), "policy": policy}
        if str(self._state.get("candidate_profile_id", "") or ""):
            self._background_signal_hits = 0
            return {"opened": False, "hits": 0, "threshold": int(policy.get("trigger_hits", BACKGROUND_CANDIDATE_TRIGGER_HITS) or BACKGROUND_CANDIDATE_TRIGGER_HITS), "policy": policy}
        if not bool(workflow.get("background_trigger_recommended", False)) and not bool(policy.get("force_recommended", False)):
            self._background_signal_hits = 0
            return {"opened": False, "hits": 0, "threshold": int(policy.get("trigger_hits", BACKGROUND_CANDIDATE_TRIGGER_HITS) or BACKGROUND_CANDIDATE_TRIGGER_HITS), "policy": policy}
        if not bool(policy.get("eligible", False)):
            self._background_signal_hits = 0
            return {
                "opened": False,
                "hits": 0,
                "threshold": int(policy.get("trigger_hits", BACKGROUND_CANDIDATE_TRIGGER_HITS) or BACKGROUND_CANDIDATE_TRIGGER_HITS),
                "policy": policy,
            }

        self._background_signal_hits += 1
        trigger_hits = int(policy.get("trigger_hits", BACKGROUND_CANDIDATE_TRIGGER_HITS) or BACKGROUND_CANDIDATE_TRIGGER_HITS)
        if self._background_signal_hits < trigger_hits:
            return {
                "opened": False,
                "hits": int(self._background_signal_hits),
                "threshold": int(trigger_hits),
                "policy": policy,
            }

        candidate_id = self.start_collection(
            reason="background_self_calibration",
            workflow_mode="background_self_calibration",
        )
        self._push_event(
            "calibration_background_candidate_opened",
            candidate_id=candidate_id,
            trigger_hits=int(trigger_hits),
            required_gap_score=float(policy.get("required_gap_score", 0.0) or 0.0),
            gap_score=float(policy.get("gap_score", 0.0) or 0.0),
            family_rule_key=str(policy.get("family_rule_key", "") or ""),
            trigger_reason=str(policy.get("reason", "") or ""),
            current_context_key=str(policy.get("current_context_key", "") or ""),
        )
        return {
            "opened": True,
            "candidate_id": str(candidate_id),
            "hits": 0,
            "threshold": int(trigger_hits),
            "policy": policy,
        }

    def append_raw_sample(self, candidate_id: str, sample: Dict[str, Any]) -> bool:
        profiles = self._state.get("profiles", {})
        prof = profiles.get(candidate_id)
        if not isinstance(prof, dict):
            return False
        if prof.get("state") not in {"collecting", "analyzed"}:
            return False
        now = self._sample_ts(sample if isinstance(sample, dict) else {})
        raw_samples = prof.setdefault("raw_samples", [])
        observation_id = str((sample or {}).get("observation_id", "") or "")
        if observation_id and any(
            isinstance(row, dict)
            and str(row.get("observation_id", "") or "") == observation_id
            for row in raw_samples
        ):
            return True
        session = prof.get("collection_session")
        if not isinstance(session, dict):
            session = build_collection_session(
                cfg=self.cfg,
                workflow_mode=str(prof.get("workflow_mode", "") or ""),
                reason=str((prof.get("analysis_report") or {}).get("reason", "manual") or "manual"),
                now=float(prof.get("created_ts", now) or now),
                active_profile_id=str(self._state.get("active_profile_id", "") or ""),
                active_profile_version=int(self._state.get("active_profile_version", 0) or 0),
            )
            prof["collection_session"] = session
        session_report = self._summarize_collection_session(session, sample_count=len(raw_samples), now=now)
        if len(raw_samples) > 0 and float(session_report.get("next_sample_due_in_s", 0.0) or 0.0) > 0.0:
            return False
        if not self._ensure_background_candidate_context(
            candidate_id=str(candidate_id),
            prof=prof,
            session=session,
            sample=dict(sample or {}),
            now=float(now),
        ):
            self._save_state(reason="background_context_changed")
            return False
        sample_payload = dict(sample or {})
        sample_payload.setdefault("ts", float(now))
        self.store.append_sample(
            build_raw_sample_journal_entry(
                candidate_id=str(candidate_id),
                sample=sample_payload,
                ts=float(now),
            )
        )
        raw_samples.append(sample_payload)
        # Bound growth to keep runtime storage manageable.
        if len(raw_samples) > AUTHORITY_RAW_SAMPLE_KEEP_LAST:
            del raw_samples[: len(raw_samples) - AUTHORITY_RAW_SAMPLE_KEEP_LAST]
        session["last_sample_ts"] = float(now)
        prof["updated_ts"] = float(now)
        self._compact_sample_journal_if_needed()
        self._mark_delta_dirty(reason="cm_raw_sample", now=float(now))
        self._maybe_checkpoint_dirty(reason="cm_raw_sample", now=float(now))
        return True

    def _ensure_background_candidate_context(
        self,
        *,
        candidate_id: str,
        prof: Dict[str, Any],
        session: Dict[str, Any],
        sample: Dict[str, Any],
        now: float,
    ) -> bool:
        if str(prof.get("workflow_mode", "") or "") != PHASE_BACKGROUND:
            return True
        sample_context_key = self._context_key_from_sample(sample)
        if not sample_context_key:
            return True
        current_context_key = str(session.get("context_key", "") or "").strip()
        if not current_context_key:
            band, cell = self._split_context_key(sample_context_key)
            session["context_key"] = sample_context_key
            session["context_band"] = int(band)
            session["context_cell"] = int(cell)
            session["context_started_ts"] = float(now)
            return True
        if sample_context_key == current_context_key:
            return True

        raw_samples = prof.get("raw_samples") if isinstance(prof.get("raw_samples"), list) else []
        previous_window_sample_count = int(len(raw_samples or []))
        band, cell = self._split_context_key(sample_context_key)
        switches = session.setdefault("context_switches", [])
        if isinstance(switches, list):
            switches.append(
                {
                    "ts": float(now),
                    "old_context_key": current_context_key,
                    "new_context_key": sample_context_key,
                    "previous_window_sample_count": previous_window_sample_count,
                }
            )
            if len(switches) > 24:
                del switches[: len(switches) - 24]
        session["previous_context_key"] = current_context_key
        session["context_key"] = sample_context_key
        session["context_band"] = int(band)
        session["context_cell"] = int(cell)
        session["context_started_ts"] = float(now)
        session["started_ts"] = float(now)
        session["last_sample_ts"] = 0.0
        prof["updated_ts"] = float(now)
        analysis = prof.get("analysis_report") if isinstance(prof.get("analysis_report"), dict) else {}
        next_analysis = dict(analysis)
        next_analysis.update(
            {
                "ok": True,
                "reason": "background_context_changed",
                "old_context_key": current_context_key,
                "new_context_key": sample_context_key,
                "generated_ts": float(now),
                "workflow_mode": PHASE_BACKGROUND,
                "candidate_retained": True,
                "previous_window_sample_count": previous_window_sample_count,
            }
        )
        prof["analysis_report"] = next_analysis
        self._append_recent_session_history(
            {
                "kind": "analysis",
                "candidate_id": str(candidate_id),
                "session_id": str(session.get("session_id", "") or ""),
                "workflow_mode": PHASE_BACKGROUND,
                "verdict": "context_window_switched",
                "recommendation": "continue_current_context_window",
                "headline": "Background context window switched",
                "summary": "Cell changed during the background check. 54GUC kept the current result open.",
                "next_step": "collect_same_context_window",
                "review_badge": "WINDOW SWITCH",
                "review_tone": "warning",
                "approval_ready": False,
                "evidence_grade": "context_window_switched",
                "blocking_gates": [],
                "sample_count": int(len(prof.get("raw_samples", []) or [])),
                "old_context_key": current_context_key,
                "new_context_key": sample_context_key,
            }
        )
        self._push_event(
            "calibration_background_context_changed",
            candidate_id=str(candidate_id),
            old_context_key=current_context_key,
            new_context_key=sample_context_key,
            candidate_retained=True,
        )
        return True

    def _analysis_samples_for_profile(
        self,
        prof: Dict[str, Any],
        raw: List[Any],
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        source_samples = [dict(v) for v in raw if isinstance(v, dict)]
        source_count = int(len(source_samples))
        if str(prof.get("workflow_mode", "") or "") != PHASE_BACKGROUND:
            return source_samples, {
                "source_raw_sample_count": source_count,
                "analysis_window_sample_count": source_count,
                "analysis_window_context_key": "",
                "analysis_window_kind": "full_candidate",
            }

        session = prof.get("collection_session") if isinstance(prof.get("collection_session"), dict) else {}
        context_key = str(session.get("context_key", "") or "").strip()
        if not context_key:
            for sample in reversed(source_samples):
                context_key = self._context_key_from_sample(sample)
                if context_key:
                    break
        if not context_key:
            return source_samples, {
                "source_raw_sample_count": source_count,
                "analysis_window_sample_count": source_count,
                "analysis_window_context_key": "",
                "analysis_window_kind": "full_candidate_no_context",
            }

        window: List[Dict[str, Any]] = []
        for sample in reversed(source_samples):
            sample_context_key = self._context_key_from_sample(sample)
            if sample_context_key == context_key:
                window.append(dict(sample))
                if len(window) >= BACKGROUND_ANALYSIS_WINDOW_MAX_SAMPLES:
                    break
                continue
            if window:
                break
        window.reverse()
        return window, {
            "source_raw_sample_count": source_count,
            "analysis_window_sample_count": int(len(window)),
            "analysis_window_context_key": context_key,
            "analysis_window_kind": "latest_contiguous_context",
        }

    def _session_for_analysis_window(
        self,
        session: Dict[str, Any],
        samples: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        out = dict(session or {})
        if not samples:
            return out
        first_ts = safe_float(samples[0].get("ts"))
        last_ts = safe_float(samples[-1].get("ts"))
        if first_ts is not None:
            out["started_ts"] = float(first_ts)
        if last_ts is not None:
            out["last_sample_ts"] = float(last_ts)
        context_key = self._context_key_from_sample(samples[-1])
        if context_key:
            band, cell = self._split_context_key(context_key)
            out["context_key"] = context_key
            out["context_band"] = int(band)
            out["context_cell"] = int(cell)
        return out

    def _reference_summary(self) -> Dict[str, Any]:
        return self._reference_bundle().get("summary", {})

    def run_analysis(self, candidate_id: str) -> Dict[str, Any]:
        profiles = self._state.get("profiles", {})
        prof = profiles.get(candidate_id)
        if not isinstance(prof, dict):
            return {"ok": False, "error": "candidate_not_found"}
        raw = list(prof.get("raw_samples", []) or [])
        workflow_mode = str(prof.get("workflow_mode", "") or "")
        analysis_raw, analysis_window = self._analysis_samples_for_profile(prof, raw)
        lineage = prof.get("lineage") if isinstance(prof.get("lineage"), dict) else {}
        runtime_context_ready = bool(self.get_workflow_status().get("runtime_context_ready", False))
        session = prof.get("collection_session") if isinstance(prof.get("collection_session"), dict) else {}
        session_for_analysis = (
            self._session_for_analysis_window(session, analysis_raw)
            if workflow_mode == PHASE_BACKGROUND
            else dict(session or {})
        )
        session_report = self._summarize_collection_session(
            session_for_analysis,
            sample_count=len(analysis_raw),
        )
        sample_summary = summarize_samples(analysis_raw)
        reference_summary = self._reference_summary() if workflow_mode in {PHASE_COMPARATIVE, PHASE_ANCHOR_SURVEY, PHASE_BACKGROUND} else {}
        required_samples = int(getattr(self.cfg, "calibration_min_samples", 30) or 30)
        coverage_ok = bool(session_report.get("ready", False))
        mode_report: Dict[str, Any]
        approval_ready = False
        recommendation = "hold"
        verdict = "inconclusive"
        metric_coverage = 0
        comparable_metrics = 0
        confidence = 0.0
        comparison_rows: List[Dict[str, Any]] = []

        if workflow_mode == PHASE_BOOTSTRAP:
            metric_coverage = sum(
                1
                for row in (sample_summary.get("metrics", {}) or {}).values()
                if isinstance(row, dict) and int(row.get("count", 0) or 0) > 0
            )
            baseline_quality = score_bootstrap_baseline(
                sample_summary,
                backend=str((self.get_workflow_status() or {}).get("backend", "") or ""),
            )
            bootstrap_primary = assess_bootstrap_primary_metrics(
                sample_summary=sample_summary,
                backend=str((self.get_workflow_status() or {}).get("backend", "") or ""),
            )
            real_observation_ready = bool(
                int(sample_summary.get("sample_count", 0) or 0) > 0
                and metric_coverage >= 1
            )
            approval_ready = bool(
                coverage_ok
                and runtime_context_ready
                and real_observation_ready
            )
            recommendation = "approve" if approval_ready else "collect_more"
            verdict = "baseline_ready" if approval_ready else ("incomplete_window" if not coverage_ok else "incomplete")
            mode_report = {
                "metric_coverage": int(metric_coverage),
                "baseline_quality": baseline_quality,
                "bootstrap_primary_metrics": bootstrap_primary,
                "real_observation_ready": bool(real_observation_ready),
                "approval_ready": bool(approval_ready),
            }
        else:
            backend_name = str((self.get_workflow_status() or {}).get("backend", "") or "")
            reference_bundle = self._reference_bundle(
                sample_summary,
                runtime_context=self.get_runtime_context(),
            )
            context_seed_report = self._maybe_seed_background_context_baseline(
                candidate_id=str(candidate_id),
                profile=prof,
                sample_summary=sample_summary,
                session_report=session_report,
                runtime_context_ready=bool(runtime_context_ready),
                backend=backend_name,
            )
            if bool(context_seed_report.get("seeded", False)):
                reference_bundle = self._reference_bundle(
                    sample_summary,
                    runtime_context=self.get_runtime_context(),
                )
            if isinstance(lineage, dict):
                lineage["reference_profile_id"] = str(reference_bundle.get("profile_id", "") or "")
                lineage["reference_profile_version"] = int(reference_bundle.get("profile_version", 0) or 0)
                lineage["reference_marker"] = str(reference_bundle.get("marker", "") or "")
            comparison = compare_candidate_to_reference(
                candidate_summary=sample_summary,
                reference_summary=reference_bundle.get("summary", {}) if isinstance(reference_bundle, dict) else {},
                workflow_mode=workflow_mode,
                backend=backend_name,
            )
            verdict = str(comparison.get("verdict", "inconclusive") or "inconclusive")
            comparable_metrics = int(comparison.get("comparable_metrics", 0) or 0)
            confidence = float(comparison.get("confidence", 0.0) or 0.0)
            comparison_rows = build_comparison_rows(
                candidate_summary=sample_summary,
                reference_summary=reference_bundle.get("summary", {}) if isinstance(reference_bundle, dict) else {},
                metric_avg=metric_avg,
            )
            candidate_quality = score_bootstrap_baseline(
                sample_summary,
                backend=backend_name,
            )
            candidate_primary = assess_bootstrap_primary_metrics(
                sample_summary=sample_summary,
                backend=backend_name,
            )
            approval_ready = bool(coverage_ok and runtime_context_ready and comparison.get("approval_ready", False))
            recommendation = (
                "approve"
                if approval_ready
                else (
                    "collect_more"
                    if not coverage_ok
                    else (
                        "use_anchor_survey"
                        if workflow_mode == PHASE_ANCHOR_SURVEY and verdict == "better"
                        else ("reject" if verdict == "worse" else ("hold" if verdict in {"equivalent", "mixed"} else "collect_more"))
                    )
                )
            )
            mode_report = {
                "comparison": comparison,
                "reference_summary": reference_bundle.get("summary", {}) if isinstance(reference_bundle, dict) else {},
                "baseline_quality": candidate_quality,
                "bootstrap_primary_metrics": candidate_primary,
                "context_baseline_seed": context_seed_report,
                "context_baseline_seeded": bool(context_seed_report.get("seeded", False)),
                "approval_ready": bool(approval_ready),
            }

        operator_report = build_operator_report(
            workflow_mode=workflow_mode,
            session_report=session_report,
            verdict=str(verdict),
            recommendation=str(recommendation),
            approval_ready=bool(approval_ready),
        )
        review_badge = build_review_badge(
            workflow_mode=workflow_mode,
            decision_ready=bool(session_report.get("ready", False)),
            verdict=str(verdict),
            recommendation=str(recommendation),
            approval_ready=bool(approval_ready),
            analysis_stale=False,
        )
        gate_rows = build_gate_rows(
            workflow_mode=workflow_mode,
            runtime_context_ready=bool(runtime_context_ready),
            coverage_ok=bool(coverage_ok),
            metric_coverage=int(metric_coverage),
            reference_present=bool(reference_summary) if workflow_mode != PHASE_BOOTSTRAP else False,
            comparable_metrics=int(comparable_metrics),
            comparable_metric_min_required=int(
                (mode_report.get("comparison", {}) if isinstance(mode_report.get("comparison", {}), dict) else {}).get(
                    "comparable_metric_min_required", 0
                )
                or 0
            ),
            context_ready=bool(
                (mode_report.get("comparison", {}) if isinstance(mode_report.get("comparison", {}), dict) else {}).get(
                    "context_ready", True
                )
            ),
            context_reason=str(
                (mode_report.get("comparison", {}) if isinstance(mode_report.get("comparison", {}), dict) else {}).get(
                    "context_reason", ""
                )
                or ""
            ),
            confidence=float(
                (mode_report.get("comparison", {}) if isinstance(mode_report.get("comparison", {}), dict) else {}).get(
                    "confidence", 0.0
                )
                or 0.0
            ),
            confidence_threshold=float(
                (mode_report.get("comparison", {}) if isinstance(mode_report.get("comparison", {}), dict) else {}).get(
                    "confidence_threshold", 0.0
                )
                or 0.0
            ),
            stability_penalty=float(
                (mode_report.get("comparison", {}) if isinstance(mode_report.get("comparison", {}), dict) else {}).get(
                    "stability_penalty", 0.0
                )
                or 0.0
            ),
            stability_penalty_max=float(
                (mode_report.get("comparison", {}) if isinstance(mode_report.get("comparison", {}), dict) else {}).get(
                    "stability_penalty_max", 0.0
                )
                or 0.0
            ),
            offpeak_confirmation_required=bool(
                (mode_report.get("comparison", {}) if isinstance(mode_report.get("comparison", {}), dict) else {}).get(
                    "offpeak_confirmation_required", False
                )
            ),
            offpeak_ready=bool(
                (mode_report.get("comparison", {}) if isinstance(mode_report.get("comparison", {}), dict) else {}).get(
                    "offpeak_ready", True
                )
            ),
            verdict=str(verdict),
            approval_ready=bool(approval_ready),
        )
        session_outcome_report = build_session_outcome_report(
            workflow_mode=workflow_mode,
            session_report=session_report,
            sample_summary=sample_summary,
            reference_summary=reference_summary if isinstance(reference_summary, dict) else {},
            lineage=lineage,
            operator_report=operator_report,
            review_badge=review_badge,
            verdict=str(verdict),
            recommendation=str(recommendation),
            approval_ready=bool(approval_ready),
            gates=gate_rows,
            comparison_rows=comparison_rows,
            confidence=float(confidence),
            score_details=mode_report.get("comparison", mode_report.get("baseline_quality", {})),
            mode_details=mode_report,
        )

        report = {
            "ok": True,
            "sample_count": int(len(analysis_raw)),
            "source_raw_sample_count": int(len(raw)),
            "analysis_window_sample_count": int(analysis_window.get("analysis_window_sample_count", len(analysis_raw)) or 0),
            "analysis_window_context_key": str(analysis_window.get("analysis_window_context_key", "") or ""),
            "analysis_window_kind": str(analysis_window.get("analysis_window_kind", "") or ""),
            "required_samples": int(required_samples),
            "coverage_ok": bool(coverage_ok),
            "workflow_mode": workflow_mode,
            "runtime_context_ready": bool(runtime_context_ready),
            "session_report": session_report,
            "sample_summary": sample_summary,
            "verdict": str(verdict),
            "recommendation": str(recommendation),
            "approval_ready": bool(approval_ready),
            "operator_report": operator_report,
            "review_badge": review_badge,
            "session_outcome_report": session_outcome_report,
            "generated_ts": float(time.time()),
        }
        report.update(mode_report)
        proposal = build_calibration_proposal(
            candidate_id=str(candidate_id),
            candidate_version=int(self._state.get("active_profile_version", 1) or 1) + 1,
            base_values=(
                prof.get("proposal_base_values")
                if isinstance(prof.get("proposal_base_values"), dict)
                else prof.get("values", {})
            ),
            raw_samples=analysis_raw,
            sample_summary=sample_summary,
            analysis_report=report,
            evidence_scope=(
                prof.get("observation_scope")
                if isinstance(prof.get("observation_scope"), dict)
                else {}
            ),
        )
        prof["calibration_proposal"] = copy.deepcopy(proposal)
        report["calibration_proposal"] = copy.deepcopy(proposal)
        proposed_values = proposal.get("proposed_values")
        if isinstance(proposed_values, dict) and not calibration_proposal_error(proposal):
            prof["values"] = copy.deepcopy(proposed_values)
        prof["analysis_report"] = report
        mark_candidate_analysis_state(
            prof,
            decision_ready=bool(session_report.get("ready", False)),
        )
        self.store.append_sample(
            build_analysis_report_journal_entry(
                candidate_id=str(candidate_id),
                workflow_mode=workflow_mode,
                report=report,
            )
        )
        self._append_recent_session_history(
            {
                "ts": float(time.time()),
                "kind": "analysis",
                "candidate_id": str(candidate_id),
                "session_id": str(session_outcome_report.get("session_id", "") or ""),
                "workflow_mode": str(workflow_mode or ""),
                "reference_profile_id": str(lineage.get("reference_profile_id", "") or ""),
                "reference_profile_version": int(lineage.get("reference_profile_version", 0) or 0),
                "verdict": str(report.get("verdict", "") or ""),
                "recommendation": str(report.get("recommendation", "") or ""),
                "headline": str(operator_report.get("headline", "") or ""),
                "summary": str(operator_report.get("summary", "") or ""),
                "next_step": str(operator_report.get("next_step", "") or ""),
                "review_badge": str(review_badge.get("label", "") or ""),
                "review_tone": str(review_badge.get("tone", "") or ""),
                "approval_ready": bool(report.get("approval_ready", False)),
                "evidence_grade": str(session_outcome_report.get("evidence_grade", "") or ""),
                "blocking_gates": list(session_outcome_report.get("blocking_gates", []) or []),
                "sample_count": int(len(analysis_raw)),
                "source_raw_sample_count": int(len(raw)),
            }
        )
        self._save_state()
        event = build_calibration_analysis_done_event(
            candidate_id=candidate_id,
            profile=prof,
            workflow_mode=workflow_mode,
            report=report,
            sample_count=len(analysis_raw),
            source_raw_sample_count=len(raw),
        )
        if event:
            kind = str(event.pop("kind", "calibration_analysis_done") or "")
            self._push_event(kind, **event)
        return report

    def approve_candidate(self, candidate_id: str, approver: str = "system") -> int:
        profiles = self._state.get("profiles", {})
        prof = profiles.get(candidate_id)
        if not isinstance(prof, dict):
            raise ValueError("candidate_not_found")
        if prof.get("state") not in {"analyzed", "collecting"}:
            raise ValueError("candidate_not_approvable")
        report = prof.get("analysis_report") if isinstance(prof.get("analysis_report"), dict) else {}
        raw = list(prof.get("raw_samples", []) or [])
        if (
            prof.get("state") == "collecting"
            or not bool(report)
            or int(report.get("source_raw_sample_count", report.get("sample_count", 0)) or 0) != len(raw)
        ):
            report = self.run_analysis(candidate_id)
            prof = profiles.get(candidate_id)
            if not isinstance(prof, dict):
                raise ValueError("candidate_not_found")
        if not bool(report.get("approval_ready", False)):
            raise ValueError("candidate_not_ready_for_approval")
        observation_scope = (
            prof.get("observation_scope")
            if isinstance(prof.get("observation_scope"), dict)
            else {}
        )
        if observation_scope:
            proposal = (
                prof.get("calibration_proposal")
                if isinstance(prof.get("calibration_proposal"), dict)
                else {}
            )
            proposal_error = calibration_proposal_error(proposal)
            if proposal_error:
                raise ValueError(f"candidate_calibration_proposal_invalid:{proposal_error}")
            current_scope = (
                self._state.get("current_observation_scope")
                if isinstance(self._state.get("current_observation_scope"), dict)
                else {}
            )
            if not evidence_scopes_match(observation_scope, current_scope):
                raise ValueError("candidate_calibration_scope_no_longer_current")
        next_version = int(self._state.get("active_profile_version", 1) or 1) + 1
        activate_candidate_profile(
            prof,
            approver=str(approver),
            version=next_version,
            runtime_context=self.get_runtime_context(),
        )
        lineage = prof.get("lineage") if isinstance(prof.get("lineage"), dict) else {}
        self._state["active_profile_id"] = candidate_id
        self._state["active_profile_version"] = next_version
        self._state["new_calibration_available"] = True
        self._state["candidate_profile_id"] = ""
        sample_summary = report.get("sample_summary") if isinstance(report.get("sample_summary"), dict) else {}
        if isinstance(sample_summary, dict):
            self._store_context_baseline(
                summary=sample_summary,
                profile_id=candidate_id,
                profile_version=next_version,
                approved_by=str(approver),
                runtime_context=prof.get("runtime_context") if isinstance(prof.get("runtime_context"), dict) else {},
            )
        self.store.append_sample(
            build_candidate_decision_sample(
                candidate_id=candidate_id,
                decision="approved",
                workflow_mode=str(prof.get("workflow_mode", "") or ""),
                lineage=lineage,
                report=report,
                version=next_version,
                approver=str(approver),
            )
        )
        self._append_recent_session_history(
            build_recent_decision_history_entry(
                candidate_id=candidate_id,
                decision="approved",
                workflow_mode=str(prof.get("workflow_mode", "") or ""),
                lineage=lineage,
                report=report,
            )
        )
        self._save_state()
        self._push_event(
            "calibration_profile_applied",
            candidate_id=candidate_id,
            version=next_version,
            approved_by=str(approver),
            workflow_mode=str(prof.get("workflow_mode", "") or ""),
        )
        if str(approver).endswith("_auto"):
            self._push_event(
                "calibration_candidate_auto_applied",
                candidate_id=candidate_id,
                version=next_version,
                approved_by=str(approver),
                workflow_mode=str(prof.get("workflow_mode", "") or ""),
            )
        self._push_event(
            "calibration_active_switched",
            candidate_id=candidate_id,
            version=next_version,
            approved_by=str(approver),
        )
        return next_version

    def reject_candidate(self, candidate_id: str, reason: str) -> None:
        profiles = self._state.get("profiles", {})
        prof = profiles.get(candidate_id)
        prior: Dict[str, Any] = {}
        session_outcome: Dict[str, Any] = {}
        lineage: Dict[str, Any] = {}
        if isinstance(prof, dict):
            prior = reject_candidate_profile(prof, reason=str(reason))
            lineage = prof.get("lineage") if isinstance(prof.get("lineage"), dict) else {}
            session_outcome = (
                prior.get("session_outcome_report")
                if isinstance(prior.get("session_outcome_report"), dict)
                else {}
            )
        if str(self._state.get("candidate_profile_id", "")) == candidate_id:
            self._state["candidate_profile_id"] = ""
        self.store.append_sample(
            build_candidate_decision_sample(
                candidate_id=candidate_id,
                decision="rejected",
                reason=str(reason),
                workflow_mode=str(prof.get("workflow_mode", "") or "") if isinstance(prof, dict) else "",
                lineage=lineage,
                report=prior,
            )
        )
        self._append_recent_session_history(
            build_recent_decision_history_entry(
                candidate_id=candidate_id,
                decision="rejected",
                workflow_mode=str(prof.get("workflow_mode", "") or "") if isinstance(prof, dict) else "",
                lineage=lineage,
                report=prior,
            )
        )
        self._save_state()
        self._push_event("calibration_candidate_rejected", candidate_id=candidate_id, reason=str(reason))

    def cancel_candidate(self, candidate_id: str, reason: str) -> None:
        profiles = self._state.get("profiles", {})
        workflow_mode = ""
        prior: Dict[str, Any] = {}
        session_outcome: Dict[str, Any] = {}
        lineage: Dict[str, Any] = {}
        result = cancel_candidate_profile(profiles, candidate_id=str(candidate_id))
        if bool(result.get("found", False)):
            workflow_mode = str(result.get("workflow_mode", "") or "")
            lineage = dict(result.get("lineage") or {})
            prior = dict(result.get("report") or {})
            session_outcome = (
                prior.get("session_outcome_report")
                if isinstance(prior.get("session_outcome_report"), dict)
                else {}
            )
        if str(self._state.get("candidate_profile_id", "")) == candidate_id:
            self._state["candidate_profile_id"] = ""
        self.store.append_sample(
            build_candidate_decision_sample(
                candidate_id=candidate_id,
                decision="canceled",
                reason=str(reason),
                workflow_mode=workflow_mode,
                lineage=lineage,
                report=prior,
            )
        )
        self._append_recent_session_history(
            build_recent_decision_history_entry(
                candidate_id=candidate_id,
                decision="canceled",
                workflow_mode=str(workflow_mode or ""),
                lineage=lineage,
                report=prior,
            )
        )
        self._save_state()
        self._push_event("calibration_candidate_canceled", candidate_id=candidate_id, reason=str(reason))
