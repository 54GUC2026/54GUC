from __future__ import annotations

import copy
import time
from typing import Any, Dict, Mapping


OPEN_CANDIDATE_STATES = {"collecting", "analyzed"}
TERMINAL_CANDIDATE_STATES = {"active", "rejected", "detached", "timed_out", "canceled"}


def candidate_state(profile: Mapping[str, Any] | None) -> str:
    return str((profile or {}).get("state", "") or "")


def assert_candidate_open(profile: Mapping[str, Any] | None, *, action: str) -> None:
    state = candidate_state(profile)
    if state not in OPEN_CANDIDATE_STATES:
        raise ValueError(f"candidate_not_open_for_{action}:{state or 'missing_state'}")


def mark_candidate_analysis_state(
    profile: Dict[str, Any],
    *,
    decision_ready: bool,
    now: float | None = None,
) -> str:
    assert_candidate_open(profile, action="analysis")
    state = "analyzed" if bool(decision_ready) else "collecting"
    profile["state"] = state
    profile["updated_ts"] = float(time.time() if now is None else now)
    return state


def activate_candidate_profile(
    profile: Dict[str, Any],
    *,
    approver: str,
    version: int,
    runtime_context: Mapping[str, Any],
    now: float | None = None,
) -> None:
    assert_candidate_open(profile, action="approval")
    profile["state"] = "active"
    profile["approved_by"] = str(approver)
    profile["version"] = int(version)
    profile["updated_ts"] = float(time.time() if now is None else now)
    profile["runtime_context"] = dict(runtime_context or {})


def reject_candidate_profile(
    profile: Dict[str, Any],
    *,
    reason: str,
    now: float | None = None,
) -> Dict[str, Any]:
    assert_candidate_open(profile, action="rejection")
    prior = copy.deepcopy(profile.get("analysis_report") if isinstance(profile.get("analysis_report"), dict) else {})
    merged = dict(prior)
    merged.update({"ok": False, "reason": str(reason), "rejected": True})
    profile["analysis_report"] = merged
    profile["state"] = "rejected"
    profile["updated_ts"] = float(time.time() if now is None else now)
    return prior


def detach_candidate_profile(
    profile: Dict[str, Any],
    *,
    reason: str,
    now: float | None = None,
) -> Dict[str, Any]:
    state = candidate_state(profile)
    if state in TERMINAL_CANDIDATE_STATES:
        raise ValueError(f"candidate_already_terminal:{state}")
    workflow_mode = str(profile.get("workflow_mode", "") or "")
    raw = profile.get("raw_samples") if isinstance(profile.get("raw_samples"), list) else []
    raw_count = int(len(raw))
    report = profile.get("analysis_report") if isinstance(profile.get("analysis_report"), dict) else {}
    report = copy.deepcopy(report)
    report.update(
        {
            "ok": False,
            "reason": str(reason),
            "detached": True,
            "decision": "detached",
            "verdict": "detached",
            "recommendation": "restart",
            "workflow_mode": workflow_mode,
            "sample_count": raw_count,
            "operator_report": {
                "headline": "Calibration candidate detached",
                "summary": "The open setup candidate belonged to another runtime identity.",
                "next_step": "restart_setup_for_current_modem",
            },
            "session_outcome_report": {
                "evidence_grade": "detached",
                "operator_digest": "The open setup candidate belonged to another runtime identity.",
                "blocking_gates": ["runtime_scope_mismatch"],
            },
        }
    )
    profile["analysis_report"] = report
    profile["state"] = "detached"
    profile["updated_ts"] = float(time.time() if now is None else now)
    return {
        "workflow_mode": workflow_mode,
        "lineage": copy.deepcopy(profile.get("lineage") if isinstance(profile.get("lineage"), dict) else {}),
        "report": report,
        "raw_count": raw_count,
    }


def time_out_candidate_profile(
    profile: Dict[str, Any],
    *,
    session_report: Mapping[str, Any],
    decision: str,
    recommendation: str,
    headline: str,
    summary: str,
    next_step: str,
    now: float | None = None,
) -> Dict[str, Any]:
    assert_candidate_open(profile, action="timeout")
    workflow_mode = str(profile.get("workflow_mode", "") or session_report.get("workflow_mode", "") or "")
    raw = profile.get("raw_samples") if isinstance(profile.get("raw_samples"), list) else []
    analysis = profile.get("analysis_report") if isinstance(profile.get("analysis_report"), dict) else {}
    analysis = dict(analysis)
    analysis.update(
        {
            "ok": False,
            "decision": str(decision),
            "verdict": "timed_out",
            "recommendation": str(recommendation),
            "workflow_mode": workflow_mode,
            "sample_count": int(len(raw)),
            "session_report": dict(session_report or {}),
            "operator_report": {
                "headline": str(headline),
                "summary": str(summary),
                "next_step": str(next_step),
            },
            "session_outcome_report": {
                "evidence_grade": "timeout",
                "operator_digest": str(summary),
                "blocking_gates": ["first_setup_timeout"],
            },
        }
    )
    profile["analysis_report"] = analysis
    profile["state"] = "timed_out"
    profile["updated_ts"] = float(time.time() if now is None else now)
    return analysis


def cancel_candidate_profile(
    profiles: Dict[str, Any],
    *,
    candidate_id: str,
) -> Dict[str, Any]:
    profile = profiles.get(candidate_id)
    if not isinstance(profile, dict):
        return {"found": False, "workflow_mode": "", "lineage": {}, "report": {}}
    workflow_mode = str(profile.get("workflow_mode", "") or "")
    lineage = copy.deepcopy(profile.get("lineage") if isinstance(profile.get("lineage"), dict) else {})
    prior = copy.deepcopy(profile.get("analysis_report") if isinstance(profile.get("analysis_report"), dict) else {})
    profile["state"] = "canceled"
    profiles.pop(candidate_id, None)
    return {
        "found": True,
        "workflow_mode": workflow_mode,
        "lineage": lineage,
        "report": prior,
    }
