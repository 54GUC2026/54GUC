"""
arch4/brain/calibration_decision_log.py

Helpers for calibration decision journaling/history payloads.
"""

from __future__ import annotations

import time
import copy
from typing import Any, Dict


def build_profile_effect_report(
    *,
    workflow_mode: str,
    decision: str,
    report: Dict[str, Any],
) -> Dict[str, Any]:
    session_outcome = (
        report.get("session_outcome_report")
        if isinstance(report.get("session_outcome_report"), dict)
        else {}
    )
    bootstrap_primary = (
        report.get("bootstrap_primary_metrics")
        if isinstance(report.get("bootstrap_primary_metrics"), dict)
        else {}
    )
    baseline_quality = (
        report.get("baseline_quality")
        if isinstance(report.get("baseline_quality"), dict)
        else {}
    )
    if str(decision or "") != "approved":
        return {
            "headline": "",
            "summary": "",
            "highlights": [],
            "regressions": [],
        }

    mode = str(workflow_mode or "")
    if mode == "bootstrap_initial":
        available = [str(v) for v in list(bootstrap_primary.get("primary_metrics_available", []) or []) if str(v or "").strip()]
        headline = "First approved baseline published"
        summary_parts = []
        if available:
            summary_parts.append("Core metrics available: " + ", ".join(available[:5]))
        quality_pct = int(baseline_quality.get("quality_pct", 0) or 0)
        if quality_pct > 0:
            summary_parts.append(f"baseline quality {quality_pct}%")
        if not summary_parts:
            summary_parts.append("The first approved reference is now active")
        return {
            "headline": str(headline),
            "summary": ". ".join(summary_parts) + ".",
            "highlights": list(available[:5]),
            "regressions": [],
        }

    strengths = [str(v) for v in list(session_outcome.get("strengths", []) or []) if str(v or "").strip()]
    weaknesses = [str(v) for v in list(session_outcome.get("weaknesses", []) or []) if str(v or "").strip()]
    decision_basis = [str(v) for v in list(session_outcome.get("decision_basis", []) or []) if str(v or "").strip()]
    operator_headline = str((report.get("operator_report") or {}).get("headline", "") or "")
    headline = operator_headline or "Approved profile changed the active reference"
    summary_parts = []
    if strengths:
        summary_parts.append("Improved: " + ", ".join(strengths[:3]))
    if weaknesses:
        summary_parts.append("Watch: " + ", ".join(weaknesses[:2]))
    if decision_basis:
        summary_parts.append("Why: " + decision_basis[0])
    if not summary_parts:
        summary_parts.append("The active reference was updated from the approved candidate")
    return {
        "headline": str(headline),
        "summary": ". ".join(summary_parts) + ".",
        "highlights": list(strengths[:4]),
        "regressions": list(weaknesses[:3]),
    }


def build_candidate_decision_sample(
    *,
    candidate_id: str,
    decision: str,
    reason: str = "",
    workflow_mode: str,
    lineage: Dict[str, Any],
    report: Dict[str, Any],
    version: int = 0,
    approver: str = "",
) -> Dict[str, Any]:
    session_outcome = (
        report.get("session_outcome_report")
        if isinstance(report.get("session_outcome_report"), dict)
        else {}
    )
    effect = build_profile_effect_report(
        workflow_mode=str(workflow_mode or ""),
        decision=str(decision or ""),
        report=report,
    )
    return {
        "kind": "cm_candidate_decision",
        "ts": float(time.time()),
        "candidate_id": str(candidate_id),
        "decision": str(decision),
        "reason": str(reason or ""),
        "workflow_mode": str(workflow_mode or ""),
        "reference_profile_id": str(lineage.get("reference_profile_id", "") or ""),
        "reference_profile_version": int(lineage.get("reference_profile_version", 0) or 0),
        "version": int(version or 0),
        "approver": str(approver or ""),
        "verdict": str(report.get("verdict", "") or ""),
        "recommendation": str(report.get("recommendation", "") or ""),
        "approval_ready": bool(report.get("approval_ready", False)),
        "headline": str((report.get("operator_report") or {}).get("headline", "") or ""),
        "review_badge": str((report.get("review_badge") or {}).get("label", "") or ""),
        "blocking_gates": list((session_outcome.get("blocking_gates", []) or [])),
        "session_id": str(session_outcome.get("session_id", "") or ""),
        "effect_headline": str(effect.get("headline", "") or ""),
        "effect_summary": str(effect.get("summary", "") or ""),
        "effect_highlights": list(effect.get("highlights", []) or []),
        "effect_regressions": list(effect.get("regressions", []) or []),
    }


def build_raw_sample_journal_entry(
    *,
    candidate_id: str,
    sample: Dict[str, Any],
    ts: float | None = None,
) -> Dict[str, Any]:
    return {
        "kind": "cm_raw_sample",
        "ts": float(time.time() if ts is None else ts),
        "candidate_id": str(candidate_id),
        "sample": dict(sample or {}),
    }


