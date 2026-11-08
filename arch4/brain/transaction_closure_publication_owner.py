"""Single publication owner for durable Brain transaction closures."""

from __future__ import annotations

from typing import Any

from arch4.brain.transaction_register import BrainTransactionClosure
from arch4.event_journal import ControllerEventKind, ForensicEventJournal


OWNER = "arch4.brain.transaction_closure_publication_owner"


def publish_brain_transaction_closure(
    *,
    journal: ForensicEventJournal,
    closure: BrainTransactionClosure,
    ts: float | None = None,
) -> dict[str, Any]:
    """Publish proof/learning evidence once after durable register closure."""

    if closure.duplicate:
        return {
            "published": False,
            "duplicate": True,
            "transaction_id": closure.transaction.transaction_id,
        }
    transaction = closure.transaction
    payload = transaction.to_event_payload()
    terminal = {
        "matched_by": closure.matched_by,
        "terminal_kind": closure.terminal_kind,
        "reward_ledger_key": closure.reward_ledger_key,
    }
    journal.publish(
        ControllerEventKind.PROOF,
        generation=transaction.generation,
        transaction_id=transaction.transaction_id,
        owner="brain_transaction_register",
        payload={**payload, **terminal},
        ts=ts,
    )
    journal.publish(
        ControllerEventKind.LEARNING,
        generation=transaction.generation,
        transaction_id=transaction.transaction_id,
        owner="learning_authority",
        payload={
            "grade": transaction.grade,
            "learning_admission": transaction.learning_admission,
            **terminal,
        },
        ts=ts,
    )
    return {
        "published": True,
        "duplicate": False,
        "transaction_id": transaction.transaction_id,
        "terminal_kind": closure.terminal_kind,
        "reward_ledger_key": closure.reward_ledger_key,
    }


__all__ = ["OWNER", "publish_brain_transaction_closure"]
