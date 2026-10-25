"""Resumable projection owner for one admitted delayed reward.

RewardLedger owns durable admission and per-destination receipts.  This owner
delivers admitted rewards to the existing destination owners and supplies the
idempotency checks required to resume after a process crash.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Mapping

from arch4.brain.crystal_ball_regrade_owner import commit_staged_roi_crystal_ball_projection
from arch4.brain.memory_action_learning_store import (
    commit_terminal_reward_learning_to_memory_store,
)
from arch4.brain.memory_io import dict_to_record
from arch4.brain.proof_referee_brain_event_owner import publish_staged_roi_brain_event
from arch4.brain.proof_referee_historical_projection_owner import (
    commit_staged_roi_historical_projection,
)
from arch4.brain.proof_referee_action_lifecycle_projection_owner import (
    action_lifecycle_contains_referee_grade,
    project_referee_grade_to_action_lifecycle,
)
from arch4.brain.proof_referee_recording_event_owner import log_action_roi_evidence_event
from arch4.brain.proof_referee_secondary_event_owner import (
    log_staged_roi_secondary_learning_event,
)
from arch4.brain.reward_ledger import (
    PROJECTION_NAMES,
    complete_reward_projection,
    projection_is_completed,
    reward_projection_entries,
    reward_projection_outbox_entry,
)
from arch4.brain.secondary_learning_receipt import secondary_learning_receipt_from_mapping
from arch4.terminal_reward_observation import TerminalRewardObservation


OWNER = "arch4.brain.proof_referee_projection_outbox_owner"
FaultInjector = Callable[[str, str, str], None]


def resume_reward_projection_outbox(
    *,
    memory: Any,
    historical: Any,
    crystal_ball: Any,
    brain_event_inbox: Any,
    logger: Any,
    action_transaction_authority: Any,
    bandit_state_store: Any = None,
    now: float | None = None,
    fault_injector: FaultInjector | None = None,
) -> list[dict[str, Any]]:
    """Resume every admitted reward whose destination truth is incomplete."""

    if memory is None:
        return []
    ts = time.time() if now is None else float(now)
    results: list[dict[str, Any]] = []
    for row in reward_projection_entries(memory):
        ledger_key = str(row.get("ledger_key") or "")
        if all(
            projection_is_completed(memory, ledger_key, projection)
            and _destination_has_projection(
                projection=projection,
                ledger_key=ledger_key,
                memory=memory,
                historical=historical,
                crystal_ball=crystal_ball,
                bandit_state_store=bandit_state_store,
                action_transaction_authority=action_transaction_authority,
                ledger_entry=row["ledger_entry"],
            )
            for projection in PROJECTION_NAMES
        ):
            continue
        results.append(
            project_reward_outbox_entry(
                memory=memory,
                historical=historical,
                crystal_ball=crystal_ball,
                brain_event_inbox=brain_event_inbox,
                logger=logger,
                action_transaction_authority=action_transaction_authority,
                bandit_state_store=bandit_state_store,
                ledger_entry=row["ledger_entry"],
                ts=ts,
                fault_injector=fault_injector,
            )
        )
    return results


def project_reward_outbox_entry(
    *,
    memory: Any,
    historical: Any,
    crystal_ball: Any,
    brain_event_inbox: Any,
    logger: Any,
    action_transaction_authority: Any,
    bandit_state_store: Any = None,
    ledger_entry: Mapping[str, Any],
    record: Any = None,
    original: Any = None,
    ts: float,
    fault_injector: FaultInjector | None = None,
) -> dict[str, Any]:
    entry = dict(ledger_entry or {})
    ledger_key = str(entry.get("ledger_key") or "")
    payload = entry.get("projection_payload") if isinstance(entry.get("projection_payload"), Mapping) else {}
    terminal_record = record or _record_from_payload(payload)
    if terminal_record is None:
        return {"ledger_key": ledger_key, "complete": False, "reason": "projection_record_missing"}
    try:
        terminal_observation = TerminalRewardObservation.from_mapping(
            dict(entry.get("terminal_observation") or {})
        )
    except (TypeError, ValueError) as exc:
        return {
            "ledger_key": ledger_key,
            "complete": False,
            "reason": f"terminal_observation_invalid:{exc}",
        }
    parent_record = original or _parent_from_terminal_record(terminal_record)
    proof_identity = dict(entry.get("proof_identity") or {})
    evidence = dict(payload.get("evidence") or entry.get("evidence") or {})
    label = str(payload.get("label") or entry.get("label") or "neutral")
    reason = str(payload.get("reason") or dict(entry.get("layered_feedback") or {}).get("reason") or "")
    referee_code = str(payload.get("referee_code") or getattr(terminal_record, "referee_outcome_code", "") or "")
    referee_level = str(payload.get("referee_level") or getattr(terminal_record, "referee_outcome_level", "") or "")
    event_ts = float(payload.get("ts", ts) or ts)

    receipts: dict[str, Any] = {}
    for projection in PROJECTION_NAMES:
        durable = _destination_has_projection(
            projection=projection,
            ledger_key=ledger_key,
            memory=memory,
            historical=historical,
            crystal_ball=crystal_ball,
            bandit_state_store=bandit_state_store,
            action_transaction_authority=action_transaction_authority,
            ledger_entry=entry,
        )
        receipt_done = projection_is_completed(memory, ledger_key, projection)
        if receipt_done and durable:
            receipts[projection] = {"completed": True, "duplicate": True}
            continue
        receipt = _deliver_projection(
            projection=projection,
            ledger_key=ledger_key,
            memory=memory,
            historical=historical,
            crystal_ball=crystal_ball,
            brain_event_inbox=brain_event_inbox,
            logger=logger,
            action_transaction_authority=action_transaction_authority,
            bandit_state_store=bandit_state_store,
            ledger_entry=entry,
            record=terminal_record,
            original=parent_record,
            terminal_observation=terminal_observation,
            proof_identity=proof_identity,
            evidence=evidence,
            label=label,
            reason=reason,
            referee_code=referee_code,
            referee_level=referee_level,
            grade_score=int(entry.get("grade", 0) or 0),
            ts=event_ts,
            secondary=_secondary_receipt(memory, ledger_key, entry),
        )
        receipts[projection] = dict(receipt)
        if not bool(receipt.get("completed", False)):
            break
        _inject(fault_injector, ledger_key, projection, "after_effect_before_receipt")
        if not receipt_done:
            complete_reward_projection(
                memory,
                ledger_key=ledger_key,
                projection=projection,
                receipt=receipt,
                ts=ts,
            )
        _inject(fault_injector, ledger_key, projection, "after_receipt")

    complete = all(
        projection_is_completed(memory, ledger_key, projection)
        and _destination_has_projection(
            projection=projection,
            ledger_key=ledger_key,
            memory=memory,
            historical=historical,
            crystal_ball=crystal_ball,
            bandit_state_store=bandit_state_store,
            action_transaction_authority=action_transaction_authority,
            ledger_entry=entry,
        )
        for projection in PROJECTION_NAMES
    )
    return {
        "schema": "delayed_reward_projection_result.v1",
        "owner": OWNER,
        "ledger_key": ledger_key,
        "complete": complete,
        "receipts": receipts,
        "secondary_learning": _secondary_receipt(memory, ledger_key, entry),
    }


def _deliver_projection(
    *,
    projection: str,
    ledger_key: str,
    memory: Any,
    historical: Any,
    crystal_ball: Any,
    brain_event_inbox: Any,
    logger: Any,
    action_transaction_authority: Any,
    bandit_state_store: Any = None,
    ledger_entry: Mapping[str, Any] | None = None,
    record: Any,
    original: Any,
    terminal_observation: TerminalRewardObservation,
    proof_identity: Mapping[str, Any],
    evidence: dict[str, Any],
    label: str,
    reason: str,
    referee_code: str,
    referee_level: str,
    grade_score: int,
    ts: float,
    secondary: dict[str, Any],
) -> dict[str, Any]:
    if projection == "memory":
        effect_key = f"memory:{ledger_key}"
        actionable = int(grade_score) != 0
        if effect_key in getattr(memory, "_reward_projection_effect_keys", set()):
            return {
                "completed": True,
                "committed": actionable,
                "duplicate": True,
                "reason": (
                    "memory_projection_already_recorded"
                    if actionable
                    else "nonactionable_terminal_grade"
                ),
                "grade_score": int(grade_score),
            }
        if not actionable:
            memory._reward_projection_effect_keys.add(effect_key)
            return {
                "completed": True,
                "committed": False,
                "reason": "nonactionable_terminal_grade",
                "grade_score": 0,
            }
        meta = commit_terminal_reward_learning_to_memory_store(
            memory,
            record,
            terminal_observation=terminal_observation,
            terminal_grade_score=int(grade_score),
        )
        if not bool((meta or {}).get("committed", False)) and str((meta or {}).get("reason") or "") != "already_committed":
            return {"completed": False, "reason": str((meta or {}).get("reason") or "memory_projection_failed")}
        memory._reward_projection_effect_keys.add(effect_key)
        return {
            "completed": True,
            "committed": True,
            "grade_score": int(grade_score),
            **dict(meta or {}),
        }

    if projection == "bandit":
        ledger_row = dict(ledger_entry or {})
        if not str(ledger_row.get("bandit_selection_receipt_id") or ""):
            return {
                "completed": True,
                "committed": False,
                "reason": "terminal_reward_not_selected_by_bandit",
                "grade_score": int(grade_score),
                "prediction_error_recorded": False,
                "policy_update_applied": False,
                "future_policy_effect_status": "not_applicable",
            }
        consume = getattr(bandit_state_store, "consume_terminal_reward", None)
        if not callable(consume):
            return {
                "completed": False,
                "reason": "contextual_bandit_state_store_unavailable",
            }
        result = dict(consume(ledger_row, now=ts) or {})
        return {
            "completed": bool(
                result.get("admitted", False) or result.get("duplicate", False)
            ),
            "committed": bool(result.get("admitted", False)),
            "grade_score": int(grade_score),
            **result,
        }

    if projection == "historical":
        result = commit_staged_roi_historical_projection(
            historical=historical,
            record=record,
            original=original,
            evidence=evidence,
            ts=ts,
            contract_label=label,
            referee_code=referee_code,
            referee_level=referee_level,
            reason=reason,
            proof_identity=proof_identity,
            reward_entry=dict(ledger_entry or {}),
            reward_ledger_key=ledger_key,
        )
        return {
            "completed": bool(result.get("historical_committed", False)),
            "grade_score": int(grade_score),
            **result,
        }

    if projection == "crystal":
        result: dict[str, Any] = {}
        commit_staged_roi_crystal_ball_projection(
            result=result,
            logger=logger,
            crystal_ball=crystal_ball,
            terminal_observation=terminal_observation,
            proof_identity=proof_identity,
            original=original,
            record=record,
            evidence=evidence,
            label=label,
            reason=reason,
            grade_score=grade_score,
            ts=ts,
            reward_entry=dict(ledger_entry or {}),
            reward_ledger_key=ledger_key,
        )
        completed = bool(
            crystal_ball is not None
            and ledger_key in getattr(crystal_ball, "_delayed_reward_projection_keys", set())
        )
        return {"completed": completed, "grade_score": int(grade_score), **result}

    if projection == "brain":
        result = publish_staged_roi_brain_event(
            proof_owner=OWNER,
            brain_event_inbox=brain_event_inbox,
            original=original,
            record=record,
            horizon=int(getattr(record, "roi_horizon_s", 0) or 0),
            label=label,
            reason=reason,
            referee_code=referee_code,
            secondary=secondary,
            evidence=evidence,
            ts=ts,
            reward_ledger_key=ledger_key,
        )
        return {
            "completed": bool(result.get("published", False)),
            "grade_score": int(grade_score),
            **result,
        }

    if projection == "evidence":
        action_evidence = log_action_roi_evidence_event(
            logger=logger,
            original=original,
            record=record,
            params=dict(getattr(original, "action_params", {}) or {}),
            horizon=int(getattr(record, "roi_horizon_s", 0) or 0),
            reason=reason,
            evidence=evidence,
            reward_ledger_key=ledger_key,
        )
        secondary_evidence = log_staged_roi_secondary_learning_event(
            logger=logger,
            record=record,
            label=label,
            reason=reason,
            referee_code=referee_code,
            referee_level=referee_level,
            result=secondary,
            reward_ledger_key=ledger_key,
        )
        return {
            "completed": bool(
                action_evidence.get("recorded", False)
                and secondary_evidence.get("recorded", False)
            ),
            "action_evidence": action_evidence,
            "secondary_evidence": secondary_evidence,
            "grade_score": int(grade_score),
        }
    if projection == "action_lifecycle":
        contract = _action_lifecycle_projection_contract(
            dict(ledger_entry or {})
        )
        return project_referee_grade_to_action_lifecycle(
            authority=action_transaction_authority,
            **contract,
        )
    raise ValueError(f"reward_projection_unknown:{projection}")


def _destination_has_projection(
    *,
    projection: str,
    ledger_key: str,
    memory: Any,
    historical: Any,
    crystal_ball: Any,
    bandit_state_store: Any,
    action_transaction_authority: Any,
    ledger_entry: Mapping[str, Any],
) -> bool:
    if projection == "memory":
        return f"memory:{ledger_key}" in getattr(memory, "_reward_projection_effect_keys", set())
    if projection == "historical":
        return ledger_key in getattr(historical, "_delayed_reward_projection_keys", set())
    if projection == "crystal":
        return ledger_key in getattr(crystal_ball, "_delayed_reward_projection_keys", set())
    if projection == "bandit":
        has_consumed = getattr(bandit_state_store, "has_consumed", None)
        if callable(has_consumed) and bool(has_consumed(ledger_key)):
            return True
    if projection == "action_lifecycle":
        contract = _action_lifecycle_projection_contract(ledger_entry)
        return action_lifecycle_contains_referee_grade(
            authority=action_transaction_authority,
            **contract,
        )
    return projection_is_completed(memory, ledger_key, projection)


def _action_lifecycle_projection_contract(
    ledger_entry: Mapping[str, Any],
) -> dict[str, Any]:
    entry = dict(ledger_entry or {})
    payload = dict(entry.get("projection_payload") or {})
    feedback = dict(entry.get("layered_feedback") or {})
    evidence = dict(payload.get("evidence") or entry.get("evidence") or {})
    horizon = int(entry.get("horizon_s", 0) or 0)
    return {
        "root_id": str(entry.get("action_root_id") or ""),
        "horizon_s": horizon,
        "grade": int(entry.get("grade", 0) or 0),
        "reason_receipt": {
            "owner": "arch4.brain.proof_feedback_owner",
            "horizon_s": horizon,
            "label": str(payload.get("label") or entry.get("label") or "neutral"),
            "reason": str(payload.get("reason") or feedback.get("reason") or ""),
            "referee_code": str(payload.get("referee_code") or ""),
            "layered_action_feedback": feedback,
            "evidence": evidence,
        },
        "graded_at": float(payload.get("ts") or entry.get("admitted_ts") or 0.0),
    }


def _secondary_receipt(memory: Any, ledger_key: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    completed = {
        projection: projection_is_completed(memory, ledger_key, projection)
        for projection in PROJECTION_NAMES
    }
    outbox = reward_projection_outbox_entry(memory, ledger_key)
    crystal_projection = dict((outbox.get("projections") or {}).get("crystal") or {})
    crystal_receipt = dict(crystal_projection.get("receipt") or {})
    bandit_projection = dict(
        (outbox.get("projections") or {}).get("bandit") or {}
    )
    bandit_receipt = dict(bandit_projection.get("receipt") or {})
    return secondary_learning_receipt_from_mapping(
        {
            "reward_admitted": True,
            "reward_duplicate": False,
            "reward_ledger_key": ledger_key,
            "terminal_grade_score": int(entry.get("grade", 0) or 0),
            "proof_identity": dict(entry.get("proof_identity") or {}),
            "memory_committed": completed["memory"],
            "historical_committed": completed["historical"],
            "crystal_ball_updated": completed["crystal"],
            "crystal_ball_regraded": bool(crystal_receipt.get("crystal_ball_regraded", False)),
            "prediction_error_recorded": bool(
                bandit_receipt.get("prediction_error_recorded", False)
            ),
            "policy_update_applied": bool(
                bandit_receipt.get("policy_update_applied", False)
            ),
            "future_policy_effect_status": str(
                bandit_receipt.get("future_policy_effect_status") or ""
            ),
            "prediction_correction": dict(
                bandit_receipt.get("prediction_correction") or {}
            ),
            "committed": any(completed.values()),
            "reason": "reward_projection_complete" if all(completed.values()) else "reward_projection_pending",
        }
    )


def _record_from_payload(payload: Mapping[str, Any]) -> Any:
    row = payload.get("record") if isinstance(payload.get("record"), Mapping) else None
    if row is None:
        return None
    return dict_to_record(dict(row))


@dataclass(frozen=True, slots=True)
class _RewardProjectionParent:
    decision_trace_id: str
    action_params: Mapping[str, Any]
    objective_metadata: Mapping[str, Any]
    proposal_objective_id: str
    proposal_intent: str
    proposal_reason: str
    lever: Any
    learning_commit_status: str


def _parent_from_terminal_record(record: Any) -> _RewardProjectionParent:
    meta = dict(getattr(record, "objective_metadata", {}) or {})
    params = dict(getattr(record, "action_params", {}) or {})
    return _RewardProjectionParent(
        decision_trace_id=str(getattr(record, "roi_parent_trace_id", "") or ""),
        action_params=params,
        objective_metadata=meta,
        proposal_objective_id=str(getattr(record, "proposal_objective_id", "") or ""),
        proposal_intent=str(getattr(record, "proposal_intent", "") or ""),
        proposal_reason=str(getattr(record, "proposal_reason", "") or ""),
        lever=getattr(record, "lever", None),
        learning_commit_status="proof_pending",
    )


def _inject(
    fault_injector: FaultInjector | None,
    ledger_key: str,
    projection: str,
    phase: str,
) -> None:
    if fault_injector is not None:
        fault_injector(ledger_key, projection, phase)


__all__ = [
    "OWNER",
    "project_reward_outbox_entry",
    "resume_reward_projection_outbox",
]
