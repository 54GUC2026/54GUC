"""Layered action-feedback owner for proof/referee learning.

This module turns proof evidence into a structured feedback package. It does
not select actions, write Memory, or update Crystal Ball. SmartLoop and proof
record builders may consume this package as graded evidence.
"""

from __future__ import annotations

from typing import Any, Mapping

SCHEMA = "layered_action_feedback.v1"
OWNER = "arch4.brain.proof_feedback_owner"


def build_layered_action_feedback(
    *,
    label: str,
    reason: str,
    cq_delta: float | None = None,
    congestion_delta: float | None = None,
    evidence: Mapping[str, Any] | None = None,
    horizon_s: int = 0,
) -> dict[str, Any]:
    """Build one owner-stamped layered proof feedback package."""

    row = evidence if isinstance(evidence, Mapping) else {}
    label_norm = str(label or "").strip().lower()
    evidence_valid = bool(row.get("grade_evidence_valid", True))
    context_status = str(row.get("grade_context_status") or row.get("context_status") or "same").strip().lower()
    context_clean = evidence_valid and context_status in {"", "same", "valid", "ok"}

    execution = _grade_execution(label_norm=label_norm, reason=reason)
    transition = _grade_transition(row)
    short_roi = _grade_short_roi(
        label_norm=label_norm,
        cq_delta=cq_delta,
        congestion_delta=congestion_delta,
        row=row,
    )
    medium_roi = _grade_medium_roi(label_norm=label_norm, row=row)
    context = _grade_context(context_clean=context_clean, row=row, reason=reason)
    opportunity = _grade_opportunity(label_norm=label_norm, row=row)

    net_score = _bounded(
        execution["score"]
        + transition["score"]
        + short_roi["score"]
        + medium_roi["score"]
        + context["score"]
        + opportunity["score"],
        -1.0,
        1.0,
    )
    if not context_clean:
        net_score = 0.0

    memory_score = _memory_grade_score(net_score=net_score, context_clean=context_clean)
    memory_grade = _memory_grade(net_score=net_score, memory_score=memory_score, context_clean=context_clean)
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "label": label_norm or "neutral",
        "reason": str(reason or ""),
        "horizon_s": int(horizon_s or 0),
        "context_clean": bool(context_clean),
        "net_score": round(float(net_score), 4),
        "memory_grade_score": int(memory_score),
        "learning_label": memory_grade["learning_label"],
        "dimensions": {
            "execution": execution,
            "transition": transition,
            "short_roi": short_roi,
            "medium_roi": medium_roi,
            "context": context,
            "opportunity": opportunity,
            "memory": memory_grade,
        },
    }


def build_referee_layered_feedback(
    *,
    label: str,
    reason: str,
    evidence: Mapping[str, Any],
    horizon_s: int,
) -> dict[str, Any]:
    """Canonical Referee evidence-to-layered-grade adapter."""

    row = dict(evidence or {})
    return build_layered_action_feedback(
        label=label,
        reason=reason,
        cq_delta=_optional_delta(row.get("cq_now"), row.get("cq_before")),
        congestion_delta=(
            _optional_delta(
                row.get("congestion_now"),
                row.get("congestion_before"),
            )
            if row.get("congestion_before_valid") is True
            and row.get("congestion_now_valid") is True
            else None
        ),
        evidence=row,
        horizon_s=int(horizon_s),
    )


def _optional_delta(after: object, before: object) -> float | None:
    if after is None or before is None:
        return None
    try:
        return float(after) - float(before)
    except (TypeError, ValueError):
        return None


