"""Durable Referee-grade adapter into ActionTransactionAuthority.

The Referee owns the factual grade.  ActionTransactionAuthority owns action
correlation and lifecycle truth.  This module is the only legal boundary
adapter between those owners; it neither calculates nor changes a grade.
"""

from __future__ import annotations

from typing import Any, Mapping

from arch4.action_transaction_authority import (
    ActionTransactionAuthority,
    GRADE_WINDOWS_S,
    grade_reason_receipt_projection,
)


OWNER = "arch4.brain.proof_referee_action_lifecycle_projection_owner"


def project_referee_grade_to_action_lifecycle(
    *,
    authority: ActionTransactionAuthority,
    root_id: str,
    horizon_s: int,
    grade: int,
    reason_receipt: Mapping[str, Any],
    graded_at: float,
) -> dict[str, Any]:
    """Idempotently project one durable Referee receipt to its action root."""

    if not isinstance(authority, ActionTransactionAuthority):
        raise TypeError("action_lifecycle_projection_authority_required")
    root = str(root_id or "").strip()
    if not root:
        raise ValueError("action_lifecycle_projection_root_missing")
    horizon = int(horizon_s)
    if horizon not in GRADE_WINDOWS_S:
        raise ValueError(f"action_lifecycle_projection_horizon_invalid:{horizon}")
    value = int(grade)
    if value < -3 or value > 3:
        raise ValueError(f"action_lifecycle_projection_grade_invalid:{value}")
    reason = dict(reason_receipt or {})
    if not reason:
        raise ValueError("action_lifecycle_projection_reason_receipt_missing")
    if str(reason.get("owner") or "") != "arch4.brain.proof_feedback_owner":
        raise ValueError("action_lifecycle_projection_referee_owner_invalid")
    if int(reason.get("horizon_s") or 0) != horizon:
        raise ValueError("action_lifecycle_projection_horizon_mismatch")

    receipt = authority.record_grade(
        root_id=root,
        horizon_s=horizon,
        grade=value,
        reason_receipt=reason,
        graded_at=float(graded_at),
    )
    retained = dict(receipt.grades or {}).get(f"{horizon}s")
    if not isinstance(retained, Mapping):
        raise RuntimeError("action_lifecycle_projection_grade_not_retained")
    if int(retained.get("grade", 0) or 0) != value:
        raise RuntimeError("action_lifecycle_projection_grade_mismatch")
    if dict(retained.get("reason_receipt") or {}) != (
        grade_reason_receipt_projection(reason)
    ):
        raise RuntimeError("action_lifecycle_projection_reason_mismatch")
    return {
        "schema": "referee_action_lifecycle_projection.v1",
        "owner": OWNER,
        "completed": True,
        "root_id": root,
        "horizon_s": horizon,
        "grade": value,
        "action_lifecycle_receipt": receipt.as_dict(),
    }


def action_lifecycle_contains_referee_grade(
    *,
    authority: ActionTransactionAuthority,
    root_id: str,
    horizon_s: int,
    grade: int,
    reason_receipt: Mapping[str, Any],
    graded_at: float,
) -> bool:
    """Return whether the canonical lifecycle already contains this receipt."""

    if not isinstance(authority, ActionTransactionAuthority):
        return False
    receipt = authority.receipt_for_root(str(root_id or ""))
    if receipt is None:
        return False
    retained = dict(receipt.grades or {}).get(f"{int(horizon_s)}s")
    if not isinstance(retained, Mapping):
        return False
    return bool(
        int(retained.get("grade", 0) or 0) == int(grade)
        and float(retained.get("graded_at", 0.0) or 0.0) == float(graded_at)
        and dict(retained.get("reason_receipt") or {})
        == grade_reason_receipt_projection(reason_receipt)
    )


__all__ = (
    "OWNER",
    "action_lifecycle_contains_referee_grade",
    "project_referee_grade_to_action_lifecycle",
)
