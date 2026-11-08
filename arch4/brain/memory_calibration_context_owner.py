"""Calibration-context marker owner for Memory.

Memory owns durable fields. This module owns calibration marker identity,
aliasing, provisional-context reconciliation, and marker learning merge rules.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from arch4.brain.memory_calibration_history_owner import (
    reassign_calibration_marker_history as memory_reassign_calibration_marker_history,
)
from arch4.brain.memory_calibration_marker_owner import (
    compact_calibration_alias_graph as marker_compact_calibration_alias_graph,
    resolve_marker_alias as marker_resolve_marker_alias,
)
from arch4.interfaces import ARCH4_VERSION

OWNER = "arch4.brain.memory_calibration_context_owner"
log = logging.getLogger("arch4.brain.memory_calibration_context_owner")


def sync_current_context_seen_actions(memory: Any) -> None:
    marker = resolve_marker_alias(memory, getattr(memory, "current_calibration_marker", "C0"))
    contexts = getattr(memory, "calibration_contexts", {}) or {}
    ctx = dict(contexts.get(marker, {}) or {}) if isinstance(contexts, dict) else {}
    memory.current_context_seen_actions = int(ctx.get("seen_actions", 0) or 0)


def load_calibration_context_state(memory: Any) -> None:
    try:
        context = memory._calibration_store.read_context_state()
    except Exception as exc:
        log.warning("Calibration CM context load failed: %s", exc)
        return
    if isinstance(context, dict) and context:
        apply_saved_calibration_context_state(memory, context)


def build_calibration_context_state(memory: Any) -> Dict[str, Any]:
    from arch4.brain.memory_namespaces import (
        capture_current_namespace,
        serialize_provider_calibration_namespaces,
    )

    capture_current_namespace(memory)
    return {
        "version": ARCH4_VERSION,
        "provider_context": getattr(memory, "_current_provider_plmn", ""),
        "providers": serialize_provider_calibration_namespaces(memory),
        "saved_ts": float(time.time()),
    }


def apply_saved_calibration_context_state(memory: Any, context: Dict[str, Any]) -> None:
    from arch4.brain.memory_namespaces import apply_calibration_context_state

    apply_calibration_context_state(memory, context)


def resolve_marker_alias(memory: Any, marker: Any) -> str:
    return marker_resolve_marker_alias(memory, marker)


def compact_calibration_alias_graph(memory: Any) -> None:
    marker_compact_calibration_alias_graph(memory)


def signature_compact_str(sig: Dict[str, Any]) -> str:
    if not sig:
        return ""
    return (
        f"mdl={sig.get('modem_model','?')};sim={sig.get('sim_id','?')};"
        f"edl={sig.get('expected_dl_mbps',0.0):.2f};"
        f"ertt={sig.get('expected_rtt_ms',0.0):.1f};"
        f"esinr={sig.get('expected_sinr_db',0.0):.1f};"
        f"gap={sig.get('gap_score',0.0):.3f}"
    )


def set_calibration_context(
    memory: Any,
    calibration_status: Optional[Dict[str, Any]],
    modem_meta: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    ts = time.time() if now is None else float(now)
    st = dict(calibration_status or {})
    meta = dict(modem_meta or {})
    sig = {
        "provider_plmn": str(meta.get("provider_plmn") or getattr(memory, "_current_provider_plmn", "") or ""),
        "modem_model": str(meta.get("model") or meta.get("modem_model") or ""),
        "sim_id": str(meta.get("sim_id") or meta.get("iccid") or meta.get("imsi") or ""),
        "expected_dl_mbps": float(st.get("expected_dl_mbps", 0.0) or 0.0),
        "expected_rtt_ms": float(st.get("expected_rtt_ms", 0.0) or 0.0),
        "expected_sinr_db": float(st.get("expected_sinr_db", 0.0) or 0.0),
        "gap_score": float(st.get("gap_score", 0.0) or 0.0),
        "baseline_ready": bool(st.get("baseline_ready", False)),
        "sample_count": int(st.get("sample_count", 0) or 0),
    }
    marker, score, reused = resolve_or_create_provisional_marker(memory, sig, ts)
    if marker != getattr(memory, "current_calibration_marker", "C0"):
        memory.current_calibration_marker = marker
        ctx_sw = getattr(memory, "calibration_contexts", {}).get(marker, {})
        memory.current_context_seen_actions = int(ctx_sw.get("seen_actions", 0) or 0)
    memory.current_calibration_signature = sig
    contexts = getattr(memory, "calibration_contexts", {})
    ctx = contexts.get(marker, {})
    ctx["signature"] = blend_signature(dict(ctx.get("signature", {}) or {}), sig, alpha=0.20)
    ctx["seed"] = context_seed(sig)
    ctx["last_seen"] = ts
    ctx["uses"] = int(ctx.get("uses", 0) or 0) + 1
    contexts[marker] = ctx
    memory.calibration_contexts = contexts
    merged = maybe_reconcile_mature_contexts(memory, ts)
    marker_eff = resolve_marker_alias(memory, marker)
    if marker_eff != marker:
        memory.current_calibration_marker = marker_eff
        if marker in memory.calibration_contexts:
            memory.calibration_contexts.pop(marker, None)
        memory.current_context_seen_actions = max(
            int(getattr(memory, "current_context_seen_actions", 0) or 0),
            int(getattr(memory.learning_cfg, "learning_marker_warmup_attempts", 0) or 0),
        )
    memory._mark_dirty()
    return {
        "marker": memory.current_calibration_marker,
        "marker_similarity": float(score),
        "marker_reused": bool(reused),
        "marker_warmup_ready": int(getattr(memory, "current_context_seen_actions", 0) or 0)
        >= int(getattr(memory.learning_cfg, "learning_marker_warmup_attempts", 0) or 0),
        "marker_warmup_seen_actions": int(getattr(memory, "current_context_seen_actions", 0) or 0),
        "marker_signature": signature_compact_str(sig),
        "marker_merged": bool(marker_eff != marker),
        "marker_merge_count": len(merged),
    }


def resolve_or_create_provisional_marker(
    memory: Any,
    sig: Dict[str, Any],
    now: float,
) -> tuple[str, float, bool]:
    cur = resolve_marker_alias(memory, getattr(memory, "current_calibration_marker", "C0"))
    contexts = getattr(memory, "calibration_contexts", {})
    cur_ctx = contexts.get(cur, {})
    cur_seed = str(cur_ctx.get("seed", "") or "")
    new_seed = context_seed(sig)
    if cur and cur_seed and (cur_seed == new_seed):
        return cur, 1.0, True
    next_n = 1
    aliases = getattr(memory, "calibration_aliases", {}) or {}
    for mk in list(contexts.keys()) + list(aliases.keys()):
        if str(mk).startswith("C"):
            try:
                next_n = max(next_n, int(str(mk)[1:]) + 1)
            except Exception:
                continue
    marker = f"C{next_n}"
    contexts[marker] = {
        "created_at": now,
        "last_seen": now,
        "uses": 0,
        "seen_actions": 0,
        "provisional": True,
        "seed": new_seed,
        "signature": sig,
    }
    memory.calibration_contexts = contexts
    return marker, 0.0, False


def maybe_reconcile_mature_contexts(memory: Any, now: float) -> List[tuple[str, str, float]]:
    cfg = getattr(memory, "learning_cfg", None)
    delay_s = float(getattr(cfg, "learning_context_compare_delay_s", 0.0) or 0.0)
    threshold = float(getattr(cfg, "learning_context_similarity_threshold", 0.0) or 0.0)
    max_reconciles_per_tick = 8
    merged: List[tuple[str, str, float]] = []
    contexts = getattr(memory, "calibration_contexts", {}) or {}
    markers = sorted(
        list(contexts.keys()),
        key=lambda mk: float((contexts.get(mk) or {}).get("created_at", 0.0) or 0.0),
    )
    stable_targets = [
        mk for mk, ctx in contexts.items()
        if not bool((ctx or {}).get("provisional", False))
    ]
    for mk in markers:
        if len(merged) >= max_reconciles_per_tick:
            break
        ctx = contexts.get(mk) or {}
        if not bool(ctx.get("provisional", False)):
            continue
        created = float(ctx.get("created_at", 0.0) or 0.0)
        if created <= 0.0 or (now - created) < delay_s:
            continue
        sig = dict(ctx.get("signature", {}) or {})
        best_mk = ""
        best_score = -1.0
        for other_mk in stable_targets:
            other_ctx = contexts.get(other_mk) or {}
            score = calibration_similarity(sig, dict(other_ctx.get("signature", {}) or {}))
            if score > best_score:
                best_score = score
                best_mk = other_mk
        if best_mk and best_score >= threshold:
            best_canonical = resolve_marker_alias(memory, best_mk)
            merge_marker_learning(memory, mk, best_canonical)
            memory.calibration_aliases[mk] = best_canonical
            memory_reassign_calibration_marker_history(memory, mk, best_canonical)
            memory.calibration_contexts.pop(mk, None)
            merged.append((mk, best_canonical, best_score))
        else:
            ctx["provisional"] = False
            memory.calibration_contexts[mk] = ctx
            stable_targets.append(mk)
    if merged:
        compact_calibration_alias_graph(memory)
    return merged


def blend_signature(old: Dict[str, Any], new: Dict[str, Any], alpha: float = 0.2) -> Dict[str, Any]:
    if not old:
        return dict(new)
    out = dict(old)
    for key in ("provider_plmn", "modem_model", "sim_id"):
        if new.get(key):
            out[key] = new.get(key)
    for key in ("expected_dl_mbps", "expected_rtt_ms", "expected_sinr_db", "gap_score"):
        ov = float(out.get(key, 0.0) or 0.0)
        nv = float(new.get(key, 0.0) or 0.0)
        out[key] = (1.0 - alpha) * ov + alpha * nv
    out["baseline_ready"] = bool(new.get("baseline_ready", out.get("baseline_ready", False)))
    out["sample_count"] = int(new.get("sample_count", out.get("sample_count", 0)))
    return out


def context_seed(sig: Dict[str, Any]) -> str:
    return (
        f"plmn={sig.get('provider_plmn','')};"
        f"model={sig.get('modem_model','')};"
        f"sim={sig.get('sim_id','')};"
        f"base={1 if bool(sig.get('baseline_ready', False)) else 0};"
        f"edl={round(float(sig.get('expected_dl_mbps', 0.0) or 0.0), 1):.1f};"
        f"ertt={round(float(sig.get('expected_rtt_ms', 0.0) or 0.0), 1):.1f};"
        f"esinr={round(float(sig.get('expected_sinr_db', 0.0) or 0.0), 1):.1f}"
    )


def merge_marker_learning(memory: Any, old_marker: str, new_marker: str) -> None:
    old_pref = f"{old_marker}|"
    new_pref = f"{new_marker}|"
    for store in (memory.band_learning_stm, memory.band_learning_ltm):
        keys = [key for key in list(store.keys()) if key.startswith(old_pref)]
        for key in keys:
            tail = key[len(old_pref):]
            new_key = f"{new_pref}{tail}"
            src = store.get(key) or {}
            dst = store.get(new_key) or _empty_learning_bucket()
            for field in ("attempts", "good", "bad", "hard_fail"):
                dst[field] = float(dst.get(field, 0.0)) + float(src.get(field, 0.0))
            dst["last_ts"] = max(float(dst.get("last_ts", 0.0)), float(src.get("last_ts", 0.0)))
            store[new_key] = dst
            store.pop(key, None)


def calibration_similarity(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    if not a or not b:
        return 0.0
    for key in ("provider_plmn", "modem_model", "sim_id"):
        va = str(a.get(key, "") or "")
        vb = str(b.get(key, "") or "")
        if va and vb and va != vb:
            return 0.0

    def num(data: Dict[str, Any], key: str) -> float:
        try:
            return float(data.get(key, 0.0) or 0.0)
        except Exception:
            return 0.0

    dl_a, dl_b = num(a, "expected_dl_mbps"), num(b, "expected_dl_mbps")
    rtt_a, rtt_b = num(a, "expected_rtt_ms"), num(b, "expected_rtt_ms")
    sinr_a, sinr_b = num(a, "expected_sinr_db"), num(b, "expected_sinr_db")
    gap_a, gap_b = num(a, "gap_score"), num(b, "gap_score")
    dl_sim = max(0.0, 1.0 - (abs(dl_a - dl_b) / max(1.0, max(dl_a, dl_b))))
    rtt_sim = max(0.0, 1.0 - (abs(rtt_a - rtt_b) / max(20.0, max(rtt_a, rtt_b))))
    sinr_sim = max(0.0, 1.0 - (abs(sinr_a - sinr_b) / 10.0))
    gap_sim = max(0.0, 1.0 - (abs(gap_a - gap_b) / 1.0))
    score = 0.35 * dl_sim + 0.25 * rtt_sim + 0.25 * sinr_sim + 0.15 * gap_sim
    return max(0.0, min(1.0, score))


def _empty_learning_bucket() -> Dict[str, Any]:
    return {
        "n": 0,
        "score": 0.5,
        "attempts": 0.0,
        "good": 0.0,
        "bad": 0.0,
        "hard_fail": 0.0,
        "last_ts": 0.0,
        "last_sinr": 0.0,
    }


__all__ = [
    "OWNER",
    "blend_signature",
    "calibration_similarity",
    "compact_calibration_alias_graph",
    "context_seed",
    "apply_saved_calibration_context_state",
    "build_calibration_context_state",
    "load_calibration_context_state",
    "merge_marker_learning",
    "maybe_reconcile_mature_contexts",
    "resolve_marker_alias",
    "resolve_or_create_provisional_marker",
    "set_calibration_context",
    "signature_compact_str",
    "sync_current_context_seen_actions",
]