def terminal_layered_action_feedback_is_valid(
    feedback: Mapping[str, Any] | None,
    *,
    horizon_s: int,
) -> bool:
    """Validate the terminal reward owner's durable package.

    Consumers must not reconstruct or silently coerce a terminal grade.  This
    validator is the one admission contract shared by secondary learning,
    proof publication, durable recovery, and SmartLoop.
    """

    if not isinstance(feedback, Mapping):
        return False
    if feedback.get("schema") != SCHEMA or feedback.get("owner") != OWNER:
        return False
    score = feedback.get("memory_grade_score")
    package_horizon = feedback.get("horizon_s")
    if type(score) is not int or type(package_horizon) is not int:
        return False
    try:
        expected_horizon = int(horizon_s)
    except (TypeError, ValueError):
        return False
    context_clean = feedback.get("context_clean") is True
    try:
        net_score = float(feedback.get("net_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    expected_score = _memory_grade_score(
        net_score=net_score,
        context_clean=context_clean,
    )
    expected_label = _memory_grade(
        net_score=net_score,
        memory_score=expected_score,
        context_clean=context_clean,
    )["learning_label"]
    dimensions = feedback.get("dimensions")
    if not isinstance(dimensions, Mapping):
        return False
    memory = dict(dimensions.get("memory") or {})
    dimension_names = (
        "execution",
        "transition",
        "short_roi",
        "medium_roi",
        "context",
        "opportunity",
    )
    try:
        reconstructed_net = _bounded(
            sum(float(dict(dimensions.get(name) or {}).get("score", 0.0) or 0.0) for name in dimension_names),
            -1.0,
            1.0,
        )
    except (TypeError, ValueError):
        return False
    return bool(
        -3 <= score <= 3
        and expected_horizon > 0
        and package_horizon == expected_horizon
        and score == expected_score
        and str(feedback.get("learning_label") or "") == expected_label
        and memory.get("memory_grade_score") == expected_score
        and str(memory.get("learning_label") or "") == expected_label
        and round(reconstructed_net, 4) == round(net_score, 4)
    )


def layered_action_outcome_label(
    feedback: Mapping[str, Any],
) -> str:
    """Project the sole layered grade into the canonical action label."""

    if not isinstance(feedback, Mapping):
        raise TypeError("layered_action_feedback_mapping_required")
    if feedback.get("schema") != SCHEMA or feedback.get("owner") != OWNER:
        raise ValueError("layered_action_feedback_owner_schema_invalid")
    score = feedback.get("memory_grade_score")
    if type(score) is not int or not -3 <= score <= 3:
        raise ValueError("layered_action_feedback_grade_invalid")
    if feedback.get("context_clean") is not True:
        return "neutral"
    if score > 0:
        return "good"
    if score < 0:
        return "bad"
    return "neutral"


def _grade_execution(*, label_norm: str, reason: str) -> dict[str, Any]:
    if label_norm == "good":
        return _dimension("good", 0.20, "proof_execution_success")
    if label_norm == "bad":
        return _dimension("bad", -0.25, str(reason or "proof_execution_regressed"))
    if label_norm == "invalid":
        return _dimension("invalid", 0.0, str(reason or "proof_invalid"))
    return _dimension("neutral", 0.0, str(reason or "proof_execution_neutral"))


def _grade_transition(row: Mapping[str, Any]) -> dict[str, Any]:
    health = str(row.get("health_stage") or "").strip().lower()
    incident = str(row.get("incident_class") or "").strip().lower()
    self_induced = bool(row.get("self_induced", False))
    if incident and self_induced:
        return _dimension("bad", -0.18, f"self_induced_incident:{incident}")
    if health in {"warning", "degraded_hold", "degraded_persist", "critical"}:
        return _dimension("bad", -0.14, f"health:{health}")
    if health in {"healthy", "normal", ""}:
        return _dimension("good", 0.05, "transition_clean")
    return _dimension("neutral", 0.0, f"transition:{health}")


def _grade_short_roi(
    *,
    label_norm: str,
    cq_delta: float | None,
    congestion_delta: float | None,
    row: Mapping[str, Any],
) -> dict[str, Any]:
    score = 0.0
    reasons: list[str] = []
    if cq_delta is not None:
        if cq_delta >= 0.05:
            score += 0.12
            reasons.append(f"cq_delta:+{cq_delta:.3f}")
        elif cq_delta <= -0.05:
            score -= 0.16
            reasons.append(f"cq_delta:{cq_delta:.3f}")
    if congestion_delta is not None:
        if congestion_delta <= -8.0:
            score += 0.12
            reasons.append(f"congestion_delta:{congestion_delta:.1f}")
        elif congestion_delta >= 8.0:
            score -= 0.16
            reasons.append(f"congestion_delta:+{congestion_delta:.1f}")
    tp_before = _float(row.get("tp_before"), 0.0)
    tp_now = _float(row.get("tp_now"), 0.0)
    if tp_before > 0.0 or tp_now > 0.0:
        tp_delta = tp_now - tp_before
        if tp_delta >= 5.0:
            score += 0.12
            reasons.append(f"dl_delta:+{tp_delta:.1f}")
        elif tp_delta <= -5.0:
            score -= 0.16
            reasons.append(f"dl_delta:{tp_delta:.1f}")
    if not reasons:
        return _dimension(label_norm if label_norm in {"good", "bad"} else "neutral", 0.0, "short_roi_mixed")
    grade = "good" if score > 0.0 else "bad" if score < 0.0 else "neutral"
    return _dimension(grade, _bounded(score, -0.28, 0.24), ";".join(reasons))


def _grade_medium_roi(*, label_norm: str, row: Mapping[str, Any]) -> dict[str, Any]:
    cast_ld_valid = bool(row.get("cast_ld_valid", False))
    cart_ld_activity = _float(row.get("cart_ld_activity"), 0.0)
    cast_ld = _float(row.get("cast_ld"), 0.0)
    if cast_ld_valid and cart_ld_activity <= 20.0 and cast_ld >= 90.0:
        return _dimension("good", 0.12, "ca_stable_under_load")
    if cast_ld_valid and (cart_ld_activity >= 60.0 or (cast_ld > 0.0 and cast_ld < 75.0)):
        return _dimension("bad", -0.16, "ca_unstable_under_load")
    return _dimension(label_norm if label_norm in {"good", "bad"} else "neutral", 0.0, "medium_roi_not_decisive")


def _grade_context(*, context_clean: bool, row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    if context_clean:
        return _dimension("good", 0.04, "context_clean")
    validity = str(row.get("grade_validity_reason") or reason or "context_dirty").strip()
    return _dimension("invalid", -0.30, validity)


def _grade_opportunity(*, label_norm: str, row: Mapping[str, Any]) -> dict[str, Any]:
    if bool(row.get("missed_candidate", False)):
        return _dimension("bad", -0.18, "opportunity_missed_candidate")
    if bool(row.get("nothing_left_on_table_proven", False)):
        return _dimension("good", 0.08, "nothing_left_on_table_proven")
    return _dimension(label_norm if label_norm in {"good", "bad"} else "neutral", 0.0, "opportunity_not_proven")


def _memory_grade_score(*, net_score: float, context_clean: bool) -> int:
    if not context_clean:
        return 0
    if net_score >= 0.55:
        return 3
    if net_score >= 0.25:
        return 2
    if net_score >= 0.12:
        return 1
    if net_score <= -0.55:
        return -3
    if net_score <= -0.25:
        return -2
    if net_score <= -0.12:
        return -1
    return 0


def _memory_grade(*, net_score: float, memory_score: int, context_clean: bool) -> dict[str, Any]:
    if not context_clean:
        return _dimension("quarantined", 0.0, "context_not_clean") | {
            "learning_label": "quarantined",
            "memory_grade_score": 0,
        }
    if memory_score >= 3:
        label = "strong_good"
    elif memory_score == 2:
        label = "good"
    elif memory_score == 1:
        label = "weak_good"
    elif memory_score <= -3:
        label = "strong_bad"
    elif memory_score == -2:
        label = "bad"
    elif memory_score == -1:
        label = "weak_bad"
    else:
        label = "neutral"
    return _dimension(label, net_score, label) | {
        "learning_label": label,
        "memory_grade_score": int(memory_score),
    }


def _dimension(grade: str, score: float, reason: str) -> dict[str, Any]:
    return {
        "grade": str(grade or "neutral"),
        "score": round(float(score), 4),
        "reason": str(reason or ""),
    }


def _bounded(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


__all__ = [
    "OWNER",
    "SCHEMA",
    "build_layered_action_feedback",
    "build_referee_layered_feedback",
    "layered_action_outcome_label",
    "terminal_layered_action_feedback_is_valid",
]
