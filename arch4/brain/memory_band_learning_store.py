"""Band-learning bucket mutation adapter.

Memory owns durable containers. This module owns how classified action proof
changes STM/LTM learning buckets.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from arch4.brain.delivered_load_memory import attach_dlst, empty_dlst, normalize_dlst
from arch4.brain.memory_calibration_context_owner import resolve_marker_alias
from arch4.brain.memory_band_learning_query_store import (
    band_seed_score,
    marker_scoped_band_learning_key,
    normalize_band_list,
)
from arch4.policy.incident_learning_policy import adapt_band_learning_outcome

OWNER = "arch4.brain.memory_band_learning_store"


def record_band_learning(
    memory: Any,
    *,
    timestamp: float,
    bands: List[int],
    outcome_label: str,
    hard_fail: bool,
    primary_after: int,
    sinr_after: float,
    primary_bw_mhz: float = 0.0,
    incident_assessment: Optional[Dict[str, Any]] = None,
    ca_active: bool = False,
    ri: int = 0,
    bq_cell: float = 0.0,
    cq_cell: float = 0.0,
    nr_anchor: bool = False,
    use_ltm: bool = True,
    dlst: Optional[Dict[str, Any]] = None,
    marker: Optional[str] = None,
    increment_current_context_seen_actions: bool = True,
    grade_weight: float = 1.0,
) -> Dict[str, Any]:
    """Apply one classified action outcome to STM/LTM bucket stores."""

    if increment_current_context_seen_actions:
        memory.current_context_seen_actions = int(getattr(memory, "current_context_seen_actions", 0) or 0) + 1
    marker = _resolve_marker(memory, marker or getattr(memory, "current_calibration_marker", "C0"))
    contexts = getattr(memory, "calibration_contexts", {})
    ctx = contexts.get(marker) if isinstance(contexts, dict) else None
    if ctx is not None:
        ctx["seen_actions"] = int(ctx.get("seen_actions", 0) or 0) + 1
        contexts[marker] = ctx
    norm = normalize_band_list(bands)
    if not norm:
        return {"use_ltm": False, "reason": "no bands"}
    dlst_record = normalize_dlst(dlst)
    hour = time.localtime(float(timestamp)).tm_hour
    key = marker_scoped_band_learning_key(memory, marker, hour, norm)
    now = float(timestamp)
    learning = adapt_band_learning_outcome(
        outcome_label=outcome_label,
        hard_fail=hard_fail,
        assessment=incident_assessment,
    )
    seed = band_seed_score(memory, norm, bw_mhz=primary_bw_mhz)
    component_kwargs = dict(
        ca_active=ca_active,
        ri=ri,
        bq_cell=bq_cell,
        cq_cell=cq_cell,
        nr_anchor=nr_anchor,
    )
    weight = max(1.0, min(3.0, float(grade_weight or 1.0)))

    update_learning_bucket(
        memory.band_learning_stm,
        key,
        learning["effective_label"],
        learning["effective_hard_fail"],
        now,
        float(sinr_after),
        primary_bw_mhz=float(primary_bw_mhz),
        step_size=0.1,
        seed_score=seed,
        dlst=dlst_record,
        grade_weight=weight,
        **component_kwargs,
    )
    effective_use_ltm = bool(learning["use_ltm"]) and bool(use_ltm)
    if effective_use_ltm:
        consolidate_learning_bucket_to_ltm(
            memory,
            key,
            learning["effective_label"],
            learning["effective_hard_fail"],
            now,
            float(sinr_after),
            primary_bw_mhz=float(primary_bw_mhz),
            seed_score=seed,
            dlst=dlst_record,
            grade_weight=weight,
            **component_kwargs,
        )
    if 3 in norm and int(primary_after or 0) == 3:
        b3k = f"{marker}|h{int(hour):02d}:B3_PRIMARY"
        update_learning_bucket(
            memory.band_learning_stm,
            b3k,
            learning["effective_label"],
            learning["effective_hard_fail"],
            now,
            float(sinr_after),
            step_size=0.1,
            seed_score=seed,
            dlst=dlst_record,
            grade_weight=weight,
            **component_kwargs,
        )
        if effective_use_ltm:
            consolidate_learning_bucket_to_ltm(
                memory,
                b3k,
                learning["effective_label"],
                learning["effective_hard_fail"],
                now,
                float(sinr_after),
                seed_score=seed,
                dlst=dlst_record,
                grade_weight=weight,
                **component_kwargs,
            )
    prune_learning_stm(memory, now)
    reason = str(learning["reason"] or "")
    if bool(learning["use_ltm"]) and not bool(use_ltm):
        reason = _append_reason(reason, "referee_ltm_disabled")
    return {
        "use_ltm": bool(effective_use_ltm),
        "reason": reason,
        "grade_weight": weight,
    }


def update_learning_bucket(
    store: Dict[str, Dict[str, Any]],
    key: str,
    outcome_label: str,
    hard_fail: bool,
    now: float,
    sinr_after: float,
    primary_bw_mhz: float = 0.0,
    ca_active: bool = False,
    ri: int = 0,
    bq_cell: float = 0.0,
    cq_cell: float = 0.0,
    nr_anchor: bool = False,
    step_size: float = 0.1,
    seed_score: float = 0.5,
    dlst: Optional[Dict[str, Any]] = None,
    grade_weight: float = 1.0,
) -> None:
    if not key:
        return
    bucket = store.get(key)
    if not bucket:
        bucket = empty_learning_bucket()
        bucket["score"] = seed_score
        store[key] = bucket
    attach_dlst(bucket, normalize_dlst(dlst))

    old_score = float(bucket.get("score", 0.5))
    weight = max(1.0, min(3.0, float(grade_weight or 1.0)))
    if outcome_label == "good":
        bucket["score"] = min(1.0, old_score + (step_size * weight))
    elif outcome_label == "bad":
        bucket["score"] = max(0.0, old_score - (step_size * weight))

    bucket["n"] = int(bucket.get("n", 0)) + 1
    bucket["attempts"] = float(bucket.get("attempts", 0.0)) + 1.0
    if outcome_label == "good":
        bucket["good"] = float(bucket.get("good", 0.0)) + weight
    elif outcome_label == "bad":
        bucket["bad"] = float(bucket.get("bad", 0.0)) + weight
    if hard_fail:
        bucket["hard_fail"] = float(bucket.get("hard_fail", 0.0)) + 1.0

    bucket["last_ts"] = float(now)
    bucket["last_sinr"] = float(sinr_after)
    _update_bucket_components(
        bucket,
        primary_bw_mhz=primary_bw_mhz,
        ca_active=ca_active,
        ri=ri,
        bq_cell=bq_cell,
        nr_anchor=nr_anchor,
        alpha=0.2,
    )


def consolidate_learning_bucket_to_ltm(
    memory: Any,
    key: str,
    outcome_label: str,
    hard_fail: bool,
    now: float,
    sinr_after: float,
    primary_bw_mhz: float = 0.0,
    ca_active: bool = False,
    ri: int = 0,
    bq_cell: float = 0.0,
    cq_cell: float = 0.0,
    nr_anchor: bool = False,
    seed_score: float = 0.5,
    dlst: Optional[Dict[str, Any]] = None,
    grade_weight: float = 1.0,
) -> None:
    if not key:
        return
    decay = float(getattr(memory.learning_cfg, "learning_ltm_decay", 0.98))
    decay = min(1.0, max(0.90, decay))
    bucket = memory.band_learning_ltm.get(key)
    if not bucket:
        bucket = empty_learning_bucket()
        bucket["score"] = seed_score
        memory.band_learning_ltm[key] = bucket
    attach_dlst(bucket, normalize_dlst(dlst))

    old_score = float(bucket.get("score", 0.5))
    weight = max(1.0, min(3.0, float(grade_weight or 1.0)))
    if outcome_label == "good":
        bucket["score"] = min(1.0, old_score + (0.05 * weight))
    elif outcome_label == "bad":
        bucket["score"] = max(0.0, old_score - (0.05 * weight))

    bucket["n"] = int(bucket.get("n", 0)) + 1
    for field in ("attempts", "good", "bad", "hard_fail"):
        bucket[field] = float(bucket.get(field, 0.0)) * decay
    bucket["attempts"] += 1.0
    if outcome_label == "good":
        bucket["good"] += weight
    elif outcome_label == "bad":
        bucket["bad"] += weight
    if hard_fail:
        bucket["hard_fail"] += 1.0
    bucket["last_ts"] = float(now)
    bucket["last_sinr"] = float(sinr_after)
    _update_bucket_components(
        bucket,
        primary_bw_mhz=primary_bw_mhz,
        ca_active=ca_active,
        ri=ri,
        bq_cell=bq_cell,
        nr_anchor=nr_anchor,
        alpha=0.1,
    )


def prune_learning_stm(memory: Any, now: float) -> None:
    window_s = max(600.0, float(getattr(memory.learning_cfg, "learning_stm_window_s", 600.0)))
    stale = []
    for key, bucket in memory.band_learning_stm.items():
        last = float(bucket.get("last_ts", 0.0))
        if last <= 0.0 or (now - last) > window_s:
            stale.append(key)
    for key in stale:
        memory.band_learning_stm.pop(key, None)


def empty_learning_bucket() -> Dict[str, Any]:
    return {
        "score": 0.5,
        "n": 0,
        "ca_rate": 0.0,
        "ri_avg": 0.0,
        "bq_avg": 0.0,
        "cq_avg": 0.0,
        "bw_mhz": 0.0,
        "nr_anchor": False,
        "last_ts": 0.0,
        "last_sinr": 0.0,
        "hard_fail": 0.0,
        "attempts": 0.0,
        "good": 0.0,
        "bad": 0.0,
        "last_primary_bw_mhz": 0.0,
        "avg_primary_bw_mhz": 0.0,
        "DLST": empty_dlst(),
    }


def _update_bucket_components(
    bucket: Dict[str, Any],
    *,
    primary_bw_mhz: float,
    ca_active: bool,
    ri: int,
    bq_cell: float,
    nr_anchor: bool,
    alpha: float,
) -> None:
    if primary_bw_mhz > 0.0:
        bucket["last_primary_bw_mhz"] = float(primary_bw_mhz)
        prev_bw = float(bucket.get("avg_primary_bw_mhz", 0.0) or 0.0)
        bucket["avg_primary_bw_mhz"] = prev_bw + (1.0 if prev_bw <= 0 else alpha) * (primary_bw_mhz - prev_bw)
        if float(bucket.get("bw_mhz", 0.0)) <= 0.0:
            bucket["bw_mhz"] = float(primary_bw_mhz)
    prev_ca = float(bucket.get("ca_rate", 0.0))
    ca_val = 1.0 if ca_active else 0.0
    bucket["ca_rate"] = prev_ca + (1.0 if bucket["n"] <= 1 else alpha) * (ca_val - prev_ca)
    if ri > 0:
        prev_ri = float(bucket.get("ri_avg", 0.0))
        bucket["ri_avg"] = prev_ri + (1.0 if prev_ri <= 0 else alpha) * (float(ri) - prev_ri)
    if bq_cell > 0.0:
        prev_bq = float(bucket.get("bq_avg", 0.0))
        bucket["bq_avg"] = prev_bq + (1.0 if prev_bq <= 0 else alpha) * (bq_cell - prev_bq)
    if nr_anchor:
        bucket["nr_anchor"] = True


def _resolve_marker(memory: Any, marker: Any) -> str:
    return resolve_marker_alias(memory, marker)


def _append_reason(existing: str, addition: str) -> str:
    existing = str(existing or "").strip(";")
    addition = str(addition or "").strip(";")
    if not addition:
        return existing
    if not existing:
        return addition
    parts = [p for p in existing.split(";") if p]
    if addition in parts:
        return existing
    return f"{existing};{addition}"


__all__ = [
    "OWNER",
    "consolidate_learning_bucket_to_ltm",
    "empty_learning_bucket",
    "prune_learning_stm",
    "record_band_learning",
    "update_learning_bucket",
]
