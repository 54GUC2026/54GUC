"""Brain-owned register for closed action-proof events."""

from __future__ import annotations

import time
from typing import Any, Mapping

from arch4.action_outcome_contract import action_outcome_contract_from_action_record
from arch4.brain.memory_action_history_owner import iter_action_history_reversed
from arch4.brain.secondary_learning_receipt import secondary_learning_receipt_projection
from arch4.brain.event_inbox import BrainEvent
from arch4.brain.transaction_closure_publication_owner import (
    publish_brain_transaction_closure,
)
from arch4.brain.transaction_register import require_brain_transaction_register
from arch4.event_journal import require_event_journal
from arch4.brain.feed_registers import (
    FEED_ACTION_PROOF_CLOSURES,
    OWNER_ACTION_PROOF_REGISTER,
    require_brain_feed_register_bank,
)

SCHEMA = "brain_action_proof_register.v1"
OWNER = OWNER_ACTION_PROOF_REGISTER
EVENT_KIND = "action_proof_closed"
FEEDBACK_EVENT_KIND = "action_proof_feedback"
MAX_RECENT_PROOFS = 64
_SMART_LOOP_IDENTITY_KEYS = (
    "selected_action_command_id",
    "selected_action_proof_id",
    "smart_loop_reason_ref",
    "smart_loop_context_key",
)


def register_action_proof_event_handler(engine: Any) -> dict[str, Any]:
    controller = getattr(engine, "controller", None)
    brain = getattr(controller, "brain", None)
    inbox = getattr(brain, "event_inbox", None)
    register_handler = getattr(inbox, "register_handler", None)
    if not callable(register_handler):
        return {"registered": False, "reason": "brain_event_inbox_unavailable"}
    register_handler(EVENT_KIND, lambda event: record_action_proof_event(engine, event))
    register_handler(
        FEEDBACK_EVENT_KIND,
        lambda event: record_action_proof_feedback_event(engine, event),
    )
    return {
        "registered": True,
        "kind": EVENT_KIND,
        "feedback_kind": FEEDBACK_EVENT_KIND,
        "kinds": [EVENT_KIND, FEEDBACK_EVENT_KIND],
        "owner": OWNER,
    }


def record_action_proof_event(engine: Any, event: BrainEvent, *, now: float | None = None) -> dict[str, Any]:
    return _record_action_proof_event(engine, event, now=now, terminal=True)


def record_action_proof_feedback_event(
    engine: Any, event: BrainEvent, *, now: float | None = None
) -> dict[str, Any]:
    """Record non-terminal Referee feedback without closing the transaction."""

    return _record_action_proof_event(engine, event, now=now, terminal=False)


def _record_action_proof_event(
    engine: Any,
    event: BrainEvent,
    *,
    now: float | None,
    terminal: bool,
) -> dict[str, Any]:
    now_ts = time.time() if now is None else float(now)
    row = _proof_row(event, now=now_ts, terminal=terminal)
    recent = _current_recent_rows(engine)
    recent.append(row)
    if len(recent) > MAX_RECENT_PROOFS:
        recent = recent[-MAX_RECENT_PROOFS:]
    surface = action_proof_surface_from_rows(recent)
    status = {
        "schema": SCHEMA,
        "owner": OWNER,
        "recent_count": len(recent),
        "last_label": row["label"],
        "last_referee_code": row["referee_code"],
        "last_parent_trace_id": row["parent_trace_id"],
        "last_child_trace_id": row["child_trace_id"],
        "last_received_ts": row["received_ts"],
    }
    _publish_action_proof_surface(engine, surface=surface, status=status, now=now_ts)
    if not terminal:
        return dict(row)
    transaction_register = require_brain_transaction_register(engine)
    if row.get("reward_admitted") is True and str(row.get("reward_ledger_key") or ""):
        closure = transaction_register.close_from_terminal_reward(row)
    else:
        closure = transaction_register.close_rejected_terminal(row)
    if closure is not None:
        publish_brain_transaction_closure(
            journal=require_event_journal(engine),
            closure=closure,
            ts=now_ts,
        )
    return dict(row)


