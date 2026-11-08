from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Optional

from arch4.brain.calibration_workflow import (
    PHASE_ANCHOR_SURVEY,
    PHASE_BACKGROUND,
    PHASE_BOOTSTRAP,
    PHASE_COMPARATIVE,
)
from arch4.operator_profiles import (
    FAMILY_HUAWEI_HILINK,
    FAMILY_OPENWRT_MM,
    FAMILY_ROUTEROS_AT,
    FAMILY_ZTE_GOFORM,
    FAMILY_ZTE_WEBUI,
    FAMILY_ZTE_ZWRT,
)


MetricAvgFn = Callable[[Dict[str, Any], str], Optional[float]]


def build_bootstrap_playbook(
    *,
    backend: str,
    recognition_family: str,
    capability_flags: Dict[str, Any],
) -> Dict[str, Any]:
    backend_name = str(backend or "").strip().lower()
    family_name = str(recognition_family or "").strip().lower()
    caps = dict(capability_flags or {})

    primary_focus = ["SINR", "RSRP"]
    secondary_focus: List[str] = ["PCI / serving cell", "CA activity", "RI"]
    limits: List[str] = []
    headline = "Use signal strength first"
    summary = "Start Guide mainly watches signal strength. Other details are only helpers."
    operator_note = "Move slowly and hold each position."

    if family_name == FAMILY_ROUTEROS_AT:
        secondary_focus = ["PCI/EARFCN continuity", "CA activity", "RI", "neighbor visibility"]
        headline = "MikroTik setup should hold the same cell"
        summary = "Start with signal strength, then use cell details as confirmation."
        operator_note = "Keep the same cell while you improve signal."
        if not bool(caps.get("cell_lock", False)):
            limits.append("cell lock not available")
    elif family_name == FAMILY_HUAWEI_HILINK:
        secondary_focus = ["PCI / serving cell", "CA state", "RI when trustworthy"]
        headline = "Huawei setup should stay simple"
        summary = (
            "Use signal strength first. Treat cell and multi-band details as helpful, not exact proof."
        )
        operator_note = "Do not over-read Huawei multi-band detail."
        limits.append("exact active CA component bands unavailable")
        if not bool(caps.get("cell_lock", False)):
            limits.append("cell lock not available")
    elif family_name == FAMILY_OPENWRT_MM:
        secondary_focus = ["PCI / serving cell", "CA activity", "RI when exposed"]
        headline = "OpenWrt setup depends on modem data"
        summary = "Use signal strength first. Treat extra details as helpers until this modem is proven."
        operator_note = "Keep setup conservative until this modem is proven."
        limits.append("detail depends on modem-manager or qmi plugin support")
    elif family_name in {FAMILY_ZTE_GOFORM, FAMILY_ZTE_ZWRT, FAMILY_ZTE_WEBUI}:
        secondary_focus = ["PCI / serving cell", "CA activity", "RI when exposed"]
        headline = "ZTE setup should stay conservative"
        summary = "Use signal strength first. Treat extra details as advisory until proven."
        operator_note = "Different firmware can expose different details."
        limits.append("field availability may vary by firmware/operator build")
    else:
        secondary_focus = ["PCI / serving cell", "CA activity", "RI after validation"]
        headline = "Antenna Guide should stay simple"
        summary = "Signal strength is enough for the first saved setup."
        operator_note = "Do not let unproven details drive setup."

    if not bool(caps.get("ca_details", False)):
        limits.append("exact CA carrier detail unavailable")
    if not bool(caps.get("neighbors", False)):
        limits.append("neighbor detail unavailable")

    return {
        "backend": backend_name,
        "recognition_family": family_name,
        "headline": str(headline),
        "summary": str(summary),
        "operator_note": str(operator_note),
        "primary_focus": list(primary_focus),
        "secondary_focus": list(secondary_focus),
        "limits": list(dict.fromkeys([str(v) for v in limits if str(v or "").strip()])),
    }


