"""Pure structural band-quality calculation shared across product roots."""

from __future__ import annotations

from typing import Any, Dict, Optional

BASE_BQ_WEIGHTS: Dict[str, float] = {
    "sinr": 0.225,
    "rsrp": 0.135,
    "ri": 0.225,
    "ca": 0.215,
    "bw": 0.200,
}


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def normalize_sinr_db(value: Any) -> Optional[float]:
    out = safe_float(value)
    if out is None:
        return None
    return clamp01((float(out) + 5.0) / 25.0)


def normalize_rsrp_db(value: Any) -> Optional[float]:
    out = safe_float(value)
    if out is None:
        return None
    return clamp01((float(out) + 120.0) / 60.0)


def normalize_ri(value: Any) -> Optional[float]:
    out = safe_float(value)
    if out is None:
        return None
    if float(out) <= 0.0:
        return None
    return clamp01((float(out) - 1.0) / 1.0)


def normalize_ca(value: Any) -> Optional[float]:
    out = safe_float(value)
    if out is None:
        return None
    return clamp01(float(out))


def normalize_bw_mhz(value: Any) -> Optional[float]:
    out = safe_float(value)
    if out is None:
        return None
    return clamp01(float(out) / 20.0)


def resolve_bq_weights(*, supports_ca: bool, supports_bw: bool) -> Dict[str, float]:
    enabled = {
        "sinr": True,
        "rsrp": True,
        "ri": True,
        "ca": bool(supports_ca),
        "bw": bool(supports_bw),
    }
    total = sum(weight for name, weight in BASE_BQ_WEIGHTS.items() if enabled.get(name, False))
    if total <= 0.0:
        return {}
    return {
        name: float(weight) / float(total)
        for name, weight in BASE_BQ_WEIGHTS.items()
        if enabled.get(name, False)
    }


def score_band_quality(
    *,
    sinr_db: Any = None,
    rsrp_db: Any = None,
    ri: Any = None,
    ca_value: Any = None,
    bw_mhz: Any = None,
    supports_ca: Optional[bool] = None,
    supports_bw: Optional[bool] = None,
) -> Dict[str, Any]:
    if supports_ca is None:
        supports_ca = ca_value is not None
    if supports_bw is None:
        supports_bw = safe_float(bw_mhz) is not None

    weights = resolve_bq_weights(
        supports_ca=bool(supports_ca),
        supports_bw=bool(supports_bw),
    )
    raw_map = {
        "sinr": safe_float(sinr_db),
        "rsrp": safe_float(rsrp_db),
        "ri": safe_float(ri),
        "ca": safe_float(ca_value) if bool(supports_ca) else None,
        "bw": safe_float(bw_mhz) if bool(supports_bw) else None,
    }
    norm_map = {
        "sinr": normalize_sinr_db(sinr_db),
        "rsrp": normalize_rsrp_db(rsrp_db),
        "ri": normalize_ri(ri),
        "ca": normalize_ca(ca_value) if bool(supports_ca) else None,
        "bw": normalize_bw_mhz(bw_mhz) if bool(supports_bw) else None,
    }

    components: Dict[str, Dict[str, Any]] = {}
    weighted_sum = 0.0
    used_weight = 0.0
    available = []
    missing = []
    for name in ("sinr", "rsrp", "ri", "ca", "bw"):
        if name not in weights:
            continue
        normalized = norm_map.get(name)
        raw = raw_map.get(name)
        weight = float(weights.get(name, 0.0) or 0.0)
        if normalized is None:
            missing.append(name)
            continue
        weighted = float(normalized) * weight
        weighted_sum += weighted
        used_weight += weight
        available.append(name)
        components[name] = {
            "raw": float(raw) if raw is not None else None,
            "normalized": float(normalized),
            "weight": float(weight),
            "weighted": float(weighted),
        }

    score = 0.0 if used_weight <= 0.0 else clamp01(weighted_sum / used_weight)
    if bool(supports_ca) and bool(supports_bw):
        variant = "with_bw_with_ca"
    elif bool(supports_bw):
        variant = "with_bw_no_ca"
    elif bool(supports_ca):
        variant = "no_bw_with_ca"
    else:
        variant = "no_bw_no_ca"

    return {
        "quality_score": float(score),
        "quality_pct": int(round(100.0 * score)),
        "metric_count": int(len(available)),
        "available_metrics": list(available),
        "missing_metrics": list(missing),
        "weights": {k: float(v) for k, v in weights.items()},
        "components": components,
        "supports_ca": bool(supports_ca),
        "supports_bw": bool(supports_bw),
        "variant": variant,
    }