def action_proof_surface_from_engine(engine: Any) -> dict[str, Any]:
    bank = require_brain_feed_register_bank(engine)
    row = bank.read_feeder(FEED_ACTION_PROOF_CLOSURES).as_dict()
    if str(row.get("error") or "") or not bool(row.get("valid", False)):
        return _unavailable_action_proof_surface("action_proof_register_invalid")
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    if str(payload.get("schema") or "") != SCHEMA or str(payload.get("owner") or "") != OWNER:
        return _unavailable_action_proof_surface("action_proof_register_owner_schema_mismatch")
    return dict(payload)


def _unavailable_action_proof_surface(reason: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "recent_count": 0,
        "recent": [],
        "last": {},
        "register_fault": True,
        "reason": str(reason or "action_proof_register_unavailable"),
    }


def action_proof_status_from_engine(engine: Any) -> dict[str, Any]:
    surface = action_proof_surface_from_engine(engine)
    last = surface.get("last_terminal") if isinstance(surface.get("last_terminal"), Mapping) else {}
    feedback = surface.get("last_feedback") if isinstance(surface.get("last_feedback"), Mapping) else {}
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "recent_count": _int(surface.get("recent_count")),
        "last_label": _text(last.get("label")),
        "last_referee_code": _text(last.get("referee_code")),
        "last_parent_trace_id": _text(last.get("parent_trace_id")),
        "last_child_trace_id": _text(last.get("child_trace_id")),
        "last_received_ts": _float(last.get("received_ts")),
        "last_feedback_horizon_s": _int(feedback.get("roi_horizon_s")),
        "last_feedback_label": _text(feedback.get("label")),
        "last_feedback_reason": _text(feedback.get("reason")),
        "last_feedback_received_ts": _float(feedback.get("received_ts")),
    }


def action_proof_surface_from_rows(rows: Any) -> dict[str, Any]:
    proof_rows = [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
    last = dict(proof_rows[-1]) if proof_rows else {}
    terminal_rows = [row for row in proof_rows if bool(row.get("terminal", True))]
    feedback_rows = [row for row in proof_rows if not bool(row.get("terminal", True))]
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "recent_count": len(proof_rows),
        "recent": proof_rows[-MAX_RECENT_PROOFS:],
        "last": last,
        "last_terminal": dict(terminal_rows[-1]) if terminal_rows else {},
        "last_feedback": dict(feedback_rows[-1]) if feedback_rows else {},
        "good_count": sum(
            1 for row in proof_rows
            if bool(row.get("terminal", True)) and str(row.get("label", "") or "") == "good"
        ),
        "bad_count": sum(
            1 for row in proof_rows
            if bool(row.get("terminal", True)) and str(row.get("label", "") or "") == "bad"
        ),
        "neutral_count": sum(
            1 for row in proof_rows
            if bool(row.get("terminal", True)) and str(row.get("label", "") or "") == "neutral"
        ),
        "feedback_count": sum(1 for row in proof_rows if not bool(row.get("terminal", True))),
    }


def action_proof_surface_from_memory(memory: Any, *, max_records: int = MAX_RECENT_PROOFS) -> dict[str, Any]:
    """Build the owner proof surface from durable admitted action history."""

    rows: list[dict[str, Any]] = []
    limit = max(1, int(max_records or MAX_RECENT_PROOFS))
    for record in iter_action_history_reversed(memory):
        row = _proof_row_from_action_record(record)
        if not row:
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    rows.reverse()
    return action_proof_surface_from_rows(rows)


def _current_recent_rows(engine: Any) -> list[dict[str, Any]]:
    surface = action_proof_surface_from_engine(engine)
    recent = surface.get("recent") if isinstance(surface.get("recent"), list) else []
    return [dict(row) for row in recent if isinstance(row, Mapping)]


