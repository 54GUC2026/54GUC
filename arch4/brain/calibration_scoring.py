from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from arch4.signal_processing.band_quality import score_band_quality
from arch4.brain.calibration_workflow import PHASE_ANCHOR_SURVEY, PHASE_BACKGROUND


COMPARATIVE_SCORE_BETTER = 2
BACKGROUND_SCORE_BETTER = 2
COMPARE_WEIGHTED_VERDICT_BETTER = 0.08
COMPARE_WEIGHTED_VERDICT_WORSE = -0.08
COMPARE_WEIGHTED_EQUIVALENT_BAND = 0.04
COMPARE_WEIGHTED_APPROVAL = 0.08
BACKGROUND_WEIGHTED_APPROVAL = 0.10
COMPARE_MIN_COMPARABLE_METRICS = 2
BACKGROUND_MIN_COMPARABLE_METRICS = 2
COMPARE_MIN_CONFIDENCE = 0.50
BACKGROUND_MIN_CONFIDENCE = 0.50
COMPARE_MAX_STABILITY_PENALTY = 0.14
BACKGROUND_MAX_STABILITY_PENALTY = 0.12


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def extract_metric(sample: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        if key in sample:
            out = safe_float(sample.get(key))
            if out is not None:
                return out
    return None


def _hour_in_window(hour: int, start_h: int, end_h: int) -> bool:
    hour_i = int(hour) % 24
    start_i = int(start_h) % 24
    end_i = int(end_h) % 24
    if start_i == end_i:
        return True
    if start_i < end_i:
        return start_i <= hour_i < end_i
    return hour_i >= start_i or hour_i < end_i


def summarize_samples(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    metric_map = {
        "sinr_db": ("sinr_db",),
        "rsrp_db": ("rsrp_db", "rsrp_dbm"),
        "ri": ("ri",),
        "bw_mhz": ("primary_bw_mhz", "dl_bandwidth_mhz", "bandwidth_mhz"),
    }
    metrics: Dict[str, Dict[str, float]] = {}
    for name, keys in metric_map.items():
        vals: List[float] = []
        for sample in samples:
            out = extract_metric(sample, *keys)
            if out is not None:
                vals.append(out)
        if vals:
            metrics[name] = {
                "count": int(len(vals)),
                "avg": float(sum(vals) / len(vals)),
                "min": float(min(vals)),
                "max": float(max(vals)),
            }
        else:
            metrics[name] = {"count": 0, "avg": 0.0, "min": 0.0, "max": 0.0}
    connected_samples = 0
    ca_active_samples = 0
    network_load_biased_samples = 0
    offpeak_samples = 0
    anchor_band_counts: Dict[int, int] = {}
    last_anchor_band = 0
    anchor_switches = 0
    context_counts: Dict[tuple[int, int], int] = {}
    context_samples = 0
    for sample in samples:
        connected = bool(sample.get("radio_connected", False))
        if connected:
            connected_samples += 1
        if connected and bool(sample.get("ca_active", False)):
            ca_active_samples += 1
        if bool(sample.get("network_load_biased", False)):
            network_load_biased_samples += 1
        if "offpeak_window" in sample:
            if bool(sample.get("offpeak_window", False)):
                offpeak_samples += 1
        else:
            ts = safe_float(sample.get("ts"))
            if ts is not None and _hour_in_window(time.localtime(float(ts)).tm_hour, 0, 6):
                offpeak_samples += 1
        band = int(extract_metric(sample, "serving_band", "primary_band") or 0)
        if band > 0:
            anchor_band_counts[band] = int(anchor_band_counts.get(band, 0) or 0) + 1
            if last_anchor_band > 0 and band != last_anchor_band:
                anchor_switches += 1
            last_anchor_band = band
        pci = int(extract_metric(sample, "pci", "serving_pci", "cell_id") or 0)
        if band > 0 and pci > 0:
            context_key = (band, pci)
            context_counts[context_key] = int(context_counts.get(context_key, 0) or 0) + 1
            context_samples += 1
    dominant_anchor_band = 0
    dominant_anchor_ratio = 0.0
    if anchor_band_counts:
        dominant_anchor_band, dominant_count = max(
            anchor_band_counts.items(),
            key=lambda item: (int(item[1]), -int(item[0])),
        )
        dominant_anchor_ratio = float(dominant_count) / float(max(1, sum(anchor_band_counts.values())))
    ca_persistence_ratio = float(ca_active_samples) / float(max(1, connected_samples)) if connected_samples > 0 else 0.0
    offpeak_ratio = float(offpeak_samples) / float(max(1, len(samples))) if samples else 0.0
    network_load_biased_ratio = (
        float(network_load_biased_samples) / float(max(1, len(samples)))
        if samples
        else 0.0
    )
    dominant_context_band = 0
    dominant_context_cell = 0
    dominant_context_ratio = 0.0
    if context_counts:
        (dominant_context_band, dominant_context_cell), dominant_context_count = max(
            context_counts.items(),
            key=lambda item: (int(item[1]), -int(item[0][0]), -int(item[0][1])),
        )
        dominant_context_ratio = float(dominant_context_count) / float(max(1, context_samples))
    ri_avg = metric_avg({"metrics": metrics}, "ri")
    return {
        "sample_count": int(len(samples)),
        "metrics": metrics,
        "connected_samples": int(connected_samples),
        "ca_active_samples": int(ca_active_samples),
        "ca_persistence_ratio": float(ca_persistence_ratio),
        "ca_avg": float(ca_persistence_ratio),
        "network_load_biased_samples": int(network_load_biased_samples),
        "network_load_biased_ratio": float(network_load_biased_ratio),
        "offpeak_samples": int(offpeak_samples),
        "offpeak_ratio": float(offpeak_ratio),
        "anchor_band_counts": {int(k): int(v) for k, v in anchor_band_counts.items()},
        "dominant_anchor_band": int(dominant_anchor_band or 0),
        "dominant_anchor_ratio": float(dominant_anchor_ratio),
        "anchor_switches": int(anchor_switches),
        "dominant_context_band": int(dominant_context_band or 0),
        "dominant_context_cell": int(dominant_context_cell or 0),
        "dominant_context_ratio": float(dominant_context_ratio),
        "context_sample_count": int(context_samples),
        "context_pure": bool(context_samples > 0 and abs(dominant_context_ratio - 1.0) < 1e-9),
        "ri_avg": float(ri_avg) if ri_avg is not None else None,
    }


def metric_presence(summary: Dict[str, Any], metric: str) -> bool:
    metrics = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
    row = metrics.get(metric) if isinstance(metrics, dict) else {}
    if not isinstance(row, dict):
        return False
    return int(row.get("count", 0) or 0) > 0


def metric_avg(summary: Dict[str, Any], metric: str) -> Optional[float]:
    metrics = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
    row = metrics.get(metric) if isinstance(metrics, dict) else {}
    if not isinstance(row, dict):
        return summary_scalar(summary, metric)
    if int(row.get("count", 0) or 0) <= 0:
        return summary_scalar(summary, metric)
    try:
        return float(row.get("avg", 0.0) or 0.0)
    except Exception:
        return summary_scalar(summary, metric)


def metric_spread(summary: Dict[str, Any], metric: str) -> Optional[float]:
    metrics = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
    row = metrics.get(metric) if isinstance(metrics, dict) else {}
    if not isinstance(row, dict):
        return None
    if int(row.get("count", 0) or 0) <= 1:
        return None
    try:
        return float(row.get("max", 0.0) or 0.0) - float(row.get("min", 0.0) or 0.0)
    except Exception:
        return None


def clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(float(lo), min(float(hi), float(value)))


def weighted_component(
    *,
    delta: float,
    better_cut: float,
    worse_cut: float,
    positive_better: bool,
    weight: float,
) -> float:
    if positive_better:
        normalized = float(delta) / float(better_cut if delta >= 0.0 else worse_cut)
    else:
        normalized = float(-delta) / float(better_cut if delta <= 0.0 else worse_cut)
    return float(weight) * clamp(normalized)


def summary_scalar(summary: Dict[str, Any], key: str) -> Optional[float]:
    if key not in summary:
        return None
    return safe_float(summary.get(key))


def score_bootstrap_baseline(sample_summary: Dict[str, Any], backend: str = "") -> Dict[str, Any]:
    sinr = metric_avg(sample_summary, "sinr_db")
    rsrp = metric_avg(sample_summary, "rsrp_db")
    ri_avg = metric_avg(sample_summary, "ri")
    ca_avg = summary_scalar(sample_summary, "ca_avg")
    bw_avg = metric_avg(sample_summary, "bw_mhz")
    result = score_band_quality(
        sinr_db=sinr,
        rsrp_db=rsrp,
        ri=ri_avg,
        ca_value=ca_avg,
        bw_mhz=bw_avg,
    )
    return {
        "quality_score": float(result.get("quality_score", 0.0) or 0.0),
        "quality_pct": int(result.get("quality_pct", 0) or 0),
        "metric_count": int(result.get("metric_count", 0) or 0),
        "available_metrics": list(result.get("available_metrics", [])),
        "missing_metrics": list(result.get("missing_metrics", [])),
        "weights": dict(result.get("weights", {})),
        "variant": str(result.get("variant", "") or ""),
    }


def assess_bootstrap_primary_metrics(
    *,
    sample_summary: Dict[str, Any],
    backend: str,
) -> Dict[str, Any]:
    backend_name = str(backend or "").strip().lower()
    primary_metrics = (
        ("sinr_db", "SINR"),
        ("rsrp_db", "RSRP"),
    )
    qualifier_metrics = (
        ("ri", "RI"),
        ("ca_avg", "CA activity"),
    )
    available_names: List[str] = []
    missing_names: List[str] = []
    for key, label in primary_metrics:
        if metric_presence(sample_summary, key):
            available_names.append(label)
        else:
            missing_names.append(label)

    present = len(available_names)
    coverage_ratio = float(present) / float(len(primary_metrics)) if primary_metrics else 0.0
    qualifier_available = [
        label
        for key, label in qualifier_metrics
        if (metric_presence(sample_summary, key) or summary_scalar(sample_summary, key) is not None)
    ]
    min_required = 2
    coverage_ready = present >= min_required
    confidence = min(1.0, max(0.0, coverage_ratio))

    if coverage_ready:
        headline = "Primary readings are good enough for the antenna reference"
        summary = "Antenna Guide can rely on core signal readings. Extra details stay as helpers when available."
    else:
        headline = "Primary readings are still too thin for the antenna reference"
        summary = "Collect more trustworthy signal readings before saving this setup."

    return {
        "backend": backend_name,
        "primary_metrics_expected": [label for _, label in primary_metrics],
        "primary_metrics_available": available_names,
        "primary_metrics_missing": missing_names,
        "qualifier_metrics_available": qualifier_available,
        "primary_metric_count": int(present),
        "primary_metric_min_required": int(min_required),
        "primary_metric_coverage_ratio": float(coverage_ratio),
        "primary_metric_confidence": float(confidence),
        "primary_metrics_ready": bool(coverage_ready),
        "headline": str(headline),
        "summary": str(summary),
    }


def compare_candidate_to_reference(
    *,
    candidate_summary: Dict[str, Any],
    reference_summary: Dict[str, Any],
    workflow_mode: str,
    backend: str = "",
) -> Dict[str, Any]:
    cand_sinr = metric_avg(candidate_summary, "sinr_db")
    ref_sinr = metric_avg(reference_summary, "sinr_db")
    cand_rsrp = metric_avg(candidate_summary, "rsrp_db")
    ref_rsrp = metric_avg(reference_summary, "rsrp_db")
    cand_ri = metric_avg(candidate_summary, "ri")
    ref_ri = metric_avg(reference_summary, "ri")
    cand_bw = metric_avg(candidate_summary, "bw_mhz")
    ref_bw = metric_avg(reference_summary, "bw_mhz")
    cand_ca_persist = summary_scalar(candidate_summary, "ca_avg")
    ref_ca_persist = summary_scalar(reference_summary, "ca_avg")
    candidate_band = int(summary_scalar(candidate_summary, "dominant_context_band") or 0)
    reference_band = int(summary_scalar(reference_summary, "dominant_context_band") or 0)
    candidate_cell = int(summary_scalar(candidate_summary, "dominant_context_cell") or 0)
    reference_cell = int(summary_scalar(reference_summary, "dominant_context_cell") or 0)
    candidate_context_pure = bool(candidate_summary.get("context_pure", False))
    reference_context_pure = bool(reference_summary.get("context_pure", False))
    same_context = bool(
        candidate_context_pure
        and reference_context_pure
        and candidate_band > 0
        and candidate_cell > 0
        and candidate_band == reference_band
        and candidate_cell == reference_cell
    )

    score = 0
    comparable_metrics = 0
    contributors: List[Dict[str, Any]] = []
    weighted_score = 0.0
    weighted_total = 0.0

    if not same_context:
        return {
            "score": 0,
            "weighted_score": 0.0,
            "weighted_threshold": float(BACKGROUND_WEIGHTED_APPROVAL if workflow_mode == PHASE_BACKGROUND else COMPARE_WEIGHTED_APPROVAL),
            "verdict": "inconclusive",
            "comparable_metrics": 0,
            "contributors": [],
            "stability_penalty": 0.0,
            "stability_penalty_max": float(BACKGROUND_MAX_STABILITY_PENALTY if workflow_mode == PHASE_BACKGROUND else COMPARE_MAX_STABILITY_PENALTY),
            "stability_rows": [],
            "confidence": 0.0,
            "confidence_threshold": float(BACKGROUND_MIN_CONFIDENCE if workflow_mode == PHASE_BACKGROUND else COMPARE_MIN_CONFIDENCE),
            "comparable_metric_min_required": int(BACKGROUND_MIN_COMPARABLE_METRICS if workflow_mode == PHASE_BACKGROUND else COMPARE_MIN_COMPARABLE_METRICS),
            "comparable_metrics_ready": False,
            "confidence_ready": False,
            "stability_ready": False,
            "candidate_network_load_biased_ratio": 0.0,
            "reference_network_load_biased_ratio": 0.0,
            "candidate_ca_persistence_ratio": float(cand_ca_persist or 0.0),
            "reference_ca_persistence_ratio": float(ref_ca_persist or 0.0),
            "candidate_offpeak_ratio": 0.0,
            "offpeak_ready": True,
            "offpeak_confirmation_required": False,
            "offpeak_confirmation_reason": "",
            "anchor_survey_mode": bool(workflow_mode == PHASE_ANCHOR_SURVEY),
            "approval_ready": False,
            "context_ready": False,
            "context_reason": "candidate and reference must use the same band and cell/PCI",
            "candidate_band": int(candidate_band),
            "candidate_cell": int(candidate_cell),
            "reference_band": int(reference_band),
            "reference_cell": int(reference_cell),
        }

    candidate_bq = score_band_quality(
        sinr_db=cand_sinr,
        rsrp_db=cand_rsrp,
        ri=cand_ri,
        ca_value=cand_ca_persist,
        bw_mhz=cand_bw,
    )
    reference_bq = score_band_quality(
        sinr_db=ref_sinr,
        rsrp_db=ref_rsrp,
        ri=ref_ri,
        ca_value=ref_ca_persist,
        bw_mhz=ref_bw,
    )

    candidate_components = dict(candidate_bq.get("components", {}) or {})
    reference_components = dict(reference_bq.get("components", {}) or {})
    for metric in ("sinr", "rsrp", "ri", "ca", "bw"):
        cand_component = candidate_components.get(metric)
        ref_component = reference_components.get(metric)
        if not isinstance(cand_component, dict) or not isinstance(ref_component, dict):
            continue
        comparable_metrics += 1
        cand_norm = float(cand_component.get("normalized", 0.0) or 0.0)
        ref_norm = float(ref_component.get("normalized", 0.0) or 0.0)
        delta = float(cand_norm - ref_norm)
        if delta > 0.0:
            score += 1
        elif delta < 0.0:
            score -= 1
        weight = float(cand_component.get("weight", 0.0) or 0.0)
        weighted = float(weight) * clamp(delta)
        weighted_score += weighted
        if abs(delta) > 1e-9:
            weighted_total += weight
        contributors.append(
            {
                "metric": str(metric),
                "candidate": cand_component.get("raw"),
                "reference": ref_component.get("raw"),
                "candidate_normalized": float(cand_norm),
                "reference_normalized": float(ref_norm),
                "delta": float(delta),
                "weighted": float(weighted),
            }
        )

    stability_rows: List[Dict[str, Any]] = []
    stability_penalty = 0.0
    for metric, penalty_cut, weight in (
        ("sinr_db", 6.0, 0.08),
        ("rsrp_db", 8.0, 0.04),
        ("ri", 0.75, 0.03),
        ("ca_persistence_ratio", 0.50, 0.03),
    ):
        if metric == "ca_persistence_ratio":
            cand_spread = None
            ref_spread = None
        else:
            cand_spread = metric_spread(candidate_summary, metric)
            ref_spread = metric_spread(reference_summary, metric)
        if cand_spread is None:
            continue
        ref_val = float(ref_spread or 0.0)
        extra_spread = max(0.0, float(cand_spread) - ref_val)
        penalty = min(float(weight), max(0.0, extra_spread / float(max(penalty_cut, 1e-9))) * float(weight))
        if penalty > 0.0:
            stability_penalty += penalty
        stability_rows.append(
            {
                "metric": str(metric),
                "candidate_spread": float(cand_spread),
                "reference_spread": float(ref_val),
                "penalty": float(penalty),
            }
        )

    normalized_weighted = 0.0 if weighted_total <= 0.0 else (weighted_score / weighted_total) - stability_penalty
    possible_metric_count = float(
        max(
            1,
            len(set(candidate_bq.get("available_metrics", [])) | set(reference_bq.get("available_metrics", []))),
        )
    )
    confidence = min(1.0, max(0.0, comparable_metrics / possible_metric_count))

    if comparable_metrics <= 0:
        verdict = "inconclusive"
    elif score >= COMPARATIVE_SCORE_BETTER and normalized_weighted >= COMPARE_WEIGHTED_VERDICT_BETTER:
        verdict = "better"
    elif score <= -COMPARATIVE_SCORE_BETTER and normalized_weighted <= COMPARE_WEIGHTED_VERDICT_WORSE:
        verdict = "worse"
    elif abs(normalized_weighted) <= COMPARE_WEIGHTED_EQUIVALENT_BAND:
        verdict = "equivalent"
    else:
        verdict = "mixed"

    background_mode = workflow_mode == PHASE_BACKGROUND
    anchor_survey_mode = workflow_mode == PHASE_ANCHOR_SURVEY
    better_threshold = BACKGROUND_SCORE_BETTER if background_mode else COMPARATIVE_SCORE_BETTER
    weighted_threshold = BACKGROUND_WEIGHTED_APPROVAL if background_mode else COMPARE_WEIGHTED_APPROVAL
    comparable_metric_min_required = (
        BACKGROUND_MIN_COMPARABLE_METRICS if background_mode else COMPARE_MIN_COMPARABLE_METRICS
    )
    confidence_threshold = BACKGROUND_MIN_CONFIDENCE if background_mode else COMPARE_MIN_CONFIDENCE
    stability_penalty_max = (
        BACKGROUND_MAX_STABILITY_PENALTY if background_mode else COMPARE_MAX_STABILITY_PENALTY
    )
    comparable_metrics_ready = comparable_metrics >= comparable_metric_min_required
    confidence_ready = confidence >= confidence_threshold
    stability_ready = stability_penalty <= stability_penalty_max
    candidate_network_load_biased_ratio = float(
        summary_scalar(candidate_summary, "network_load_biased_ratio") or 0.0
    )
    reference_network_load_biased_ratio = float(
        summary_scalar(reference_summary, "network_load_biased_ratio") or 0.0
    )
    candidate_offpeak_ratio = float(summary_scalar(candidate_summary, "offpeak_ratio") or 0.0)
    rf_improved = sum(
        1
        for row in contributors
        if str(row.get("metric", "") or "") in {"sinr", "rsrp", "ri", "ca"}
        and float(row.get("weighted", 0.0) or 0.0) > 0.0
    )
    transport_improved = any(
        str(row.get("metric", "") or "") in {"rtt_ms", "throughput_dl_mbps"}
        and float(row.get("weighted", 0.0) or 0.0) > 0.0
        for row in contributors
    )
    offpeak_ready = candidate_offpeak_ratio >= 0.50
    offpeak_confirmation_required = bool(
        background_mode
        and verdict == "better"
        and not offpeak_ready
        and (
            candidate_network_load_biased_ratio >= 0.50
            or (transport_improved and rf_improved <= 0)
        )
    )
    offpeak_confirmation_reason = ""
    if offpeak_confirmation_required:
        if candidate_network_load_biased_ratio >= 0.50:
            offpeak_confirmation_reason = (
                "candidate looks load-biased and still needs an off-peak confirmation window"
            )
        else:
            offpeak_confirmation_reason = (
                "candidate improved mainly on throughput/RTT without matching RF improvement outside off-peak"
            )
    approval_ready = bool(
        not anchor_survey_mode
        and verdict == "better"
        and score >= better_threshold
        and normalized_weighted >= weighted_threshold
        and comparable_metrics_ready
        and confidence_ready
        and stability_ready
        and not offpeak_confirmation_required
    )
    return {
        "score": int(score),
        "weighted_score": float(normalized_weighted),
        "weighted_threshold": float(weighted_threshold),
        "verdict": str(verdict),
        "comparable_metrics": int(comparable_metrics),
        "contributors": contributors,
        "stability_penalty": float(min(0.20, stability_penalty)),
        "stability_penalty_max": float(stability_penalty_max),
        "stability_rows": stability_rows,
        "confidence": float(confidence),
        "confidence_threshold": float(confidence_threshold),
        "comparable_metric_min_required": int(comparable_metric_min_required),
        "comparable_metrics_ready": bool(comparable_metrics_ready),
        "confidence_ready": bool(confidence_ready),
        "stability_ready": bool(stability_ready),
        "candidate_network_load_biased_ratio": float(candidate_network_load_biased_ratio),
        "reference_network_load_biased_ratio": float(reference_network_load_biased_ratio),
        "candidate_ca_persistence_ratio": float(cand_ca_persist or 0.0),
        "reference_ca_persistence_ratio": float(ref_ca_persist or 0.0),
        "candidate_offpeak_ratio": float(candidate_offpeak_ratio),
        "offpeak_ready": bool(offpeak_ready),
        "offpeak_confirmation_required": bool(offpeak_confirmation_required),
        "offpeak_confirmation_reason": str(offpeak_confirmation_reason),
        "anchor_survey_mode": bool(anchor_survey_mode),
        "approval_ready": bool(approval_ready),
        "context_ready": True,
        "context_reason": "",
        "candidate_band": int(candidate_band),
        "candidate_cell": int(candidate_cell),
        "reference_band": int(reference_band),
        "reference_cell": int(reference_cell),
        "candidate_quality_score": float(candidate_bq.get("quality_score", 0.0) or 0.0),
        "reference_quality_score": float(reference_bq.get("quality_score", 0.0) or 0.0),
        "quality_variant": str(candidate_bq.get("variant", "") or reference_bq.get("variant", "") or ""),
    }