def build_operator_report(
    *,
    workflow_mode: str,
    session_report: Dict[str, Any],
    verdict: str,
    recommendation: str,
    approval_ready: bool,
) -> Dict[str, Any]:
    ready = bool(session_report.get("ready", False))
    mode = str(workflow_mode or "")
    if not ready:
        if mode == PHASE_BOOTSTRAP:
            headline = "Antenna Guide is still collecting"
            summary = "Keep the antenna steady."
        elif mode == PHASE_ANCHOR_SURVEY:
            headline = "Anchor check still running"
            summary = "Hold each band position steady."
        elif mode == PHASE_COMPARATIVE:
            headline = "Comparison still running"
            summary = "Hold the current antenna position."
        else:
            headline = "Background check still running"
            summary = "Still collecting signal quality."
        next_step = "collect_more"
    elif approval_ready:
        if mode == PHASE_BOOTSTRAP:
            headline = "Antenna reference saved"
            summary = "54GUC saved this antenna position."
        elif mode == PHASE_ANCHOR_SURVEY:
            headline = "Anchor result ready"
            summary = "One band position looks best."
        elif mode == PHASE_COMPARATIVE:
            headline = "Better setup found"
            summary = "54GUC will use this better antenna position."
        else:
            headline = "Signal improved"
            summary = "54GUC will use this better setup."
        next_step = "auto_apply"
    elif verdict == "worse":
        if mode == PHASE_BACKGROUND:
            headline = "Saved setup kept"
            summary = "The new result was worse."
            next_step = "closed"
        else:
            headline = "New result is worse"
            summary = "Move back toward the previous position."
            next_step = "reposition"
    elif verdict == "equivalent":
        headline = "No real improvement"
        summary = "Keep the saved setup unless a clearly better result appears."
        next_step = "keep_saved_setup"
    elif mode == PHASE_ANCHOR_SURVEY and verdict == "better":
        headline = "Better band position found"
        summary = "Use it to guide the next test."
        next_step = "use_anchor_result"
    elif verdict == "better":
        headline = "Looks better, not ready"
        summary = "Keep collecting before trusting it."
        next_step = "collect_more"
    elif verdict == "mixed":
        headline = "Mixed result"
        summary = "Some readings improved, but not enough."
        next_step = "collect_more"
    elif mode == PHASE_BOOTSTRAP:
        headline = "Antenna Guide needs more data"
        summary = "Keep collecting signal readings."
        next_step = "collect_more"
    else:
        headline = "More checking needed"
        summary = "54GUC needs cleaner evidence before using this result."
        next_step = "collect_more"
    return {
        "headline": str(headline),
        "summary": str(summary),
        "next_step": str(next_step),
    }


def gate_debt_hint(gate: str, detail: str, *, workflow_mode: str, verdict: str) -> str:
    name = str(gate or "").strip()
    info = str(detail or "").strip()
    if name == "runtime_context_ready":
        return "finish modem check"
    if name == "session_window_complete":
        return "finish the current check"
    if name == "metric_coverage_ready":
        return "collect usable signal readings"
    if name == "reference_profile_ready":
        return "save a setup before comparing"
    if name == "comparable_metrics_ready":
        return "collect more matching readings"
    if name == "comparison_metric_depth_ready":
        return "collect more same-cell readings"
    if name == "comparison_confidence_ready":
        return "collect a cleaner same-cell check"
    if name == "comparison_stability_ready":
        return "wait for a steadier signal"
    if name == "context_ready":
        return "compare only against the same band and serving cell/PCI"
    if name == "comparison_offpeak_confirmation_ready":
        return "check again during a quieter time"
    if name == "material_improvement":
        if str(verdict or "") == "mixed":
            return "resolve the mixed result"
        if str(verdict or "") == "equivalent":
            return "find a clearly better position"
        if str(verdict or "") == "worse":
            return "move back toward the previous position"
        return "show a clear improvement"
    if name == "approval_ready":
        if str(workflow_mode or "") == PHASE_BOOTSTRAP:
            return "finish the antenna reference"
        return "clear the remaining approval blockers"
    if info:
        return info
    return name.replace("_", " ")


