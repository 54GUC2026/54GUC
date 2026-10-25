"""Single owner of selected-action lifecycle and typed outcome correlation."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from threading import RLock
import time
from typing import Any, Mapping

from arch4.action_transaction import (
    ActionTransactionKey,
)
from arch4.action_origin import (
    is_human_operator_origin,
    require_action_origin_name,
)
from arch4.core.selected_action_register import (
    SelectedActionClaimContext,
    SelectedActionPublication,
    SelectedActionStage,
    SelectedActionTransaction,
    SelectedActionDomainResultReceipt,
    SelectedActionTerminalRouteReceipt,
    TERMINAL_ROUTER_OWNER,
    create_selected_action_store,
)
from arch4.command_transactions import (
    COMMAND_AUTHORITY_OWNER,
    CommandLifecycle,
    CommandLifecycleStage,
    CommandTerminalReceipt,
    CommandTerminalStatus,
    CommandTransaction,
    RadioMutationLease,
)
from arch4.memory_store.event_journal import JsonlEventJournal
from arch4.payload_budget import retained_payload_bytes


OWNER = "arch4.action_transaction_authority"
SCHEMA = "ActionTransactionAuthority.v3"
JOURNAL_SCHEMA = "ActionTransactionAuthorityEvent.v4"
LIFECYCLE_KEY_SCHEMA = "ActionLifecycleKey.v1"
MAX_RECENT = 128
MAX_LIFECYCLE_RECEIPT_BYTES = 32 * 1024
GRADE_WINDOWS_S = (60, 90, 360, 600)
_LIFECYCLE_MAP_TAG = "__action_lifecycle_mapping__"
_LIFECYCLE_SEQUENCE_TAG = "__action_lifecycle_sequence__"


class ActionLifecycleStage(str, Enum):
    SELECTED = "selected"
    SELECTION_CLAIMED = "selection_claimed"
    ADMITTED = "admitted"
    QUEUED = "queued"
    EXECUTING = "executing"
    CONTROLLER_RECEIPT = "controller_receipt"
    MODEM_ACCEPTED = "modem_accepted"
    TARGET_CONFIRMED = "target_confirmed"
    STREAM_SETTLED = "stream_settled"
    CHART_CONTINUED = "chart_continued"
    GRADE_1M = "grade_1m"
    GRADE_EMERGENCY = "grade_emergency"
    GRADE_6M = "grade_6m"
    GRADE_10M = "grade_10m"
    COMPLETE = "complete"
    REJECTED = "rejected"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


_ORDER = {
    ActionLifecycleStage.SELECTED: 10,
    ActionLifecycleStage.SELECTION_CLAIMED: 20,
    ActionLifecycleStage.ADMITTED: 30,
    ActionLifecycleStage.QUEUED: 40,
    ActionLifecycleStage.EXECUTING: 50,
    ActionLifecycleStage.CONTROLLER_RECEIPT: 60,
    ActionLifecycleStage.MODEM_ACCEPTED: 70,
    ActionLifecycleStage.TARGET_CONFIRMED: 80,
    ActionLifecycleStage.STREAM_SETTLED: 90,
    ActionLifecycleStage.CHART_CONTINUED: 100,
    ActionLifecycleStage.GRADE_1M: 110,
    ActionLifecycleStage.GRADE_EMERGENCY: 115,
    ActionLifecycleStage.GRADE_6M: 120,
    ActionLifecycleStage.GRADE_10M: 130,
    ActionLifecycleStage.COMPLETE: 140,
}
_FAILURE_STAGES = frozenset(
    {
        ActionLifecycleStage.REJECTED,
        ActionLifecycleStage.FAILED,
        ActionLifecycleStage.TIMED_OUT,
        ActionLifecycleStage.CANCELLED,
    }
)
_TERMINAL_STAGES = _FAILURE_STAGES | {ActionLifecycleStage.COMPLETE}


def _freeze_lifecycle_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return (
            _LIFECYCLE_MAP_TAG,
            tuple(
                sorted(
                    (
                        (str(key), _freeze_lifecycle_value(item))
                        for key, item in value.items()
                    ),
                    key=lambda row: row[0],
                )
            ),
        )
    if isinstance(value, (set, frozenset)):
        return (
            _LIFECYCLE_SEQUENCE_TAG,
            tuple(
                sorted(
                    (_freeze_lifecycle_value(item) for item in value),
                    key=repr,
                )
            ),
        )
    if isinstance(value, (list, tuple)):
        return (
            _LIFECYCLE_SEQUENCE_TAG,
            tuple(_freeze_lifecycle_value(item) for item in value),
        )
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(
        "action_lifecycle_target_value_not_serializable:"
        + type(value).__name__
    )


def _thaw_lifecycle_value(value: Any) -> Any:
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and value[0] == _LIFECYCLE_MAP_TAG
        and isinstance(value[1], tuple)
    ):
        return {
            str(item[0]): _thaw_lifecycle_value(item[1])
            for item in value[1]
        }
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and value[0] == _LIFECYCLE_SEQUENCE_TAG
        and isinstance(value[1], tuple)
    ):
        return [_thaw_lifecycle_value(item) for item in value[1]]
    return value


@dataclass(frozen=True, slots=True)
class ActionLifecycleKey:
    """Bounded action correlation retained by the lifecycle owner.

    Full Brain and Bandit receipts remain with their canonical decision and
    command owners.  Lifecycle needs their immutable IDs, never another copy
    of either decision graph.
    """

    root_id: str
    command_id: str
    proof_id: str
    session_scope_key: str
    identity_key: str
    source_generation: int
    source: str
    origin_kind: str
    correlation_id: str
    correlation_source: str
    lever: str
    target_shape: tuple[Any, ...]
    opportunity_id: str = ""
    opportunity_set_id: str = ""
    decision_id: str = ""
    decision_receipt_id: str = ""
    bandit_selection_receipt_id: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "root_id",
            "command_id",
            "proof_id",
            "session_scope_key",
            "identity_key",
            "source",
            "origin_kind",
            "correlation_id",
            "correlation_source",
            "lever",
        ):
            if not str(getattr(self, field_name) or "").strip():
                raise ValueError(f"action_lifecycle_key_{field_name}_missing")
        require_action_origin_name(self.origin_kind)
        if int(self.source_generation or 0) <= 0:
            raise ValueError("action_lifecycle_key_source_generation_missing")
        if not self.target_shape_dict():
            raise ValueError("action_lifecycle_key_target_shape_missing")

    @property
    def operator_initiated(self) -> bool:
        return is_human_operator_origin(self.origin_kind)

    def target_shape_dict(self) -> dict[str, Any]:
        value = _thaw_lifecycle_value(self.target_shape)
        if not isinstance(value, dict):
            raise ValueError("action_lifecycle_key_target_shape_invalid")
        return value

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": LIFECYCLE_KEY_SCHEMA,
            "owner": OWNER,
            "root_id": self.root_id,
            "command_id": self.command_id,
            "proof_id": self.proof_id,
            "session_scope_key": self.session_scope_key,
            "identity_key": self.identity_key,
            "source_generation": int(self.source_generation),
            "source": self.source,
            "origin_kind": self.origin_kind,
            "correlation_id": self.correlation_id,
            "correlation_source": self.correlation_source,
            "lever": self.lever,
            "target_shape": self.target_shape_dict(),
            "opportunity_id": self.opportunity_id,
            "opportunity_set_id": self.opportunity_set_id,
            "decision_id": self.decision_id,
            "decision_receipt_id": self.decision_receipt_id,
            "bandit_selection_receipt_id": self.bandit_selection_receipt_id,
        }

    @classmethod
    def from_action_key(cls, key: ActionTransactionKey) -> "ActionLifecycleKey":
        if not isinstance(key, ActionTransactionKey):
            raise TypeError("action_transaction_key_required")
        return cls(
            root_id=key.root_id,
            command_id=key.command_id,
            proof_id=key.proof_id,
            session_scope_key=key.matrix_identity.session_scope_key,
            identity_key=key.identity_key,
            source_generation=key.matrix_identity.matrix_sequence,
            source=key.source,
            origin_kind=key.origin_kind,
            correlation_id=key.correlation_id,
            correlation_source=key.correlation_source,
            lever=key.lever,
            target_shape=_freeze_lifecycle_value(
                key.target.to_mapping()
            ),
            opportunity_id=key.opportunity_id,
            opportunity_set_id=key.opportunity_set_id,
            decision_id=key.decision_id,
            decision_receipt_id=key.decision_receipt_id,
            bandit_selection_receipt_id=key.bandit_selection_receipt_id,
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ActionLifecycleKey":
        row = dict(raw or {})
        if row.get("schema") != LIFECYCLE_KEY_SCHEMA or row.get("owner") != OWNER:
            raise ValueError("action_lifecycle_key_contract_invalid")
        return cls(
            root_id=str(row.get("root_id") or ""),
            command_id=str(row.get("command_id") or ""),
            proof_id=str(row.get("proof_id") or ""),
            session_scope_key=str(row.get("session_scope_key") or ""),
            identity_key=str(row.get("identity_key") or ""),
            source_generation=int(row.get("source_generation") or 0),
            source=str(row.get("source") or ""),
            origin_kind=str(row.get("origin_kind") or ""),
            correlation_id=str(row.get("correlation_id") or ""),
            correlation_source=str(row.get("correlation_source") or ""),
            lever=str(row.get("lever") or ""),
            target_shape=_freeze_lifecycle_value(
                dict(row.get("target_shape") or {})
            ),
            opportunity_id=str(row.get("opportunity_id") or ""),
            opportunity_set_id=str(row.get("opportunity_set_id") or ""),
            decision_id=str(row.get("decision_id") or ""),
            decision_receipt_id=str(row.get("decision_receipt_id") or ""),
            bandit_selection_receipt_id=str(
                row.get("bandit_selection_receipt_id") or ""
            ),
        )


@dataclass(frozen=True, slots=True)
class ActionLifecycleReceipt:
    command_id: str
    stage: ActionLifecycleStage
    changed_ts: float
    root_id: str = ""
    origin_kind: str = ""
    action_kind: str = ""
    action_key: ActionLifecycleKey | ActionTransactionKey | None = None
    evidence: Mapping[str, Any] | None = None
    grades: Mapping[str, Any] | None = None
    timeline: Mapping[str, float] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.action_key, ActionTransactionKey):
            object.__setattr__(
                self,
                "action_key",
                ActionLifecycleKey.from_action_key(self.action_key),
            )
        elif self.action_key is not None and not isinstance(
            self.action_key,
            ActionLifecycleKey,
        ):
            raise TypeError("action_lifecycle_key_required")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            "command_id": self.command_id,
            "root_id": self.root_id,
            "origin_kind": self.origin_kind,
            "action_kind": self.action_kind,
            "stage": self.stage.value,
            "changed_ts": float(self.changed_ts),
            "action_transaction": self.action_key.as_dict() if self.action_key else {},
            "evidence": deepcopy(dict(self.evidence or {})),
            "grades": deepcopy(dict(self.grades or {})),
            "timeline": {
                str(key): float(value)
                for key, value in dict(self.timeline or {}).items()
            },
            "terminal": self.stage in _TERMINAL_STAGES,
        }


class ActionTransactionAuthority:
    """Own every action transition; subordinate stores expose no public truth."""

    def __init__(
        self,
        *,
        persistence_dir: str | Path | None = None,
        max_recent: int = MAX_RECENT,
    ) -> None:
        self._lock = RLock()
        self._selection_store = create_selected_action_store(
            persistence_dir=persistence_dir,
        )
        self._active: dict[str, ActionLifecycleReceipt] = {}
        self._recent: deque[ActionLifecycleReceipt] = deque(
            maxlen=max(1, int(max_recent or MAX_RECENT))
        )
        self._sequence = 0
        self._restore_error = ""
        self._journal = (
            JsonlEventJournal(
                Path(persistence_dir), "action_transaction_authority.jsonl"
            )
            if persistence_dir is not None
            else None
        )
        if self._journal is not None:
            self._restore()

    # Selection store adapter. These are lifecycle transitions owned here.
    def record_selected(
        self,
        *,
        command_id: str,
        origin_kind: str,
        action_kind: str,
        selection_receipt: Mapping[str, Any],
        now: float | None = None,
    ) -> ActionLifecycleReceipt:
        """Idempotently admit one canonical Brain selection receipt."""

        command = str(command_id or "").strip()
        with self._lock:
            retained = self._active.get(command) or self._recent_by_command(
                command
            )
        if retained is not None:
            if retained.origin_kind != require_action_origin_name(origin_kind):
                raise RuntimeError("action_selection_origin_rebind")
            if retained.action_kind != str(action_kind or "").strip():
                raise RuntimeError("action_selection_kind_rebind")
            return retained
        return self._transition_command(
            command_id=command,
            origin_kind=origin_kind,
            action_kind=action_kind,
            stage=ActionLifecycleStage.SELECTED,
            now=now,
            evidence={
                "selection": _selection_receipt_projection(selection_receipt)
            },
        )

    def publish(
        self,
        *,
        selected_action: Mapping[str, Any],
        proof_template: Mapping[str, Any],
        now: float | None = None,
    ) -> SelectedActionPublication:
        result = self._selection_store.publish(
            selected_action=selected_action,
            proof_template=proof_template,
            now=now,
        )
        if result.accepted:
            transaction = result.transaction
            if transaction is None:
                raise RuntimeError(
                    "accepted_selected_action_transaction_missing"
                )
            selected_row = dict(transaction.selected_action or {})
            self.record_selected(
                command_id=transaction.command_id,
                origin_kind="smart_loop",
                action_kind=str(selected_row.get("lever") or "cellular_action"),
                selection_receipt=transaction.as_dict(
                    include_selected_action=False
                ),
                now=now,
            )
        return result

    def claim(
        self,
        *,
        consumer: str,
        claim_context: SelectedActionClaimContext,
        now: float | None = None,
        lease_s: float | None = None,
    ) -> SelectedActionTransaction | None:
        before = dict(self._selection_store.status().get("active") or {})
        transaction = self._selection_store.claim(
            consumer=consumer,
            claim_context=claim_context,
            now=now,
            lease_s=lease_s,
        )
        if transaction is not None:
            self._transition_command(
                command_id=transaction.command_id,
                stage=ActionLifecycleStage.SELECTION_CLAIMED,
                now=now,
                evidence={"claimed_by": transaction.claimed_by},
            )
        elif before:
            rejected = self._selection_store.read_terminal()
            if (
                rejected is not None
                and rejected.stage is SelectedActionStage.REJECTED
                and rejected.command_id
                == str(before.get("command_id") or "")
            ):
                self._project_selection_rejection(
                    rejected,
                    now=now,
                    receipt={
                        "owner": "arch4.action_transaction_authority",
                        "stage": "claim_context",
                    },
                )
        return transaction

    def handoff_to_command_authority(
        self,
        *,
        command_id: str,
        action_key: ActionTransactionKey,
        receipt: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> SelectedActionTransaction:
        transaction = self._selection_store.handoff_to_command_authority(
            command_id=command_id,
            action_key=action_key,
            receipt=receipt,
            now=now,
        )
        self._advance(
            action_key,
            ActionLifecycleStage.QUEUED,
            now=float(time.time() if now is None else now),
            evidence={
                "command_handoff": _command_handoff_projection(receipt)
            },
        )
        return transaction

    def accept_command_terminal(
        self,
        *,
        command_id: str,
        terminal_receipt: CommandTerminalReceipt | Mapping[str, Any],
    ) -> ActionLifecycleReceipt:
        receipt = (
            terminal_receipt
            if isinstance(terminal_receipt, CommandTerminalReceipt)
            else CommandTerminalReceipt.from_mapping(terminal_receipt)
        )
        if receipt.transaction.command_id != str(command_id or ""):
            raise RuntimeError("action_lifecycle_terminal_command_mismatch")
        lifecycle_receipt = self._recent_by_command(receipt.transaction.command_id)
        if lifecycle_receipt is not None:
            retained_terminal = dict(lifecycle_receipt.evidence or {}).get(
                "command_terminal_receipt"
            )
            if isinstance(retained_terminal, Mapping) and dict(
                retained_terminal
            ) == _command_terminal_projection(receipt):
                return lifecycle_receipt
            raise RuntimeError(
                "action_lifecycle_terminal_receipt_after_terminal"
            )
        return self._accept_transport_terminal(receipt)

    def accept_selection_terminal(
        self,
        *,
        command_id: str,
        terminal_receipt: CommandTerminalReceipt,
    ) -> SelectedActionTransaction | None:
        """Update the private selection store only when this is its command."""

        if not isinstance(terminal_receipt, CommandTerminalReceipt):
            raise TypeError("selection_terminal_receipt_required")
        active_selection = dict(self._selection_store.status().get("active") or {})
        if str(active_selection.get("command_id") or "") != str(command_id or ""):
            return None
        return self._selection_store.accept_command_terminal(
            command_id=command_id,
            terminal_receipt=terminal_receipt,
        )

    def accept_cellular_claim(
        self,
        *,
        lease: RadioMutationLease,
        receipt: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> ActionLifecycleReceipt:
        if not isinstance(lease, RadioMutationLease):
            raise TypeError("action_lifecycle_cellular_lease_required")
        key = lease.transaction.action_key
        if key is None:
            raise RuntimeError("action_lifecycle_cellular_action_key_missing")
        self.transition(
            action_key=key,
            stage=ActionLifecycleStage.ADMITTED,
            now=now,
            evidence={
                "command_claim": {
                    "lease_owner": lease.owner,
                    "acquired_ts": lease.acquired_ts,
                    **dict(receipt or {}),
                }
            },
        )
        return self.transition(
            action_key=key,
            stage=ActionLifecycleStage.QUEUED,
            now=now,
            evidence={"command_root_id": lease.root_id},
        )

    def accept_cellular_lifecycle(
        self,
        lifecycle: CommandLifecycle,
        *,
        now: float | None = None,
    ) -> ActionLifecycleReceipt:
        if not isinstance(lifecycle, CommandLifecycle):
            raise TypeError("action_lifecycle_command_lifecycle_required")
        current = float(time.time() if now is None else now)
        self._ensure_cellular_claim(lifecycle.lease, now=current)
        lifecycle_projection = _command_lifecycle_projection(lifecycle)
        if lifecycle.stage in {
            CommandLifecycleStage.DISPATCHED,
            CommandLifecycleStage.ACCEPTED,
            CommandLifecycleStage.CONFIRMED,
        }:
            self._advance(
                lifecycle.key,
                ActionLifecycleStage.EXECUTING,
                now=current,
                evidence={"command_lifecycle": lifecycle_projection},
            )
            result = self._advance(
                lifecycle.key,
                ActionLifecycleStage.CONTROLLER_RECEIPT,
                now=current,
                evidence={"command_lifecycle": lifecycle_projection},
            )
        else:
            result = self._active_by_root(lifecycle.key.root_id)
        if lifecycle.stage in {
            CommandLifecycleStage.ACCEPTED,
            CommandLifecycleStage.CONFIRMED,
        }:
            result = self._advance(
                lifecycle.key,
                ActionLifecycleStage.MODEM_ACCEPTED,
                now=float(lifecycle.accepted_ts or current),
                evidence={"command_lifecycle": lifecycle_projection},
            )
        if lifecycle.stage is CommandLifecycleStage.CONFIRMED:
            result = self._advance(
                lifecycle.key,
                ActionLifecycleStage.TARGET_CONFIRMED,
                now=float(lifecycle.confirmed_ts or current),
                evidence={"command_lifecycle": lifecycle_projection},
            )
        if result is None:
            raise RuntimeError("action_lifecycle_cellular_projection_missing")
        return result

    def reject_selection(
        self,
        *,
        command_id: str,
        reason: str,
        now: float | None = None,
        receipt: Mapping[str, Any] | None = None,
    ) -> SelectedActionTransaction:
        transaction = self._selection_store.reject_selection(
            command_id=command_id,
            reason=reason,
            now=now,
            receipt=receipt,
        )
        self._project_selection_rejection(
            transaction,
            now=now,
            receipt=receipt,
        )
        return transaction

    def reject_unadmitted(
        self,
        *,
        command_id: str,
        origin_kind: str,
        action_kind: str,
        reason: str,
        now: float | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> ActionLifecycleReceipt:
        """Record a typed boundary rejection before an action key exists."""

        return self._transition_command(
            command_id=command_id,
            origin_kind=origin_kind,
            action_kind=action_kind,
            stage=ActionLifecycleStage.REJECTED,
            now=now,
            evidence={"reason": str(reason or "rejected"), **dict(evidence or {})},
        )

    def expire_overdue(
        self, *, now: float | None = None
    ) -> SelectedActionTransaction | None:
        before = dict(self._selection_store.status().get("active") or {})
        transaction = self._selection_store.expire_overdue(now=now)
        if (
            transaction is not None
            and transaction.stage is SelectedActionStage.REJECTED
            and str(before.get("stage") or "")
            not in {
                SelectedActionStage.TERMINAL.value,
                SelectedActionStage.REJECTED.value,
            }
        ):
            self._project_selection_rejection(
                transaction,
                now=now,
                receipt={
                    "owner": "arch4.action_transaction_authority",
                    "stage": "selection_expiry",
                },
            )
        return transaction

    def read_terminal(self) -> SelectedActionTransaction | None:
        return self._selection_store.read_terminal()

    def read_selected_transaction(
        self,
    ) -> SelectedActionTransaction | None:
        return self._selection_store.read_active()

    def accept_selected_domain_result(
        self,
        receipt: SelectedActionDomainResultReceipt,
    ) -> SelectedActionTransaction:
        return self._selection_store.accept_domain_result(receipt)

    def acknowledge_non_optimization_terminal(
        self,
        *,
        route_receipt: SelectedActionTerminalRouteReceipt,
    ) -> SelectedActionTransaction:
        if not isinstance(route_receipt, SelectedActionTerminalRouteReceipt):
            raise TypeError("selected_action_terminal_route_receipt_required")
        if route_receipt.owner != TERMINAL_ROUTER_OWNER:
            raise RuntimeError("selected_action_terminal_router_owner_invalid")
        transaction = self._selection_store.read_terminal()
        if transaction is None:
            raise RuntimeError("selected_action_terminal_receipt_missing")
        if transaction.command_id != route_receipt.command_id:
            raise RuntimeError("selected_action_terminal_route_command_mismatch")
        selected = dict(transaction.selected_action or {})
        if str(selected.get("owner_class") or "") == "optimization":
            raise RuntimeError("optimization_terminal_router_forbidden")
        if transaction.stage is not route_receipt.terminal_stage:
            raise RuntimeError("selected_action_terminal_route_stage_mismatch")
        if transaction.stage is SelectedActionStage.TERMINAL:
            domain_result = SelectedActionDomainResultReceipt.from_mapping(
                dict(transaction.domain_result_receipt or {})
            )
            if (
                domain_result.receipt_id
                != route_receipt.domain_result_receipt_id
            ):
                raise RuntimeError(
                    "selected_action_terminal_route_domain_result_mismatch"
                )
        return self._selection_store.acknowledge_terminal_consumed(
            command_id=route_receipt.command_id
        )

    def acknowledge_optimization_terminal(
        self, *, command_id: str
    ) -> SelectedActionTransaction:
        transaction = self._selection_store.read_terminal()
        if transaction is None:
            raise RuntimeError("selected_action_terminal_receipt_missing")
        if transaction.command_id != str(command_id or ""):
            raise RuntimeError("optimization_terminal_command_mismatch")
        if str(transaction.selected_action.get("owner_class") or "") != (
            "optimization"
        ):
            raise RuntimeError("non_optimization_terminal_smart_loop_forbidden")
        return self._selection_store.acknowledge_terminal_consumed(
            command_id=command_id
        )

    def acknowledge_terminal_consumed(
        self, *, command_id: str
    ) -> SelectedActionTransaction:
        raise RuntimeError(
            "old_selected_terminal_acknowledgement_path_deleted"
        )

    def _project_selection_rejection(
        self,
        transaction: SelectedActionTransaction,
        *,
        now: float | None,
        receipt: Mapping[str, Any] | None,
    ) -> None:
        retained = self._recent_by_command(transaction.command_id)
        if retained is None:
            self._transition_command(
                command_id=transaction.command_id,
                stage=ActionLifecycleStage.REJECTED,
                now=now,
                evidence={
                    "reason": _bounded_reason(
                        transaction.terminal_reason
                    )[0],
                    "receipt": _selection_rejection_projection(receipt),
                },
            )
        elif retained.stage is not ActionLifecycleStage.REJECTED:
            raise RuntimeError(
                "selection_rejection_public_lifecycle_conflict:"
                + retained.stage.value
            )
        from arch4.core.selected_action_terminal_router import (
            route_selected_action_terminal,
        )

        route_selected_action_terminal(self, now=now)

    def record_admitted(
        self,
        *,
        action_key: ActionTransactionKey,
        admission_receipt: Mapping[str, Any],
        now: float | None = None,
    ) -> ActionLifecycleReceipt:
        return self.transition(
            action_key=action_key,
            stage=ActionLifecycleStage.ADMITTED,
            now=now,
            evidence={
                "admission_receipt": _admission_receipt_projection(
                    admission_receipt
                )
            },
        )

    def transition(
        self,
        *,
        action_key: ActionTransactionKey,
        stage: ActionLifecycleStage,
        now: float | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> ActionLifecycleReceipt:
        if not isinstance(action_key, ActionTransactionKey):
            raise TypeError("action_lifecycle_requires_action_transaction_key")
        if not isinstance(stage, ActionLifecycleStage):
            raise TypeError("action_lifecycle_requires_typed_stage")
        return self._transition_command(
            command_id=action_key.command_id,
            root_id=action_key.root_id,
            action_key=action_key,
            origin_kind=action_key.origin_kind,
            action_kind=action_key.lever,
            stage=stage,
            now=now,
            evidence=evidence,
        )

    def record_chart_receipt(
        self,
        *,
        root_id: str,
        source_sequence: int,
        chart_sequence: int,
        rendered_at: float,
    ) -> ActionLifecycleReceipt:
        receipt = self._active_by_root(root_id)
        if receipt is None or receipt.action_key is None:
            raise RuntimeError("action_chart_receipt_root_unknown")
        if receipt.stage not in _ORDER or (
            _ORDER[receipt.stage] < _ORDER[ActionLifecycleStage.TARGET_CONFIRMED]
        ):
            raise RuntimeError(
                f"action_chart_receipt_before_target:{receipt.stage.value}"
            )
        retained_chart = dict(receipt.evidence or {}).get("chart_receipt")
        if isinstance(retained_chart, Mapping):
            if (
                int(retained_chart.get("source_sequence") or 0)
                == int(source_sequence)
                and int(retained_chart.get("chart_sequence") or 0)
                == int(chart_sequence)
            ):
                return receipt
            raise RuntimeError("action_chart_receipt_replacement_forbidden")
        evidence = {
            "chart_receipt": {
                "source_sequence": int(source_sequence),
                "chart_sequence": int(chart_sequence),
                "rendered_at": float(rendered_at),
            }
        }
        if _ORDER[receipt.stage] > _ORDER[ActionLifecycleStage.CHART_CONTINUED]:
            return self._transition_command(
                command_id=receipt.command_id,
                root_id=receipt.root_id,
                action_key=receipt.action_key,
                origin_kind=receipt.origin_kind,
                action_kind=receipt.action_kind,
                stage=receipt.stage,
                now=rendered_at,
                evidence=evidence,
                grades=receipt.grades,
                timeline_events={
                    ActionLifecycleStage.CHART_CONTINUED.value: float(
                        rendered_at
                    )
                },
            )
        return self._transition_command(
            command_id=receipt.command_id,
            root_id=receipt.root_id,
            action_key=receipt.action_key,
            origin_kind=receipt.origin_kind,
            action_kind=receipt.action_kind,
            stage=ActionLifecycleStage.CHART_CONTINUED,
            now=rendered_at,
            evidence=evidence,
        )

    def record_grade(
        self,
        *,
        root_id: str,
        horizon_s: int,
        grade: int,
        reason_receipt: Mapping[str, Any],
        graded_at: float | None = None,
    ) -> ActionLifecycleReceipt:
        horizon = int(horizon_s)
        if horizon not in GRADE_WINDOWS_S:
            raise ValueError(f"action_grade_horizon_invalid:{horizon}")
        value = int(grade)
        if value < -3 or value > 3:
            raise ValueError(f"action_grade_out_of_range:{value}")
        current = self.receipt_for_root(root_id)
        if current is None or current.action_key is None:
            raise RuntimeError("action_grade_root_unknown")
        grades = dict(current.grades or {})
        key = f"{horizon}s"
        retained = grades.get(key)
        reason_projection = grade_reason_receipt_projection(reason_receipt)
        if (
            reason_projection.get("owner")
            != "arch4.brain.proof_feedback_owner"
        ):
            raise ValueError("action_grade_reason_owner_invalid")
        if int(reason_projection.get("horizon_s") or 0) != horizon:
            raise ValueError("action_grade_reason_horizon_mismatch")
        incoming = {
            "grade": value,
            "graded_at": float(time.time() if graded_at is None else graded_at),
            "reason_receipt": reason_projection,
        }
        if retained is not None:
            if retained != incoming:
                raise RuntimeError(f"action_grade_receipt_replaced:{key}")
            return current
        if current.stage in _TERMINAL_STAGES:
            raise RuntimeError(
                f"action_grade_after_terminal_forbidden:{current.stage.value}:{key}"
            )
        grades[key] = incoming
        stage = {
            60: ActionLifecycleStage.GRADE_1M,
            90: ActionLifecycleStage.GRADE_EMERGENCY,
            360: ActionLifecycleStage.GRADE_6M,
            600: ActionLifecycleStage.GRADE_10M,
        }[horizon]
        result = self._transition_command(
            command_id=current.command_id,
            root_id=current.root_id,
            action_key=current.action_key,
            stage=stage,
            now=incoming["graded_at"],
            evidence=current.evidence,
            grades=grades,
        )
        if horizon in {90, 600}:
            return self._transition_command(
                command_id=current.command_id,
                root_id=current.root_id,
                action_key=current.action_key,
                origin_kind=current.origin_kind,
                action_kind=current.action_kind,
                stage=ActionLifecycleStage.COMPLETE,
                now=incoming["graded_at"],
                evidence={"grading_complete": True},
            )
        return result

    def active_receipt(self) -> ActionLifecycleReceipt | None:
        with self._lock:
            if not self._active:
                return None
            return max(self._active.values(), key=lambda item: item.changed_ts)

    def latest_receipt(self) -> ActionLifecycleReceipt | None:
        """Return the newest canonical receipt for projection reconciliation."""

        with self._lock:
            candidates = list(self._active.values()) + list(self._recent)
            if not candidates:
                return None
            return max(candidates, key=lambda item: item.changed_ts)

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = self.active_receipt()
            return {
                "schema": SCHEMA,
                "owner": OWNER,
                "sequence": int(self._sequence),
                "active": active.as_dict() if active is not None else {},
                "active_count": len(self._active),
                "recent_count": len(self._recent),
                "selection_store": self._selection_store.status(),
                "restore_error": self._restore_error,
            }

    def selection_status(self) -> dict[str, Any]:
        """Internal selection-store projection exposed only through this owner."""

        return self._selection_store.status()

    def receipt_for_root(self, root_id: str) -> ActionLifecycleReceipt | None:
        active = self._active_by_root(root_id)
        if active is not None:
            return active
        root = str(root_id or "").strip()
        with self._lock:
            for receipt in reversed(self._recent):
                if receipt.root_id == root:
                    return receipt
        return None

    def recent_receipts(self) -> tuple[ActionLifecycleReceipt, ...]:
        with self._lock:
            return tuple(self._recent)

    def _transition_command(
        self,
        *,
        command_id: str,
        stage: ActionLifecycleStage,
        now: float | None,
        root_id: str = "",
        action_key: ActionLifecycleKey | ActionTransactionKey | None = None,
        origin_kind: str = "",
        action_kind: str = "",
        evidence: Mapping[str, Any] | None = None,
        grades: Mapping[str, Any] | None = None,
        timeline_events: Mapping[str, float] | None = None,
    ) -> ActionLifecycleReceipt:
        command = str(command_id or "").strip()
        if not command:
            raise ValueError("action_lifecycle_command_id_missing")
        changed_ts = float(time.time() if now is None else now)
        lifecycle_key = _lifecycle_key(action_key)
        with self._lock:
            previous = self._active.get(command)
            typed_origin = require_action_origin_name(
                origin_kind
                or (lifecycle_key.origin_kind if lifecycle_key else "")
                or (previous.origin_kind if previous else "")
            )
            kind = str(
                action_kind
                or (lifecycle_key.lever if lifecycle_key else "")
                or (previous.action_kind if previous else "")
            ).strip()
            if not kind:
                raise ValueError("action_lifecycle_action_kind_missing")
            if previous is not None:
                if previous.stage in _TERMINAL_STAGES:
                    if previous.stage is stage:
                        return previous
                    raise RuntimeError(
                        f"action_lifecycle_terminal_overwrite:{previous.stage.value}->{stage.value}"
                    )
                if stage not in _FAILURE_STAGES and _ORDER[stage] < _ORDER[previous.stage]:
                    raise RuntimeError(
                        f"action_lifecycle_stage_regression:{previous.stage.value}->{stage.value}"
                    )
                if previous.root_id and root_id and previous.root_id != root_id:
                    raise RuntimeError("action_lifecycle_root_rebind")
                if (
                    previous.action_key
                    and lifecycle_key
                    and previous.action_key != lifecycle_key
                ):
                    raise RuntimeError("action_lifecycle_action_key_rebind")
                if previous.origin_kind != typed_origin:
                    raise RuntimeError("action_lifecycle_origin_rebind")
                if previous.action_kind != kind:
                    raise RuntimeError("action_lifecycle_action_kind_rebind")
            merged_evidence = dict(previous.evidence or {}) if previous else {}
            merged_evidence.update(deepcopy(dict(evidence or {})))
            timeline = dict(previous.timeline or {}) if previous else {}
            for event_name, event_ts in dict(timeline_events or {}).items():
                try:
                    ActionLifecycleStage(str(event_name))
                except ValueError as exc:
                    raise ValueError(
                        f"action_lifecycle_timeline_event_invalid:{event_name}"
                    ) from exc
                retained_event_ts = timeline.get(str(event_name))
                if (
                    retained_event_ts is not None
                    and float(retained_event_ts) != float(event_ts)
                ):
                    raise RuntimeError(
                        f"action_lifecycle_timeline_event_replaced:{event_name}"
                    )
                timeline.setdefault(str(event_name), float(event_ts))
            retained_stage_ts = timeline.get(stage.value)
            if retained_stage_ts is not None and retained_stage_ts != changed_ts:
                if previous is not None and previous.stage is stage:
                    changed_ts = float(previous.changed_ts)
                else:
                    raise RuntimeError(
                        f"action_lifecycle_stage_timestamp_replaced:{stage.value}"
                    )
            timeline.setdefault(stage.value, changed_ts)
            receipt = ActionLifecycleReceipt(
                command_id=command,
                stage=stage,
                changed_ts=changed_ts,
                root_id=str(root_id or (previous.root_id if previous else "")),
                origin_kind=typed_origin,
                action_kind=kind,
                action_key=lifecycle_key or (previous.action_key if previous else None),
                evidence=merged_evidence,
                grades=deepcopy(dict(grades or (previous.grades if previous else {}) or {})),
                timeline=timeline,
            )
            retained_payload_bytes(
                receipt.as_dict(),
                max_bytes=MAX_LIFECYCLE_RECEIPT_BYTES,
                label="action lifecycle receipt",
            )
            self._active[command] = receipt
            if stage in _TERMINAL_STAGES:
                self._recent.append(receipt)
                self._active.pop(command, None)
            self._commit(event=stage.value, receipt=receipt)
            return receipt

    def _active_by_root(self, root_id: str) -> ActionLifecycleReceipt | None:
        root = str(root_id or "").strip()
        if not root:
            return None
        with self._lock:
            for receipt in self._active.values():
                if receipt.root_id == root:
                    return receipt
        return None

    def _recent_by_command(self, command_id: str) -> ActionLifecycleReceipt | None:
        command = str(command_id or "").strip()
        with self._lock:
            for receipt in reversed(self._recent):
                if receipt.command_id == command:
                    return receipt
        return None

    def _advance(
        self,
        key: ActionLifecycleKey | ActionTransactionKey,
        stage: ActionLifecycleStage,
        *,
        now: float,
        evidence: Mapping[str, Any] | None = None,
    ) -> ActionLifecycleReceipt:
        current = self._active_by_root(key.root_id)
        if current is not None and current.stage in _ORDER:
            if _ORDER[current.stage] > _ORDER[stage]:
                return current
        return self._transition_command(
            command_id=key.command_id,
            root_id=key.root_id,
            action_key=key,
            origin_kind=key.origin_kind,
            action_kind=key.lever,
            stage=stage,
            now=now,
            evidence=evidence,
        )

    def _ensure_cellular_claim(
        self, lease: RadioMutationLease, *, now: float
    ) -> None:
        if self._active_by_root(lease.root_id) is None:
            self.accept_cellular_claim(
                lease=lease,
                receipt={"reconciled_from_command_authority": True},
                now=now,
            )

    def _accept_transport_terminal(
        self, receipt: CommandTerminalReceipt
    ) -> ActionLifecycleReceipt:
        transaction = receipt.transaction
        current = float(receipt.terminal_ts or time.time())
        terminal_projection = _command_terminal_projection(receipt)
        key = receipt.action_key
        self._ensure_cellular_claim(
            RadioMutationLease(
                transaction=transaction,
                owner="command_authority_terminal_recovery",
                acquired_ts=float(transaction.created_ts or current),
                expires_ts=float(transaction.expires_at_ts or current),
            ),
            now=current,
        )
        if receipt.terminal_status in {
            CommandTerminalStatus.ACCEPTED,
            CommandTerminalStatus.CONFIRMED,
            CommandTerminalStatus.SETTLED,
        }:
            self._advance(
                key,
                ActionLifecycleStage.EXECUTING,
                now=current,
                evidence={"command_terminal_receipt": terminal_projection},
            )
            self._advance(
                key,
                ActionLifecycleStage.CONTROLLER_RECEIPT,
                now=current,
                evidence={"command_terminal_receipt": terminal_projection},
            )
            result = self._advance(
                key,
                ActionLifecycleStage.MODEM_ACCEPTED,
                now=float(receipt.accepted_ts or current),
                evidence={"command_terminal_receipt": terminal_projection},
            )
            if receipt.terminal_status in {
                CommandTerminalStatus.CONFIRMED,
                CommandTerminalStatus.SETTLED,
            }:
                result = self._advance(
                    key,
                    ActionLifecycleStage.TARGET_CONFIRMED,
                    now=float(receipt.confirmed_ts or current),
                    evidence={"command_terminal_receipt": terminal_projection},
                )
            if receipt.terminal_status is CommandTerminalStatus.SETTLED:
                result = self._advance(
                    key,
                    ActionLifecycleStage.STREAM_SETTLED,
                    now=float(receipt.settled_ts or current),
                    evidence={"command_terminal_receipt": terminal_projection},
                )
            if (
                transaction.cellular_completion is not None
                and transaction.cellular_completion.proof == "execution_only"
            ):
                result = self._advance(
                    key,
                    ActionLifecycleStage.COMPLETE,
                    now=current,
                    evidence={"command_terminal_receipt": terminal_projection},
                )
            return result
        return self._transition_command(
            command_id=key.command_id,
            root_id=key.root_id,
            action_key=key,
            origin_kind=key.origin_kind,
            action_kind=key.lever,
            stage=_failure_stage(receipt.terminal_status),
            now=current,
            evidence={
                "command_terminal_receipt": terminal_projection
            },
        )

    def _commit(
        self,
        *,
        event: str,
        receipt: ActionLifecycleReceipt,
    ) -> None:
        self._sequence += 1
        payload = {
            "schema": JOURNAL_SCHEMA,
            "owner": OWNER,
            "sequence": self._sequence,
            "event": str(event or "transition"),
            "receipt": receipt.as_dict(),
        }
        payload["checksum"] = _checksum(payload)
        if self._journal is not None:
            self._journal.append(payload)

    def _restore(self) -> None:
        assert self._journal is not None
        newest_sequence = 0
        invalid = 0
        previous_sequence: int | None = None
        newest_by_command: dict[str, ActionLifecycleReceipt] = {}
        for row in self._journal.iter_events_reverse():
            try:
                if row.get("schema") != JOURNAL_SCHEMA or row.get("owner") != OWNER:
                    raise ValueError("owner_schema")
                if str(row.get("checksum") or "") != _checksum(row):
                    raise ValueError("checksum")
                sequence = int(row.get("sequence") or 0)
                if sequence <= 0:
                    raise ValueError("sequence")
                if previous_sequence is not None and sequence >= previous_sequence:
                    raise ValueError("sequence")
                receipt = _receipt_from_mapping(
                    dict(row.get("receipt") or {})
                )
                if not newest_sequence:
                    newest_sequence = sequence
                previous_sequence = sequence
                newest_by_command.setdefault(receipt.command_id, receipt)
            except Exception:
                invalid += 1
        if not newest_sequence:
            if invalid:
                self._restore_error = f"no_valid_action_lifecycle_event:{invalid}"
            return
        receipts = list(newest_by_command.values())
        self._active = {
            item.command_id: item
            for item in receipts
            if item.stage not in _TERMINAL_STAGES
        }
        recent = sorted(
            (
                item
                for item in receipts
                if item.stage in _TERMINAL_STAGES
            ),
            key=lambda item: item.changed_ts,
        )[-self._recent.maxlen :]
        self._recent = deque(recent, maxlen=self._recent.maxlen)
        self._sequence = newest_sequence
        if invalid:
            self._restore_error = f"ignored_invalid_action_lifecycle_events:{invalid}"


def _receipt_from_mapping(raw: Mapping[str, Any]) -> ActionLifecycleReceipt:
    row = dict(raw or {})
    if row.get("schema") != SCHEMA or row.get("owner") != OWNER:
        raise ValueError("action_lifecycle_receipt_contract_invalid")
    key_raw = row.get("action_transaction")
    key = (
        ActionLifecycleKey.from_mapping(dict(key_raw))
        if isinstance(key_raw, Mapping) and key_raw
        else None
    )
    receipt = ActionLifecycleReceipt(
        command_id=str(row.get("command_id") or ""),
        root_id=str(row.get("root_id") or ""),
        origin_kind=require_action_origin_name(row.get("origin_kind")),
        action_kind=str(row.get("action_kind") or ""),
        stage=ActionLifecycleStage(str(row.get("stage") or "")),
        changed_ts=float(row.get("changed_ts") or 0.0),
        action_key=key,
        evidence=deepcopy(dict(row.get("evidence") or {})),
        grades=deepcopy(dict(row.get("grades") or {})),
        timeline={
            str(name): float(value)
            for name, value in dict(row.get("timeline") or {}).items()
        },
    )
    retained_payload_bytes(
        receipt.as_dict(),
        max_bytes=MAX_LIFECYCLE_RECEIPT_BYTES,
        label="restored action lifecycle receipt",
    )
    return receipt


def _failure_stage(status: CommandTerminalStatus) -> ActionLifecycleStage:
    try:
        return {
            CommandTerminalStatus.FAILED: ActionLifecycleStage.FAILED,
            CommandTerminalStatus.REJECTED: ActionLifecycleStage.REJECTED,
            CommandTerminalStatus.TIMED_OUT: ActionLifecycleStage.TIMED_OUT,
            CommandTerminalStatus.CANCELLED: ActionLifecycleStage.CANCELLED,
        }[status]
    except KeyError as exc:
        raise ValueError(f"action_lifecycle_status_not_failure:{status.value}") from exc


def _command_transaction_projection(
    transaction: CommandTransaction,
) -> dict[str, Any]:
    return {
        "root_id": transaction.root_id,
        "family": transaction.family.value,
        "command": transaction.command,
        "command_id": transaction.command_id,
        "source": transaction.source,
        "origin_kind": transaction.origin_kind,
        "correlation_id": transaction.correlation_id,
        "correlation_source": transaction.correlation_source,
        "session_scope_key": transaction.session_scope_key,
        "identity_key": transaction.identity_key,
        "source_generation": int(transaction.source_generation),
    }


def _lifecycle_key(
    key: ActionLifecycleKey | ActionTransactionKey | None,
) -> ActionLifecycleKey | None:
    if key is None:
        return None
    if isinstance(key, ActionLifecycleKey):
        return key
    if isinstance(key, ActionTransactionKey):
        return ActionLifecycleKey.from_action_key(key)
    raise TypeError("action_lifecycle_key_required")


def _selection_receipt_projection(
    receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    row = dict(receipt or {})
    if "context_key" in row:
        raise ValueError("action_lifecycle_ambiguous_context_forbidden")
    forbidden_owner_copies = tuple(
        name
        for name in (
            "brain_decision_receipt",
            "bandit_selection_receipt",
            "selected_action",
            "proof_template",
            "command_terminal_receipt",
            "action_transaction",
        )
        if row.get(name)
    )
    if forbidden_owner_copies:
        raise ValueError(
            "action_lifecycle_selection_full_owner_receipt_forbidden:"
            + ",".join(forbidden_owner_copies)
        )
    projection = {
        "owner": str(row.get("owner") or ""),
        "schema": str(row.get("schema") or ""),
        "command_id": str(row.get("command_id") or ""),
        "proof_id": str(row.get("proof_id") or ""),
        "opportunity_set_id": str(row.get("opportunity_set_id") or ""),
        "opportunity_id": str(
            row.get("opportunity_id")
            or row.get("selected_opportunity_id")
            or ""
        ),
        "decision_id": str(row.get("decision_id") or ""),
        "decision_receipt_id": str(row.get("decision_receipt_id") or ""),
        "bandit_selection_receipt_id": str(
            row.get("bandit_selection_receipt_id") or ""
        ),
        "learning_context_key": str(
            row.get("learning_context_key") or ""
        ),
        "action_context_key": str(row.get("action_context_key") or ""),
        "identity_key": str(
            row.get("identity_key") or row.get("identity_code") or ""
        ),
        "session_scope_key": str(row.get("session_scope_key") or ""),
        "source_epoch": str(row.get("source_epoch") or ""),
        "source_sequence": int(row.get("source_sequence") or 0),
        "receipt_sha256": _mapping_digest(row),
    }
    return {
        key: value
        for key, value in projection.items()
        if value not in ("", 0)
    }


def _command_handoff_projection(
    receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    row = dict(receipt or {})
    projection = {
        "owner": str(row.get("owner") or ""),
        "schema": str(row.get("schema") or ""),
        "command_id": str(row.get("command_id") or ""),
        "proof_id": str(row.get("proof_id") or ""),
        "reason": _bounded_reason(row.get("reason"))[0],
        "receipt_sha256": _mapping_digest(row),
    }
    return {key: value for key, value in projection.items() if value != ""}


def _selection_rejection_projection(
    receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    row = dict(receipt or {})
    projection = {
        "owner": str(row.get("owner") or ""),
        "schema": str(row.get("schema") or ""),
        "stage": str(row.get("stage") or ""),
        "guard": str(row.get("guard") or ""),
        "boundary": str(row.get("boundary") or ""),
        "decision_trace_id": str(row.get("decision_trace_id") or ""),
        "receipt_sha256": _mapping_digest(row),
    }
    return {key: value for key, value in projection.items() if value != ""}


def _admission_receipt_projection(
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    row = dict(receipt or {})
    descriptor = dict(row.get("descriptor") or {})
    origin = dict(row.get("origin") or {})
    action_key = dict(row.get("action_transaction") or {})
    reason, reason_sha256, truncated = _bounded_reason(row.get("reason"))
    return {
        "owner": str(row.get("owner") or ""),
        "schema": str(row.get("schema") or ""),
        "accepted": bool(row.get("accepted", False)),
        "reason": reason,
        "reason_sha256": reason_sha256,
        "reason_truncated": truncated,
        "identity_key": str(row.get("identity_key") or ""),
        "lever": str(descriptor.get("lever") or ""),
        "family": str(descriptor.get("family") or ""),
        "verification": str(descriptor.get("verification") or ""),
        "proof": str(descriptor.get("proof") or ""),
        "origin_kind": str(origin.get("kind") or ""),
        "origin_source": str(origin.get("source") or ""),
        "command_id": str(action_key.get("command_id") or ""),
        "root_id": str(action_key.get("root_id") or ""),
        "receipt_sha256": _mapping_digest(row),
    }


def _command_lifecycle_projection(
    lifecycle: CommandLifecycle,
) -> dict[str, Any]:
    reason, reason_sha256, truncated = _bounded_reason(lifecycle.reason)
    return {
        "owner": COMMAND_AUTHORITY_OWNER,
        "stage": lifecycle.stage.value,
        "deadline_ts": float(lifecycle.deadline_ts),
        "accepted_ts": float(lifecycle.accepted_ts),
        "confirmed_ts": float(lifecycle.confirmed_ts),
        "target_generation": int(lifecycle.target_generation),
        "settle_generation": int(lifecycle.settle_generation),
        "reason": reason,
        "reason_sha256": reason_sha256,
        "reason_truncated": truncated,
        "lease_owner": lifecycle.lease.owner,
        "lease_acquired_ts": float(lifecycle.lease.acquired_ts),
        "lease_expires_ts": float(lifecycle.lease.expires_ts),
        "command_transaction": _command_transaction_projection(
            lifecycle.lease.transaction
        ),
        "completion": lifecycle.completion.as_dict(),
    }


def _command_terminal_projection(
    receipt: CommandTerminalReceipt,
) -> dict[str, Any]:
    reason, reason_sha256, truncated = _bounded_reason(receipt.reason)
    return {
        "owner": COMMAND_AUTHORITY_OWNER,
        "receipt_sequence": int(receipt.receipt_sequence),
        "transaction_root_id": receipt.root_id,
        "family": receipt.transaction.family.value,
        "command": receipt.transaction.command,
        "command_id": receipt.transaction.command_id,
        "session_scope_key": receipt.transaction.session_scope_key,
        "identity_key": receipt.transaction.identity_key,
        "source_generation": int(receipt.transaction.source_generation),
        "terminal_status": receipt.terminal_status.value,
        "reason": reason,
        "reason_sha256": reason_sha256,
        "reason_truncated": truncated,
        "terminal_ts": float(receipt.terminal_ts),
        "accepted_ts": float(receipt.accepted_ts),
        "confirmed_ts": float(receipt.confirmed_ts),
        "settled_ts": float(receipt.settled_ts),
        "target_generation": int(receipt.target_generation),
        "settle_generation": int(receipt.settle_generation),
        "proof_ready": bool(receipt.proof_ready),
        "command_transaction": _command_transaction_projection(
            receipt.transaction
        ),
    }


def grade_reason_receipt_projection(
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Project Referee evidence without copying its owned evidence graph."""

    row = dict(receipt or {})
    feedback = dict(row.get("layered_action_feedback") or {})
    reason, reason_sha256, truncated = _bounded_reason(row.get("reason"))
    return {
        "owner": str(row.get("owner") or ""),
        "horizon_s": int(row.get("horizon_s") or 0),
        "label": str(row.get("label") or ""),
        "reason": reason,
        "reason_sha256": reason_sha256,
        "reason_truncated": truncated,
        "referee_code": str(row.get("referee_code") or ""),
        "memory_grade_score": int(
            feedback.get("memory_grade_score") or 0
        ),
        "learning_label": str(feedback.get("learning_label") or ""),
        "context_clean": bool(feedback.get("context_clean", False)),
        "receipt_sha256": _mapping_digest(row),
    }


def _bounded_reason(value: Any, *, max_chars: int = 512) -> tuple[str, str, bool]:
    text = str(value or "")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
    if len(text) <= max_chars:
        return text, digest, False
    return text[:max_chars], digest, True


def _mapping_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value or {}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checksum(payload: Mapping[str, Any]) -> str:
    clean = {key: value for key, value in dict(payload).items() if key != "checksum"}
    encoded = json.dumps(
        clean,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_action_transaction_authority(owner: object) -> ActionTransactionAuthority:
    authority = getattr(owner, "action_transaction_authority", None)
    if not isinstance(authority, ActionTransactionAuthority):
        raise RuntimeError("action_transaction_authority_unavailable")
    return authority


__all__ = (
    "ActionLifecycleKey",
    "ActionLifecycleReceipt",
    "ActionLifecycleStage",
    "ActionTransactionAuthority",
    "GRADE_WINDOWS_S",
    "LIFECYCLE_KEY_SCHEMA",
    "MAX_LIFECYCLE_RECEIPT_BYTES",
    "OWNER",
    "SCHEMA",
    "grade_reason_receipt_projection",
    "require_action_transaction_authority",
)
