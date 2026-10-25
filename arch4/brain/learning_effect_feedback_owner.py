"""Canonical referee-feedback summary owner for Brain learning effect."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

OWNER = "arch4.brain.learning_effect_feedback_owner"
SCHEMA = "brain_learning_effect_feedback_summary.v1"

_FEEDBACK_PRIORITY = {
    "outcome_signal": 40,
    "counterfactual_diagnostic": 30,
    "valid_no_action": 20,
    "governor_blocked_no_action": 10,
    "low_information": 0,
}


def feedback_rows(
    *,
    referee_feedback_quality: Mapping[str, Any] | None,
    referee_feedback_qualities: Iterable[Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    if isinstance(referee_feedback_quality, Mapping) and referee_feedback_quality:
        rows.append(referee_feedback_quality)
    if referee_feedback_qualities is not None:
        rows.extend(row for row in referee_feedback_qualities if isinstance(row, Mapping) and row)
    return rows


def feedback_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            "quality": "",
            "count": 0,
            "low_information_count": 0,
            "valid_no_action_count": 0,
            "counterfactual_diagnostic_count": 0,
            "outcome_signal_count": 0,
            "learning_signal": False,
            "diagnostic_signal": False,
            "reason": "",
        }
    primary = max(
        rows,
        key=lambda row: (
            _FEEDBACK_PRIORITY.get(_text(row.get("quality")), -1),
            int(bool(row.get("learning_signal"))),
            int(bool(row.get("diagnostic_signal"))),
        ),
    )
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "quality": _text(primary.get("quality")),
        "count": len(rows),
        "low_information_count": _count_quality(rows, "low_information"),
        "valid_no_action_count": _count_quality(rows, "valid_no_action"),
        "counterfactual_diagnostic_count": _count_quality(rows, "counterfactual_diagnostic"),
        "outcome_signal_count": _count_quality(rows, "outcome_signal"),
        "learning_signal": any(_bool(row.get("learning_signal")) for row in rows),
        "diagnostic_signal": any(_bool(row.get("diagnostic_signal")) for row in rows),
        "reason": _text(primary.get("reason")),
    }


def _count_quality(rows: Iterable[Mapping[str, Any]], quality: str) -> int:
    return sum(1 for row in rows if _text(row.get("quality")) == quality)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return bool(value)


__all__ = [
    "OWNER",
    "SCHEMA",
    "feedback_rows",
    "feedback_summary",
]