def build_evidence_debt(
    *,
    workflow_mode: str,
    session_report: Dict[str, Any],
    gates: List[Dict[str, Any]],
    verdict: str,
    approval_ready: bool,
) -> Dict[str, Any]:
    if bool(approval_ready):
        return {
            "headline": "Ready",
            "summary": "Current readings are good enough.",
            "items": [],
        }
    items: List[str] = []
    for row in list(gates or []):
        if not isinstance(row, dict) or bool(row.get("ok", False)):
            continue
        hint = gate_debt_hint(
            str(row.get("gate", "") or ""),
            str(row.get("detail", "") or ""),
            workflow_mode=str(workflow_mode or ""),
            verdict=str(verdict or ""),
        )
        if hint and hint not in items:
            items.append(hint)
    if not items and not bool(session_report.get("ready", False)):
        items.append("finish the current check")
    if not items:
        items.append("collect a cleaner check")
    headline = "More readings needed"
    if str(workflow_mode or "") == PHASE_BOOTSTRAP:
        headline = "Antenna Guide needs more readings"
    elif str(workflow_mode or "") == PHASE_COMPARATIVE:
        headline = "Comparison not ready"
    elif str(workflow_mode or "") == PHASE_ANCHOR_SURVEY:
        headline = "Anchor check needs more readings"
    elif str(workflow_mode or "") == PHASE_BACKGROUND:
        headline = "Background check needs confirmation"
    summary = "Still needed: " + "; ".join(items[:3]) + "."
    return {
        "headline": str(headline),
        "summary": str(summary),
        "items": items[:5],
    }


def build_review_badge(
    *,
    workflow_mode: str,
    decision_ready: bool,
    verdict: str,
    recommendation: str,
    approval_ready: bool,
    analysis_stale: bool,
) -> Dict[str, str]:
    mode = str(workflow_mode or "")
    if analysis_stale:
        return {
            "label": "CHECK AGAIN",
            "tone": "warning",
            "reason": "readings changed after the last check",
        }
    if not decision_ready:
        if mode == PHASE_BOOTSTRAP:
            return {
                "label": "COLLECT REFERENCE",
                "tone": "info",
                "reason": "Antenna Guide is still collecting readings",
            }
        if mode == PHASE_ANCHOR_SURVEY:
            return {
                "label": "COLLECT SURVEY",
                "tone": "info",
                "reason": "the anchor check is still collecting readings",
            }
        if mode == PHASE_COMPARATIVE:
            return {
                "label": "COLLECT COMPARE",
                "tone": "info",
                "reason": "the comparison is still collecting readings",
            }
        return {
            "label": "COLLECT BACKGROUND",
            "tone": "info",
            "reason": "background check is still collecting readings",
        }
    if approval_ready:
        if mode == PHASE_ANCHOR_SURVEY:
            return {
                "label": "SURVEY READY",
                "tone": "good",
                "reason": "one band position looks best",
            }
        return {
            "label": "AUTO APPLY",
            "tone": "good",
            "reason": "current checks passed",
        }
    if mode == PHASE_ANCHOR_SURVEY and verdict == "better":
        return {
            "label": "SURVEY READY",
            "tone": "good",
            "reason": "one band position looks best",
        }
    if recommendation == "reject" or verdict == "worse":
        return {
            "label": "REJECT RESULT",
            "tone": "bad",
            "reason": "this result is worse than the saved setup",
        }
    if verdict == "equivalent":
        return {
            "label": "KEEP CURRENT",
            "tone": "steady",
            "reason": "this result is not clearly better",
        }
    if verdict == "mixed":
        return {
            "label": "MIXED RESULT",
            "tone": "warning",
            "reason": "some readings improved, but not enough",
        }
    return {
        "label": "CHECKING",
        "tone": "warning",
        "reason": "this result needs more evidence",
    }


