"""Canonical query owner for learned band-memory projections.

Memory owns durable containers. This module owns the query/scoring algorithms
that turn those containers into learned-band evidence for projection owners.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Mapping, Optional

from arch4.brain.memory_calibration_context_owner import resolve_marker_alias
from arch4.brain.memory_action_history_owner import (
    action_history_records,
    iter_action_history_reversed,
)
from arch4.brain.congestion_signals import QUIET_ENTRY_RTT_DELTA_MOD_MS
from arch4.interfaces import LeverType

OWNER = "arch4.brain.memory_band_learning_query_store"


def _norm_bands(bands: Iterable[Any]) -> List[int]:
    out: List[int] = []
    for raw in bands or []:
        try:
            band = int(raw)
        except (TypeError, ValueError):
            continue
        if band > 0:
            out.append(band)
    return sorted(set(out))


def normalize_band_list(bands: Iterable[Any]) -> List[int]:
    return _norm_bands(bands)


def _resolve_marker(memory: Any) -> str:
    marker = str(getattr(memory, "current_calibration_marker", "") or "C0")
    return resolve_marker_alias(memory, marker)


def _marker_key(memory: Any, marker: str, key: str) -> str:
    m = str(marker or "C0").strip() or "C0"
    return f"{m}|{key}" if key else f"{m}|"


def _band_learning_key(hour: int, bands: Iterable[Any]) -> str:
    sig = "+".join(f"B{band}" for band in _norm_bands(bands))
    return f"h{int(hour):02d}:{sig}" if sig else ""


def band_learning_key(hour: int, bands: Iterable[Any]) -> str:
    return _band_learning_key(hour, bands)


def marker_scoped_band_learning_key(memory: Any, marker: str, hour: int, bands: Iterable[Any]) -> str:
    return _marker_key(memory, marker, _band_learning_key(hour, bands))


def read_bucket_score(bucket: Dict[str, Any], step_size: float = 0.05) -> float:
    if int(bucket.get("n", 0) or 0) > 0:
        return float(bucket.get("score", 0.5) or 0.5)
    good = float(bucket.get("good", 0.0) or 0.0)
    bad = float(bucket.get("bad", 0.0) or 0.0)
    if good <= 0.0 and bad <= 0.0:
        return float(bucket.get("score", 0.5) or 0.5)
    return max(0.0, min(1.0, 0.5 + (good * step_size) - (bad * step_size)))


def band_seed_score(memory: Any, norm_bands: Iterable[Any], bw_mhz: float = 0.0) -> float:
    bands = _norm_bands(norm_bands)
    priority = dict(getattr(memory, "band_priority", {}) or {})
    if not priority or not bands:
        return 0.5
    scores = [float(priority.get(band, 0.5) or 0.5) for band in bands]
    seed = sum(scores) / len(scores)
    bw = float(bw_mhz or 0.0)
    if bw >= 20.0:
        bw_factor = 1.0
    elif bw >= 15.0:
        bw_factor = 0.875
    elif bw >= 10.0:
        bw_factor = 0.75
    elif bw > 0.0:
        bw_factor = 0.5
    else:
        bw_factor = 1.0
    return max(0.0, min(1.0, seed * bw_factor))


def should_try_band(memory: Any, band: int) -> bool:
    bk = getattr(memory, "bands", {}).get(int(band or 0))
    if bk is None:
        return True
    if not bool(getattr(bk, "never_works", False)):
        return True
    elapsed = time.time() - float(getattr(bk, "last_tried", 0.0) or 0.0)
    learning_cfg = getattr(memory, "learning_cfg", None)
    retry_s = float(getattr(learning_cfg, "band_retry_interval_s", 3600.0) or 3600.0)
    return elapsed > retry_s


def should_block_band_action(
    memory: Any,
    hour: int,
    bands: List[int],
    *,
    now: Optional[float] = None,
    current_sinr: Optional[float] = None,
) -> tuple[bool, str]:
    cfg = getattr(memory, "learning_cfg", None)
    if not bool(getattr(cfg, "learning_enabled", True)):
        return False, ""
    ts = time.time() if now is None else float(now)
    norm = _norm_bands(bands)
    if not norm:
        return False, ""
    marker = _resolve_marker(memory)
    key = _marker_key(memory, marker, _band_learning_key(int(hour), norm))
    seen_actions = int(getattr(memory, "current_context_seen_actions", 0) or 0)
    warmup_attempts = int(getattr(cfg, "learning_marker_warmup_attempts", 0) or 0)
    if seen_actions < warmup_attempts:
        b3_key = _marker_key(memory, marker, f"h{int(hour):02d}:B3_PRIMARY")
        if not getattr(memory, "band_learning_stm", {}).get(key) and not getattr(memory, "band_learning_ltm", {}).get(key) and not (
            3 in norm and (getattr(memory, "band_learning_stm", {}).get(b3_key) or getattr(memory, "band_learning_ltm", {}).get(b3_key))
        ):
            return False, "marker warmup"

    def _can_retry_on_improved_rf(bucket: Dict[str, Any]) -> bool:
        if current_sinr is None:
            return False
        last_ts = float(bucket.get("last_ts", 0.0) or 0.0)
        if last_ts <= 0.0:
            return False
        retry_min_s = float(getattr(cfg, "learning_retry_min_elapsed_s", 0.0) or 0.0)
        if (ts - last_ts) < retry_min_s:
            return False
        last_sinr = float(bucket.get("last_sinr", 0.0) or 0.0)
        retry_db = float(getattr(cfg, "learning_retry_sinr_improvement_db", 0.0) or 0.0)
        return float(current_sinr) >= (last_sinr + retry_db)

    stm = getattr(memory, "band_learning_stm", {}).get(key)
    if stm:
        attempts = float(stm.get("attempts", 0.0) or 0.0)
        bad = float(stm.get("bad", 0.0) or 0.0)
        hard = float(stm.get("hard_fail", 0.0) or 0.0)
        last = float(stm.get("last_ts", 0.0) or 0.0)
        hard_count = float(getattr(cfg, "learning_stm_hard_fail_block_count", 1.0) or 1.0)
        hard_window = float(getattr(cfg, "learning_stm_hard_fail_block_s", 3600.0) or 3600.0)
        if hard >= hard_count and (ts - last) <= hard_window:
            if _can_retry_on_improved_rf(stm):
                return False, "STM retry allowed by improved RF"
            return True, f"STM hard-fail guard for {key}"
        min_attempts = float(getattr(cfg, "learning_stm_min_attempts", 2.0) or 2.0)
        threshold = float(getattr(cfg, "learning_stm_bad_rate_threshold", 0.70) or 0.70)
        if attempts >= min_attempts:
            bad_rate = bad / max(1.0, attempts)
            if bad_rate >= threshold:
                return True, f"STM bad-rate {bad_rate:.2f} for {key}"

    if 3 in norm:
        b3_key = _marker_key(memory, marker, f"h{int(hour):02d}:B3_PRIMARY")
        b3_bucket = getattr(memory, "band_learning_stm", {}).get(b3_key)
        if b3_bucket:
            attempts = float(b3_bucket.get("attempts", 0.0) or 0.0)
            bad = float(b3_bucket.get("bad", 0.0) or 0.0)
            hard = float(b3_bucket.get("hard_fail", 0.0) or 0.0)
            last = float(b3_bucket.get("last_ts", 0.0) or 0.0)
            hard_count = float(getattr(cfg, "learning_stm_hard_fail_block_count", 1.0) or 1.0)
            hard_window = float(getattr(cfg, "learning_stm_hard_fail_block_s", 3600.0) or 3600.0)
            if hard >= hard_count and (ts - last) <= hard_window:
                if _can_retry_on_improved_rf(b3_bucket):
                    return False, "STM B3-primary retry allowed by improved RF"
                return True, "STM B3-primary hard-fail guard"
            min_attempts = float(getattr(cfg, "learning_stm_min_attempts", 2.0) or 2.0)
            threshold = float(getattr(cfg, "learning_stm_bad_rate_threshold", 0.70) or 0.70)
            if attempts >= min_attempts:
                bad_rate = bad / max(1.0, attempts)
                if bad_rate >= threshold:
                    return True, f"STM B3-primary bad-rate {bad_rate:.2f}"

    ltm = getattr(memory, "band_learning_ltm", {}).get(key)
    if ltm:
        attempts = float(ltm.get("attempts", 0.0) or 0.0)
        bad = float(ltm.get("bad", 0.0) or 0.0)
        min_attempts = float(getattr(cfg, "learning_ltm_min_attempts", 3.0) or 3.0)
        threshold = float(getattr(cfg, "learning_ltm_bad_rate_threshold", 0.70) or 0.70)
        if attempts >= min_attempts:
            bad_rate = bad / max(1.0, attempts)
            if bad_rate >= threshold:
                return True, f"LTM bad-rate {bad_rate:.2f} for {key}"
    return False, ""


def is_probe_exhausted(memory: Any, hour: int, bands: list, *, now: float | None = None) -> bool:
    cfg = getattr(memory, "learning_cfg", None)
    norm = _norm_bands(bands)
    if not norm:
        return False

    marker = _resolve_marker(memory)
    ltm_key = _marker_key(memory, marker, _band_learning_key(hour, norm))

    stm = getattr(memory, "band_learning_stm", {}).get(ltm_key)
    if stm:
        stm_n = float(stm.get("attempts", 0.0) or 0.0)
        stm_bad = float(stm.get("bad", 0.0) or 0.0)
        if stm_n >= float(getattr(cfg, "learning_stm_min_attempts", 2.0) or 2.0):
            bad_rate = stm_bad / max(1.0, stm_n)
            threshold = float(getattr(cfg, "learning_stm_bad_rate_threshold", 0.70) or 0.70)
            if bad_rate >= threshold:
                return True

    ltm = getattr(memory, "band_learning_ltm", {}).get(ltm_key)
    if not ltm:
        return False

    ltm_n = float(ltm.get("attempts", 0.0) or 0.0)
    if ltm_n < float(getattr(cfg, "probe_exhaustion_min_attempts", 2.0) or 2.0):
        return False
    score = read_bucket_score(ltm, step_size=0.05)
    if score >= float(getattr(cfg, "probe_exhaustion_score_floor", 0.35) or 0.35):
        return False
    ts = time.time() if now is None else float(now)
    last_ts = float(ltm.get("last_ts", 0.0) or 0.0)
    recency_s = float(getattr(cfg, "probe_exhaustion_recency_s", 7200.0) or 7200.0)
    if last_ts <= 0.0 or (ts - last_ts) > recency_s:
        return False
    return True


def _iter_known_outage_hour_buckets(
    memory: Any,
    *,
    store: Dict[str, Dict[str, Any]],
    marker: str,
    hour: int,
    min_attempts: float,
    bad_rate_threshold: float,
) -> List[Dict[str, Any]]:
    prefix = _marker_key(memory, marker, f"h{int(hour):02d}:")
    evidence: List[Dict[str, Any]] = []
    for key, bucket in dict(store or {}).items():
        if not str(key).startswith(prefix):
            continue
        tail = str(key)[len(prefix):]
        if not tail or tail.endswith("_PRIMARY"):
            continue
        attempts = float(bucket.get("attempts", 0.0) or 0.0)
        good = float(bucket.get("good", 0.0) or 0.0)
        bad = float(bucket.get("bad", 0.0) or 0.0)
        hard = float(bucket.get("hard_fail", 0.0) or 0.0)
        bad_rate = bad / max(1.0, attempts)
        good_rate = good / max(1.0, attempts)
        if hard < 1.0 and not (
            attempts >= float(min_attempts)
            and bad_rate >= float(bad_rate_threshold)
            and good_rate <= 0.25
        ):
            continue
        evidence.append(
            {
                "bucket_key": str(key),
                "band_sig": tail,
                "attempts": attempts,
                "good": good,
                "bad": bad,
                "hard_fail": hard,
                "bad_rate": bad_rate,
                "last_ts": float(bucket.get("last_ts", 0.0) or 0.0),
                "last_sinr": float(bucket.get("last_sinr", 0.0) or 0.0),
            }
        )
    return evidence


def should_hold_known_outage_hour(
    memory: Any,
    *,
    hour: int,
    now: Optional[float] = None,
    current_sinr: Optional[float] = None,
) -> tuple[bool, str]:
    cfg = getattr(memory, "learning_cfg", None)
    if not bool(getattr(cfg, "learning_enabled", True)):
        return False, ""
    seen_actions = int(getattr(memory, "current_context_seen_actions", 0) or 0)
    warmup_attempts = int(getattr(cfg, "learning_marker_warmup_attempts", 0) or 0)
    if seen_actions < warmup_attempts:
        return False, ""

    ts = time.time() if now is None else float(now)
    marker = _resolve_marker(memory)
    retry_db = float(getattr(cfg, "learning_retry_sinr_improvement_db", 0.0) or 0.0)
    stm_hold_s = max(600.0, float(getattr(cfg, "learning_retry_min_elapsed_s", 0.0) or 0.0))

    def _rf_retry_allowed(evidence: List[Dict[str, Any]]) -> bool:
        if current_sinr is None or not evidence:
            return False
        best_last_sinr = max(float(row.get("last_sinr", 0.0) or 0.0) for row in evidence)
        return float(current_sinr) >= (best_last_sinr + retry_db)

    stm = _iter_known_outage_hour_buckets(
        memory,
        store=getattr(memory, "band_learning_stm", {}),
        marker=marker,
        hour=hour,
        min_attempts=float(getattr(cfg, "learning_stm_min_attempts", 2.0) or 2.0),
        bad_rate_threshold=float(getattr(cfg, "learning_stm_bad_rate_threshold", 0.70) or 0.70),
    )
    if len(stm) >= 2:
        newest_ts = max(float(row.get("last_ts", 0.0) or 0.0) for row in stm)
        if (ts - newest_ts) <= stm_hold_s:
            if _rf_retry_allowed(stm):
                return False, "known outage retry allowed by improved RF"
            return True, f"known outage hour h{int(hour):02d} STM ({len(stm)} failed bandsets)"

    ltm = _iter_known_outage_hour_buckets(
        memory,
        store=getattr(memory, "band_learning_ltm", {}),
        marker=marker,
        hour=hour,
        min_attempts=float(getattr(cfg, "learning_ltm_min_attempts", 3.0) or 3.0),
        bad_rate_threshold=float(getattr(cfg, "learning_ltm_bad_rate_threshold", 0.70) or 0.70),
    )
    if len(ltm) >= 2:
        if _rf_retry_allowed(ltm):
            return False, "known outage retry allowed by improved RF"
        return True, f"known outage hour h{int(hour):02d} LTM ({len(ltm)} failed bandsets)"

    return False, ""


def band_learning_evidence(memory: Any, *, hour: int, bands: list) -> Dict[str, Any]:
    norm = _norm_bands(bands)
    if not norm:
        return {
            "present": False,
            "norm_bands": [],
            "stm_attempts": 0.0,
            "ltm_attempts": 0.0,
            "ltm_avg_primary_bw_mhz": 0.0,
        }
    marker = _resolve_marker(memory)
    key = _marker_key(memory, marker, _band_learning_key(int(hour), norm))
    stm_bucket = dict(getattr(memory, "band_learning_stm", {}).get(key) or {})
    ltm_bucket = dict(getattr(memory, "band_learning_ltm", {}).get(key) or {})
    stm_attempts = float(stm_bucket.get("attempts", 0.0) or 0.0)
    ltm_attempts = float(ltm_bucket.get("attempts", 0.0) or 0.0)
    return {
        "present": bool(stm_bucket or ltm_bucket),
        "norm_bands": list(norm),
        "stm_attempts": stm_attempts,
        "ltm_attempts": ltm_attempts,
        "ltm_avg_primary_bw_mhz": float(ltm_bucket.get("avg_primary_bw_mhz", 0.0) or 0.0),
    }


def band_learning_memory_summary(memory: Any) -> Dict[str, Any]:
    stm_count = len(getattr(memory, "band_learning_stm", {}) or {})
    ltm_count = len(getattr(memory, "band_learning_ltm", {}) or {})
    action_count = len(action_history_records(memory))
    band_count = len(getattr(memory, "bands", {}) or {})
    cell_count = len(getattr(memory, "cells", {}) or {})
    baseline_count = len(getattr(memory, "baselines", {}) or {})
    return {
        "stm_count": int(stm_count),
        "ltm_count": int(ltm_count),
        "action_count": int(action_count),
        "band_count": int(band_count),
        "cell_count": int(cell_count),
        "baseline_count": int(baseline_count),
        "band_learning_empty": (int(stm_count) + int(ltm_count)) == 0,
        "broader_memory_present": any(
            int(v) > 0 for v in (action_count, band_count, cell_count, baseline_count)
        ),
    }


def learned_bandsets_for_hour(
    memory: Any,
    *,
    hour: int,
    min_attempts: float = 2.0,
) -> List[List[int]]:
    """Enumerate evidenced band-set shapes in the active marker/hour scope."""

    marker = _resolve_marker(memory)
    prefix = _marker_key(memory, marker, f"h{int(hour):02d}:")
    stm = getattr(memory, "band_learning_stm", {}) or {}
    ltm = getattr(memory, "band_learning_ltm", {}) or {}
    threshold = max(1.0, float(min_attempts or 0.0))
    candidates: set[tuple[int, ...]] = set()
    for key in sorted(set(stm) | set(ltm)):
        text = str(key or "")
        if not text.startswith(prefix):
            continue
        tail = text[len(prefix):]
        bands = _bandset_from_learning_bucket_tail(tail)
        if not bands:
            continue
        stm_bucket = dict(stm.get(key) or {})
        ltm_bucket = dict(ltm.get(key) or {})
        attempts = max(
            float(
                stm_bucket.get("attempts", stm_bucket.get("n", 0.0))
                or 0.0
            ),
            float(
                ltm_bucket.get("attempts", ltm_bucket.get("n", 0.0))
                or 0.0
            ),
        )
        if attempts >= threshold:
            candidates.add(tuple(bands))
    return [list(bands) for bands in sorted(candidates)]


def _bandset_from_learning_bucket_tail(value: Any) -> List[int]:
    text = str(value or "").strip().upper()
    if not text or text.endswith("_PRIMARY"):
        return []
    bands: list[int] = []
    for token in text.split("+"):
        part = token.strip()
        if not part.startswith("B") or not part[1:].isdigit():
            return []
        band = int(part[1:])
        if band <= 0:
            return []
        bands.append(band)
    return _norm_bands(bands)


def external_memory_band_hour_quality(memory: Mapping[str, Any]) -> Dict[str, Dict[str, float]]:
    """Project durable band-learning stores from persisted memory JSON.

    External-memory projection may use historical learned evidence, but the
    durable store names and scoring rules stay inside the memory query owner.
    """

    if not isinstance(memory, Mapping):
        return {"quality": {}, "counts": {}}
    merged: Dict[tuple[int, int], Dict[str, float]] = {}
    for store, weight in (
        (_mapping_store(memory, "band_learning_ltm"), 1.0),
        (_mapping_store(memory, "band_learning_stm"), 0.6),
    ):
        for key, bucket in store.items():
            parsed = _band_hour_from_learning_bucket_key(key)
            if parsed is None or not isinstance(bucket, Mapping):
                continue
            band, hour = parsed
            attempts = float(bucket.get("n", 0.0) or bucket.get("attempts", 0.0) or 0.0)
            if attempts <= 0.0:
                continue
            score = read_bucket_score(dict(bucket), step_size=0.05)
            row = merged.setdefault((band, hour), {"weighted": 0.0, "count": 0.0})
            evidence = max(1.0, attempts) * float(weight)
            row["weighted"] += score * evidence
            row["count"] += evidence
    quality = {
        f"{band}:{hour}": round(row["weighted"] / max(1.0, row["count"]), 4)
        for (band, hour), row in merged.items()
    }
    counts = {f"{band}:{hour}": min(200, int(row["count"])) for (band, hour), row in merged.items()}
    return {"quality": quality, "counts": counts}


def _mapping_store(memory: Mapping[str, Any], name: str) -> Dict[str, Any]:
    value = memory.get(name)
    return dict(value) if isinstance(value, Mapping) else {}


def _band_hour_from_learning_bucket_key(value: Any) -> tuple[int, int] | None:
    text = str(value or "")
    marker = text.find("h")
    while marker >= 0:
        hour_text = text[marker + 1 : marker + 3]
        colon = text.find(":", marker + 3)
        if colon == marker + 3 and len(hour_text) == 2 and hour_text.isdigit():
            hour = int(hour_text)
            band = _first_band_from_text(text[colon + 1 :])
            if band > 0:
                return band, hour
        marker = text.find("h", marker + 1)
    return None


def _first_band_from_text(value: Any) -> int:
    for token in str(value or "").replace("+", " ").replace("|", " ").split():
        if len(token) >= 2 and token[0].upper() == "B" and token[1:].isdigit():
            band = int(token[1:])
            if band > 0:
                return band
    return 0


def best_known_bands(memory: Any, top_n: int = 4) -> List[int]:
    working = [
        (band, bk)
        for band, bk in getattr(memory, "bands", {}).items()
        if int(getattr(bk, "times_appeared_in_ca", 0) or 0) > 0
    ]
    working.sort(key=lambda item: float(getattr(item[1], "avg_throughput_with", 0.0) or 0.0), reverse=True)
    return [int(band) for band, _bk in working[: int(top_n or 4)]]


def known_good_bandset(memory: Any) -> Optional[List[int]]:
    current_provider = str(getattr(memory, "_current_provider_plmn", "") or "")
    for rec in iter_action_history_reversed(memory):
        if current_provider and getattr(rec, "provider_plmn", ""):
            if rec.provider_plmn != current_provider:
                continue
        if (
            rec.lever == LeverType.BAND_SELECTION
            and bool(getattr(rec, "success", False))
            and float(getattr(rec, "improvement", 0.0) or 0.0) >= 0.0
            and "+" in str(getattr(rec, "ca_signature_after", "") or "")
        ):
            return list(getattr(rec, "action_params", {}).get("bands", []))
    working = [
        int(band)
        for band, bk in getattr(memory, "bands", {}).items()
        if int(getattr(bk, "times_appeared_in_ca", 0) or 0) > 0
    ]
    return sorted(working) if working else None


def ranked_bandsets_for_hour(
    memory: Any,
    hour: int,
    candidate_sets: List[List[int]],
    *,
    min_attempts: float = 2.0,
    rtt_pressure_level: str = "normal",
    rtt_delta: float = 0.0,
    rtt_delta_valid: bool = False,
    rsrq_gap: float = 0.0,
    rsrq_gap_valid: bool = False,
) -> List[tuple]:
    marker = _resolve_marker(memory)
    congestion_active = (
        (rtt_delta_valid and rtt_delta >= QUIET_ENTRY_RTT_DELTA_MOD_MS)
        or ((not rtt_delta_valid) and rtt_pressure_level in {"busy", "alarm"})
        or (rsrq_gap_valid and rsrq_gap > 4.0)
    )
    results = []
    for bands in candidate_sets:
        norm = _norm_bands(bands)
        if not norm:
            continue
        ltm_key = _marker_key(memory, marker, _band_learning_key(hour, norm))
        stm_bucket = getattr(memory, "band_learning_stm", {}).get(ltm_key)
        stm_n = int(stm_bucket.get("n", 0) or stm_bucket.get("attempts", 0)) if stm_bucket else 0
        stm_score = 0.5
        has_stm = stm_bucket is not None and stm_n >= 1
        if has_stm:
            stm_score = read_bucket_score(stm_bucket, step_size=0.1)

        ltm_bucket = getattr(memory, "band_learning_ltm", {}).get(ltm_key)
        ltm_n = int(ltm_bucket.get("n", 0) or ltm_bucket.get("attempts", 0)) if ltm_bucket else 0
        ltm_score = 0.5
        has_ltm = ltm_bucket is not None and ltm_n >= min_attempts
        if has_ltm:
            ltm_score = read_bucket_score(ltm_bucket, step_size=0.05)

        if has_stm and has_ltm:
            stm_w = min(0.70, 0.30 + 0.08 * min(5.0, float(stm_n)))
            blended = stm_w * stm_score + (1.0 - stm_w) * ltm_score
            source = "stm+ltm"
            total_n = stm_n + ltm_n
        elif has_stm:
            blended = stm_score
            source = "stm"
            total_n = stm_n
        elif has_ltm:
            blended = ltm_score
            source = "ltm"
            total_n = ltm_n
        else:
            sig = "+".join(f"B{band}" for band in norm)
            conf = getattr(memory, "band_confidence_ltm", {}).get(sig)
            if conf and int(conf.get("chosen_count", 0) or 0) >= int(min_attempts):
                blended = float(conf.get("score", 0.5) or 0.5)
                total_n = float(conf.get("chosen_count", 0) or 0)
                source = "confidence_ltm"
            else:
                blended = band_seed_score(memory, norm)
                total_n = 0.0
                source = "untested"

        if congestion_active and has_stm and stm_score < 0.35:
            blended = min(blended, 0.25)
            source = source + "_congestion_veto"

        results.append((list(norm), round(blended, 4), total_n, source))
    results.sort(key=lambda row: (row[1], row[2]), reverse=True)
    return results


def best_bandset_for_hour(
    memory: Any,
    hour: int,
    candidate_sets: List[List[int]],
    *,
    min_attempts: float = 2.0,
    min_score: float = 0.55,
    rtt_pressure_level: str = "normal",
    rtt_delta: float = 0.0,
    rtt_delta_valid: bool = False,
    rsrq_gap: float = 0.0,
    rsrq_gap_valid: bool = False,
) -> Optional[List[int]]:
    ranked = ranked_bandsets_for_hour(
        memory,
        hour,
        candidate_sets,
        min_attempts=min_attempts,
        rtt_pressure_level=rtt_pressure_level,
        rtt_delta=rtt_delta,
        rtt_delta_valid=rtt_delta_valid,
        rsrq_gap=rsrq_gap,
        rsrq_gap_valid=rsrq_gap_valid,
    )
    if not ranked:
        return None
    top_bands, top_score, _top_attempts, top_source = ranked[0]
    if "untested" in top_source or top_score < min_score:
        return None
    return top_bands


__all__ = [
    "OWNER",
    "band_learning_evidence",
    "band_learning_memory_summary",
    "band_seed_score",
    "best_bandset_for_hour",
    "best_known_bands",
    "external_memory_band_hour_quality",
    "is_probe_exhausted",
    "known_good_bandset",
    "learned_bandsets_for_hour",
    "ranked_bandsets_for_hour",
    "read_bucket_score",
    "should_block_band_action",
    "should_hold_known_outage_hour",
    "should_try_band",
]
