"""Self-learning action-family ROI summary owner."""

from __future__ import annotations

from typing import Any, Mapping

OWNER = "arch4.brain.self_learning_action_family_roi_owner"
SCHEMA = "self_learning_action_family_roi.v1"


def action_family_roi_summary(action_roi: Mapping[str, Any]) -> dict[str, Any]:
    roi = _mapping(action_roi)
    sections = {
        "pband": _mapping(roi.get("by_pband")),
        "ca_pool": _mapping(roi.get("by_ca_pool")),
        "ca_pool_anchor": _mapping(roi.get("by_ca_pool_anchor")),
        "ca_pool_delta": _mapping(roi.get("by_ca_pool_delta")),
    }
    family_rows: dict[str, Any] = {}
    total_attempts = 0
    total_bad = 0
    total_good = 0
    total_contexts = 0
    worst_penalty = 0.0
    worst_key = ""
    best_benefit = 0.0
    best_key = ""
    for family, rows in sections.items():
        attempts = 0
        bad = 0
        good = 0
        contexts = 0
        penalty = 0.0
        penalty_key = ""
        benefit = 0.0
        benefit_key = ""
        for key, raw in rows.items():
            item = _mapping(raw)
            item_attempts = _int(item.get("attempts"), 0)
            item_bad = _int(item.get("bad"), 0)
            item_good = _int(item.get("good"), 0)
            item_penalty = _float(item.get("penalty"), 0.0)
            item_benefit = _float(item.get("benefit"), 0.0)
            item_contexts = _mapping(item.get("contexts"))
            attempts += item_attempts
            bad += item_bad
            good += item_good
            contexts += len(item_contexts)
            if item_penalty > penalty:
                penalty = item_penalty
                penalty_key = _text(key)
            if item_benefit > benefit:
                benefit = item_benefit
                benefit_key = _text(key)
        family_rows[family] = {
            "known_shapes": len(rows),
            "attempts": attempts,
            "bad": bad,
            "good": good,
            "contexts": contexts,
            "worst_penalty": round(penalty, 4),
            "worst_key": penalty_key,
            "best_benefit": round(benefit, 4),
            "best_key": benefit_key,
        }
        total_attempts += attempts
        total_bad += bad
        total_good += good
        total_contexts += contexts
        if penalty > worst_penalty:
            worst_penalty = penalty
            worst_key = penalty_key
        if benefit > best_benefit:
            best_benefit = benefit
            best_key = benefit_key
    return {
        "schema": SCHEMA,
        "available": bool(total_attempts > 0 or any(row["known_shapes"] > 0 for row in family_rows.values())),
        "attempts": total_attempts,
        "bad": total_bad,
        "good": total_good,
        "contexts": total_contexts,
        "worst_penalty": round(worst_penalty, 4),
        "worst_key": worst_key,
        "best_benefit": round(best_benefit, 4),
        "best_key": best_key,
        "families": family_rows,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = ["OWNER", "SCHEMA", "action_family_roi_summary"]
