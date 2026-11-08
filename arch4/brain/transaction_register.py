"""Durable Brain-owned lifecycle register for delayed radio transactions."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Any, Mapping

from arch4.action_transaction import terminal_reward_horizon_s
from arch4.brain.transaction import BrainTransaction, BrainTransactionStage
from arch4.memory_store.event_journal import JsonlEventJournal


OWNER = "arch4.brain.transaction_register"
SCHEMA = "brain_transaction_register.v2"
JOURNAL_SCHEMA = "brain_transaction_register_event.v1"
PENDING_RECEIPT_SCHEMA = "brain_transaction_pending_receipt.v1"
TERMINAL_RECEIPT_SCHEMA = "brain_transaction_terminal_receipt.v1"
DEFAULT_TIMEOUT_GRACE_S = 300.0
JOURNAL_COMPACTION_INTERVAL = 64
_TERMINAL_READBACK_FIELDS = frozenset(
    {
        "write_accepted",
        "target_confirmed",
        "target_visible",
        "chart_fresh",
        "chart_continued",
        "confirmation_ms",
        "target_visible_ms",
        "chart_continued_ms",
    }
)


@dataclass(frozen=True)
class BrainTransactionClosure:
    transaction: BrainTransaction
    matched_by: str
    terminal_kind: str = ""
    reward_ledger_key: str = ""
    duplicate: bool = False


class BrainTransactionRegister:
    """Retain executed actions until reward, rejection, or timeout is durable.

    Every mutation is append-first.  The last valid journal row contains the
    complete register state, making a restart equivalent to continuing the
    prior Brain clock rather than silently discarding an open transaction.
    """

    _COUNTER_NAMES = (
        "registered_count",
        "closed_count",
        "reward_closed_count",
        "rejected_count",
        "timeout_count",
        "unmatched_proof_count",
        "duplicate_terminal_count",
    )

    def __init__(
        self,
        *,
        max_pending: int = 128,
        max_recent_closed: int = 128,
        persistence_dir: str | Path | None = None,
        timeout_grace_s: float = DEFAULT_TIMEOUT_GRACE_S,
    ) -> None:
        self._max_pending = max(1, int(max_pending))
        self._max_recent_closed = max(1, int(max_recent_closed))
        self._timeout_grace_s = max(1.0, float(timeout_grace_s or DEFAULT_TIMEOUT_GRACE_S))
        self._lock = threading.RLock()
        self._pending: dict[str, BrainTransaction] = {}
        self._pending_receipts: dict[str, dict[str, Any]] = {}
        self._recent_closed: deque[dict[str, Any]] = deque(maxlen=self._max_recent_closed)
        self._counters = {name: 0 for name in self._COUNTER_NAMES}
        self._sequence = 0
        self._restore_error = ""
        self._compaction_error = ""
        self._compaction_count = 0
        self._journal: JsonlEventJournal | None = None
        if persistence_dir is not None:
            self.bind_persistence(persistence_dir)

    def bind_persistence(self, persistence_dir: str | Path) -> None:
        """Bind once to Memory's persistence directory and replay state."""

        root = Path(persistence_dir).expanduser().resolve()
        with self._lock:
            if self._journal is not None:
                if self._journal.root_dir != root:
                    raise RuntimeError("brain_transaction_register_persistence_rebind")
                return
            if self._pending or self._recent_closed or self._sequence:
                raise RuntimeError("brain_transaction_register_late_persistence_bind")
            self._journal = JsonlEventJournal(root, "brain_transaction_register.jsonl")
            self._restore_from_journal_locked()

    def register_executed(
        self,
        transaction: BrainTransaction,
        *,
        now: float | None = None,
        terminal_due_ts: float | None = None,
        timeout_at_ts: float | None = None,
    ) -> BrainTransaction:
        if not isinstance(transaction, BrainTransaction):
            raise TypeError("brain_transaction_register_requires_transaction")
        if transaction.stage is not BrainTransactionStage.EXECUTED:
            raise RuntimeError(f"brain_transaction_register_from_{transaction.stage.value}")
        execution = transaction.execution
        if execution is None or execution.action_success is not True:
            raise RuntimeError("brain_transaction_pending_requires_successful_execution")
        if transaction.action_key is None or transaction.transaction_id != transaction.action_key.root_id:
            raise RuntimeError("brain_transaction_action_root_missing")
        now_ts = float(time.time() if now is None else now)
        expected_horizon = float(terminal_reward_horizon_s(transaction.action_key))
        due_ts = float(terminal_due_ts or (now_ts + expected_horizon))
        timeout_ts = float(timeout_at_ts or (due_ts + self._timeout_grace_s))
        if due_ts <= now_ts or timeout_ts <= due_ts:
            raise ValueError("brain_transaction_pending_deadline_invalid")
        with self._lock:
            existing = self._pending.get(transaction.transaction_id)
            if existing is not None:
                if existing == transaction:
                    return existing
                raise RuntimeError(
                    f"brain_transaction_identity_collision:{transaction.transaction_id}"
                )
            terminal = self._terminal_by_transaction_id(transaction.transaction_id)
            if terminal is not None:
                raise RuntimeError(
                    f"brain_transaction_already_terminal:{transaction.transaction_id}"
                )
            if len(self._pending) >= self._max_pending:
                raise RuntimeError("brain_transaction_pending_capacity_exhausted")
            self._assert_correlation_available(transaction)
            next_pending = dict(self._pending)
            next_pending[transaction.transaction_id] = transaction
            next_receipts = dict(self._pending_receipts)
            next_receipts[transaction.transaction_id] = {
                "schema": PENDING_RECEIPT_SCHEMA,
                "owner": OWNER,
                "transaction_id": transaction.transaction_id,
                "generation": int(transaction.generation),
                "action_root_id": transaction.action_key.root_id,
                "command_id": transaction.action_key.command_id,
                "proof_id": transaction.action_key.proof_id,
                "session_scope_key": (
                    transaction.action_key.matrix_identity.session_scope_key
                ),
                "identity_key": transaction.action_key.identity_key,
                "source_generation": int(
                    transaction.action_key.matrix_identity.matrix_sequence
                ),
                "opportunity_id": transaction.action_key.opportunity_id,
                "opportunity_set_id": transaction.action_key.opportunity_set_id,
                "decision_id": transaction.action_key.decision_id,
                "decision_receipt_id": transaction.action_key.decision_receipt_id,
                "bandit_selection_receipt_id": (
                    transaction.action_key.bandit_selection_receipt_id
                ),
                "decision_trace_id": self._trace_id(transaction),
                "objective_id": self._objective_id(transaction),
                "opened_at_ts": round(now_ts, 6),
                "terminal_due_ts": round(due_ts, 6),
                "timeout_at_ts": round(timeout_ts, 6),
                "status": "pending_terminal_referee",
                "transaction": transaction.to_event_payload(),
            }
            counters = self._next_counters(registered_count=1)
            self._commit_locked(
                event="pending_registered",
                pending=next_pending,
                pending_receipts=next_receipts,
                recent_closed=self._recent_closed,
                counters=counters,
            )
            return transaction

    def close_immediate(
        self,
        transaction: BrainTransaction,
        *,
        proof: Mapping[str, Any] | None,
        grade: str,
        learning_admission: str,
    ) -> BrainTransactionClosure:
        """Durably close a non-deferred rejection/no-action execution."""

        if transaction.stage is not BrainTransactionStage.EXECUTED:
            raise RuntimeError(
                f"brain_transaction_immediate_close_from_{transaction.stage.value}"
            )
        closed = transaction.with_proof(
            proof=proof,
            grade=grade,
            learning_admission=learning_admission,
        ).close()
        terminal_kind = (
            "rejected"
            if str(learning_admission or "").strip().lower() in {"blocked", "rejected"}
            else "immediate_no_reward"
        )
        closure = BrainTransactionClosure(
            closed,
            "immediate_terminal_outcome",
            terminal_kind=terminal_kind,
        )
        with self._lock:
            duplicate = self._terminal_by_transaction_id(transaction.transaction_id)
            if duplicate is not None:
                return self._duplicate_closure(duplicate)
            recent = self._recent_with_closure(closure, proof=proof)
            counters = self._next_counters(
                closed_count=1,
                rejected_count=1 if terminal_kind == "rejected" else 0,
            )
            self._commit_locked(
                event="immediate_rejected",
                pending=self._pending,
                pending_receipts=self._pending_receipts,
                recent_closed=recent,
                counters=counters,
            )
        return closure

    def close_from_terminal_reward(
        self,
        proof: Mapping[str, Any],
    ) -> BrainTransactionClosure | None:
        """Close exactly once from the action-aware terminal RewardLedger grade."""

        payload = dict(proof or {})
        self._validate_terminal_reward(payload)
        reward_key = self._text(payload.get("reward_ledger_key"))
        with self._lock:
            duplicate = self._terminal_by_reward_key(reward_key)
            if duplicate is not None:
                self._commit_duplicate_locked("duplicate_terminal_reward")
                return self._duplicate_closure(duplicate)
            transaction, matched_by = self._find_pending(payload)
            if transaction is None:
                self._commit_unmatched_locked("unmatched_terminal_reward")
                return None
            self._validate_transaction_horizon(transaction, payload)
            closed = transaction.with_proof(
                proof=payload,
                grade=self._text(payload.get("label") or payload.get("referee_code") or "ungraded"),
                learning_admission="admitted",
            ).close()
            closure = BrainTransactionClosure(
                closed,
                matched_by,
                terminal_kind="reward",
                reward_ledger_key=reward_key,
            )
            self._commit_terminal_locked(
                closure,
                proof=payload,
                event="terminal_reward_closed",
                counter_deltas={"closed_count": 1, "reward_closed_count": 1},
            )
            return closure

    def close_rejected_terminal(
        self,
        proof: Mapping[str, Any],
    ) -> BrainTransactionClosure | None:
        """Close a terminal Referee rejection without creating a reward."""

        payload = dict(proof or {})
        self._validate_terminal_rejection(payload)
        with self._lock:
            transaction, matched_by = self._find_pending(payload)
            if transaction is None:
                terminal = self._terminal_by_proof(payload)
                if terminal is not None:
                    self._commit_duplicate_locked("duplicate_terminal_rejection")
                    return self._duplicate_closure(terminal)
                self._commit_unmatched_locked("unmatched_terminal_rejection")
                return None
            closed = transaction.with_proof(
                proof=payload,
                grade="rejected",
                learning_admission="blocked",
            ).close()
            closure = BrainTransactionClosure(
                closed,
                matched_by,
                terminal_kind="rejected",
            )
            self._commit_terminal_locked(
                closure,
                proof=payload,
                event="terminal_rejection_closed",
                counter_deltas={"closed_count": 1, "rejected_count": 1},
            )
            return closure

    def close_expired(self, *, now: float | None = None) -> list[BrainTransactionClosure]:
        """Durably close pending receipts whose terminal proof timed out."""

        now_ts = float(time.time() if now is None else now)
        with self._lock:
            expired_ids = sorted(
                transaction_id
                for transaction_id, receipt in self._pending_receipts.items()
                if now_ts >= float(receipt.get("timeout_at_ts", 0.0) or 0.0)
            )
            if not expired_ids:
                return []
            next_pending = dict(self._pending)
            next_receipts = dict(self._pending_receipts)
            recent = deque(self._recent_closed, maxlen=self._max_recent_closed)
            closures: list[BrainTransactionClosure] = []
            for transaction_id in expired_ids:
                transaction = next_pending.pop(transaction_id)
                pending_receipt = next_receipts.pop(transaction_id)
                proof = {
                    "schema": "brain_transaction_timeout_proof.v1",
                    "owner": OWNER,
                    "terminal": True,
                    "reward_admitted": False,
                    "reward_ledger_key": "",
                    "grade_kind": "terminal_timeout",
                    "action_root_id": (
                        transaction.action_key.root_id
                        if transaction.action_key is not None
                        else ""
                    ),
                    "parent_trace_id": self._trace_id(transaction),
                    "objective_id": self._objective_id(transaction),
                    "reason": "terminal_reward_timeout",
                    "terminal_due_ts": pending_receipt.get("terminal_due_ts"),
                    "timeout_at_ts": pending_receipt.get("timeout_at_ts"),
                }
                closed = transaction.with_proof(
                    proof=proof,
                    grade="timeout",
                    learning_admission="blocked",
                ).close()
                closure = BrainTransactionClosure(
                    closed,
                    "transaction_id",
                    terminal_kind="timeout",
                )
                closures.append(closure)
                recent.append(self._terminal_receipt(closure, proof=proof, closed_at_ts=now_ts))
            counters = self._next_counters(
                closed_count=len(closures),
                timeout_count=len(closures),
            )
            self._commit_locked(
                event="terminal_timeout_closed",
                pending=next_pending,
                pending_receipts=next_receipts,
                recent_closed=recent,
                counters=counters,
            )
            return closures

    def pending_transaction(self, transaction_id: str) -> BrainTransaction | None:
        with self._lock:
            return self._pending.get(str(transaction_id or ""))

    def pending_receipt(self, transaction_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._pending_receipts.get(str(transaction_id or "")) or {})

    def update_lifecycle_readback(
        self,
        *,
        action_root_id: str,
        readback: Mapping[str, Any],
    ) -> BrainTransaction:
        root_id = str(action_root_id or "").strip()
        if not root_id:
            raise ValueError("brain_transaction_action_root_missing")
        with self._lock:
            transaction = self._pending.get(root_id)
            if transaction is None:
                raise RuntimeError(f"brain_transaction_pending_root_missing:{root_id}")
            updated = transaction.with_readback_update(readback)
            pending = dict(self._pending)
            pending[root_id] = updated
            receipts = dict(self._pending_receipts)
            receipt = dict(receipts[root_id])
            receipt["transaction"] = updated.to_event_payload()
            receipt["lifecycle_readback"] = dict(updated.readback)
            receipts[root_id] = receipt
            self._commit_locked(
                event="lifecycle_readback_updated",
                pending=pending,
                pending_receipts=receipts,
                recent_closed=self._recent_closed,
                counters=self._counters,
            )
            return updated

    def status(self, *, now: float | None = None) -> dict[str, Any]:
        now_ts = float(time.time() if now is None else now)
        with self._lock:
            pending = []
            for transaction_id, receipt in self._pending_receipts.items():
                row = dict(receipt)
                row.pop("transaction", None)
                row["pending_age_s"] = round(
                    max(0.0, now_ts - float(receipt.get("opened_at_ts", now_ts) or now_ts)),
                    3,
                )
                pending.append(row)
            last_closed = dict(self._recent_closed[-1]) if self._recent_closed else {}
            return {
                "schema": SCHEMA,
                "owner": OWNER,
                "pending_count": len(self._pending),
                "pending_capacity": self._max_pending,
                **dict(self._counters),
                "pending": pending,
                "last_closed": dict(last_closed.get("transaction") or {}),
                "last_terminal_receipt": last_closed,
                "sequence": int(self._sequence),
                "durable": self._journal is not None,
                "restore_error": self._restore_error,
                "compaction_error": self._compaction_error,
                "compaction_count": int(self._compaction_count),
                "journal": (
                    self._journal.writer_status()
                    if self._journal is not None
                    else {}
                ),
            }

    def _commit_terminal_locked(
        self,
        closure: BrainTransactionClosure,
        *,
        proof: Mapping[str, Any],
        event: str,
        counter_deltas: Mapping[str, int],
    ) -> None:
        transaction_id = closure.transaction.transaction_id
        next_pending = dict(self._pending)
        next_receipts = dict(self._pending_receipts)
        next_pending.pop(transaction_id, None)
        next_receipts.pop(transaction_id, None)
        recent = self._recent_with_closure(closure, proof=proof)
        self._commit_locked(
            event=event,
            pending=next_pending,
            pending_receipts=next_receipts,
            recent_closed=recent,
            counters=self._next_counters(**dict(counter_deltas)),
        )

    def _commit_duplicate_locked(self, event: str) -> None:
        self._commit_locked(
            event=event,
            pending=self._pending,
            pending_receipts=self._pending_receipts,
            recent_closed=self._recent_closed,
            counters=self._next_counters(duplicate_terminal_count=1),
        )

    def _commit_unmatched_locked(self, event: str) -> None:
        self._commit_locked(
            event=event,
            pending=self._pending,
            pending_receipts=self._pending_receipts,
            recent_closed=self._recent_closed,
            counters=self._next_counters(unmatched_proof_count=1),
        )

    def _commit_locked(
        self,
        *,
        event: str,
        pending: Mapping[str, BrainTransaction],
        pending_receipts: Mapping[str, Mapping[str, Any]],
        recent_closed: deque[dict[str, Any]],
        counters: Mapping[str, int],
    ) -> None:
        sequence = int(self._sequence) + 1
        recent = deque(recent_closed, maxlen=self._max_recent_closed)
        payload = self._journal_payload(
            event=event,
            sequence=sequence,
            pending=pending,
            pending_receipts=pending_receipts,
            recent_closed=recent,
            counters=counters,
        )
        if self._journal is not None:
            self._journal.append(payload)
        self._pending = dict(pending)
        self._pending_receipts = {
            key: dict(value) for key, value in pending_receipts.items() if key in pending
        }
        self._recent_closed = recent
        self._counters = dict(payload["counters"])
        self._sequence = sequence
        if (
            self._journal is not None
            and sequence % JOURNAL_COMPACTION_INTERVAL == 0
        ):
            self._compact_journal_locked(payload)

    @staticmethod
    def _journal_payload(
        *,
        event: str,
        sequence: int,
        pending: Mapping[str, BrainTransaction],
        pending_receipts: Mapping[str, Mapping[str, Any]],
        recent_closed: deque[dict[str, Any]],
        counters: Mapping[str, int],
    ) -> dict[str, Any]:
        payload = {
            "schema": JOURNAL_SCHEMA,
            "owner": OWNER,
            "event": str(event or "transition"),
            "sequence": int(sequence),
            "pending": {
                key: dict(pending_receipts[key])
                for key in pending
                if key in pending_receipts
            },
            "recent_closed": [dict(row) for row in recent_closed],
            "counters": {
                name: int(counters.get(name, 0) or 0)
                for name in BrainTransactionRegister._COUNTER_NAMES
            },
        }
        payload["checksum"] = _checksum(payload)
        return payload

    def _compact_journal_locked(self, payload: Mapping[str, Any]) -> None:
        if self._journal is None:
            return
        try:
            self._journal.rewrite([dict(payload)])
        except Exception as exc:
            # The preceding append is already the durable transaction.  A
            # failed optional compaction must not roll back or block live
            # Controller state; the exact failure remains visible in status.
            self._compaction_error = f"{type(exc).__name__}: {exc}"
            return
        self._compaction_error = ""
        self._compaction_count += 1

    def _restore_from_journal_locked(self) -> None:
        assert self._journal is not None
        last: Mapping[str, Any] | None = next(
            self._journal.iter_events_reverse(
                limit=1,
                validator=lambda raw: self._validate_journal_row(
                    raw,
                    after_sequence=0,
                ),
            ),
            None,
        )
        if last is None:
            if self._journal.path.exists() and self._journal.path.stat().st_size > 0:
                self._restore_error = "no_valid_brain_transaction_event"
            return
        pending_receipts = dict(last.get("pending") or {})
        pending: dict[str, BrainTransaction] = {}
        for transaction_id, receipt_raw in pending_receipts.items():
            receipt = dict(receipt_raw or {})
            transaction = BrainTransaction.from_event_payload(
                dict(receipt.get("transaction") or {})
            )
            if transaction.stage is not BrainTransactionStage.EXECUTED:
                raise ValueError("restored_brain_transaction_not_executed")
            if transaction.transaction_id != str(transaction_id):
                raise ValueError("restored_brain_transaction_identity_mismatch")
            pending[str(transaction_id)] = transaction
        self._pending = pending
        self._pending_receipts = {
            str(key): dict(value) for key, value in pending_receipts.items()
        }
        self._recent_closed = deque(
            (
                self._compact_terminal_receipt(dict(row))
                for row in list(last.get("recent_closed") or [])
                if isinstance(row, Mapping)
            ),
            maxlen=self._max_recent_closed,
        )
        counters = dict(last.get("counters") or {})
        self._counters = {
            name: max(0, int(counters.get(name, 0) or 0))
            for name in self._COUNTER_NAMES
        }
        self._sequence = int(last.get("sequence", 0) or 0)
        compacted = self._journal_payload(
            event="restore_compacted",
            sequence=self._sequence,
            pending=self._pending,
            pending_receipts=self._pending_receipts,
            recent_closed=self._recent_closed,
            counters=self._counters,
        )
        self._compact_journal_locked(compacted)

    def _validate_journal_row(self, raw: Mapping[str, Any], *, after_sequence: int) -> None:
        if str(raw.get("schema") or "") != JOURNAL_SCHEMA:
            raise ValueError("schema")
        if str(raw.get("owner") or "") != OWNER:
            raise ValueError("owner")
        if str(raw.get("checksum") or "") != _checksum(raw):
            raise ValueError("checksum")
        if int(raw.get("sequence", 0) or 0) <= int(after_sequence):
            raise ValueError("sequence")
        pending = raw.get("pending")
        recent = raw.get("recent_closed")
        if not isinstance(pending, Mapping) or not isinstance(recent, list):
            raise ValueError("state")
        for transaction_id, receipt_raw in pending.items():
            receipt = dict(receipt_raw or {})
            if receipt.get("schema") != PENDING_RECEIPT_SCHEMA or receipt.get("owner") != OWNER:
                raise ValueError("pending_receipt_contract")
            transaction = BrainTransaction.from_event_payload(dict(receipt.get("transaction") or {}))
            if transaction.transaction_id != str(transaction_id):
                raise ValueError("pending_receipt_identity")
        for receipt_raw in recent:
            receipt = dict(receipt_raw or {})
            if receipt.get("schema") != TERMINAL_RECEIPT_SCHEMA or receipt.get("owner") != OWNER:
                raise ValueError("terminal_receipt_contract")
            transaction = BrainTransaction.from_event_payload(dict(receipt.get("transaction") or {}))
            if transaction.stage is not BrainTransactionStage.CLOSED:
                raise ValueError("terminal_transaction_not_closed")

    def _find_pending(self, proof: Mapping[str, Any]) -> tuple[BrainTransaction | None, str]:
        identity = proof.get("proof_identity")
        identity_row = dict(identity) if isinstance(identity, Mapping) else {}
        root_id = self._text(
            proof.get("action_root_id")
            or identity_row.get("action_root_id")
        )
        if not root_id:
            return None, ""
        with self._lock:
            transaction = self._pending.get(root_id)
            if transaction is not None and transaction.action_key is not None:
                if transaction.action_key.root_id == root_id:
                    return transaction, "action_root_id"
        return None, ""

    def _assert_correlation_available(self, transaction: BrainTransaction) -> None:
        key = transaction.action_key
        if key is None or transaction.transaction_id != key.root_id:
            raise RuntimeError("brain_transaction_action_root_missing")
        if key.root_id in self._pending:
            raise RuntimeError(f"brain_transaction_correlation_collision:{key.root_id}")

    def _recent_with_closure(
        self,
        closure: BrainTransactionClosure,
        *,
        proof: Mapping[str, Any] | None,
    ) -> deque[dict[str, Any]]:
        recent = deque(self._recent_closed, maxlen=self._max_recent_closed)
        recent.append(self._terminal_receipt(closure, proof=proof))
        return recent

    def _terminal_receipt(
        self,
        closure: BrainTransactionClosure,
        *,
        proof: Mapping[str, Any] | None,
        closed_at_ts: float | None = None,
    ) -> dict[str, Any]:
        transaction = closure.transaction
        return {
            "schema": TERMINAL_RECEIPT_SCHEMA,
            "owner": OWNER,
            "transaction_id": transaction.transaction_id,
            "generation": int(transaction.generation),
            "terminal_kind": str(closure.terminal_kind or ""),
            "matched_by": str(closure.matched_by or ""),
            "reward_ledger_key": str(closure.reward_ledger_key or ""),
            "closed_at_ts": round(float(time.time() if closed_at_ts is None else closed_at_ts), 6),
            "proof_fingerprint": _checksum(dict(proof or {})),
            "transaction": self._compact_terminal_transaction(transaction),
        }

    @staticmethod
    def _compact_terminal_transaction(
        transaction: BrainTransaction,
    ) -> dict[str, Any]:
        """Retain terminal correlation truth without replaying decision graphs."""

        action_key = transaction.action_key
        compact_action_key: dict[str, Any] = {}
        if action_key is not None:
            full_action_key = action_key.as_dict()
            compact_action_key = {
                name: full_action_key[name]
                for name in (
                    "schema",
                    "owner",
                    "root_id",
                    "command_id",
                    "proof_id",
                    "session_scope_key",
                    "identity_key",
                    "source_generation",
                    "source",
                    "origin_kind",
                    "correlation_id",
                    "correlation_source",
                    "lever",
                    "target_shape",
                )
            }
        execution = transaction.execution
        execution_summary = None
        if execution is not None:
            execution_summary = {
                "decision_trace_id": str(execution.decision_trace_id or ""),
                "action_lever": str(execution.action_lever or ""),
                "action_intent": str(execution.action_intent or ""),
                "objective_id": str(execution.objective_id or ""),
                "action_suppressed": bool(execution.action_suppressed),
                "action_executed": bool(execution.action_executed),
                "action_origin": str(execution.action_origin or ""),
                "action_success": execution.action_success,
                "action_error": str(execution.action_error or ""),
                "action_root_id": str(execution.action_root_id or ""),
                "command_id": str(execution.command_id or ""),
                "proof_id": str(execution.proof_id or ""),
                "session_scope_key": str(execution.session_scope_key or ""),
                "identity_key": str(execution.identity_key or ""),
                "source_generation": int(execution.source_generation or 0),
                "target_shape": (
                    dict(compact_action_key.get("target_shape") or {})
                ),
            }
        readback = {
            str(key): value
            for key, value in transaction.readback
            if str(key) in _TERMINAL_READBACK_FIELDS
        }
        return {
            "transaction_id": transaction.transaction_id,
            "generation": int(transaction.generation),
            "stage": transaction.stage.value,
            "candidates": [],
            "selection": None,
            "no_action_reason": str(transaction.no_action_reason or ""),
            "arbitration": None,
            "execution": execution_summary,
            "readback": readback,
            "proof": {},
            "grade": str(transaction.grade or ""),
            "learning_admission": str(transaction.learning_admission or ""),
            "action_transaction": compact_action_key,
        }

    @classmethod
    def _compact_terminal_receipt(
        cls,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        row = dict(receipt or {})
        transaction = BrainTransaction.from_event_payload(
            dict(row.get("transaction") or {})
        )
        row["transaction"] = cls._compact_terminal_transaction(transaction)
        return row

    def _terminal_by_transaction_id(self, transaction_id: str) -> dict[str, Any] | None:
        wanted = str(transaction_id or "")
        for receipt in reversed(self._recent_closed):
            if str(receipt.get("transaction_id") or "") == wanted:
                return dict(receipt)
        return None

    def _terminal_by_reward_key(self, reward_key: str) -> dict[str, Any] | None:
        wanted = str(reward_key or "")
        for receipt in reversed(self._recent_closed):
            if wanted and str(receipt.get("reward_ledger_key") or "") == wanted:
                return dict(receipt)
        return None

    def _terminal_by_proof(self, proof: Mapping[str, Any]) -> dict[str, Any] | None:
        fingerprint = _checksum(dict(proof or {}))
        identity = proof.get("proof_identity")
        identity_row = dict(identity) if isinstance(identity, Mapping) else {}
        root_id = self._text(proof.get("action_root_id") or identity_row.get("action_root_id"))
        for receipt in reversed(self._recent_closed):
            if str(receipt.get("proof_fingerprint") or "") == fingerprint:
                return dict(receipt)
            transaction = BrainTransaction.from_event_payload(
                dict(receipt.get("transaction") or {})
            )
            if root_id and transaction.action_key is not None and transaction.action_key.root_id == root_id:
                return dict(receipt)
        return None

    @staticmethod
    def _duplicate_closure(receipt: Mapping[str, Any]) -> BrainTransactionClosure:
        transaction = BrainTransaction.from_event_payload(dict(receipt.get("transaction") or {}))
        return BrainTransactionClosure(
            transaction=transaction,
            matched_by=str(receipt.get("matched_by") or "durable_terminal_receipt"),
            terminal_kind=str(receipt.get("terminal_kind") or ""),
            reward_ledger_key=str(receipt.get("reward_ledger_key") or ""),
            duplicate=True,
        )

    def _next_counters(self, **deltas: int) -> dict[str, int]:
        counters = dict(self._counters)
        for name, delta in deltas.items():
            if name not in counters:
                raise RuntimeError(f"brain_transaction_counter_unknown:{name}")
            counters[name] = int(counters[name]) + int(delta or 0)
        return counters

    @staticmethod
    def _validate_terminal_reward(payload: Mapping[str, Any]) -> None:
        if payload.get("terminal") is not True:
            raise ValueError("brain_transaction_reward_not_terminal")
        if str(payload.get("grade_kind") or "") != "terminal_layered_reward":
            raise ValueError("brain_transaction_reward_grade_kind_invalid")
        if int(payload.get("roi_horizon_s", 0) or 0) <= 0:
            raise ValueError("brain_transaction_reward_horizon_invalid")
        if payload.get("reward_admitted") is not True:
            raise ValueError("brain_transaction_reward_not_admitted")
        if not str(payload.get("reward_ledger_key") or ""):
            raise ValueError("brain_transaction_reward_ledger_key_missing")

    @staticmethod
    def _validate_terminal_rejection(payload: Mapping[str, Any]) -> None:
        if payload.get("terminal") is not True:
            raise ValueError("brain_transaction_rejection_not_terminal")
        if str(payload.get("grade_kind") or "") != "terminal_layered_reward":
            raise ValueError("brain_transaction_rejection_grade_kind_invalid")
        if int(payload.get("roi_horizon_s", 0) or 0) <= 0:
            raise ValueError("brain_transaction_rejection_horizon_invalid")
        if payload.get("reward_admitted") is not False:
            raise ValueError("brain_transaction_rejection_reward_state_invalid")
        if str(payload.get("reward_ledger_key") or ""):
            raise ValueError("brain_transaction_rejection_reward_key_forbidden")

    @staticmethod
    def _trace_id(transaction: BrainTransaction) -> str:
        execution = transaction.execution
        if execution is not None and str(execution.decision_trace_id or ""):
            return str(execution.decision_trace_id)
        arbitration = transaction.arbitration
        return str(getattr(arbitration, "decision_trace_id", "") or "")

    @staticmethod
    def _objective_id(transaction: BrainTransaction) -> str:
        execution = transaction.execution
        if execution is not None and str(execution.objective_id or ""):
            return str(execution.objective_id)
        selection = transaction.selection
        return str(getattr(selection, "objective_id", "") or "")

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _validate_transaction_horizon(
        transaction: BrainTransaction,
        payload: Mapping[str, Any],
    ) -> None:
        key = transaction.action_key
        if key is None:
            raise ValueError("brain_transaction_action_root_missing")
        expected = terminal_reward_horizon_s(key)
        actual = int(payload.get("roi_horizon_s", 0) or 0)
        if actual != expected:
            raise ValueError(
                f"brain_transaction_reward_horizon_mismatch:{expected}:{actual}"
            )


def _checksum(payload: Mapping[str, Any]) -> str:
    canonical = {
        str(key): value
        for key, value in dict(payload or {}).items()
        if str(key) != "checksum"
    }
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def require_brain_transaction_register(owner: object) -> BrainTransactionRegister:
    controller = getattr(owner, "controller", None)
    brain = getattr(controller, "brain", None)
    register = getattr(brain, "transaction_register", None)
    if not isinstance(register, BrainTransactionRegister):
        raise RuntimeError("brain_transaction_register_unavailable")
    memory = getattr(brain, "memory", None)
    memory_dir = getattr(memory, "memory_dir", None)
    if memory_dir is not None:
        register.bind_persistence(memory_dir)
    return register


__all__ = [
    "BrainTransactionClosure",
    "BrainTransactionRegister",
    "require_brain_transaction_register",
]