def _publish_action_proof_surface(
    engine: Any,
    *,
    surface: Mapping[str, Any],
    status: Mapping[str, Any],
    now: float,
) -> None:
    bank = require_brain_feed_register_bank(engine)
    payload = dict(surface or {})
    payload["status"] = dict(status or {})
    bank.write_feeder(
        FEED_ACTION_PROOF_CLOSURES,
        owner=OWNER,
        payload=payload,
        source="brain_action_proof_register",
        updated_at=float(now),
        valid=True,
        stale_after_s=0.0,
    )


def action_proof_closure_from_mapping(row: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt a replay/exported action-proof event row through the register contract."""
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else row
    secondary = _secondary_learning_payload(payload)
    secondary_projection = secondary_learning_receipt_projection(secondary)
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), Mapping) else {}
    layered_feedback = (
        payload.get("layered_feedback")
        if isinstance(payload.get("layered_feedback"), Mapping)
        else {}
    )
    proof_identity = payload.get("proof_identity") if isinstance(payload.get("proof_identity"), Mapping) else {}
    return {
        "schema": "brain_action_proof_closure.v1",
        "owner": OWNER,
        "event_id": _text(row.get("event_id")),
        "source": _text(row.get("source") or row.get("event") or row.get("type") or row.get("record_type")),
        "action_root_id": _text(
            payload.get("action_root_id") or proof_identity.get("action_root_id")
        ),
        "parent_trace_id": _text(payload.get("parent_trace_id")),
        "child_trace_id": _text(payload.get("child_trace_id")),
        "roi_horizon_s": _int(payload.get("roi_horizon_s")),
        "label": _text(payload.get("label")),
        "reason": _text(payload.get("reason")),
        "referee_code": _text(payload.get("referee_code")),
        "referee_level": _text(payload.get("referee_level")),
        "learning_commit_status": _text(payload.get("learning_commit_status")),
        "parent_status": _text(payload.get("parent_status")),
        "terminal": bool(payload.get("terminal", True)),
        "memory_admitted": bool(payload.get("memory_admitted", True)),
        "grade_kind": _text(payload.get("grade_kind") or "final"),
        **secondary_projection,
        **_smart_loop_identity(payload),
        "layered_feedback": dict(layered_feedback),
        "proof_identity": dict(proof_identity),
        **_proof_evidence_projection(evidence),
        "received_ts": _float(row.get("received_ts") or row.get("ts")),
    }


def action_proof_roi_surface(surface: Mapping[str, Any]) -> dict[str, Any]:
    proof = surface if isinstance(surface, Mapping) else {}
    recent = proof.get("recent") if isinstance(proof.get("recent"), list) else []
    last_any = proof.get("last") if isinstance(proof.get("last"), Mapping) else {}
    terminal_rows = [
        row for row in recent
        if isinstance(row, Mapping) and bool(row.get("terminal", True))
    ]
    last = terminal_rows[-1] if terminal_rows else {}
    return {
        "action_proof_recent_count": len(terminal_rows),
        "action_proof_feedback_count": sum(
            1 for row in recent
            if isinstance(row, Mapping) and not bool(row.get("terminal", True))
        ),
        "action_proof_bad_count": _int(proof.get("bad_count")),
        "action_proof_good_count": _int(proof.get("good_count")),
        "last_action_proof_label": _text(last.get("label")),
        "last_action_proof_referee_code": _text(last.get("referee_code")),
        "last_action_proof_reason": _text(last.get("reason")),
        "last_action_proof_secondary_learning_status": _text(
            last.get("secondary_learning_status")
        ),
        "last_action_proof_selected_action_command_id": _text(last.get("selected_action_command_id")),
        "last_action_proof_selected_action_proof_id": _text(last.get("selected_action_proof_id")),
        "last_action_proof_smart_loop_reason_ref": _text(last.get("smart_loop_reason_ref")),
        "last_action_proof_smart_loop_context_key": _text(last.get("smart_loop_context_key")),
        "last_action_feedback_horizon_s": _int(last_any.get("roi_horizon_s"))
        if not bool(last_any.get("terminal", True))
        else 0,
        "last_action_feedback_label": _text(last_any.get("label"))
        if not bool(last_any.get("terminal", True))
        else "",
        "last_action_feedback_reason": _text(last_any.get("reason"))
        if not bool(last_any.get("terminal", True))
        else "",
        "action_proof_memory_commit_count": _count_bool(recent, "memory_committed"),
        "action_proof_historical_commit_count": _count_bool(recent, "historical_committed"),
        "action_proof_crystal_ball_update_count": _count_bool(recent, "crystal_ball_updated"),
        "action_proof_crystal_ball_regrade_count": _count_bool(recent, "crystal_ball_regraded"),
    }


def _proof_row(event: BrainEvent, *, now: float, terminal: bool = True) -> dict[str, Any]:
    payload = dict(event.payload or {}) if isinstance(event.payload, Mapping) else {}
    secondary = _secondary_learning_payload(payload)
    secondary_projection = secondary_learning_receipt_projection(secondary)
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), Mapping) else {}
    layered_feedback = (
        payload.get("layered_feedback")
        if isinstance(payload.get("layered_feedback"), Mapping)
        else {}
    )
    proof_identity = (
        payload.get("proof_identity")
        if isinstance(payload.get("proof_identity"), Mapping)
        else {}
    )
    return {
        "schema": "brain_action_proof_closure.v1",
        "owner": OWNER,
        "event_id": str(event.event_id or ""),
        "source": str(event.source or ""),
        "action_root_id": _text(
            payload.get("action_root_id") or proof_identity.get("action_root_id")
        ),
        "parent_trace_id": _text(payload.get("parent_trace_id")),
        "child_trace_id": _text(payload.get("child_trace_id")),
        "roi_horizon_s": _int(payload.get("roi_horizon_s")),
        "label": _text(payload.get("label")),
        "reason": _text(payload.get("reason")),
        "referee_code": _text(payload.get("referee_code")),
        "referee_level": _text(payload.get("referee_level")),
        "learning_commit_status": _text(payload.get("learning_commit_status")),
        "parent_status": _text(payload.get("parent_status")),
        "terminal": bool(payload.get("terminal", terminal)) if not terminal else True,
        "memory_admitted": bool(payload.get("memory_admitted", terminal)),
        "grade_kind": _text(payload.get("grade_kind") or ("final" if terminal else "smart_loop_interim")),
        **secondary_projection,
        **_smart_loop_identity(payload),
        "layered_feedback": dict(layered_feedback),
        "proof_identity": dict(proof_identity),
        **_proof_evidence_projection(evidence),
        "received_ts": round(float(now), 3),
    }


def _proof_row_from_action_record(record: Any) -> dict[str, Any]:
    commit_status = _text(getattr(record, "learning_commit_status", "")).lower()
    if commit_status in {"", "blocked", "pending", "skipped"}:
        return {}
    objective_metadata = (
        getattr(record, "objective_metadata", None)
        if isinstance(getattr(record, "objective_metadata", None), Mapping)
        else {}
    )
    if _text(objective_metadata.get("staged_roi_parent_status")).lower() == "proof_closed":
        return {}
    contract = action_outcome_contract_from_action_record(record)
    label = _text(contract.get("learning_outcome_label")).lower()
    code = _text(contract.get("code"))
    level = _text(contract.get("level"))
    if label not in {"good", "bad", "neutral"}:
        return {}
    if not code and not level:
        return {}
    secondary = (
        objective_metadata.get("secondary_learning")
        if isinstance(objective_metadata.get("secondary_learning"), Mapping)
        else {
            "memory_committed": commit_status in {"committed", "classified", "proof_closed"},
            "historical_committed": False,
            "crystal_ball_updated": False,
            "crystal_ball_regraded": False,
        }
    )
    secondary_projection = secondary_learning_receipt_projection(secondary)
    evidence = (
        objective_metadata.get("staged_roi_evidence")
        if isinstance(objective_metadata.get("staged_roi_evidence"), Mapping)
        else {}
    )
    layered_feedback = (
        objective_metadata.get("layered_action_feedback")
        if isinstance(objective_metadata.get("layered_action_feedback"), Mapping)
        else {}
    )
    proof_identity = (
        objective_metadata.get("delayed_reward_proof_identity")
        if isinstance(objective_metadata.get("delayed_reward_proof_identity"), Mapping)
        else {}
    )
    reward_receipt = (
        objective_metadata.get("reward_ledger_receipt")
        if isinstance(objective_metadata.get("reward_ledger_receipt"), Mapping)
        else {}
    )
    durable_memory_admitted = bool(reward_receipt.get("admitted") is True)
    return {
        "schema": "brain_action_proof_closure.v1",
        "owner": OWNER,
        "event_id": "",
        "source": "durable_action_history",
        "action_root_id": _text(proof_identity.get("action_root_id")),
        "parent_trace_id": _text(getattr(record, "roi_parent_trace_id", "")),
        "child_trace_id": _text(getattr(record, "decision_trace_id", "")),
        "roi_horizon_s": _int(getattr(record, "roi_horizon_s", 0)),
        "label": label,
        "reason": _text(contract.get("reason") or getattr(record, "learning_policy_reason", "")),
        "referee_code": code,
        "referee_level": level,
        "learning_commit_status": commit_status,
        "parent_status": _text(getattr(record, "objective_status", "")),
        "terminal": True,
        "memory_admitted": durable_memory_admitted,
        "grade_kind": "terminal_layered_reward",
        **secondary_projection,
        **_smart_loop_identity(objective_metadata),
        "layered_feedback": dict(layered_feedback),
        "proof_identity": dict(proof_identity),
        **_proof_evidence_projection(evidence),
        "received_ts": round(_float(getattr(record, "timestamp", 0.0)), 3),
    }


def _text(value: Any) -> str:
    return str(value or "").strip()


def _secondary_learning_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    secondary = payload.get("secondary_learning")
    if isinstance(secondary, Mapping):
        return secondary
    return payload


def _smart_loop_identity(payload: Mapping[str, Any]) -> dict[str, str]:
    return {key: _text(payload.get(key)) for key in _SMART_LOOP_IDENTITY_KEYS}


def _proof_evidence_projection(evidence: Mapping[str, Any]) -> dict[str, Any]:
    row = evidence if isinstance(evidence, Mapping) else {}
    return {
        "grade_context_status": _text(row.get("grade_context_status")),
        "grade_evidence_valid": bool(row.get("grade_evidence_valid", False)),
        "cq_before": _optional_float(row.get("cq_before")),
        "cq_now": _optional_float(row.get("cq_now")),
        "congestion_before": _optional_float(row.get("congestion_before")),
        "congestion_now": _optional_float(row.get("congestion_now")),
        "congestion_before_valid": bool(row.get("congestion_before_valid", False)),
        "congestion_now_valid": bool(row.get("congestion_now_valid", False)),
    }


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _count_bool(rows: list[Any], key: str) -> int:
    return sum(1 for row in rows if isinstance(row, Mapping) and bool(row.get(key, False)))


__all__ = [
    "EVENT_KIND",
    "FEEDBACK_EVENT_KIND",
    "OWNER",
    "SCHEMA",
    "action_proof_closure_from_mapping",
    "action_proof_roi_surface",
    "action_proof_status_from_engine",
    "action_proof_surface_from_memory",
    "action_proof_surface_from_engine",
    "action_proof_surface_from_rows",
    "record_action_proof_event",
    "record_action_proof_feedback_event",
    "register_action_proof_event_handler",
]
