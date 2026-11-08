"""Canonical receipt for staged-ROI secondary learning commits.

This module owns the public shape of the "what did we commit into learning
stores after a proof closed?" answer. Runtime may still expose simple booleans
for support bundles, but those booleans must be produced through this owner.
"""

from __future__ import annotations

from typing import Any, Mapping

SCHEMA = "brain_secondary_learning_receipt.v1"
OWNER = "arch4.brain.secondary_learning_receipt"


def empty_secondary_learning_receipt() -> dict[str, Any]:
    return secondary_learning_receipt_from_mapping({})


def secondary_learning_receipt_from_mapping(receipt: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize runtime/replay secondary-learning receipts."""
    row = receipt if isinstance(receipt, Mapping) else {}
    learning_gate = row.get("learning_gate") if isinstance(row.get("learning_gate"), Mapping) else {}
    memory_committed = _bool(row.get("memory_committed"))
    historical_committed = _bool(row.get("historical_committed"))
    crystal_ball_updated = _bool(row.get("crystal_ball_updated"))
    crystal_ball_regraded = _bool(row.get("crystal_ball_regraded"))
    reward_admitted = _bool(row.get("reward_admitted"))
    reward_duplicate = _bool(row.get("reward_duplicate"))
    prediction_error_recorded = _bool(
        row.get("prediction_error_recorded")
    )
    policy_update_applied = _bool(row.get("policy_update_applied"))
    committed_any = bool(
        reward_admitted
        or memory_committed
        or historical_committed
        or crystal_ball_updated
        or crystal_ball_regraded
        or _bool(row.get("committed"))
    )
    memory_error = _text(row.get("memory_error"))
    historical_error = _text(row.get("historical_error"))
    crystal_ball_error = _text(row.get("crystal_ball_error"))
    reward_ledger_error = _text(row.get("reward_ledger_error"))
    error_count = sum(
        1
        for value in (reward_ledger_error, memory_error, historical_error, crystal_ball_error)
        if value
    )
    reason = _text(row.get("reason"))
    status = _status(
        committed_any=committed_any,
        reason=reason,
        error_count=error_count,
        learning_gate=learning_gate,
    )
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "status": status,
        "committed": committed_any,
        "reason": reason,
        "reward_admitted": reward_admitted,
        "reward_duplicate": reward_duplicate,
        "reward_ledger_key": _text(row.get("reward_ledger_key")),
        "reward_ledger_reason": _text(row.get("reward_ledger_reason")),
        "reward_ledger_error": reward_ledger_error,
        "terminal_grade_score": _optional_int(row.get("terminal_grade_score")),
        "proof_identity": dict(row.get("proof_identity"))
        if isinstance(row.get("proof_identity"), Mapping)
        else {},
        "memory_committed": memory_committed,
        "memory_reason": _text(row.get("memory_reason")),
        "memory_error": memory_error,
        "historical_committed": historical_committed,
        "historical_reason": _text(row.get("historical_reason")),
        "historical_error": historical_error,
        "crystal_ball_updated": crystal_ball_updated,
        "crystal_ball_regraded": crystal_ball_regraded,
        "crystal_ball_error": crystal_ball_error,
        "prediction_error_recorded": prediction_error_recorded,
        "policy_update_applied": policy_update_applied,
        "future_policy_effect_status": _text(
            row.get("future_policy_effect_status")
        ),
        "prediction_correction": dict(row.get("prediction_correction"))
        if isinstance(row.get("prediction_correction"), Mapping)
        else {},
        "learning_gate": dict(learning_gate),
        "learning_gate_allowed": _bool(learning_gate.get("learning_allowed")) if learning_gate else False,
        "learning_gate_block_reason": _text(learning_gate.get("block_reason")) if learning_gate else "",
        "error_count": error_count,
        "targets_committed": sum(
            1 for value in (memory_committed, historical_committed, crystal_ball_updated) if value
        ),
        "crystal_ball_regrade_committed": crystal_ball_regraded,
    }


def secondary_learning_receipt_projection(receipt: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project the receipt fields allowed in action-proof/support rows."""
    row = secondary_learning_receipt_from_mapping(receipt)
    return {
        "secondary_learning_status": _text(row.get("status")),
        "secondary_learning_committed": _bool(row.get("committed")),
        "secondary_learning_reason": _text(row.get("reason")),
        "reward_admitted": _bool(row.get("reward_admitted")),
        "reward_duplicate": _bool(row.get("reward_duplicate")),
        "reward_ledger_key": _text(row.get("reward_ledger_key")),
        "memory_committed": _bool(row.get("memory_committed")),
        "historical_committed": _bool(row.get("historical_committed")),
        "crystal_ball_updated": _bool(row.get("crystal_ball_updated")),
        "crystal_ball_regraded": _bool(row.get("crystal_ball_regraded")),
        "prediction_error_recorded": _bool(
            row.get("prediction_error_recorded")
        ),
        "policy_update_applied": _bool(row.get("policy_update_applied")),
        "future_policy_effect_status": _text(
            row.get("future_policy_effect_status")
        ),
        "secondary_learning_error_count": _int(row.get("error_count")),
    }


def _status(
    *,
    committed_any: bool,
    reason: str,
    error_count: int,
    learning_gate: Mapping[str, Any],
) -> str:
    if committed_any and error_count:
        return "partial_with_errors"
    if committed_any:
        return "committed"
    if error_count:
        return "error"
    if reason == "not_learning_grade":
        return "not_learning_grade"
    if reason or (learning_gate and not _bool(learning_gate.get("learning_allowed"))):
        return "blocked"
    return "not_committed"


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "committed", "ok", "success"}
    return bool(value)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    if type(value) is not int:
        return None
    return int(value)


__all__ = [
    "OWNER",
    "SCHEMA",
    "empty_secondary_learning_receipt",
    "secondary_learning_receipt_from_mapping",
    "secondary_learning_receipt_projection",
]