def build_analysis_report_journal_entry(
    *,
    candidate_id: str,
    workflow_mode: str,
    report: Dict[str, Any],
    ts: float | None = None,
) -> Dict[str, Any]:
    session_outcome = (
        report.get("session_outcome_report")
        if isinstance(report.get("session_outcome_report"), dict)
        else {}
    )
    operator_report = (
        report.get("operator_report")
        if isinstance(report.get("operator_report"), dict)
        else {}
    )
    review_badge = (
        report.get("review_badge")
        if isinstance(report.get("review_badge"), dict)
        else {}
    )
    return {
        "kind": "cm_analysis_report",
        "ts": float(time.time() if ts is None else ts),
        "candidate_id": str(candidate_id),
        "workflow_mode": str(workflow_mode or ""),
        "session_id": str(session_outcome.get("session_id", "") or ""),
        "headline": str(operator_report.get("headline", "") or ""),
        "review_badge": str(review_badge.get("label", "") or ""),
        "blocking_gates": list(session_outcome.get("blocking_gates", []) or []),
        "report": copy.deepcopy(report),
    }


def build_external_knowledge_import_journal_entry(
    *,
    path: str,
    source: str,
    changed: bool,
    size_bytes: int,
    provider_hint_present: bool,
    device_hint_present: bool,
    site_hint_count: int,
    knowledge: Dict[str, Any],
    ts: float | None = None,
) -> Dict[str, Any]:
    return {
        "kind": "cm_external_knowledge_import",
        "ts": float(time.time() if ts is None else ts),
        "path": str(path or ""),
        "source": str(source or "file_import"),
        "changed": bool(changed),
        "size_bytes": int(size_bytes or 0),
        "provider_hint_present": bool(provider_hint_present),
        "device_hint_present": bool(device_hint_present),
        "site_hint_count": int(site_hint_count or 0),
        "knowledge": copy.deepcopy(dict(knowledge or {})),
    }


def build_context_baseline_seeded_journal_entry(
    *,
    candidate_id: str,
    workflow_mode: str,
    context_key: str,
    seed_report: Dict[str, Any],
    ts: float | None = None,
) -> Dict[str, Any]:
    return {
        "kind": "cm_context_baseline_seeded",
        "ts": float(seed_report.get("ts", time.time() if ts is None else ts) or 0.0),
        "candidate_id": str(candidate_id),
        "workflow_mode": str(workflow_mode or ""),
        "context_key": str(context_key or ""),
        "reason": str(seed_report.get("reason", "") or ""),
        "sample_count": int(seed_report.get("sample_count", 0) or 0),
        "quality_pct": int(seed_report.get("quality_pct", 0) or 0),
    }


def build_context_baseline_seeded_history_entry(
    *,
    candidate_id: str,
    workflow_mode: str,
    seed_report: Dict[str, Any],
    ts: float | None = None,
) -> Dict[str, Any]:
    return {
        "ts": float(seed_report.get("ts", time.time() if ts is None else ts) or 0.0),
        "kind": "decision",
        "candidate_id": str(candidate_id),
        "workflow_mode": str(workflow_mode or ""),
        "decision": "context_baseline_seeded",
        "verdict": "equivalent",
        "recommendation": "hold",
        "headline": "Background context baseline seeded",
        "summary": "A clean background window became the first baseline for this modem context.",
        "next_step": "observe",
        "review_badge": "SEEDED",
        "review_tone": "info",
        "approval_ready": False,
        "evidence_grade": "steady",
        "blocking_gates": [],
        "sample_count": int(seed_report.get("sample_count", 0) or 0),
    }


def build_recent_decision_history_entry(
    *,
    candidate_id: str,
    decision: str,
    workflow_mode: str,
    lineage: Dict[str, Any],
    report: Dict[str, Any],
) -> Dict[str, Any]:
    session_outcome = (
        report.get("session_outcome_report")
        if isinstance(report.get("session_outcome_report"), dict)
        else {}
    )
    effect = build_profile_effect_report(
        workflow_mode=str(workflow_mode or ""),
        decision=str(decision or ""),
        report=report,
    )
    return {
        "ts": float(time.time()),
        "kind": "decision",
        "candidate_id": str(candidate_id),
        "session_id": str(session_outcome.get("session_id", "") or ""),
        "workflow_mode": str(workflow_mode or ""),
        "reference_profile_id": str(lineage.get("reference_profile_id", "") or ""),
        "reference_profile_version": int(lineage.get("reference_profile_version", 0) or 0),
        "decision": str(decision),
        "verdict": str(report.get("verdict", "") or ""),
        "recommendation": str(report.get("recommendation", "") or ""),
        "headline": str((report.get("operator_report") or {}).get("headline", "") or ""),
        "summary": str((report.get("operator_report") or {}).get("summary", "") or ""),
        "next_step": str((report.get("operator_report") or {}).get("next_step", "") or ""),
        "review_badge": str((report.get("review_badge") or {}).get("label", "") or ""),
        "review_tone": str((report.get("review_badge") or {}).get("tone", "") or ""),
        "approval_ready": bool(report.get("approval_ready", False)),
        "evidence_grade": str(session_outcome.get("evidence_grade", "") or ""),
        "blocking_gates": list((session_outcome.get("blocking_gates", []) or [])),
        "sample_count": int(report.get("sample_count", 0) or 0),
        "effect_headline": str(effect.get("headline", "") or ""),
        "effect_summary": str(effect.get("summary", "") or ""),
        "effect_highlights": list(effect.get("highlights", []) or []),
        "effect_regressions": list(effect.get("regressions", []) or []),
    }
