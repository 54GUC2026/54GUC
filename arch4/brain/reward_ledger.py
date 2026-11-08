"""Durable admission owner for delayed SmartLoop/Referee rewards.

The Referee remains the only grader.  This owner does not calculate or alter a
grade; it durably admits one already owner-stamped terminal grade and rejects
duplicate delivery before secondary learning fanout is allowed to run.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from arch4.brain.memory_io import record_to_dict
from arch4.evidence_scope import EvidenceScope
from arch4.terminal_reward_contract import (
    TERMINAL_REWARD_FORMULA_GENERATION as FORMULA_GENERATION,
    TERMINAL_REWARD_OWNER as OWNER,
    TERMINAL_REWARD_SCHEMA as TERMINAL_SCHEMA,
    reward_context_key as _reward_context_key,
    terminal_reward_formula_vector,
    terminal_reward_entry_error as _terminal_reward_entry_error,
    terminal_reward_prediction,
)


TRAJECTORY_SCHEMA = "RewardTrajectoryMilestone.v3"
TERMINAL_EVENT_KIND = "terminal_reward_commit"
PROJECTION_RECEIPT_EVENT_KIND = "reward_projection_receipt"
MILESTONE_PROJECTION_RECEIPT_EVENT_KIND = (
    "reward_trajectory_projection_receipt"
)
PROJECTION_OUTBOX_SCHEMA = "RewardProjectionOutbox.v3"
PROJECTION_RECEIPT_SCHEMA = "RewardProjectionReceipt.v3"
REWARD_QUARANTINE_SCHEMA = "RewardEvidenceQuarantine.v1"
MILESTONE_PROJECTION_RECEIPT_SCHEMA = "RewardTrajectoryProjectionReceipt.v2"
PROJECTION_NAMES = (
    "memory",
    "bandit",
    "historical",
    "crystal",
    "brain",
    "evidence",
    "action_lifecycle",
)
TRAJECTORY_PROJECTION_NAMES = ("brain", "action_lifecycle")


def admit_terminal_reward(
    memory: Any,
    *,
    record: Any,
    proof_identity: Mapping[str, Any],
    terminal_observation: Mapping[str, Any],
    layered_feedback: Mapping[str, Any],
    evidence: Mapping[str, Any],
    ts: float,
) -> dict[str, Any]:
    """Durably admit one terminal reward before any learning projection.

    The deterministic key includes proof, terminal event, context, and reward
    formula generation.  An identical delivery is a successful idempotent
    duplicate; a conflicting delivery for the same key fails closed.
    """

    if memory is None:
        return _receipt(reason="missing_memory_store")
    entry = build_terminal_reward_entry(
        record=record,
        proof_identity=proof_identity,
        terminal_observation=terminal_observation,
        layered_feedback=layered_feedback,
        evidence=evidence,
        ts=ts,
    )
    invalid_reason = _terminal_reward_entry_error(entry)
    if invalid_reason:
        return _receipt(reason=invalid_reason, entry=entry)

    ledger, _trajectory = ensure_reward_ledger_state(memory)
    ledger_key = str(entry["ledger_key"])
    existing = ledger.get(ledger_key)
    if isinstance(existing, Mapping):
        if _same_terminal_reward(existing, entry):
            return _receipt(
                admitted=False,
                duplicate=True,
                reason="terminal_reward_already_admitted",
                entry=existing,
            )
        return _receipt(reason="terminal_reward_ledger_conflict", entry=existing, conflict=entry)

    journal_event = {
        "kind": TERMINAL_EVENT_KIND,
        "ts": float(ts),
        "record": dict(entry),
    }
    try:
        memory._append_journal_event_strict(journal_event)
    except Exception as exc:
        return _receipt(
            reason="terminal_reward_journal_append_failed",
            entry=entry,
            error=type(exc).__name__,
        )

    apply_terminal_reward_entry(memory, entry)
    memory._mark_dirty(events=1)
    return _receipt(admitted=True, reason="terminal_reward_admitted", entry=entry)


def build_terminal_reward_entry(
    *,
    record: Any,
    proof_identity: Mapping[str, Any],
    terminal_observation: Mapping[str, Any],
    layered_feedback: Mapping[str, Any],
    evidence: Mapping[str, Any],
    ts: float,
) -> dict[str, Any]:
    identity = dict(proof_identity or {})
    feedback = dict(layered_feedback or {})
    evidence_row = dict(evidence or {})
    observation = dict(terminal_observation or {})
    reward_scope = _reward_evidence_scope_from_record(record)
    parent_trace_id = str(identity.get("parent_trace_id") or getattr(record, "roi_parent_trace_id", "") or "")
    proof_id = str(identity.get("selected_action_proof_id") or parent_trace_id)
    event_id = str(identity.get("child_trace_id") or getattr(record, "decision_trace_id", "") or "")
    context_key = _reward_context_key(identity)
    horizon_s = int(getattr(record, "roi_horizon_s", 0) or 0)
    key_parts = {
        "action_root_id": str(identity.get("action_root_id") or ""),
        "proof_id": proof_id,
        "event_id": event_id,
        "context_key": context_key,
        "opportunity_id": str(identity.get("opportunity_id") or ""),
        "opportunity_set_id": str(identity.get("opportunity_set_id") or ""),
        "decision_id": str(identity.get("decision_id") or ""),
        "decision_receipt_id": str(identity.get("decision_receipt_id") or ""),
        "bandit_selection_receipt_id": str(
            identity.get("bandit_selection_receipt_id") or ""
        ),
        "terminal_observation_id": str(
            observation.get("observation_id") or ""
        ),
        "evidence_scope_id": reward_scope.scope_id,
        "formula_generation": FORMULA_GENERATION,
        "horizon_s": horizon_s,
    }
    score = feedback.get("memory_grade_score")
    prediction = terminal_reward_prediction(
        bandit_selection_receipt=identity.get("bandit_selection_receipt"),
        grade=int(score or 0),
    )
    key_parts["prediction_id"] = str(prediction.get("prediction_id") or "")
    ledger_key = hashlib.sha256(_canonical_json(key_parts).encode("utf-8")).hexdigest()
    return {
        "schema": TERMINAL_SCHEMA,
        "owner": OWNER,
        "ledger_key": ledger_key,
        **key_parts,
        "parent_trace_id": parent_trace_id,
        "command_id": str(identity.get("selected_action_command_id") or ""),
        "bandit_selection_receipt": dict(
            identity.get("bandit_selection_receipt") or {}
        ),
        "brain_decision_receipt": dict(
            identity.get("brain_decision_receipt") or {}
        ),
        "terminal_observation": observation,
        "evidence_scope": reward_scope.as_dict(),
        "grade": score,
        "label": str(feedback.get("learning_label") or feedback.get("label") or "neutral"),
        "proof_identity": identity,
        "layered_feedback": feedback,
        "prediction": prediction,
        "evidence": _reward_evidence(evidence_row),
        "admitted_ts": round(float(ts), 3),
        "projection_payload": {
            "record": record_to_dict(record),
            "evidence": dict(evidence_row),
            "label": str(feedback.get("learning_label") or feedback.get("label") or "neutral"),
            "reason": str(feedback.get("reason") or ""),
            "referee_code": str(getattr(record, "referee_outcome_code", "") or ""),
            "referee_level": str(getattr(record, "referee_outcome_level", "") or ""),
            "ts": round(float(ts), 3),
        },
    }


def build_nonterminal_trajectory_entry(record: Any, *, horizon: int, ts: float) -> dict[str, Any]:
    """Build the durable 1m/6m observation row without creating a reward."""

    meta_raw = getattr(record, "objective_metadata", {}) or {}
    meta = dict(meta_raw) if isinstance(meta_raw, Mapping) else {}
    feedback_rows = meta.get("smart_loop_feedback") if isinstance(meta.get("smart_loop_feedback"), list) else []
    feedback = next(
        (
            dict(row)
            for row in reversed(feedback_rows)
            if isinstance(row, Mapping) and int(row.get("horizon_s", 0) or 0) == int(horizon)
        ),
        {},
    )
    proof_id = str(meta.get("selected_action_proof_id") or getattr(record, "decision_trace_id", "") or "")
    event_id = f"{getattr(record, 'decision_trace_id', '')}:feedback:{int(horizon)}"
    context_key = str(meta.get("smart_loop_context_key") or "")
    key_parts = {
        "proof_id": proof_id,
        "event_id": event_id,
        "context_key": context_key,
        "formula_generation": FORMULA_GENERATION,
        "horizon_s": int(horizon),
    }
    trajectory_key = hashlib.sha256(_canonical_json(key_parts).encode("utf-8")).hexdigest()
    return {
        "schema": TRAJECTORY_SCHEMA,
        "owner": OWNER,
        "trajectory_key": trajectory_key,
        **key_parts,
        "terminal": False,
        "memory_admitted": False,
        "strategic_reward": False,
        "feedback": feedback,
        "evidence_scope": _reward_evidence_scope_from_record(record).as_dict(),
        "projection_payload": {
            "record": record_to_dict(record),
            "feedback": feedback,
        },
        "projections": {
            name: {"status": "pending"}
            for name in TRAJECTORY_PROJECTION_NAMES
        },
        "recorded_ts": round(float(ts), 3),
    }


def apply_terminal_reward_entry(memory: Any, entry: Mapping[str, Any]) -> bool:
    """Apply a terminal entry during live admission, snapshot load, or replay."""

    row = dict(entry or {})
    invalid_reason = _terminal_reward_entry_error(row)
    if invalid_reason:
        raise ValueError(invalid_reason)
    ledger, _trajectory = ensure_reward_ledger_state(memory)
    key = str(row["ledger_key"])
    existing = ledger.get(key)
    if isinstance(existing, Mapping):
        if not _same_terminal_reward(existing, row):
            raise ValueError("terminal_reward_ledger_conflict")
        return False
    ledger[key] = row
    _ensure_projection_outbox_entry(memory, row)
    return True


def complete_reward_projection(
    memory: Any,
    *,
    ledger_key: str,
    projection: str,
    receipt: Mapping[str, Any],
    ts: float,
) -> dict[str, Any]:
    """Durably mark one destination projection complete for one reward."""

    name = str(projection or "").strip().lower()
    if name not in PROJECTION_NAMES:
        raise ValueError(f"reward_projection_name_invalid:{name}")
    ledger, _trajectory = ensure_reward_ledger_state(memory)
    key = str(ledger_key or "")
    if key not in ledger:
        raise ValueError("reward_projection_ledger_key_unknown")
    outbox = reward_projection_outbox_entry(memory, key)
    existing = dict((outbox.get("projections") or {}).get(name) or {})
    if existing.get("status") == "completed":
        return existing
    row = {
        "schema": PROJECTION_RECEIPT_SCHEMA,
        "owner": OWNER,
        "ledger_key": key,
        "projection": name,
        "status": "completed",
        "receipt": dict(receipt or {}),
        "completed_ts": round(float(ts), 3),
    }
    memory._append_journal_event_strict(
        {
            "kind": PROJECTION_RECEIPT_EVENT_KIND,
            "ts": float(ts),
            "record": dict(row),
        }
    )
    apply_reward_projection_receipt(memory, row)
    memory._mark_dirty(events=1)
    return row


def apply_reward_projection_receipt(memory: Any, receipt: Mapping[str, Any]) -> bool:
    row = dict(receipt or {})
    if row.get("schema") != PROJECTION_RECEIPT_SCHEMA or row.get("owner") != OWNER:
        raise ValueError("reward_projection_receipt_owner_schema_invalid")
    key = str(row.get("ledger_key") or "")
    name = str(row.get("projection") or "").strip().lower()
    if not key or name not in PROJECTION_NAMES or row.get("status") != "completed":
        raise ValueError("reward_projection_receipt_invalid")
    ledger, _trajectory = ensure_reward_ledger_state(memory)
    if key not in ledger:
        raise ValueError("reward_projection_receipt_ledger_missing")
    outbox = _ensure_projection_outbox_entry(memory, ledger[key])
    projections = outbox["projections"]
    existing = dict(projections.get(name) or {})
    if existing.get("status") == "completed":
        if _canonical_json(existing.get("receipt")) != _canonical_json(row.get("receipt")):
            raise ValueError("reward_projection_receipt_conflict")
        return False
    projections[name] = row
    return True


def reward_projection_outbox_entry(memory: Any, ledger_key: str) -> dict[str, Any]:
    ledger, _trajectory = ensure_reward_ledger_state(memory)
    key = str(ledger_key or "")
    entry = ledger.get(key)
    if not isinstance(entry, Mapping):
        return {}
    return _ensure_projection_outbox_entry(memory, entry)


def reward_ledger_entry(memory: Any, ledger_key: str) -> dict[str, Any]:
    ledger, _trajectory = ensure_reward_ledger_state(memory)
    row = ledger.get(str(ledger_key or ""))
    return dict(row) if isinstance(row, Mapping) else {}


def pending_reward_projection_entries(memory: Any) -> list[dict[str, Any]]:
    ledger, _trajectory = ensure_reward_ledger_state(memory)
    rows: list[dict[str, Any]] = []
    for key, entry in ledger.items():
        if not isinstance(entry, Mapping):
            continue
        outbox = _ensure_projection_outbox_entry(memory, entry)
        projections = outbox.get("projections") if isinstance(outbox.get("projections"), Mapping) else {}
        if any(dict(projections.get(name) or {}).get("status") != "completed" for name in PROJECTION_NAMES):
            rows.append({"ledger_entry": dict(entry), "outbox": outbox, "ledger_key": str(key)})
    return rows


def reward_projection_entries(memory: Any) -> list[dict[str, Any]]:
    ledger, _trajectory = ensure_reward_ledger_state(memory)
    return [
        {
            "ledger_entry": dict(entry),
            "outbox": _ensure_projection_outbox_entry(memory, entry),
            "ledger_key": str(key),
        }
        for key, entry in ledger.items()
        if isinstance(entry, Mapping)
    ]


def projection_is_completed(memory: Any, ledger_key: str, projection: str) -> bool:
    outbox = reward_projection_outbox_entry(memory, ledger_key)
    row = dict((outbox.get("projections") or {}).get(str(projection or "")) or {})
    return row.get("status") == "completed"


def apply_nonterminal_trajectory_entry(memory: Any, entry: Mapping[str, Any]) -> bool:
    """Restore one nonterminal observation; never project it as reward."""

    row = dict(entry or {})
    if row.get("schema") != TRAJECTORY_SCHEMA or row.get("owner") != OWNER:
        raise ValueError("nonterminal_trajectory_owner_schema_invalid")
    if (
        row.get("terminal") is not False
        or row.get("memory_admitted") is not False
        or row.get("strategic_reward") is not False
    ):
        raise ValueError("nonterminal_trajectory_reward_forbidden")
    horizon = row.get("horizon_s")
    if type(horizon) is not int or horizon not in {60, 360}:
        raise ValueError("nonterminal_trajectory_horizon_invalid")
    key = str(row.get("trajectory_key") or "")
    if not key:
        raise ValueError("nonterminal_trajectory_key_missing")
    try:
        scope = EvidenceScope.from_mapping(
            dict(row.get("evidence_scope") or {})
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"nonterminal_trajectory_evidence_scope_invalid:{exc}"
        ) from exc
    if scope.formula_vector != terminal_reward_formula_vector():
        raise ValueError("nonterminal_trajectory_formula_vector_invalid")
    _ledger, trajectory = ensure_reward_ledger_state(memory)
    existing = trajectory.get(key)
    if isinstance(existing, Mapping):
        if _canonical_json(existing) != _canonical_json(row):
            raise ValueError("nonterminal_trajectory_conflict")
        return False
    trajectory[key] = row
    return True


def complete_trajectory_projection(
    memory: Any,
    *,
    trajectory_key: str,
    projection: str,
    receipt: Mapping[str, Any],
    ts: float,
) -> dict[str, Any]:
    _ledger, trajectory = ensure_reward_ledger_state(memory)
    key = str(trajectory_key or "")
    entry = trajectory.get(key)
    if not isinstance(entry, dict):
        raise ValueError("trajectory_projection_key_unknown")
    name = str(projection or "").strip().lower()
    if name not in TRAJECTORY_PROJECTION_NAMES:
        raise ValueError(f"trajectory_projection_name_invalid:{name}")
    projections = entry.get("projections")
    if not isinstance(projections, dict):
        raise ValueError("trajectory_projection_register_missing")
    existing = dict(projections.get(name) or {})
    if existing.get("status") == "completed":
        return existing
    row = {
        "schema": MILESTONE_PROJECTION_RECEIPT_SCHEMA,
        "owner": OWNER,
        "trajectory_key": key,
        "projection": name,
        "status": "completed",
        "receipt": dict(receipt or {}),
        "completed_ts": round(float(ts), 3),
    }
    memory._append_journal_event_strict(
        {
            "kind": MILESTONE_PROJECTION_RECEIPT_EVENT_KIND,
            "ts": float(ts),
            "record": dict(row),
        }
    )
    apply_trajectory_projection_receipt(memory, row)
    memory._mark_dirty(events=1)
    return row


def apply_trajectory_projection_receipt(
    memory: Any, receipt: Mapping[str, Any]
) -> bool:
    row = dict(receipt or {})
    if (
        row.get("schema") != MILESTONE_PROJECTION_RECEIPT_SCHEMA
        or row.get("owner") != OWNER
        or row.get("status") != "completed"
    ):
        raise ValueError("trajectory_projection_receipt_invalid")
    _ledger, trajectory = ensure_reward_ledger_state(memory)
    key = str(row.get("trajectory_key") or "")
    entry = trajectory.get(key)
    if not isinstance(entry, dict):
        raise ValueError("trajectory_projection_entry_missing")
    name = str(row.get("projection") or "").strip().lower()
    if name not in TRAJECTORY_PROJECTION_NAMES:
        raise ValueError("trajectory_projection_name_invalid")
    projections = entry.get("projections")
    if not isinstance(projections, dict):
        raise ValueError("trajectory_projection_register_missing")
    existing = dict(projections.get(name) or {})
    if existing.get("status") == "completed":
        if _canonical_json(existing) != _canonical_json(row):
            raise ValueError("trajectory_projection_receipt_conflict")
        return False
    projections[name] = row
    return True


def pending_trajectory_projection_entries(memory: Any) -> list[dict[str, Any]]:
    _ledger, trajectory = ensure_reward_ledger_state(memory)
    return [
        dict(row)
        for row in trajectory.values()
        if isinstance(row, Mapping)
        and any(
            dict(dict(row.get("projections") or {}).get(name) or {}).get(
                "status"
            )
            != "completed"
            for name in TRAJECTORY_PROJECTION_NAMES
        )
    ]


def trajectory_projection_is_completed(
    memory: Any,
    *,
    trajectory_key: str,
    projection: str,
) -> bool:
    _ledger, trajectory = ensure_reward_ledger_state(memory)
    entry = trajectory.get(str(trajectory_key or ""))
    if not isinstance(entry, Mapping):
        return False
    name = str(projection or "").strip().lower()
    if name not in TRAJECTORY_PROJECTION_NAMES:
        raise ValueError(f"trajectory_projection_name_invalid:{name}")
    return (
        dict(dict(entry.get("projections") or {}).get(name) or {}).get("status")
        == "completed"
    )


def ensure_reward_ledger_state(memory: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    ledger = getattr(memory, "_reward_ledger", None)
    if not isinstance(ledger, dict):
        ledger = {}
        memory._reward_ledger = ledger
    trajectory = getattr(memory, "_reward_trajectory_feedback", None)
    if not isinstance(trajectory, dict):
        trajectory = {}
        memory._reward_trajectory_feedback = trajectory
    if not isinstance(getattr(memory, "_reward_projection_outbox", None), dict):
        memory._reward_projection_outbox = {}
    if not isinstance(getattr(memory, "_reward_projection_effect_keys", None), set):
        memory._reward_projection_effect_keys = set()
    if not isinstance(getattr(memory, "_reward_evidence_quarantine", None), list):
        memory._reward_evidence_quarantine = []
    return ledger, trajectory


def reward_ledger_snapshot(memory: Any) -> dict[str, Any]:
    ledger, _trajectory = ensure_reward_ledger_state(memory)
    return {str(key): dict(value) for key, value in ledger.items() if isinstance(value, Mapping)}


def reward_trajectory_snapshot(memory: Any) -> dict[str, Any]:
    _ledger, trajectory = ensure_reward_ledger_state(memory)
    return {str(key): dict(value) for key, value in trajectory.items() if isinstance(value, Mapping)}


def reward_projection_outbox_snapshot(memory: Any) -> dict[str, Any]:
    ensure_reward_ledger_state(memory)
    return {
        str(key): dict(value)
        for key, value in memory._reward_projection_outbox.items()
        if isinstance(value, Mapping)
    }


def reward_projection_effect_keys_snapshot(memory: Any) -> list[str]:
    ensure_reward_ledger_state(memory)
    return sorted(str(key) for key in memory._reward_projection_effect_keys if str(key))


def restore_reward_ledger_snapshot(
    memory: Any,
    *,
    terminal_rows: Mapping[str, Any] | None,
    trajectory_rows: Mapping[str, Any] | None,
    projection_rows: Mapping[str, Any] | None = None,
    projection_effect_keys: Any = None,
) -> None:
    memory._reward_ledger = {}
    memory._reward_trajectory_feedback = {}
    memory._reward_projection_outbox = {}
    memory._reward_projection_effect_keys = {
        str(key) for key in (projection_effect_keys or []) if str(key or "")
    }
    memory._reward_evidence_quarantine = list(
        getattr(memory, "_reward_evidence_quarantine", []) or []
    )
    for key, value in dict(terminal_rows or {}).items():
        if isinstance(value, Mapping):
            try:
                apply_terminal_reward_entry(memory, value)
            except (TypeError, ValueError) as exc:
                quarantine_reward_evidence(
                    memory,
                    kind="terminal_reward",
                    reason=str(exc),
                    row=dict(value),
                    source=f"snapshot:{key}",
                )
    for key, value in dict(trajectory_rows or {}).items():
        if isinstance(value, Mapping):
            try:
                apply_nonterminal_trajectory_entry(memory, value)
            except (TypeError, ValueError) as exc:
                quarantine_reward_evidence(
                    memory,
                    kind="trajectory_milestone",
                    reason=str(exc),
                    row=dict(value),
                    source=f"snapshot:{key}",
                )
    for key, value in dict(projection_rows or {}).items():
        if not isinstance(value, Mapping) or str(key) not in memory._reward_ledger:
            continue
        _ensure_projection_outbox_entry(memory, memory._reward_ledger[str(key)])
        rows = value.get("projections") if isinstance(value.get("projections"), Mapping) else {}
        for name, receipt in rows.items():
            if isinstance(receipt, Mapping) and dict(receipt).get("status") == "completed":
                try:
                    apply_reward_projection_receipt(memory, receipt)
                except (TypeError, ValueError) as exc:
                    quarantine_reward_evidence(
                        memory,
                        kind="reward_projection_receipt",
                        reason=str(exc),
                        row=dict(receipt),
                        source=f"snapshot:{key}:{name}",
                    )


def quarantine_reward_evidence(
    memory: Any,
    *,
    kind: str,
    reason: str,
    row: Mapping[str, Any],
    source: str,
) -> dict[str, Any]:
    ensure_reward_ledger_state(memory)
    entry = {
        "schema": REWARD_QUARANTINE_SCHEMA,
        "owner": OWNER,
        "kind": str(kind or "unknown"),
        "reason": str(reason or "reward_evidence_invalid"),
        "source": str(source or "unknown"),
        "row": dict(row or {}),
    }
    memory._reward_evidence_quarantine.append(entry)
    if len(memory._reward_evidence_quarantine) > 512:
        memory._reward_evidence_quarantine = (
            memory._reward_evidence_quarantine[-512:]
        )
    return entry


def reward_evidence_quarantine_snapshot(memory: Any) -> list[dict[str, Any]]:
    ensure_reward_ledger_state(memory)
    return [
        dict(row)
        for row in memory._reward_evidence_quarantine
        if isinstance(row, Mapping)
    ]


def _ensure_projection_outbox_entry(memory: Any, ledger_entry: Mapping[str, Any]) -> dict[str, Any]:
    ensure_reward_ledger_state(memory)
    key = str(ledger_entry.get("ledger_key") or "")
    if not key:
        raise ValueError("reward_projection_outbox_ledger_key_missing")
    existing = memory._reward_projection_outbox.get(key)
    if isinstance(existing, dict):
        projections = existing.setdefault("projections", {})
        for name in PROJECTION_NAMES:
            projections.setdefault(name, {"status": "pending"})
        return existing
    row = {
        "schema": PROJECTION_OUTBOX_SCHEMA,
        "owner": OWNER,
        "ledger_key": key,
        "projections": {name: {"status": "pending"} for name in PROJECTION_NAMES},
    }
    memory._reward_projection_outbox[key] = row
    return row


def _same_terminal_reward(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    fields = (
        "ledger_key",
        "proof_id",
        "event_id",
        "context_key",
        "formula_generation",
        "horizon_s",
        "grade",
        "label",
        "proof_identity",
        "action_root_id",
        "opportunity_id",
        "opportunity_set_id",
        "decision_id",
        "decision_receipt_id",
        "bandit_selection_receipt_id",
        "brain_decision_receipt",
        "bandit_selection_receipt",
        "terminal_observation",
        "evidence_scope",
        "layered_feedback",
        "prediction",
        "evidence",
    )
    if not all(_canonical_json(left.get(field)) == _canonical_json(right.get(field)) for field in fields):
        return False
    left_payload = dict(left.get("projection_payload") or {})
    right_payload = dict(right.get("projection_payload") or {})
    left_payload.pop("ts", None)
    right_payload.pop("ts", None)
    return _canonical_json(left_payload) == _canonical_json(right_payload)


def _reward_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "grade_evidence_valid",
        "grade_context_status",
        "grade_context_reason",
        "cq_before",
        "cq_now",
        "congestion_before",
        "congestion_now",
        "congestion_before_valid",
        "congestion_now_valid",
        "rtt_before",
        "rtt_now",
        "sinr_before",
        "sinr_now",
        "rsrq_before",
        "rsrq_now",
        "tp_before",
        "tp_now",
    )
    return {key: evidence.get(key) for key in keys if key in evidence}


def _reward_evidence_scope_from_record(record: Any) -> EvidenceScope:
    try:
        action_scope = EvidenceScope.from_mapping(
            dict(getattr(record, "evidence_scope", {}) or {})
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"terminal_reward_action_evidence_scope_invalid:{exc}"
        ) from exc
    return EvidenceScope(
        identity_code=action_scope.identity_code,
        identity_status=action_scope.identity_status,
        identity_catalog_version=action_scope.identity_catalog_version,
        identity_catalog_checksum=action_scope.identity_catalog_checksum,
        session_scope_key=action_scope.session_scope_key,
        source_epoch=action_scope.source_epoch,
        source_sequence=action_scope.source_sequence,
        observed_at=action_scope.observed_at,
        evidence_age_s=action_scope.evidence_age_s,
        confidence=action_scope.confidence,
        evidence_count=action_scope.evidence_count,
        formula_vector=terminal_reward_formula_vector(),
        portability=action_scope.portability,
        access_type_id=action_scope.access_type_id,
        model_family_id=action_scope.model_family_id,
        provider_plmn=action_scope.provider_plmn,
        sim_scope_hash=action_scope.sim_scope_hash,
        site_scope_id=action_scope.site_scope_id,
        cell_scope_key=action_scope.cell_scope_key,
        serving_band=action_scope.serving_band,
        action_shape_key=action_scope.action_shape_key,
    )


def _receipt(
    *,
    admitted: bool = False,
    duplicate: bool = False,
    reason: str,
    entry: Mapping[str, Any] | None = None,
    conflict: Mapping[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    row = dict(entry or {})
    return {
        "schema": "delayed_reward_ledger_receipt.v1",
        "owner": OWNER,
        "admitted": bool(admitted),
        "duplicate": bool(duplicate),
        "reason": str(reason or ""),
        "ledger_key": str(row.get("ledger_key") or ""),
        "grade": row.get("grade") if type(row.get("grade")) is int else None,
        "horizon_s": int(row.get("horizon_s", 0) or 0),
        "error": str(error or ""),
        "conflict": dict(conflict or {}),
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "FORMULA_GENERATION",
    "OWNER",
    "TERMINAL_EVENT_KIND",
    "PROJECTION_NAMES",
    "TRAJECTORY_PROJECTION_NAMES",
    "PROJECTION_RECEIPT_EVENT_KIND",
    "MILESTONE_PROJECTION_RECEIPT_EVENT_KIND",
    "REWARD_QUARANTINE_SCHEMA",
    "admit_terminal_reward",
    "apply_nonterminal_trajectory_entry",
    "apply_trajectory_projection_receipt",
    "apply_terminal_reward_entry",
    "apply_reward_projection_receipt",
    "build_nonterminal_trajectory_entry",
    "ensure_reward_ledger_state",
    "complete_reward_projection",
    "complete_trajectory_projection",
    "pending_trajectory_projection_entries",
    "pending_reward_projection_entries",
    "projection_is_completed",
    "trajectory_projection_is_completed",
    "restore_reward_ledger_snapshot",
    "quarantine_reward_evidence",
    "reward_evidence_quarantine_snapshot",
    "reward_ledger_snapshot",
    "reward_ledger_entry",
    "reward_projection_effect_keys_snapshot",
    "reward_projection_entries",
    "reward_projection_outbox_entry",
    "reward_projection_outbox_snapshot",
    "reward_trajectory_snapshot",
]
