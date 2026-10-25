"""Persistence adapter for action-proof records.

This module owns the proof-specific store operations. Memory remains the
durable container and journal writer; proof/referee code does not call Memory's
old public proof mutation methods directly.
"""

from __future__ import annotations

import time
from typing import Any

from arch4.brain.memory_io import record_to_dict
from arch4.brain.reward_ledger import (
    apply_nonterminal_trajectory_entry,
    build_nonterminal_trajectory_entry,
)
from arch4.learning_gate_contract import LearningGateDecision

OWNER = "arch4.brain.proof_memory_store"


def record_staged_roi_action_record(memory: Any, record: Any) -> dict[str, Any]:
    """Store a delayed ROI evidence record through proof-memory storage."""

    if memory is None:
        return {"stored": False, "reason": "missing_memory_store"}
    if record is None:
        return {"stored": False, "reason": "missing_record"}
    gate_decision = LearningGateDecision.from_mapping(
        getattr(record, "learning_gate", None)
        if isinstance(getattr(record, "learning_gate", None), dict)
        else {}
    )
    record.learning_gate = gate_decision.as_dict()
    if not gate_decision.learning_allowed:
        record.learning_ltm_applied = False
        record.learning_commit_status = "blocked"
        record.learning_policy_reason = _append_reason(
            str(getattr(record, "learning_policy_reason", "") or ""),
            str(gate_decision.block_reason or "learning_gate_blocked"),
        )
    if not _action_record_scope_valid(memory, record):
        return {
            "stored": False,
            "reason": "evidence_scope_quarantined",
            "learning_gate": dict(record.learning_gate or {}),
        }
    try:
        _append_journal_event_strict(
            memory,
            {
                "kind": "staged_roi_action_record",
                "ts": float(
                    getattr(record, "timestamp", time.time())
                    or time.time()
                ),
                "record": record_to_dict(record),
            },
        )
    except Exception as exc:
        return {
            "stored": False,
            "reason": (
                "terminal_child_journal_append_failed:"
                f"{type(exc).__name__}"
            ),
            "learning_gate": dict(record.learning_gate or {}),
        }
    if not _upsert_action_history_record(memory, record):
        raise RuntimeError(
            "terminal_child_scope_changed_after_durable_admission"
        )
    _mark_dirty(memory, events=1)
    return {
        "stored": True,
        "reason": str(getattr(record, "learning_commit_status", "") or "recorded"),
        "learning_gate": dict(record.learning_gate or {}),
    }


def close_action_proof_record(memory: Any, record: Any, *, ts: float) -> dict[str, Any]:
    """Persist final proof-close state for an existing action record."""

    if memory is None:
        return {"closed": False, "reason": "missing_memory_store"}
    if record is None:
        return {"closed": False, "reason": "missing_record"}
    if not _action_record_scope_valid(memory, record):
        return {"closed": False, "reason": "evidence_scope_quarantined"}
    try:
        _append_journal_event_strict(
            memory,
            {
                "kind": "action_proof_close",
                "ts": float(ts or time.time()),
                "record": record_to_dict(record),
            },
        )
    except Exception as exc:
        return {
            "closed": False,
            "reason": (
                "proof_close_journal_append_failed:"
                f"{type(exc).__name__}"
            ),
        }
    if not _upsert_action_history_record(memory, record):
        raise RuntimeError(
            "proof_close_scope_changed_after_durable_admission"
        )
    _mark_dirty(memory, events=1)
    return {"closed": True, "reason": "proof_closed"}


def update_staged_roi_projection_record(
    memory: Any, record: Any, *, ts: float
) -> dict[str, Any]:
    """Persist projection receipts on an already admitted terminal child."""

    if memory is None:
        return {"updated": False, "reason": "missing_memory_store"}
    if record is None:
        return {"updated": False, "reason": "missing_record"}
    if not _action_record_scope_valid(memory, record):
        return {"updated": False, "reason": "evidence_scope_quarantined"}
    try:
        _append_journal_event_strict(
            memory,
            {
                "kind": "staged_roi_projection_state",
                "ts": float(ts or time.time()),
                "record": record_to_dict(record),
            },
        )
    except Exception as exc:
        return {
            "updated": False,
            "reason": (
                "terminal_projection_state_journal_append_failed:"
                f"{type(exc).__name__}"
            ),
        }
    if not _upsert_action_history_record(memory, record):
        raise RuntimeError(
            "terminal_projection_scope_changed_after_durable_admission"
        )
    _mark_dirty(memory, events=1)
    return {"updated": True, "reason": "projection_state_recorded"}


def record_action_proof_feedback(memory: Any, record: Any, *, ts: float, horizon: int) -> dict[str, Any]:
    """Persist a non-terminal Referee feedback receipt for an open proof."""

    if memory is None:
        return {"recorded": False, "reason": "missing_memory_store"}
    if record is None:
        return {"recorded": False, "reason": "missing_record"}
    if not _action_record_scope_valid(memory, record):
        return {"recorded": False, "reason": "evidence_scope_quarantined"}
    trajectory = build_nonterminal_trajectory_entry(record, horizon=int(horizon), ts=float(ts))
    try:
        _append_journal_event_strict(
            memory,
            {
                "kind": "action_proof_feedback",
                "ts": float(ts or time.time()),
                "horizon_s": int(horizon or 0),
                "record": record_to_dict(record),
                "trajectory_feedback": trajectory,
            },
        )
    except Exception as exc:
        return {
            "recorded": False,
            "reason": (
                "proof_feedback_journal_append_failed:"
                f"{type(exc).__name__}"
            ),
        }
    apply_nonterminal_trajectory_entry(memory, trajectory)
    if not _upsert_action_history_record(memory, record):
        raise RuntimeError(
            "proof_feedback_scope_changed_after_durable_admission"
        )
    _mark_dirty(memory, events=1)
    return {
        "recorded": True,
        "reason": "proof_feedback_recorded",
        "trajectory_key": str(trajectory.get("trajectory_key") or ""),
    }


def _append_action_history_record(memory: Any, record: Any) -> bool:
    return bool(memory._append_action_history_record(record))


def _upsert_action_history_record(memory: Any, record: Any) -> bool:
    return bool(memory._upsert_action_history_record(record))


def _action_record_scope_valid(memory: Any, record: Any) -> bool:
    return bool(memory._action_record_scope_valid(record))


def _append_journal_event_strict(
    memory: Any, event: dict[str, Any]
) -> None:
    memory._append_journal_event_strict(event)


def _mark_dirty(memory: Any, *, events: int = 0) -> None:
    memory._mark_dirty(events=events)


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
    "close_action_proof_record",
    "record_action_proof_feedback",
    "record_staged_roi_action_record",
    "update_staged_roi_projection_record",
]