def build_gate_rows(
    *,
    workflow_mode: str,
    runtime_context_ready: bool,
    coverage_ok: bool,
    metric_coverage: int = 0,
    reference_present: bool = False,
    comparable_metrics: int = 0,
    comparable_metric_min_required: int = 0,
    context_ready: bool = True,
    context_reason: str = "",
    confidence: float = 0.0,
    confidence_threshold: float = 0.0,
    stability_penalty: float = 0.0,
    stability_penalty_max: float = 0.0,
    offpeak_confirmation_required: bool = False,
    offpeak_ready: bool = True,
    verdict: str = "",
    approval_ready: bool = False,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = [
        {
            "gate": "runtime_context_ready",
            "ok": bool(runtime_context_ready),
            "detail": "router identity and capability probe available"
            if runtime_context_ready
            else "router identity or capability probe still incomplete",
        },
        {
            "gate": "session_window_complete",
            "ok": bool(coverage_ok),
            "detail": "sample and duration window complete"
            if coverage_ok
            else "sample or duration window still incomplete",
        },
    ]
    if str(workflow_mode or "") == PHASE_BOOTSTRAP:
        rows.append(
            {
                "gate": "metric_coverage_ready",
                "ok": bool(metric_coverage > 0),
                "detail": f"{int(metric_coverage)} metrics with usable coverage",
            }
        )
    else:
        rows.append(
            {
                "gate": "context_ready",
                "ok": bool(context_ready),
                "detail": (
                    "result and saved setup use the same band and cell"
                    if context_ready
                    else (str(context_reason) or "result and saved setup are not on the same band/cell")
                ),
            }
        )
        rows.append(
            {
                "gate": "reference_profile_ready",
                "ok": bool(reference_present),
                "detail": "approved reference profile summary available"
                if reference_present
                else "approved reference profile summary missing",
            }
        )
        rows.append(
            {
                "gate": "comparable_metrics_ready",
                "ok": bool(comparable_metrics > 0),
                "detail": f"{int(comparable_metrics)} comparable metrics",
            }
        )
        rows.append(
            {
                "gate": "comparison_metric_depth_ready",
                "ok": bool(comparable_metrics >= max(1, int(comparable_metric_min_required))),
                "detail": (
                    f"{int(comparable_metrics)}/{int(max(1, comparable_metric_min_required))} comparable metrics"
                ),
            }
        )
        rows.append(
            {
                "gate": "comparison_confidence_ready",
                "ok": bool(float(confidence) >= float(confidence_threshold)),
                "detail": f"confidence {float(confidence):.2f}/{float(confidence_threshold):.2f}",
            }
        )
        rows.append(
            {
                "gate": "comparison_stability_ready",
                "ok": bool(float(stability_penalty) <= float(stability_penalty_max)),
                "detail": f"stability penalty {float(stability_penalty):.3f}/{float(stability_penalty_max):.3f}",
            }
        )
        rows.append(
            {
                "gate": "comparison_offpeak_confirmation_ready",
                "ok": bool((not offpeak_confirmation_required) or offpeak_ready),
                "detail": (
                    "off-peak confirmation not required"
                    if not offpeak_confirmation_required
                    else ("off-peak confirmation complete" if offpeak_ready else "off-peak confirmation still required")
                ),
            }
        )
        rows.append(
            {
                "gate": "material_improvement",
                "ok": bool(verdict == "better"),
                "detail": f"candidate verdict={str(verdict or 'unknown')}",
            }
        )
    rows.append(
        {
            "gate": "approval_ready",
            "ok": bool(approval_ready),
            "detail": "candidate satisfies current mode approval rules"
            if approval_ready
            else "candidate is not yet approvable",
        }
    )
    return rows


def build_comparison_rows(
    *,
    candidate_summary: Dict[str, Any],
    reference_summary: Dict[str, Any],
    metric_avg: MetricAvgFn,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    metric_specs = (
        ("sinr_db", True),
        ("rsrp_db", True),
        ("ri", True),
        ("ca_persistence_ratio", True),
    )
    for metric, positive_better in metric_specs:
        cand = metric_avg(candidate_summary, metric)
        ref = metric_avg(reference_summary, metric)
        if cand is None or ref is None:
            continue
        delta = float(cand - ref)
        delta_pct = None
        if abs(ref) > 1e-9:
            delta_pct = float((cand - ref) / abs(ref))
        if positive_better:
            direction = "better" if delta > 0.0 else ("worse" if delta < 0.0 else "equal")
        else:
            direction = "better" if delta < 0.0 else ("worse" if delta > 0.0 else "equal")
        rows.append(
            {
                "metric": str(metric),
                "candidate_avg": float(cand),
                "reference_avg": float(ref),
                "delta": float(delta),
                "delta_pct": float(delta_pct) if delta_pct is not None else None,
                "direction": str(direction),
            }
        )
    return rows


def build_comparison_highlights(rows: List[Dict[str, Any]]) -> List[str]:
    if not isinstance(rows, list) or not rows:
        return []
    highlights: List[str] = []
    metric_labels = {
        "sinr_db": "SINR",
        "rsrp_db": "RSRP",
        "ri": "RI",
        "ca_persistence_ratio": "CA persistence",
    }
    for row in rows:
        if not isinstance(row, dict):
            continue
        metric = str(row.get("metric", "") or "")
        delta = row.get("delta")
        direction = str(row.get("direction", "") or "")
        try:
            delta_val = float(delta)
        except Exception:
            continue
        if abs(delta_val) < 1e-9 or direction == "equal":
            continue
        label = metric_labels.get(metric, metric.replace("_", " ").upper())
        if metric == "ca_persistence_ratio":
            pretty = f"{label} {delta_val * 100.0:+.0f}%"
        elif metric == "ri":
            pretty = f"{label} {delta_val:+.1f}"
        else:
            pretty = f"{label} {delta_val:+.1f} dB"
        highlights.append(pretty)
    return highlights[:4]


def build_comparison_regressions(rows: List[Dict[str, Any]]) -> List[str]:
    if not isinstance(rows, list) or not rows:
        return []
    regressions: List[str] = []
    metric_labels = {
        "sinr_db": "SINR",
        "rsrp_db": "RSRP",
        "ri": "RI",
        "ca_persistence_ratio": "CA persistence",
    }
    for row in rows:
        if not isinstance(row, dict):
            continue
        direction = str(row.get("direction", "") or "")
        if direction != "worse":
            continue
        metric = str(row.get("metric", "") or "")
        try:
            delta_val = float(row.get("delta"))
        except Exception:
            continue
        label = metric_labels.get(metric, metric.replace("_", " ").upper())
        if metric == "ca_persistence_ratio":
            pretty = f"{label} {delta_val * 100.0:+.0f}%"
        elif metric == "ri":
            pretty = f"{label} {delta_val:+.1f}"
        else:
            pretty = f"{label} {delta_val:+.1f} dB"
        regressions.append(pretty)
    return regressions[:4]


def build_summary_highlights(summary: Dict[str, Any], *, metric_avg: MetricAvgFn) -> List[str]:
    if not isinstance(summary, dict):
        return []
    out: List[str] = []
    sinr = metric_avg(summary, "sinr_db")
    rsrp = metric_avg(summary, "rsrp_db")
    ri = metric_avg(summary, "ri")
    ca_persistence = metric_avg(summary, "ca_persistence_ratio")
    if sinr is not None:
        out.append(f"SINR {float(sinr):.1f} dB")
    if rsrp is not None:
        out.append(f"RSRP {float(rsrp):.1f} dBm")
    if ri is not None:
        out.append(f"RI {float(ri):.1f}")
    if ca_persistence is not None and float(ca_persistence) > 0.0:
        out.append(f"CA persistence {float(ca_persistence) * 100.0:.0f}%")
    return out[:4]


def build_session_outcome_report(
    *,
    workflow_mode: str,
    session_report: Dict[str, Any],
    sample_summary: Dict[str, Any],
    reference_summary: Dict[str, Any],
    lineage: Dict[str, Any],
    operator_report: Dict[str, Any],
    review_badge: Dict[str, Any],
    verdict: str,
    recommendation: str,
    approval_ready: bool,
    gates: List[Dict[str, Any]],
    comparison_rows: List[Dict[str, Any]],
    confidence: float = 0.0,
    score_details: Optional[Dict[str, Any]] = None,
    mode_details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    blocking_gates = [str(row.get("gate", "") or "") for row in gates if not bool(row.get("ok", False))]
    blocking_hints = [
        gate_debt_hint(
            str(row.get("gate", "") or ""),
            str(row.get("detail", "") or ""),
            workflow_mode=str(workflow_mode or ""),
            verdict=str(verdict or ""),
        )
        for row in gates
        if isinstance(row, dict) and not bool(row.get("ok", False))
    ]
    comparison = (
        mode_details.get("comparison")
        if isinstance(mode_details, dict) and isinstance(mode_details.get("comparison"), dict)
        else {}
    )
    context_ready = bool(comparison.get("context_ready", True))
    evidence_grade = "not_comparable" if not context_ready else "weak"
    if bool(approval_ready):
        evidence_grade = "strong"
    elif context_ready and str(verdict or "") == "better":
        evidence_grade = "promising"
    elif context_ready and str(verdict or "") in {"equivalent", "baseline_ready"}:
        evidence_grade = "steady"
    elif context_ready and str(verdict or "") == "worse":
        evidence_grade = "negative"
    evidence_debt = build_evidence_debt(
        workflow_mode=str(workflow_mode or ""),
        session_report=session_report,
        gates=gates,
        verdict=str(verdict or ""),
        approval_ready=bool(approval_ready),
    )
    comparison_highlights = build_comparison_highlights(comparison_rows)
    comparison_regressions = build_comparison_regressions(comparison_rows)
    strengths: List[str] = []
    weaknesses: List[str] = []
    decision_basis: List[str] = []
    mode_data = dict(mode_details or {})

    if str(workflow_mode or "") == PHASE_BOOTSTRAP:
        bootstrap_primary = (
            mode_data.get("bootstrap_primary_metrics")
            if isinstance(mode_data.get("bootstrap_primary_metrics"), dict)
            else {}
        )
        baseline_quality = (
            mode_data.get("baseline_quality")
            if isinstance(mode_data.get("baseline_quality"), dict)
            else {}
        )
        available = [str(v) for v in list(bootstrap_primary.get("primary_metrics_available", []) or []) if str(v or "").strip()]
        missing = [str(v) for v in list(bootstrap_primary.get("primary_metrics_missing", []) or []) if str(v or "").strip()]
        if available:
            strengths.append("Core metrics available: " + ", ".join(available[:5]))
        if bool(bootstrap_primary.get("primary_metrics_ready", False)):
            decision_basis.append("Main signal readings are ready")
        if int(baseline_quality.get("quality_pct", 0) or 0) > 0:
            strengths.append(f"Setup quality {int(baseline_quality.get('quality_pct', 0) or 0)}%")
        if missing:
            weaknesses.append("Missing core metrics: " + ", ".join(missing[:5]))
        if blocking_hints:
            decision_basis.extend(blocking_hints[:3])
    else:
        if comparison_highlights:
            strengths.extend(comparison_highlights[:3])
        if comparison_regressions:
            weaknesses.extend(comparison_regressions[:3])
        if not bool(comparison.get("context_ready", True)):
            context_reason = str(comparison.get("context_reason", "") or "").strip()
            weaknesses.append(
                context_reason
                or "Result was not collected on the same band and cell as the saved setup"
            )
        if float(comparison.get("candidate_network_load_biased_ratio", 0.0) or 0.0) >= 0.50:
            weaknesses.append("Readings were biased by load or interference")
        if bool(approval_ready):
            decision_basis.append("Improvement passed the checks")
        elif str(workflow_mode or "") == PHASE_ANCHOR_SURVEY and str(verdict or "") == "better":
            decision_basis.append("This band position looks best")
        elif str(verdict or "") == "better":
            decision_basis.append("Result looks better, but is not ready")
        elif str(verdict or "") == "equivalent":
            decision_basis.append("Result is not clearly better than saved setup")
        elif str(verdict or "") == "worse":
            decision_basis.append("Result is worse than saved setup")
        elif str(verdict or "") == "mixed":
            decision_basis.append("Result is mixed")
        if int(comparison.get("comparable_metrics", 0) or 0) > 0:
            decision_basis.append(
                f"Comparable metrics {int(comparison.get('comparable_metrics', 0) or 0)}/{int(comparison.get('comparable_metric_min_required', 0) or 0)}"
            )
        if float(comparison.get("confidence_threshold", 0.0) or 0.0) > 0.0:
            decision_basis.append(
                f"Confidence {float(comparison.get('confidence', 0.0) or 0.0):.2f}/{float(comparison.get('confidence_threshold', 0.0) or 0.0):.2f}"
            )
        if float(comparison.get("stability_penalty_max", 0.0) or 0.0) > 0.0:
            decision_basis.append(
                f"Stability penalty {float(comparison.get('stability_penalty', 0.0) or 0.0):.3f}/{float(comparison.get('stability_penalty_max', 0.0) or 0.0):.3f}"
            )
        if bool(comparison.get("offpeak_confirmation_required", False)):
            weaknesses.append("Quieter-time check still needed")
            decision_basis.append(str(comparison.get("offpeak_confirmation_reason", "") or "Quieter-time check still needed"))
        dominant_anchor = int(sample_summary.get("dominant_anchor_band", 0) or 0)
        dominant_anchor_ratio = float(sample_summary.get("dominant_anchor_ratio", 0.0) or 0.0)
        if str(workflow_mode or "") == PHASE_ANCHOR_SURVEY and dominant_anchor > 0:
            strengths.append(
                f"B{dominant_anchor} was strongest {dominant_anchor_ratio * 100.0:.0f}% of the check"
            )
        if int(sample_summary.get("anchor_switches", 0) or 0) > 0:
            weaknesses.append(f"Anchor switched {int(sample_summary.get('anchor_switches', 0) or 0)} times")
        if blocking_hints:
            decision_basis.extend(blocking_hints[:3])

    strengths = list(dict.fromkeys([str(v) for v in strengths if str(v or "").strip()]))[:5]
    weaknesses = list(dict.fromkeys([str(v) for v in weaknesses if str(v or "").strip()]))[:5]
    decision_basis = list(dict.fromkeys([str(v) for v in decision_basis if str(v or "").strip()]))[:5]

    if str(workflow_mode or "") == PHASE_BOOTSTRAP:
        operator_digest = (
            f"Antenna Guide: {operator_report.get('headline', '') or 'result being checked'} | "
            f"{'; '.join(decision_basis[:2]) if decision_basis else 'awaiting more readings'}"
        ).strip()
    elif str(workflow_mode or "") == PHASE_COMPARATIVE:
        operator_digest = (
            f"Compare result: {operator_report.get('headline', '') or 'result being checked'} | "
            f"{'; '.join(decision_basis[:2]) if decision_basis else 'awaiting more readings'}"
        ).strip()
    elif str(workflow_mode or "") == PHASE_ANCHOR_SURVEY:
        operator_digest = (
            f"Anchor check: {operator_report.get('headline', '') or 'result being checked'} | "
            f"{'; '.join(decision_basis[:2]) if decision_basis else 'awaiting more readings'}"
        ).strip()
    else:
        operator_digest = (
            f"Background result: {operator_report.get('headline', '') or 'result being checked'} | "
            f"{'; '.join(decision_basis[:2]) if decision_basis else 'awaiting more readings'}"
        ).strip()
    return {
        "session_id": str(session_report.get("session_id", "") or ""),
        "workflow_mode": str(workflow_mode or ""),
        "reason": str(session_report.get("reason", "") or ""),
        "started_ts": float(session_report.get("started_ts", 0.0) or 0.0),
        "last_sample_ts": float(session_report.get("last_sample_ts", 0.0) or 0.0),
        "duration_s": float(session_report.get("duration_s", 0.0) or 0.0),
        "sample_count": int(session_report.get("sample_count", 0) or 0),
        "progress_pct": int(session_report.get("progress_pct", 0) or 0),
        "decision_ready": bool(session_report.get("ready", False)),
        "operator_note": str(session_report.get("operator_note", "") or ""),
        "verdict": str(verdict or ""),
        "recommendation": str(recommendation or ""),
        "approval_ready": bool(approval_ready),
        "confidence": float(confidence or 0.0),
        "evidence_grade": str(evidence_grade),
        "review_badge": str(review_badge.get("label", "") or ""),
        "review_tone": str(review_badge.get("tone", "") or ""),
        "review_reason": str(review_badge.get("reason", "") or ""),
        "headline": str(operator_report.get("headline", "") or ""),
        "summary": str(operator_report.get("summary", "") or ""),
        "next_step": str(operator_report.get("next_step", "") or ""),
        "gates": list(gates or []),
        "blocking_gates": blocking_gates,
        "blocking_hints": blocking_hints[:5],
        "evidence_debt": evidence_debt,
        "lineage": copy.deepcopy(lineage if isinstance(lineage, dict) else {}),
        "sample_summary": copy.deepcopy(sample_summary if isinstance(sample_summary, dict) else {}),
        "reference_summary": copy.deepcopy(reference_summary if isinstance(reference_summary, dict) else {}),
        "comparison_rows": list(comparison_rows or []),
        "comparison_highlights": comparison_highlights,
        "comparison_regressions": comparison_regressions,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "decision_basis": decision_basis,
        "operator_digest": str(operator_digest),
        "score_details": copy.deepcopy(score_details if isinstance(score_details, dict) else {}),
    }
