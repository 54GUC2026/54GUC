"""Durable handoff for a customer-selected action. It owns selection, context claim,
CommandAuthority handoff, and the final command receipt. It does not own modem writes,
target confirmation, settlement, or result grading.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Any, Mapping

from arch4.action_session_scope import CurrentSessionActionScope
from arch4.action_transaction import ActionTransactionKey
from arch4.customer_contracts.optimization_action import (
    validate_optimization_proof_template,
)
from arch4.customer_contracts.selected_action import SelectedActionContract
from arch4.command_transactions import CommandTerminalReceipt
from arch4.command_transactions import (
    COMMAND_AUTHORITY_OWNER,
    CommandTerminalStatus,
)
from arch4.memory_store.event_journal import JsonlEventJournal


SCHEMA = "SelectedActionStore.v4"
OWNER = "arch4.action_transaction_authority"
JOURNAL_SCHEMA = "SelectedActionStoreEvent.v5"
ARCHIVE_SCHEMA = "SelectedActionArchiveReceipt.v1"
COMMAND_OUTCOME_SCHEMA = "SelectedActionCommandOutcome.v1"
DOMAIN_RESULT_SCHEMA = "SelectedActionDomainResultReceipt.v1"
TERMINAL_ROUTE_SCHEMA = "SelectedActionTerminalRouteReceipt.v1"
TERMINAL_ROUTER_OWNER = "arch4.core.selected_action_terminal_router"
DEFAULT_CLAIM_LEASE_S = 30.0
JOURNAL_COMPACTION_INTERVAL = 32
MAX_SELECTED_ACTION_JOURNAL_ROW_BYTES = 512 * 1024
MAX_SELECTED_ACTION_JOURNAL_FILE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SelectedActionClaimContext:
    """Current facts required before a saved selection can be claimed."""

    action_scope: CurrentSessionActionScope
    context_key: str
    schema: str = "SelectedActionClaimContext.v1"
    owner: str = OWNER

    def __post_init__(self) -> None:
        if not isinstance(self.action_scope, CurrentSessionActionScope):
            raise TypeError("selected_action_claim_scope_required")
        if not str(self.context_key or "").strip():
            raise ValueError("selected_action_claim_context_key_required")

    @property
    def claim_context_id(self) -> str:
        return _checksum(self._content())

    def _content(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "action_scope": self.action_scope.as_dict(),
            "context_key": str(self.context_key),
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._content(), "claim_context_id": self.claim_context_id}

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "SelectedActionClaimContext":
        row = dict(raw or {})
        if (
            row.get("schema") != "SelectedActionClaimContext.v1"
            or row.get("owner") != OWNER
        ):
            raise ValueError("selected_action_claim_context_contract_invalid")
        value = cls(
            action_scope=CurrentSessionActionScope.from_mapping(
                dict(row.get("action_scope") or {})
            ),
            context_key=str(row.get("context_key") or ""),
        )
        if str(row.get("claim_context_id") or "") != value.claim_context_id:
            raise ValueError("selected_action_claim_context_checksum_invalid")
        return value


class SelectedActionStage(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    HANDED_OFF = "handed_off"
    TERMINAL = "terminal"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class SelectedActionCommandOutcome:
    """Bounded Command Authority projection consumed by SmartLoop."""

    receipt_sequence: int
    transaction_root_id: str
    command_id: str
    terminal_status: CommandTerminalStatus
    reason: str
    reason_sha256: str
    reason_truncated: bool
    terminal_ts: float
    accepted_ts: float = 0.0
    confirmed_ts: float = 0.0
    settled_ts: float = 0.0
    target_generation: int = 0
    settle_generation: int = 0
    proof_ready: bool = False
    retry_after_s: float = 0.0
    command_receipt_checksum: str = ""

    def __post_init__(self) -> None:
        if int(self.receipt_sequence or 0) <= 0:
            raise ValueError("selected_action_command_outcome_sequence_missing")
        if not self.transaction_root_id or not self.command_id:
            raise ValueError("selected_action_command_outcome_identity_missing")
        if not isinstance(self.terminal_status, CommandTerminalStatus):
            raise TypeError("selected_action_command_outcome_status_required")
        if float(self.terminal_ts or 0.0) <= 0.0:
            raise ValueError("selected_action_command_outcome_time_missing")
        if len(self.reason) > 512:
            raise ValueError("selected_action_command_outcome_reason_oversized")
        if len(self.reason_sha256) != 64:
            raise ValueError("selected_action_command_outcome_reason_hash_invalid")
        if self.reason_truncated:
            if len(self.reason) != 512:
                raise ValueError(
                    "selected_action_command_outcome_truncated_reason_invalid"
                )
        elif self.reason_sha256 != hashlib.sha256(
            self.reason.encode("utf-8")
        ).hexdigest():
            raise ValueError(
                "selected_action_command_outcome_reason_hash_mismatch"
            )
        if len(self.command_receipt_checksum) != 64:
            raise ValueError(
                "selected_action_command_outcome_source_checksum_invalid"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": COMMAND_OUTCOME_SCHEMA,
            "owner": OWNER,
            "source_owner": COMMAND_AUTHORITY_OWNER,
            "receipt_sequence": int(self.receipt_sequence),
            "transaction_root_id": self.transaction_root_id,
            "command_id": self.command_id,
            "terminal_status": self.terminal_status.value,
            "reason": self.reason,
            "reason_sha256": self.reason_sha256,
            "reason_truncated": bool(self.reason_truncated),
            "terminal_ts": float(self.terminal_ts),
            "accepted_ts": float(self.accepted_ts),
            "confirmed_ts": float(self.confirmed_ts),
            "settled_ts": float(self.settled_ts),
            "target_generation": int(self.target_generation),
            "settle_generation": int(self.settle_generation),
            "proof_ready": bool(self.proof_ready),
            "retry_after_s": float(self.retry_after_s),
            "command_receipt_checksum": self.command_receipt_checksum,
        }

    @classmethod
    def from_terminal_receipt(
        cls,
        receipt: CommandTerminalReceipt,
    ) -> "SelectedActionCommandOutcome":
        if not isinstance(receipt, CommandTerminalReceipt):
            raise TypeError("command_terminal_receipt_required")
        full = receipt.as_dict()
        reason = str(receipt.reason or "")
        receipts = dict(full.get("receipts") or {})
        retry_values: list[Any] = []
        for name in ("retry_after_s", "remaining_s", "cooldown_s"):
            retry_values.append(receipts.get(name))
        for nested in receipts.values():
            if isinstance(nested, Mapping):
                for name in ("retry_after_s", "remaining_s", "cooldown_s"):
                    retry_values.append(nested.get(name))
        retry_after_s = max(
            (max(0.0, _float(value)) for value in retry_values),
            default=0.0,
        )
        return cls(
            receipt_sequence=receipt.receipt_sequence,
            transaction_root_id=receipt.root_id,
            command_id=receipt.transaction.command_id,
            terminal_status=receipt.terminal_status,
            reason=reason[:512],
            reason_sha256=hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            reason_truncated=len(reason) > 512,
            terminal_ts=receipt.terminal_ts,
            accepted_ts=receipt.accepted_ts,
            confirmed_ts=receipt.confirmed_ts,
            settled_ts=receipt.settled_ts,
            target_generation=receipt.target_generation,
            settle_generation=receipt.settle_generation,
            proof_ready=receipt.proof_ready,
            retry_after_s=retry_after_s,
            command_receipt_checksum=str(full.get("checksum") or ""),
        )

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "SelectedActionCommandOutcome":
        row = dict(raw or {})
        if (
            row.get("schema") != COMMAND_OUTCOME_SCHEMA
            or row.get("owner") != OWNER
            or row.get("source_owner") != COMMAND_AUTHORITY_OWNER
        ):
            raise ValueError("selected_action_command_outcome_contract_invalid")
        return cls(
            receipt_sequence=_int(row.get("receipt_sequence")),
            transaction_root_id=str(row.get("transaction_root_id") or ""),
            command_id=str(row.get("command_id") or ""),
            terminal_status=CommandTerminalStatus(
                str(row.get("terminal_status") or "")
            ),
            reason=str(row.get("reason") or ""),
            reason_sha256=str(row.get("reason_sha256") or ""),
            reason_truncated=bool(row.get("reason_truncated", False)),
            terminal_ts=_float(row.get("terminal_ts")),
            accepted_ts=_float(row.get("accepted_ts")),
            confirmed_ts=_float(row.get("confirmed_ts")),
            settled_ts=_float(row.get("settled_ts")),
            target_generation=_int(row.get("target_generation")),
            settle_generation=_int(row.get("settle_generation")),
            proof_ready=bool(row.get("proof_ready", False)),
            retry_after_s=max(0.0, _float(row.get("retry_after_s"))),
            command_receipt_checksum=str(
                row.get("command_receipt_checksum") or ""
            ),
        )


@dataclass(frozen=True, slots=True)
class SelectedActionDomainResultReceipt:
    """Durable proof that domain-result handling finished."""

    command_id: str
    transaction_root_id: str
    owner_class: str
    result_owner: str
    result_status: str
    persisted_ts: float
    result_key: str
    schema: str = DOMAIN_RESULT_SCHEMA
    owner: str = OWNER

    def __post_init__(self) -> None:
        if not str(self.command_id or "").strip():
            raise ValueError("selected_action_domain_result_command_missing")
        if not str(self.transaction_root_id or "").strip():
            raise ValueError("selected_action_domain_result_root_missing")
        if str(self.owner_class or "") == "optimization":
            raise ValueError("optimization_domain_result_route_forbidden")
        if not str(self.owner_class or "").strip():
            raise ValueError("selected_action_domain_result_owner_class_missing")
        if not str(self.result_owner or "").strip():
            raise ValueError("selected_action_domain_result_owner_missing")
        if str(self.result_status or "") not in {"succeeded", "failed"}:
            raise ValueError("selected_action_domain_result_status_invalid")
        if float(self.persisted_ts or 0.0) <= 0.0:
            raise ValueError("selected_action_domain_result_time_missing")
        if not str(self.result_key or "").strip():
            raise ValueError("selected_action_domain_result_key_missing")

    @property
    def receipt_id(self) -> str:
        return _checksum(self._content())

    def _content(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "command_id": self.command_id,
            "transaction_root_id": self.transaction_root_id,
            "owner_class": self.owner_class,
            "result_owner": self.result_owner,
            "result_status": self.result_status,
            "persisted_ts": round(float(self.persisted_ts), 6),
            "result_key": self.result_key,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._content(), "receipt_id": self.receipt_id}

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "SelectedActionDomainResultReceipt":
        row = dict(raw or {})
        if row.get("schema") != DOMAIN_RESULT_SCHEMA or row.get("owner") != OWNER:
            raise ValueError("selected_action_domain_result_contract_invalid")
        value = cls(
            command_id=str(row.get("command_id") or ""),
            transaction_root_id=str(row.get("transaction_root_id") or ""),
            owner_class=str(row.get("owner_class") or ""),
            result_owner=str(row.get("result_owner") or ""),
            result_status=str(row.get("result_status") or ""),
            persisted_ts=_float(row.get("persisted_ts")),
            result_key=str(row.get("result_key") or ""),
        )
        if str(row.get("receipt_id") or "") != value.receipt_id:
            raise ValueError("selected_action_domain_result_checksum_invalid")
        return value


@dataclass(frozen=True, slots=True)
class SelectedActionTerminalRouteReceipt:
    """Permission issued only by the stateless final-result router."""

    command_id: str
    terminal_stage: SelectedActionStage
    routed_ts: float
    domain_result_receipt_id: str = ""
    schema: str = TERMINAL_ROUTE_SCHEMA
    owner: str = TERMINAL_ROUTER_OWNER

    def __post_init__(self) -> None:
        if not str(self.command_id or "").strip():
            raise ValueError("selected_action_terminal_route_command_missing")
        if self.terminal_stage not in {
            SelectedActionStage.TERMINAL,
            SelectedActionStage.REJECTED,
        }:
            raise ValueError("selected_action_terminal_route_stage_invalid")
        if float(self.routed_ts or 0.0) <= 0.0:
            raise ValueError("selected_action_terminal_route_time_missing")
        if (
            self.terminal_stage is SelectedActionStage.TERMINAL
            and not str(self.domain_result_receipt_id or "").strip()
        ):
            raise ValueError("selected_action_terminal_route_domain_result_missing")
        if (
            self.terminal_stage is SelectedActionStage.REJECTED
            and self.domain_result_receipt_id
        ):
            raise ValueError("selected_action_rejection_domain_result_forbidden")

    def as_dict(self) -> dict[str, Any]:
        content = {
            "schema": self.schema,
            "owner": self.owner,
            "command_id": self.command_id,
            "terminal_stage": self.terminal_stage.value,
            "routed_ts": round(float(self.routed_ts), 6),
            "domain_result_receipt_id": self.domain_result_receipt_id,
        }
        return {**content, "receipt_id": _checksum(content)}


@dataclass(frozen=True)
class SelectedActionTransaction:
    selected_action: Mapping[str, Any]
    proof_template: Mapping[str, Any]
    stage: SelectedActionStage
    published_ts: float
    claimed_ts: float = 0.0
    claim_lease_expires_ts: float = 0.0
    handed_off_ts: float = 0.0
    terminal_ts: float = 0.0
    claimed_by: str = ""
    claim_attempts: int = 0
    terminal_reason: str = ""
    action_key: ActionTransactionKey | None = None
    handoff_receipt: Mapping[str, Any] | None = None
    command_outcome: Mapping[str, Any] | None = None
    domain_result_receipt: Mapping[str, Any] | None = None

    @property
    def command_id(self) -> str:
        return str(self.selected_action.get("command_id") or "")

    @property
    def proof_id(self) -> str:
        return str(self.selected_action.get("proof_id") or "")

    @property
    def command_root_id(self) -> str:
        return self.action_key.root_id if self.action_key is not None else ""

    @property
    def terminal(self) -> bool:
        return self.stage in {
            SelectedActionStage.TERMINAL,
            SelectedActionStage.REJECTED,
        }

    def terminal_outcome(self) -> SelectedActionCommandOutcome | None:
        raw = self.command_outcome
        if not isinstance(raw, Mapping) or not raw:
            return None
        return SelectedActionCommandOutcome.from_mapping(raw)

    def as_dict(self, *, include_selected_action: bool = True) -> dict[str, Any]:
        selected = SelectedActionContract.from_mapping(self.selected_action)
        row: dict[str, Any] = {
            "command_id": self.command_id,
            "proof_id": self.proof_id,
            "selected_action_id": str(
                self.selected_action.get("selected_action_id") or ""
            ),
            "owner_class": selected.owner_class,
            "lever": selected.lever,
            "optimization": selected.optimization,
            "stage": self.stage.value,
            "published_ts": round(float(self.published_ts or 0.0), 6),
            "claimed_ts": round(float(self.claimed_ts or 0.0), 6),
            "claim_lease_expires_ts": round(
                float(self.claim_lease_expires_ts or 0.0), 6
            ),
            "handed_off_ts": round(float(self.handed_off_ts or 0.0), 6),
            "terminal_ts": round(float(self.terminal_ts or 0.0), 6),
            "claimed_by": str(self.claimed_by or ""),
            "claim_attempts": int(self.claim_attempts or 0),
            "terminal_reason": str(self.terminal_reason or ""),
            "command_root_id": self.command_root_id,
            "action_transaction": (
                self.action_key.as_dict() if self.action_key else {}
            ),
        }
        if include_selected_action:
            row["selected_action"] = dict(self.selected_action)
            row["proof_template"] = dict(self.proof_template)
            row["handoff_receipt"] = dict(self.handoff_receipt or {})
            row["command_outcome"] = dict(self.command_outcome or {})
            row["domain_result_receipt"] = dict(
                self.domain_result_receipt or {}
            )
        return row

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any]
    ) -> "SelectedActionTransaction":
        row = dict(raw or {})
        if "command_terminal_receipt" in row:
            raise ValueError("selected_action_full_terminal_receipt_deleted")
        command_outcome = dict(row.get("command_outcome") or {})
        if command_outcome:
            SelectedActionCommandOutcome.from_mapping(command_outcome)
        domain_result_receipt = dict(row.get("domain_result_receipt") or {})
        if domain_result_receipt:
            SelectedActionDomainResultReceipt.from_mapping(
                domain_result_receipt
            )
        return cls(
            selected_action=dict(row.get("selected_action") or {}),
            proof_template=dict(row.get("proof_template") or {}),
            stage=SelectedActionStage(str(row.get("stage") or "")),
            published_ts=_float(row.get("published_ts")),
            claimed_ts=_float(row.get("claimed_ts")),
            claim_lease_expires_ts=_float(
                row.get("claim_lease_expires_ts")
            ),
            handed_off_ts=_float(row.get("handed_off_ts")),
            terminal_ts=_float(row.get("terminal_ts")),
            claimed_by=str(row.get("claimed_by") or ""),
            claim_attempts=_int(row.get("claim_attempts")),
            terminal_reason=str(row.get("terminal_reason") or ""),
            action_key=(
                ActionTransactionKey.from_mapping(
                    dict(row.get("action_transaction") or {})
                )
                if isinstance(row.get("action_transaction"), Mapping)
                and row.get("action_transaction")
                else None
            ),
            handoff_receipt=dict(row.get("handoff_receipt") or {}),
            command_outcome=command_outcome,
            domain_result_receipt=domain_result_receipt,
        )


@dataclass(frozen=True, slots=True)
class SelectedActionArchiveReceipt:
    """Small final record used to reject repeated or colliding commands. Large decision
    graphs exist only while the selection is active.
    """

    command_id: str
    proof_id: str
    selected_action_id: str
    selected_action_sha256: str
    proof_template_sha256: str
    owner_class: str
    lever: str
    terminal_stage: SelectedActionStage
    terminal_reason: str
    terminal_ts: float
    command_root_id: str
    opportunity_id: str
    opportunity_set_id: str
    decision_id: str
    decision_receipt_id: str
    bandit_selection_receipt_id: str
    session_scope_key: str
    identity_code: str
    source_epoch: str
    source_sequence: int
    action_context_key: str
    schema: str = ARCHIVE_SCHEMA
    owner: str = OWNER

    def __post_init__(self) -> None:
        if not self.command_id or not self.proof_id:
            raise ValueError("selected_action_archive_identity_missing")
        for name in (
            "selected_action_id",
            "selected_action_sha256",
            "proof_template_sha256",
        ):
            if len(str(getattr(self, name) or "")) != 64:
                raise ValueError(f"selected_action_archive_{name}_invalid")
        if not self.owner_class or not self.lever:
            raise ValueError("selected_action_archive_action_shape_missing")
        if self.terminal_stage not in {
            SelectedActionStage.TERMINAL,
            SelectedActionStage.REJECTED,
        }:
            raise ValueError("selected_action_archive_stage_invalid")
        if float(self.terminal_ts or 0.0) <= 0.0:
            raise ValueError("selected_action_archive_time_missing")
        if len(self.terminal_reason) > 512:
            raise ValueError("selected_action_archive_reason_oversized")
        if int(self.source_sequence or 0) <= 0:
            raise ValueError("selected_action_archive_source_sequence_missing")

    def _content(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "command_id": self.command_id,
            "proof_id": self.proof_id,
            "selected_action_id": self.selected_action_id,
            "selected_action_sha256": self.selected_action_sha256,
            "proof_template_sha256": self.proof_template_sha256,
            "owner_class": self.owner_class,
            "lever": self.lever,
            "terminal_stage": self.terminal_stage.value,
            "terminal_reason": self.terminal_reason,
            "terminal_ts": round(float(self.terminal_ts), 6),
            "command_root_id": self.command_root_id,
            "opportunity_id": self.opportunity_id,
            "opportunity_set_id": self.opportunity_set_id,
            "decision_id": self.decision_id,
            "decision_receipt_id": self.decision_receipt_id,
            "bandit_selection_receipt_id": self.bandit_selection_receipt_id,
            "session_scope_key": self.session_scope_key,
            "identity_code": self.identity_code,
            "source_epoch": self.source_epoch,
            "source_sequence": int(self.source_sequence),
            "action_context_key": self.action_context_key,
        }

    @property
    def receipt_id(self) -> str:
        return _checksum(self._content())

    def as_dict(self) -> dict[str, Any]:
        return {**self._content(), "receipt_id": self.receipt_id}

    def matches(
        self,
        *,
        selected_action: Mapping[str, Any],
        proof_template: Mapping[str, Any],
    ) -> bool:
        return (
            self.selected_action_sha256
            == _mapping_sha256(selected_action)
            and self.proof_template_sha256
            == _mapping_sha256(proof_template)
        )

    @classmethod
    def from_transaction(
        cls,
        transaction: SelectedActionTransaction,
    ) -> "SelectedActionArchiveReceipt":
        if not isinstance(transaction, SelectedActionTransaction):
            raise TypeError("selected_action_archive_transaction_required")
        if not transaction.terminal:
            raise ValueError("selected_action_archive_terminal_required")
        selected = SelectedActionContract.from_mapping(
            transaction.selected_action
        )
        brain_receipt = selected.brain_receipt_dict()
        bandit_receipt = selected.bandit_receipt_dict()
        return cls(
            command_id=selected.command_id,
            proof_id=selected.proof_id,
            selected_action_id=str(
                transaction.selected_action.get("selected_action_id") or ""
            ),
            selected_action_sha256=_mapping_sha256(
                transaction.selected_action
            ),
            proof_template_sha256=_mapping_sha256(
                transaction.proof_template
            ),
            owner_class=selected.owner_class,
            lever=selected.lever,
            terminal_stage=transaction.stage,
            terminal_reason=str(transaction.terminal_reason or "")[:512],
            terminal_ts=float(transaction.terminal_ts),
            command_root_id=transaction.command_root_id,
            opportunity_id=selected.opportunity_id,
            opportunity_set_id=selected.opportunity_set_id,
            decision_id=selected.decision_id,
            decision_receipt_id=selected.decision_receipt_id,
            bandit_selection_receipt_id=str(
                bandit_receipt.get("selection_receipt_id") or ""
            ),
            session_scope_key=str(
                brain_receipt.get("session_scope_key") or ""
            ),
            identity_code=str(brain_receipt.get("identity_code") or ""),
            source_epoch=str(brain_receipt.get("source_epoch") or ""),
            source_sequence=_int(brain_receipt.get("source_sequence")),
            action_context_key=str(
                brain_receipt.get("action_context_key") or ""
            ),
        )

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "SelectedActionArchiveReceipt":
        row = dict(raw or {})
        expected_fields = {
            "schema",
            "owner",
            "command_id",
            "proof_id",
            "selected_action_id",
            "selected_action_sha256",
            "proof_template_sha256",
            "owner_class",
            "lever",
            "terminal_stage",
            "terminal_reason",
            "terminal_ts",
            "command_root_id",
            "opportunity_id",
            "opportunity_set_id",
            "decision_id",
            "decision_receipt_id",
            "bandit_selection_receipt_id",
            "session_scope_key",
            "identity_code",
            "source_epoch",
            "source_sequence",
            "action_context_key",
            "receipt_id",
        }
        if set(row) != expected_fields:
            raise ValueError("selected_action_archive_fields_invalid")
        if row.get("schema") != ARCHIVE_SCHEMA or row.get("owner") != OWNER:
            raise ValueError("selected_action_archive_contract_invalid")
        value = cls(
            command_id=str(row.get("command_id") or ""),
            proof_id=str(row.get("proof_id") or ""),
            selected_action_id=str(row.get("selected_action_id") or ""),
            selected_action_sha256=str(
                row.get("selected_action_sha256") or ""
            ),
            proof_template_sha256=str(
                row.get("proof_template_sha256") or ""
            ),
            owner_class=str(row.get("owner_class") or ""),
            lever=str(row.get("lever") or ""),
            terminal_stage=SelectedActionStage(
                str(row.get("terminal_stage") or "")
            ),
            terminal_reason=str(row.get("terminal_reason") or ""),
            terminal_ts=_float(row.get("terminal_ts")),
            command_root_id=str(row.get("command_root_id") or ""),
            opportunity_id=str(row.get("opportunity_id") or ""),
            opportunity_set_id=str(row.get("opportunity_set_id") or ""),
            decision_id=str(row.get("decision_id") or ""),
            decision_receipt_id=str(
                row.get("decision_receipt_id") or ""
            ),
            bandit_selection_receipt_id=str(
                row.get("bandit_selection_receipt_id") or ""
            ),
            session_scope_key=str(row.get("session_scope_key") or ""),
            identity_code=str(row.get("identity_code") or ""),
            source_epoch=str(row.get("source_epoch") or ""),
            source_sequence=_int(row.get("source_sequence")),
            action_context_key=str(row.get("action_context_key") or ""),
        )
        if str(row.get("receipt_id") or "") != value.receipt_id:
            raise ValueError("selected_action_archive_checksum_invalid")
        return value


@dataclass(frozen=True)
class SelectedActionPublication:
    accepted: bool
    transaction: SelectedActionTransaction | None
    reason: str = ""
    duplicate: bool = False
    archive_receipt: SelectedActionArchiveReceipt | None = None

    def __post_init__(self) -> None:
        if self.accepted and self.transaction is None:
            raise ValueError("accepted_selected_action_transaction_missing")
        if not self.accepted and self.archive_receipt is None:
            raise ValueError("rejected_selected_action_archive_missing")


class _SelectedActionStore:
    """Private durable selection store used only by ActionTransactionAuthority."""

    _COUNTER_NAMES = (
        "published_count",
        "claimed_count",
        "reclaimed_count",
        "handed_off_count",
        "terminal_count",
        "rejected_count",
        "context_rejected_count",
        "duplicate_count",
    )

    def __init__(
        self,
        *,
        max_recent: int = 64,
        persistence_dir: str | Path | None = None,
        claim_lease_s: float = DEFAULT_CLAIM_LEASE_S,
    ) -> None:
        self._lock = threading.RLock()
        self._max_recent = max(1, int(max_recent))
        self._claim_lease_s = max(
            1.0, float(claim_lease_s or DEFAULT_CLAIM_LEASE_S)
        )
        self._active: SelectedActionTransaction | None = None
        self._recent: deque[SelectedActionArchiveReceipt] = deque(
            maxlen=self._max_recent
        )
        self._sequence = 0
        self._counters = {name: 0 for name in self._COUNTER_NAMES}
        self._restore_error = ""
        self._compaction_error = ""
        self._compaction_count = 0
        self._persistence_write_blocked = False
        self._journal = (
            JsonlEventJournal(
                Path(persistence_dir), "selected_action_store.jsonl"
            )
            if persistence_dir is not None
            else None
        )
        if self._journal is not None:
            self._restore_from_journal()

    def publish(
        self,
        *,
        selected_action: Mapping[str, Any],
        proof_template: Mapping[str, Any],
        now: float | None = None,
    ) -> SelectedActionPublication:
        now_ts = float(time.time() if now is None else now)
        selected_action_row = dict(selected_action or {})
        proof_row = dict(proof_template or {})
        self._validate_selected_action(selected_action_row, proof_row)
        command_id = str(selected_action_row["command_id"])
        with self._lock:
            if self._active is not None:
                if self._active.command_id == command_id:
                    if (
                        _mapping_sha256(self._active.selected_action)
                        != _mapping_sha256(selected_action_row)
                        or _mapping_sha256(self._active.proof_template)
                        != _mapping_sha256(proof_row)
                    ):
                        raise RuntimeError(
                            "selected_action_active_content_collision"
                        )
                    self._commit_locked(
                        active=self._active,
                        event="duplicate_active",
                        counter_deltas={"duplicate_count": 1},
                    )
                    return SelectedActionPublication(
                        True,
                        self._active,
                        reason="already_latched",
                        duplicate=True,
                    )
                raise RuntimeError(
                    "selected_action_store_busy:"
                    + self._active.command_id
                )
            recent = self._recent_by_id(command_id)
            if recent is not None:
                if not recent.matches(
                    selected_action=selected_action_row,
                    proof_template=proof_row,
                ):
                    raise RuntimeError(
                        "selected_action_archive_content_collision"
                    )
                self._commit_locked(
                    active=None,
                    event="duplicate_terminal",
                    counter_deltas={"duplicate_count": 1},
                )
                return SelectedActionPublication(
                    False,
                    None,
                    reason="terminal_command_already_recorded",
                    duplicate=True,
                    archive_receipt=recent,
                )
            transaction = SelectedActionTransaction(
                selected_action=selected_action_row,
                proof_template=proof_row,
                stage=SelectedActionStage.PENDING,
                published_ts=now_ts,
            )
            self._commit_locked(
                active=transaction,
                event="published",
                counter_deltas={"published_count": 1},
            )
            return SelectedActionPublication(True, transaction)

    def claim(
        self,
        *,
        consumer: str,
        claim_context: SelectedActionClaimContext,
        now: float | None = None,
        lease_s: float | None = None,
    ) -> SelectedActionTransaction | None:
        consumer_name = str(consumer or "").strip()
        if not consumer_name:
            raise ValueError("selected_action_consumer_missing")
        if not isinstance(claim_context, SelectedActionClaimContext):
            raise TypeError("selected_action_claim_context_required")
        now_ts = float(time.time() if now is None else now)
        lease_duration = max(
            1.0, float(self._claim_lease_s if lease_s is None else lease_s)
        )
        with self._lock:
            transaction = self._active
            if transaction is None or transaction.terminal:
                return None
            context_error = self._claim_context_error(
                transaction,
                claim_context=claim_context,
                now=now_ts,
            )
            if context_error:
                self._reject_locked(
                    transaction,
                    reason=f"claim_context_rejected:{context_error}",
                    receipt={
                        "owner": OWNER,
                        "reason": context_error,
                        "claim_context": claim_context.as_dict(),
                    },
                    now=now_ts,
                    context_rejected=True,
                )
                return None
            if transaction.stage is SelectedActionStage.PENDING:
                updated = replace(
                    transaction,
                    stage=SelectedActionStage.CLAIMED,
                    claimed_ts=now_ts,
                    claim_lease_expires_ts=now_ts + lease_duration,
                    claimed_by=consumer_name,
                    claim_attempts=max(
                        1, int(transaction.claim_attempts or 0) + 1
                    ),
                )
                self._commit_locked(
                    active=updated,
                    event="claimed",
                    counter_deltas={"claimed_count": 1},
                )
                return updated
            if transaction.stage is SelectedActionStage.CLAIMED:
                if now_ts >= float(
                    transaction.claim_lease_expires_ts or 0.0
                ):
                    updated = replace(
                        transaction,
                        claimed_ts=now_ts,
                        claim_lease_expires_ts=now_ts + lease_duration,
                        claimed_by=consumer_name,
                        claim_attempts=max(
                            1, int(transaction.claim_attempts or 0) + 1
                        ),
                    )
                    self._commit_locked(
                        active=updated,
                        event="claim_recovered",
                        counter_deltas={
                            "claimed_count": 1,
                            "reclaimed_count": 1,
                        },
                    )
                    return updated
                if transaction.claimed_by != consumer_name:
                    raise RuntimeError(
                        "selected_action_claimed_by:"
                        + transaction.claimed_by
                    )
                return transaction
            return None

    def handoff_to_command_authority(
        self,
        *,
        command_id: str,
        action_key: ActionTransactionKey,
        receipt: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> SelectedActionTransaction:
        if not isinstance(action_key, ActionTransactionKey):
            raise TypeError("selected_action_action_key_required")
        now_ts = float(time.time() if now is None else now)
        with self._lock:
            transaction = self._require_active(command_id)
            if transaction.stage is SelectedActionStage.HANDED_OFF:
                if transaction.action_key != action_key:
                    raise RuntimeError(
                        "selected_action_command_handoff_rebind"
                    )
                return transaction
            if transaction.stage is not SelectedActionStage.CLAIMED:
                raise RuntimeError(
                    "selected_action_handoff_from_"
                    + transaction.stage.value
                )
            if action_key.command_id != transaction.command_id:
                raise RuntimeError(
                    "selected_action_handoff_command_mismatch"
                )
            if action_key.proof_id != transaction.proof_id:
                raise RuntimeError(
                    "selected_action_handoff_proof_mismatch"
                )
            updated = replace(
                transaction,
                stage=SelectedActionStage.HANDED_OFF,
                handed_off_ts=now_ts,
                claim_lease_expires_ts=0.0,
                action_key=action_key,
                handoff_receipt=dict(receipt or {}),
            )
            self._commit_locked(
                active=updated,
                event="command_authority_handoff",
                counter_deltas={"handed_off_count": 1},
            )
            return updated

    def accept_command_terminal(
        self,
        *,
        command_id: str,
        terminal_receipt: CommandTerminalReceipt | Mapping[str, Any],
    ) -> SelectedActionTransaction:
        receipt = (
            terminal_receipt
            if isinstance(terminal_receipt, CommandTerminalReceipt)
            else CommandTerminalReceipt.from_mapping(terminal_receipt)
        )
        with self._lock:
            transaction = self._require_active(command_id)
            if transaction.stage is SelectedActionStage.TERMINAL:
                retained = transaction.terminal_outcome()
                if retained != SelectedActionCommandOutcome.from_terminal_receipt(
                    receipt
                ):
                    raise RuntimeError(
                        "selected_action_terminal_receipt_replaced"
                    )
                return transaction
            if transaction.stage is not SelectedActionStage.HANDED_OFF:
                raise RuntimeError(
                    "selected_action_terminal_from_"
                    + transaction.stage.value
                )
            if transaction.action_key is None:
                raise RuntimeError(
                    "selected_action_terminal_without_action_key"
                )
            if (
                receipt.transaction.command_id
                != transaction.command_id
            ):
                raise RuntimeError(
                    "selected_action_terminal_command_mismatch"
                )
            if receipt.root_id != transaction.action_key.root_id:
                raise RuntimeError(
                    "selected_action_terminal_root_mismatch"
                )
            if receipt.action_key != transaction.action_key:
                raise RuntimeError(
                    "selected_action_terminal_action_key_mismatch"
                )
            updated = replace(
                transaction,
                stage=SelectedActionStage.TERMINAL,
                terminal_ts=float(receipt.terminal_ts),
                terminal_reason=str(
                    receipt.reason or receipt.terminal_status.value
                ),
                command_outcome=(
                    SelectedActionCommandOutcome.from_terminal_receipt(
                        receipt
                    ).as_dict()
                ),
            )
            self._commit_locked(
                active=updated,
                event="command_terminal_projected",
                counter_deltas={"terminal_count": 1},
            )
            return updated

    def reject_selection(
        self,
        *,
        command_id: str,
        reason: str,
        now: float | None = None,
        receipt: Mapping[str, Any] | None = None,
    ) -> SelectedActionTransaction:
        now_ts = float(time.time() if now is None else now)
        with self._lock:
            transaction = self._require_active(command_id)
            if transaction.stage is SelectedActionStage.REJECTED:
                return transaction
            if transaction.stage is SelectedActionStage.HANDED_OFF:
                raise RuntimeError(
                    "selected_action_rejection_after_command_handoff"
                )
            if transaction.stage is SelectedActionStage.TERMINAL:
                raise RuntimeError(
                    "selected_action_rejection_after_terminal"
                )
            return self._reject_locked(
                transaction,
                reason=str(reason or "selection_rejected"),
                receipt=dict(receipt or {}),
                now=now_ts,
            )

    def expire_overdue(
        self, *, now: float | None = None
    ) -> SelectedActionTransaction | None:
        """Expire only selections which have not reached CommandAuthority."""

        now_ts = float(time.time() if now is None else now)
        with self._lock:
            transaction = self._active
            if transaction is None or transaction.terminal:
                return transaction
            if transaction.stage is SelectedActionStage.HANDED_OFF:
                return transaction
            expires_at = _float(
                transaction.selected_action.get("expires_at_ts")
            )
            if expires_at <= 0.0:
                created = _float(
                    transaction.selected_action.get("created_ts")
                )
                lifetime = _float(
                    transaction.selected_action.get("expires_after_s")
                )
                if created > 0.0 and lifetime > 0.0:
                    expires_at = created + lifetime
            if expires_at <= 0.0 or now_ts < expires_at:
                return transaction
            return self._reject_locked(
                transaction,
                reason="selected_action_expired_before_command_handoff",
                receipt={
                    "owner": OWNER,
                    "stage": transaction.stage.value,
                    "timeout": True,
                },
                now=now_ts,
            )

    def read_terminal(self) -> SelectedActionTransaction | None:
        """Read terminal truth without clearing it."""

        with self._lock:
            transaction = self._active
            if transaction is None or not transaction.terminal:
                return None
            return transaction

    def read_active(self) -> SelectedActionTransaction | None:
        """Read the immutable selected transaction without changing it."""

        with self._lock:
            return self._active

    def accept_domain_result(
        self,
        receipt: SelectedActionDomainResultReceipt,
    ) -> SelectedActionTransaction:
        """Save a non-optimization result before final routing."""

        if not isinstance(receipt, SelectedActionDomainResultReceipt):
            raise TypeError("selected_action_domain_result_receipt_required")
        with self._lock:
            transaction = self._require_active(receipt.command_id)
            if transaction.stage not in {
                SelectedActionStage.HANDED_OFF,
                SelectedActionStage.TERMINAL,
            }:
                raise RuntimeError(
                    "selected_action_domain_result_from_"
                    + transaction.stage.value
                )
            selected = SelectedActionContract.from_mapping(
                transaction.selected_action
            )
            if selected.optimization:
                raise RuntimeError(
                    "optimization_domain_result_route_forbidden"
                )
            if receipt.owner_class != selected.owner_class:
                raise RuntimeError(
                    "selected_action_domain_result_owner_class_mismatch"
                )
            if transaction.action_key is None:
                raise RuntimeError(
                    "selected_action_domain_result_action_key_missing"
                )
            if receipt.transaction_root_id != transaction.action_key.root_id:
                raise RuntimeError(
                    "selected_action_domain_result_root_mismatch"
                )
            retained = dict(transaction.domain_result_receipt or {})
            if retained:
                current = SelectedActionDomainResultReceipt.from_mapping(
                    retained
                )
                if current.receipt_id != receipt.receipt_id:
                    raise RuntimeError(
                        "selected_action_domain_result_replaced"
                    )
                return transaction
            updated = replace(
                transaction,
                domain_result_receipt=receipt.as_dict(),
            )
            self._commit_locked(
                active=updated,
                event="domain_result_persisted",
            )
            return updated

    def acknowledge_terminal_consumed(
        self,
        *,
        command_id: str,
    ) -> SelectedActionTransaction:
        with self._lock:
            transaction = self._active
            if transaction is None or not transaction.terminal:
                raise RuntimeError(
                    "selected_action_terminal_receipt_missing"
                )
            if transaction.command_id != str(command_id or ""):
                raise RuntimeError(
                    "selected_action_terminal_receipt_mismatch:"
                    + transaction.command_id
                )
            recent = deque(self._recent, maxlen=self._max_recent)
            recent.append(
                SelectedActionArchiveReceipt.from_transaction(transaction)
            )
            self._commit_locked(
                active=None,
                recent=recent,
                event="terminal_acknowledged",
            )
            return transaction

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = self._active
            recent = list(self._recent)
            return {
                "schema": SCHEMA,
                "owner": OWNER,
                "sequence": int(self._sequence),
                "active": (
                    active.as_dict(include_selected_action=False)
                    if active
                    else None
                ),
                "recent": [
                    row.as_dict()
                    for row in recent[-8:]
                ],
                **{
                    name: int(self._counters[name])
                    for name in self._COUNTER_NAMES
                },
                "durable": self._journal is not None,
                "restore_error": str(self._restore_error or ""),
                "compaction_error": str(self._compaction_error or ""),
                "compaction_count": int(self._compaction_count),
                "persistence_write_blocked": bool(
                    self._persistence_write_blocked
                ),
                "journal": (
                    self._journal.writer_status()
                    if self._journal is not None
                    else {}
                ),
                "claim_lease_s": float(self._claim_lease_s),
            }

    @staticmethod
    def _claim_context_error(
        transaction: SelectedActionTransaction,
        *,
        claim_context: SelectedActionClaimContext,
        now: float,
    ) -> str:
        try:
            selected = SelectedActionContract.from_mapping(
                transaction.selected_action
            )
        except (TypeError, ValueError):
            return "selected_action_invalid"
        receipt = selected.brain_receipt_dict()
        scope = claim_context.action_scope
        selected_session = str(receipt.get("session_scope_key") or "")
        selected_epoch = str(receipt.get("source_epoch") or "")
        selected_identity = str(receipt.get("identity_code") or "")
        selected_sequence = _int(receipt.get("source_sequence"))
        selected_context = str(receipt.get("action_context_key") or "")
        if not selected_session or selected_session != scope.session_scope_key:
            return "session_scope_changed"
        if not selected_epoch or selected_epoch != scope.source_epoch:
            return "source_epoch_changed"
        if selected_sequence <= 0:
            return "selected_source_sequence_missing"
        if int(scope.source_sequence) < selected_sequence:
            return "current_source_sequence_behind_selection"
        if selected_identity != str(scope.identity_code or ""):
            return "identity_code_changed"
        if not selected_context or selected_context != claim_context.context_key:
            return "brain_context_changed"
        expires_at = float(selected.expires_at_ts or 0.0)
        if expires_at <= 0.0 or float(now) >= expires_at:
            return "selected_action_expired_before_claim"
        return ""

    def _reject_locked(
        self,
        transaction: SelectedActionTransaction,
        *,
        reason: str,
        receipt: Mapping[str, Any],
        now: float,
        context_rejected: bool = False,
    ) -> SelectedActionTransaction:
        updated = replace(
            transaction,
            stage=SelectedActionStage.REJECTED,
            terminal_ts=float(now),
            terminal_reason=str(reason or "selection_rejected"),
            handoff_receipt={
                **dict(transaction.handoff_receipt or {}),
                **dict(receipt or {}),
                "rejection": dict(receipt or {}),
            },
        )
        counters = {"rejected_count": 1}
        if context_rejected:
            counters["context_rejected_count"] = 1
        self._commit_locked(
            active=updated,
            event="selection_rejected",
            counter_deltas=counters,
        )
        return updated

    def _commit_locked(
        self,
        *,
        active: SelectedActionTransaction | None,
        event: str,
        recent: deque[SelectedActionArchiveReceipt] | None = None,
        counter_deltas: Mapping[str, int] | None = None,
    ) -> None:
        if self._persistence_write_blocked:
            raise RuntimeError(
                "selected_action_persistence_blocked:"
                + str(self._restore_error or "invalid_durable_state")
            )
        next_sequence = int(self._sequence) + 1
        next_recent = deque(
            self._recent if recent is None else recent,
            maxlen=self._max_recent,
        )
        next_counters = dict(self._counters)
        for name, delta in dict(counter_deltas or {}).items():
            if name not in next_counters:
                raise RuntimeError(
                    "selected_action_counter_unknown:" + name
                )
            next_counters[name] = (
                int(next_counters[name]) + int(delta or 0)
            )
        payload = self._journal_payload(
            event=event,
            sequence=next_sequence,
            active=active,
            recent=next_recent,
            counters=next_counters,
        )
        if self._journal is not None:
            self._journal.append(payload)
        self._active = active
        self._recent = next_recent
        self._counters = next_counters
        self._sequence = next_sequence
        if self._journal is None:
            return
        journal_size = _int(
            self._journal.writer_status().get("current_size_bytes")
        )
        if (
            next_sequence % JOURNAL_COMPACTION_INTERVAL == 0
            or journal_size > MAX_SELECTED_ACTION_JOURNAL_FILE_BYTES
        ):
            self._compact_journal_locked(payload)

    @classmethod
    def _journal_payload(
        cls,
        *,
        event: str,
        sequence: int,
        active: SelectedActionTransaction | None,
        recent: deque[SelectedActionArchiveReceipt],
        counters: Mapping[str, int],
    ) -> dict[str, Any]:
        payload = {
            "schema": JOURNAL_SCHEMA,
            "owner": OWNER,
            "event": str(event or "transition"),
            "sequence": int(sequence),
            "active": (
                active.as_dict(include_selected_action=True)
                if active is not None
                else None
            ),
            "recent": [item.as_dict() for item in recent],
            "counters": {
                name: max(0, _int(counters.get(name)))
                for name in cls._COUNTER_NAMES
            },
        }
        payload["checksum"] = _checksum(payload)
        row_bytes = _journal_row_bytes(payload)
        if row_bytes > MAX_SELECTED_ACTION_JOURNAL_ROW_BYTES:
            raise RuntimeError(
                "selected_action_journal_row_oversized:"
                + str(row_bytes)
                + ":"
                + str(MAX_SELECTED_ACTION_JOURNAL_ROW_BYTES)
            )
        return payload

    def _compact_journal_locked(self, payload: Mapping[str, Any]) -> None:
        if self._journal is None:
            return
        try:
            self._journal.rewrite([dict(payload)])
        except Exception as exc:
            # The append is already durable. Keep serving it and report the
            # compaction failure. A later transition retries while the file is
            # over its size limit.
            self._compaction_error = f"{type(exc).__name__}: {exc}"
            return
        self._compaction_error = ""
        self._compaction_count += 1

    def _restore_from_journal(self) -> None:
        assert self._journal is not None
        newest: Mapping[str, Any] | None = next(
            self._journal.iter_events_reverse(limit=1),
            None,
        )
        if newest is None:
            if self._journal.path.exists() and self._journal.path.stat().st_size:
                self._restore_error = "no_decodable_selected_action_event"
                self._persistence_write_blocked = True
            return

        if str(newest.get("schema") or "") == JOURNAL_SCHEMA:
            last = next(
                self._journal.iter_events_reverse(
                    limit=1,
                    validator=self._validate_v5_journal_row,
                ),
                None,
            )
            if last is None:
                self._restore_error = "no_valid_selected_action_v5_event"
                self._persistence_write_blocked = True
                return
            if str(newest.get("checksum") or "") != str(
                last.get("checksum") or ""
            ):
                self._restore_error = "ignored_invalid_newest_selected_action_v5_event"
            self._restore_v5_row(last)
            compacted = self._journal_payload(
                event="restore_compacted",
                sequence=self._sequence,
                active=self._active,
                recent=self._recent,
                counters=self._counters,
            )
            self._compact_journal_locked(compacted)
            return

        self._restore_error = (
            "illegal_selected_action_journal_schema:"
            + str(newest.get("schema") or "missing")
        )
        self._persistence_write_blocked = True

    def _restore_v5_row(self, raw: Mapping[str, Any]) -> None:
        self._validate_v5_journal_row(raw)
        row = dict(raw)
        active_raw = row.get("active")
        self._active = (
            SelectedActionTransaction.from_dict(active_raw)
            if isinstance(active_raw, Mapping)
            else None
        )
        self._recent = deque(
            (
                SelectedActionArchiveReceipt.from_mapping(item)
                for item in list(row.get("recent") or [])
            ),
            maxlen=self._max_recent,
        )
        counters = dict(row.get("counters") or {})
        self._counters = {
            name: max(0, _int(counters.get(name)))
            for name in self._COUNTER_NAMES
        }
        self._sequence = _int(row.get("sequence"))

    @classmethod
    def _validate_v5_journal_row(cls, raw: Mapping[str, Any]) -> None:
        row = dict(raw or {})
        if set(row) != {
            "schema",
            "owner",
            "event",
            "sequence",
            "active",
            "recent",
            "counters",
            "checksum",
        }:
            raise ValueError("selected_action_v5_fields_invalid")
        if row.get("schema") != JOURNAL_SCHEMA or row.get("owner") != OWNER:
            raise ValueError("selected_action_v5_contract_invalid")
        if str(row.get("checksum") or "") != _checksum(row):
            raise ValueError("selected_action_v5_checksum_invalid")
        if _int(row.get("sequence")) <= 0:
            raise ValueError("selected_action_v5_sequence_invalid")
        if _journal_row_bytes(row) > MAX_SELECTED_ACTION_JOURNAL_ROW_BYTES:
            raise ValueError("selected_action_v5_row_oversized")
        counters = row.get("counters")
        if not isinstance(counters, Mapping) or set(counters) != set(
            cls._COUNTER_NAMES
        ):
            raise ValueError("selected_action_v5_counters_invalid")
        if any(_int(value) < 0 for value in counters.values()):
            raise ValueError("selected_action_v5_counter_negative")
        active_raw = row.get("active")
        active_command = ""
        if active_raw is not None:
            if not isinstance(active_raw, Mapping):
                raise ValueError("selected_action_v5_active_invalid")
            active = SelectedActionTransaction.from_dict(active_raw)
            _validate_selected_action_pair(
                active.selected_action,
                active.proof_template,
            )
            active_command = active.command_id
        recent_raw = row.get("recent")
        if not isinstance(recent_raw, list):
            raise ValueError("selected_action_v5_recent_invalid")
        command_ids: set[str] = set()
        for item in recent_raw:
            if not isinstance(item, Mapping):
                raise ValueError("selected_action_v5_archive_invalid")
            receipt = SelectedActionArchiveReceipt.from_mapping(item)
            if receipt.command_id == active_command:
                raise ValueError("selected_action_v5_active_archived_collision")
            if receipt.command_id in command_ids:
                raise ValueError("selected_action_v5_archive_duplicate")
            command_ids.add(receipt.command_id)

    def _require_active(
        self, command_id: str
    ) -> SelectedActionTransaction:
        wanted = str(command_id or "").strip()
        transaction = self._active
        if transaction is None:
            raise RuntimeError("selected_action_store_empty")
        if transaction.command_id != wanted:
            raise RuntimeError(
                "selected_action_store_identity_mismatch:"
                + wanted
                + ":"
                + transaction.command_id
            )
        return transaction

    def _recent_by_id(
        self, command_id: str
    ) -> SelectedActionArchiveReceipt | None:
        for receipt in reversed(self._recent):
            if receipt.command_id == command_id:
                return receipt
        return None

    @staticmethod
    def _validate_selected_action(
        selected_action: Mapping[str, Any],
        proof_template: Mapping[str, Any],
    ) -> None:
        _validate_selected_action_pair(selected_action, proof_template)


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
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mapping_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload or {}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _journal_row_bytes(payload: Mapping[str, Any]) -> int:
    encoded = json.dumps(
        dict(payload or {}),
        ensure_ascii=True,
        allow_nan=False,
    ) + "\n"
    return len(encoded.encode("utf-8"))


def _validate_selected_action_pair(
    selected_action: Mapping[str, Any],
    proof_template: Mapping[str, Any],
) -> None:
    value = SelectedActionContract.from_mapping(selected_action)
    proof = dict(proof_template or {})
    if value.optimization and not proof:
        raise ValueError(
            "selected_action_optimization_proof_template_missing"
        )
    if not proof:
        return
    if value.optimization:
        validate_optimization_proof_template(
            proof,
            selected_action=value.as_dict(),
        )
    if str(proof.get("command_id") or "") != value.command_id:
        raise ValueError(
            "selected_action_proof_template_command_mismatch"
        )
    if str(proof.get("proof_id") or "") != value.proof_id:
        raise ValueError(
            "selected_action_proof_template_identity_mismatch"
        )


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def create_selected_action_store(
    *,
    max_recent: int = 64,
    persistence_dir: str | Path | None = None,
    claim_lease_s: float = DEFAULT_CLAIM_LEASE_S,
) -> _SelectedActionStore:
    return _SelectedActionStore(
        max_recent=max_recent,
        persistence_dir=persistence_dir,
        claim_lease_s=claim_lease_s,
    )


__all__ = (
    "ARCHIVE_SCHEMA",
    "COMMAND_OUTCOME_SCHEMA",
    "DEFAULT_CLAIM_LEASE_S",
    "JOURNAL_COMPACTION_INTERVAL",
    "JOURNAL_SCHEMA",
    "MAX_SELECTED_ACTION_JOURNAL_FILE_BYTES",
    "MAX_SELECTED_ACTION_JOURNAL_ROW_BYTES",
    "OWNER",
    "SCHEMA",
    "SelectedActionPublication",
    "SelectedActionClaimContext",
    "SelectedActionStage",
    "SelectedActionTransaction",
    "DOMAIN_RESULT_SCHEMA",
    "SelectedActionDomainResultReceipt",
    "SelectedActionArchiveReceipt",
    "SelectedActionCommandOutcome",
    "SelectedActionTerminalRouteReceipt",
    "TERMINAL_ROUTE_SCHEMA",
    "TERMINAL_ROUTER_OWNER",
    "create_selected_action_store",
)
