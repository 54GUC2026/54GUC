"""Typed command admission, physical leases, and transport receipts."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import threading
import time
from typing import Any, Mapping

from arch4.action_admission import (
    ActionVerification,
    action_descriptor,
    selected_action_context_from_action,
)
from arch4.action_transaction import (
    ActionTransactionKey,
    action_transaction_key_from_action,
)
from arch4.action_origin import (
    is_human_operator_origin,
    require_action_origin_name,
)
from arch4.core.lever_action_adapter import lever_action_authority
from arch4.interfaces import LeverAction, LeverType
from arch4.core_contracts.physical_commands import PhysicalCommandReceipt
from arch4.core_contracts.emergency_reachability import RecoveryTimingProfile
from arch4.core_contracts.commands import (
    CoreCommandExecutionReceipt,
    CoreCommandRequest,
    core_command_request_root,
)
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.atomic_io import atomic_write_json
from arch4.canonical_json import canonical_json_bytes
from arch4.radio_target_contract import TargetConfirmationReceipt


COMMAND_AUTHORITY_SCHEMA = "command_authority.lifecycle.v11"
COMMAND_AUTHORITY_OWNER = "arch4.command_transactions.CommandAuthority"
COMMAND_AUTHORITY_STATE_SCHEMA = "command_authority.state.v1"


def _command_transition_persists(event: str) -> bool:
    """Return whether one transition changes crash-recovery state."""

    name = str(event or "")
    if name == "command_authority_initialized":
        # The canonical current-state artifact must exist before the first
        # command so validators and callers can distinguish an idle owner from
        # a missing owner.  This is one startup write, not command telemetry.
        return True
    if name == "core_command_dispatched":
        # One durable process-restart fence is committed before the physical
        # command gate opens.  This is a rare command-state transition, not a
        # high-frequency telemetry write.
        return True
    if (
        name == "cellular_transaction_accepted"
        or name == "command_admission_frozen"
        or name == "command_admission_reopened_after_restart"
        or name == "command_runtime_identity_reconciled"
        or name == "core_command_terminal"
        or name.startswith("cellular_terminal_")
    ):
        return True
    return False
COMMAND_AUTHORITY_STATE_FILENAME = "command_authority.json"
COMMAND_TERMINAL_RECEIPT_SCHEMA = "command_terminal_receipt.v7"
COMMAND_PROJECTION_OBLIGATION_SCHEMA = "command_projection_obligation.v1"
COMMAND_PROJECTION_ACK_SCHEMA = "command_projection_ack.v1"
COMMAND_ROOT_SCHEMA = "CoreCommandRoot.v1"
DEFAULT_LEASE_DEADLINE_S = 300.0
DEFAULT_WRITE_RECEIPT_DEADLINE_S = 60.0
DEFAULT_TARGET_DEADLINE_S = 180.0
DEFAULT_SETTLE_DEADLINE_S = 45.0
MAX_DEDUPE_IDENTITIES = 512
MAX_TERMINAL_RECEIPTS = 256
_REQUIRED_COMMAND_METADATA_FIELDS = (
    "command_id",
    "created_ts",
    "expires_after_s",
    "dedupe_key",
    "replay_policy",
    "risk_level",
)


def _command_metadata_rejection(
    data: Mapping[str, Any],
    *,
    now: float,
) -> tuple[str, str]:
    missing = [
        field
        for field in _REQUIRED_COMMAND_METADATA_FIELDS
        if data.get(field) in (None, "", [], {})
    ]
    if missing:
        return "missing_command_metadata", ",".join(missing)
    if str(data.get("replay_policy") or "").strip().lower() != "reject_duplicate":
        return "invalid_command_metadata", "replay_policy"
    for field_name in ("created_ts", "expires_after_s"):
        try:
            value = float(data.get(field_name, 0.0) or 0.0)
        except (TypeError, ValueError):
            return "invalid_command_metadata", field_name
        if not math.isfinite(value) or value <= 0.0:
            return "invalid_command_metadata", field_name
    expires_at = data.get("expires_at_ts")
    if expires_at not in (None, ""):
        try:
            deadline = float(expires_at)
        except (TypeError, ValueError):
            return "invalid_command_metadata", "expires_at_ts"
        if not math.isfinite(deadline) or deadline <= 0.0:
            return "invalid_command_metadata", "expires_at_ts"
    else:
        deadline = float(data["created_ts"]) + float(data["expires_after_s"])
    if now > deadline:
        return "expired", ""
    return "", ""


def _exact_fields(
    row: Mapping[str, Any],
    expected: frozenset[str],
    reason: str,
) -> None:
    if set(row) != set(expected):
        raise ValueError(reason)


def _exact_bool(value: Any, reason: str) -> bool:
    if type(value) is not bool:
        raise TypeError(reason)
    return value


def _exact_string(value: Any, reason: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise TypeError(reason)
    text = value.strip()
    if required and not text:
        raise ValueError(reason)
    return text


def _exact_int(value: Any, reason: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise TypeError(reason)
    return value


def _exact_number(value: Any, reason: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(reason)
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < minimum:
        raise ValueError(reason)
    return parsed


def _exact_mapping(value: Any, reason: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(reason)
    return dict(value)


def _runtime_namespace_key(
    identity: MatrixGenerationIdentity,
) -> str:
    if not isinstance(identity, MatrixGenerationIdentity):
        raise TypeError("command_matrix_identity_required")
    return hashlib.sha256(
        canonical_json_bytes(identity.namespace.as_dict())
    ).hexdigest()


class TransactionFamily(str, Enum):
    CELLULAR_OPERATOR = "cellular_operator"
    CELLULAR_AUTONOMOUS = "cellular_autonomous"
    EXPORT = "export"


@dataclass(frozen=True)
class TransactionPolicy:
    interlock: str
    receipt: str
    readback: str
    completion: str
    event: str
    proof: str
    learning: str


TRANSACTION_POLICIES: Mapping[TransactionFamily, TransactionPolicy] = {
    TransactionFamily.CELLULAR_OPERATOR: TransactionPolicy(
        interlock="cellular_mutation_window",
        receipt="command_terminal_receipt",
        readback="modem_live_priority_register_refresh",
        completion="action_specific_causal_completion",
        event="operator_cellular_action",
        proof="referee_after_command_settle",
        learning="operator_proof_admission",
    ),
    TransactionFamily.CELLULAR_AUTONOMOUS: TransactionPolicy(
        interlock="tsar_and_cellular_mutation_window",
        receipt="command_terminal_receipt",
        readback="modem_live_register_generation",
        completion="action_specific_causal_completion",
        event="autonomous_cellular_action",
        proof="referee_after_command_settle",
        learning="graded_proof_only",
    ),
    TransactionFamily.EXPORT: TransactionPolicy(
        interlock="heavy_work_lane_budget",
        receipt="command_terminal_receipt",
        readback="immutable_result_register",
        completion="terminal_result_register",
        event="heavy_work_terminal",
        proof="typed_result_readback",
        learning="prohibited",
    ),
}


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return (
            "__cgf_mapping_v1__",
            tuple(
                sorted(
                ((str(key), _freeze(item)) for key, item in value.items()),
                key=lambda row: row[0],
                )
            ),
        )
    if isinstance(value, (set, frozenset)):
        return (
            "__cgf_set_v1__",
            tuple(sorted((_freeze(item) for item in value), key=repr)),
        )
    if isinstance(value, (list, tuple)):
        return (
            "__cgf_sequence_v1__",
            tuple(_freeze(item) for item in value),
        )
    return value


def _thaw(value: Any) -> Any:
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], tuple)
    ):
        marker, payload = value
        if marker == "__cgf_mapping_v1__":
            if any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                for item in payload
            ):
                raise ValueError("command_frozen_mapping_invalid")
            return {item[0]: _thaw(item[1]) for item in payload}
        if marker == "__cgf_sequence_v1__":
            return [_thaw(item) for item in payload]
        if marker == "__cgf_set_v1__":
            return {_thaw(item) for item in payload}
    if isinstance(value, tuple):
        raise ValueError("command_frozen_value_untagged")
    return value


@dataclass(frozen=True)
class CellularCompletionContract:
    """Action-specific command completion separated from delayed ROI proof."""

    verification: str
    completion: str
    proof: str
    learning: str
    target_required: bool
    continuation_required: bool
    terminal_on_modem_acceptance: bool
    reachability_measurement_enabled: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "verification": self.verification,
            "completion": self.completion,
            "proof": self.proof,
            "learning": self.learning,
            "target_required": bool(self.target_required),
            "continuation_required": bool(self.continuation_required),
            "terminal_on_modem_acceptance": bool(
                self.terminal_on_modem_acceptance
            ),
            "reachability_measurement_enabled": bool(
                self.reachability_measurement_enabled
            ),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CellularCompletionContract":
        row = dict(raw or {})
        _exact_fields(
            row,
            frozenset(
                {
                    "verification",
                    "completion",
                    "proof",
                    "learning",
                    "target_required",
                    "continuation_required",
                    "terminal_on_modem_acceptance",
                    "reachability_measurement_enabled",
                }
            ),
            "cellular_completion_fields_invalid",
        )
        return cls(
            verification=_exact_string(
                row.get("verification"),
                "cellular_completion_verification_invalid",
            ),
            completion=_exact_string(
                row.get("completion"),
                "cellular_completion_completion_invalid",
            ),
            proof=_exact_string(
                row.get("proof"),
                "cellular_completion_proof_invalid",
            ),
            learning=_exact_string(
                row.get("learning"),
                "cellular_completion_learning_invalid",
            ),
            target_required=_exact_bool(
                row.get("target_required"),
                "cellular_completion_target_required_bool",
            ),
            continuation_required=_exact_bool(
                row.get("continuation_required"),
                "cellular_completion_continuation_required_bool",
            ),
            terminal_on_modem_acceptance=_exact_bool(
                row.get("terminal_on_modem_acceptance"),
                "cellular_completion_terminal_on_acceptance_bool",
            ),
            reachability_measurement_enabled=_exact_bool(
                row.get("reachability_measurement_enabled"),
                "cellular_completion_reachability_measurement_enabled_bool",
            ),
        )


@dataclass(frozen=True)
class CommandTransaction:
    root_id: str
    public_command_root_id: str
    family: TransactionFamily
    command: str
    command_id: str
    dedupe_key: str
    source: str
    origin_kind: str
    correlation_id: str
    correlation_source: str
    risk_level: str
    created_ts: float
    expires_at_ts: float
    matrix_identity: MatrixGenerationIdentity
    identity_key: str
    params: tuple[tuple[str, Any], ...]
    authority: tuple[tuple[str, Any], ...]
    action_key: ActionTransactionKey | None = None
    cellular_completion: CellularCompletionContract | None = None

    def __post_init__(self) -> None:
        for name in (
            "root_id",
            "public_command_root_id",
            "command",
            "command_id",
            "identity_key",
            "correlation_id",
            "correlation_source",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"command_transaction_{name}_missing")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("command_transaction_matrix_identity_required")
        require_action_origin_name(self.origin_kind)
        if self.family not in {
            TransactionFamily.CELLULAR_OPERATOR,
            TransactionFamily.CELLULAR_AUTONOMOUS,
        }:
            raise ValueError("non_cellular_command_transaction_illegal")
        if self.action_key is None or self.cellular_completion is None:
            raise ValueError("cellular_command_contract_missing")
        if self.root_id != self.action_key.root_id:
            raise ValueError("cellular_command_root_mismatch")
        if self.matrix_identity != self.action_key.matrix_identity:
            raise ValueError("cellular_command_matrix_identity_mismatch")
        expected_family = (
            TransactionFamily.CELLULAR_OPERATOR
            if is_human_operator_origin(self.origin_kind)
            else TransactionFamily.CELLULAR_AUTONOMOUS
        )
        if self.family is not expected_family:
            raise ValueError("cellular_command_origin_family_mismatch")

    @property
    def policy(self) -> TransactionPolicy:
        return TRANSACTION_POLICIES[self.family]

    def params_dict(self) -> dict[str, Any]:
        return dict(_thaw(self.params))

    def authority_dict(self) -> dict[str, Any]:
        return dict(_thaw(self.authority))


@dataclass(frozen=True)
class CellularCommandAdmission:
    accepted: bool
    envelope: tuple[tuple[str, Any], ...]
    transaction: CommandTransaction | None = None
    lease: "RadioMutationLease | None" = None
    reason: str = ""

    def envelope_dict(self) -> dict[str, Any]:
        return dict(_thaw(self.envelope))


@dataclass(frozen=True)
class RadioMutationLease:
    """The one physical cellular-command lease shared by every typed origin."""

    transaction: CommandTransaction
    owner: str
    acquired_ts: float
    expires_ts: float

    @property
    def root_id(self) -> str:
        return self.transaction.root_id


class CommandLifecycleStage(str, Enum):
    CLAIMED = "claimed"
    DISPATCHED = "dispatched"
    ACCEPTED = "accepted"
    CONFIRMED = "confirmed"


class CommandTerminalStatus(str, Enum):
    ACCEPTED = "accepted"
    CONFIRMED = "confirmed"
    SETTLED = "settled"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class CommandRootState(str, Enum):
    RECEIVED = "received"
    VALIDATED = "validated"
    ADMITTED = "admitted"
    DISPATCHED = "dispatched"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class CoreCommandRoot:
    """The sole request-to-terminal command state retained by CommandAuthority."""

    request: CoreCommandRequest
    request_root_id: str
    state: CommandRootState
    received_at: float
    updated_at: float
    physical_action_root_id: str = ""
    terminal_reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.request, CoreCommandRequest):
            raise TypeError("command_root_typed_request_required")
        if self.request_root_id != core_command_request_root(self.request):
            raise ValueError("command_root_request_digest_mismatch")
        if not isinstance(self.state, CommandRootState):
            raise TypeError("command_root_typed_state_required")
        received = _exact_number(
            self.received_at,
            "command_root_received_at_invalid",
        )
        updated = _exact_number(
            self.updated_at,
            "command_root_updated_at_invalid",
        )
        if updated < received:
            raise ValueError("command_root_time_order_invalid")
        physical_root = _exact_string(
            self.physical_action_root_id,
            "command_root_physical_action_root_invalid",
            required=False,
        )
        terminal_reason = _exact_string(
            self.terminal_reason,
            "command_root_terminal_reason_invalid",
            required=False,
        )
        if self.state in {
            CommandRootState.RECEIVED,
            CommandRootState.VALIDATED,
            CommandRootState.ADMITTED,
        } and physical_root:
            raise ValueError("command_root_physical_root_before_dispatch")
        if self.state is CommandRootState.DISPATCHED and not physical_root:
            raise ValueError("command_root_dispatched_physical_root_required")
        if self.state is CommandRootState.TERMINAL and not terminal_reason:
            raise ValueError("command_root_terminal_reason_required")
        if self.state is not CommandRootState.TERMINAL and terminal_reason:
            raise ValueError("command_root_early_terminal_reason_forbidden")
        object.__setattr__(self, "received_at", received)
        object.__setattr__(self, "updated_at", updated)
        object.__setattr__(self, "physical_action_root_id", physical_root)
        object.__setattr__(self, "terminal_reason", terminal_reason)

    @property
    def matrix_identity(self) -> MatrixGenerationIdentity:
        return self.request.matrix_identity

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": COMMAND_ROOT_SCHEMA,
            "request": self.request.to_payload(),
            "request_root_id": self.request_root_id,
            "state": self.state.value,
            "received_at": self.received_at,
            "updated_at": self.updated_at,
            "physical_action_root_id": self.physical_action_root_id,
            "terminal_reason": self.terminal_reason,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CoreCommandRoot":
        row = dict(raw or {})
        _exact_fields(
            row,
            frozenset(
                {
                    "schema",
                    "request",
                    "request_root_id",
                    "state",
                    "received_at",
                    "updated_at",
                    "physical_action_root_id",
                    "terminal_reason",
                }
            ),
            "command_root_fields_invalid",
        )
        if row.get("schema") != COMMAND_ROOT_SCHEMA:
            raise ValueError("command_root_schema_invalid")
        return cls(
            request=CoreCommandRequest.from_payload(
                _exact_mapping(
                    row.get("request"),
                    "command_root_request_mapping_required",
                )
            ),
            request_root_id=_exact_string(
                row.get("request_root_id"),
                "command_root_request_digest_invalid",
            ),
            state=CommandRootState(
                _exact_string(
                    row.get("state"),
                    "command_root_state_invalid",
                )
            ),
            received_at=_exact_number(
                row.get("received_at"),
                "command_root_received_at_invalid",
            ),
            updated_at=_exact_number(
                row.get("updated_at"),
                "command_root_updated_at_invalid",
            ),
            physical_action_root_id=_exact_string(
                row.get("physical_action_root_id"),
                "command_root_physical_action_root_invalid",
                required=False,
            ),
            terminal_reason=_exact_string(
                row.get("terminal_reason"),
                "command_root_terminal_reason_invalid",
                required=False,
            ),
        )


@dataclass(frozen=True)
class CommandLifecycle:
    lease: RadioMutationLease
    stage: CommandLifecycleStage
    deadline_ts: float
    accepted_ts: float = 0.0
    confirmed_ts: float = 0.0
    target_generation: int = 0
    settle_generation: int = 0
    reason: str = ""
    receipts: tuple[Any, ...] = field(default_factory=lambda: _freeze({}))

    @property
    def key(self) -> ActionTransactionKey:
        key = self.lease.transaction.action_key
        if key is None:
            raise RuntimeError("cellular_lifecycle_action_key_missing")
        return key

    @property
    def completion(self) -> CellularCompletionContract:
        contract = self.lease.transaction.cellular_completion
        if contract is None:
            raise RuntimeError("cellular_lifecycle_completion_contract_missing")
        return contract

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": COMMAND_AUTHORITY_SCHEMA,
            "owner": COMMAND_AUTHORITY_OWNER,
            "stage": self.stage.value,
            "deadline_ts": float(self.deadline_ts),
            "accepted_ts": float(self.accepted_ts),
            "confirmed_ts": float(self.confirmed_ts),
            "target_generation": int(self.target_generation),
            "settle_generation": int(self.settle_generation),
            "reason": str(self.reason or ""),
            "receipts": dict(_thaw(self.receipts)),
            "lease": _lease_to_dict(self.lease),
            "action_transaction": self.key.as_dict(),
            "completion": self.completion.as_dict(),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CommandLifecycle":
        row = dict(raw or {})
        _exact_fields(
            row,
            frozenset(
                {
                    "schema",
                    "owner",
                    "stage",
                    "deadline_ts",
                    "accepted_ts",
                    "confirmed_ts",
                    "target_generation",
                    "settle_generation",
                    "reason",
                    "receipts",
                    "lease",
                    "action_transaction",
                    "completion",
                }
            ),
            "command_lifecycle_fields_invalid",
        )
        if (
            row.get("schema") != COMMAND_AUTHORITY_SCHEMA
            or row.get("owner") != COMMAND_AUTHORITY_OWNER
        ):
            raise ValueError("command_lifecycle_contract_invalid")
        if not isinstance(row.get("lease"), Mapping):
            raise TypeError("command_lifecycle_lease_mapping_required")
        if not isinstance(row.get("action_transaction"), Mapping):
            raise TypeError("command_lifecycle_action_key_mapping_required")
        if not isinstance(row.get("completion"), Mapping):
            raise TypeError("command_lifecycle_completion_mapping_required")
        value = cls(
            lease=_lease_from_dict(dict(row.get("lease"))),
            stage=CommandLifecycleStage(
                _exact_string(
                    row.get("stage"),
                    "command_lifecycle_stage_invalid",
                )
            ),
            deadline_ts=_exact_number(
                row.get("deadline_ts"),
                "command_lifecycle_deadline_invalid",
            ),
            accepted_ts=_exact_number(
                row.get("accepted_ts"),
                "command_lifecycle_accepted_ts_invalid",
            ),
            confirmed_ts=_exact_number(
                row.get("confirmed_ts"),
                "command_lifecycle_confirmed_ts_invalid",
            ),
            target_generation=_exact_int(
                row.get("target_generation"),
                "command_lifecycle_target_generation_invalid",
            ),
            settle_generation=_exact_int(
                row.get("settle_generation"),
                "command_lifecycle_settle_generation_invalid",
            ),
            reason=_exact_string(
                row.get("reason"),
                "command_lifecycle_reason_invalid",
                required=False,
            ),
            receipts=_freeze(
                _exact_mapping(
                    row.get("receipts"),
                    "command_lifecycle_receipts_mapping_required",
                )
            ),
        )
        if value.key.as_dict() != dict(row.get("action_transaction")):
            raise ValueError("command_lifecycle_action_key_changed")
        if value.completion.as_dict() != dict(row.get("completion")):
            raise ValueError("command_lifecycle_completion_changed")
        receipts = dict(_thaw(value.receipts))
        target_receipt = receipts.get("target")
        if target_receipt is not None:
            if not isinstance(target_receipt, Mapping):
                raise TypeError("command_target_receipt_mapping_required")
            target_confirmation = TargetConfirmationReceipt.from_mapping(
                target_receipt
            )
            if (
                target_confirmation.transaction_root_id
                != value.key.root_id
                or target_confirmation.request_id != value.key.command_id
                or target_confirmation.matrix_identity.namespace
                != value.key.matrix_identity.namespace
                or target_confirmation.target != value.key.target
                or target_confirmation.matrix_identity.matrix_sequence
                != value.target_generation
            ):
                raise ValueError("command_target_receipt_lifecycle_mismatch")
        if {"reachability", "reachability_unavailable"}.intersection(receipts):
            raise ValueError(
                "command_lifecycle_embedded_reachability_forbidden"
            )
        return value


@dataclass(frozen=True)
class CommandTerminalReceipt:
    receipt_sequence: int
    transaction: CommandTransaction
    terminal_status: CommandTerminalStatus
    reason: str
    terminal_ts: float
    accepted_ts: float = 0.0
    confirmed_ts: float = 0.0
    settled_ts: float = 0.0
    target_generation: int = 0
    settle_generation: int = 0
    proof_ready: bool = False
    receipts: tuple[Any, ...] = field(default_factory=lambda: _freeze({}))

    @property
    def root_id(self) -> str:
        return self.transaction.root_id

    @property
    def action_key(self) -> ActionTransactionKey:
        key = self.transaction.action_key
        if key is None:
            raise RuntimeError("cellular_terminal_action_key_missing")
        return key

    def validate_settlement_proof(self) -> None:
        """Validate the sole Command Authority definition of settled proof."""

        if self.terminal_status is not CommandTerminalStatus.SETTLED:
            raise ValueError("command_terminal_receipt_not_settled")
        if not self.proof_ready:
            raise ValueError("command_terminal_receipt_proof_not_ready")
        receipts = dict(_thaw(self.receipts))
        missing = frozenset({"modem", "target", "settle"}).difference(receipts)
        if missing:
            raise ValueError(
                "command_terminal_receipt_settlement_proof_incomplete:"
                + ",".join(sorted(missing))
            )
        completion = self.transaction.cellular_completion
        if completion is None:
            raise ValueError(
                "command_terminal_receipt_completion_contract_missing"
            )
        if {"reachability", "reachability_unavailable"}.intersection(receipts):
            raise ValueError(
                "command_terminal_receipt_embedded_reachability_forbidden"
            )

    def as_dict(self) -> dict[str, Any]:
        content = {
            "schema": COMMAND_TERMINAL_RECEIPT_SCHEMA,
            "owner": COMMAND_AUTHORITY_OWNER,
            "receipt_sequence": int(self.receipt_sequence),
            "transaction_root_id": self.root_id,
            "command_root_id": self.transaction.public_command_root_id,
            "family": self.transaction.family.value,
            "command": self.transaction.command,
            "command_id": self.transaction.command_id,
            "matrix_identity": self.transaction.matrix_identity.as_dict(),
            "identity_key": self.transaction.identity_key,
            "terminal_status": self.terminal_status.value,
            "reason": str(self.reason or ""),
            "terminal_ts": float(self.terminal_ts),
            "accepted_ts": float(self.accepted_ts),
            "confirmed_ts": float(self.confirmed_ts),
            "settled_ts": float(self.settled_ts),
            "target_generation": int(self.target_generation),
            "settle_generation": int(self.settle_generation),
            "proof_ready": bool(self.proof_ready),
            "accepted": self.terminal_status
            in {
                CommandTerminalStatus.ACCEPTED,
                CommandTerminalStatus.CONFIRMED,
                CommandTerminalStatus.SETTLED,
            },
            "confirmed": self.terminal_status
            in {CommandTerminalStatus.CONFIRMED, CommandTerminalStatus.SETTLED},
            "settled": self.terminal_status is CommandTerminalStatus.SETTLED,
            "receipts": dict(_thaw(self.receipts)),
            "transaction": _transaction_to_dict(self.transaction),
            "action_transaction": (
                self.action_key.as_dict() if self.action_key is not None else {}
            ),
        }
        return {**content, "checksum": _checksum(content)}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CommandTerminalReceipt":
        row = dict(raw or {})
        _exact_fields(
            row,
            frozenset(
                {
                    "schema",
                    "owner",
                    "receipt_sequence",
                    "transaction_root_id",
                    "command_root_id",
                    "family",
                    "command",
                    "command_id",
                    "matrix_identity",
                    "identity_key",
                    "terminal_status",
                    "reason",
                    "terminal_ts",
                    "accepted_ts",
                    "confirmed_ts",
                    "settled_ts",
                    "target_generation",
                    "settle_generation",
                    "proof_ready",
                    "accepted",
                    "confirmed",
                    "settled",
                    "receipts",
                    "transaction",
                    "action_transaction",
                    "checksum",
                }
            ),
            "command_terminal_receipt_fields_invalid",
        )
        if (
            row.get("schema") != COMMAND_TERMINAL_RECEIPT_SCHEMA
            or row.get("owner") != COMMAND_AUTHORITY_OWNER
        ):
            raise ValueError("command_terminal_receipt_contract_invalid")
        if str(row.get("checksum") or "") != _checksum(row):
            raise ValueError("command_terminal_receipt_checksum_invalid")
        value = cls(
            receipt_sequence=_exact_int(
                row.get("receipt_sequence"),
                "command_terminal_receipt_sequence_invalid",
                minimum=1,
            ),
            transaction=_transaction_from_dict(dict(row.get("transaction") or {})),
            terminal_status=CommandTerminalStatus(
                _exact_string(
                    row.get("terminal_status"),
                    "command_terminal_receipt_status_invalid",
                )
            ),
            reason=_exact_string(
                row.get("reason"),
                "command_terminal_receipt_reason_invalid",
            ),
            terminal_ts=_exact_number(
                row.get("terminal_ts"),
                "command_terminal_receipt_terminal_ts_invalid",
            ),
            accepted_ts=_exact_number(
                row.get("accepted_ts"),
                "command_terminal_receipt_accepted_ts_invalid",
            ),
            confirmed_ts=_exact_number(
                row.get("confirmed_ts"),
                "command_terminal_receipt_confirmed_ts_invalid",
            ),
            settled_ts=_exact_number(
                row.get("settled_ts"),
                "command_terminal_receipt_settled_ts_invalid",
            ),
            target_generation=_exact_int(
                row.get("target_generation"),
                "command_terminal_receipt_target_generation_invalid",
            ),
            settle_generation=_exact_int(
                row.get("settle_generation"),
                "command_terminal_receipt_settle_generation_invalid",
            ),
            proof_ready=_exact_bool(
                row.get("proof_ready"),
                "command_terminal_receipt_proof_ready_bool",
            ),
            receipts=_freeze(
                _exact_mapping(
                    row.get("receipts"),
                    "command_terminal_receipt_receipts_mapping_required",
                )
            ),
        )
        if value.root_id != _exact_string(
            row.get("transaction_root_id"),
            "command_terminal_receipt_root_invalid",
        ):
            raise ValueError("command_terminal_receipt_root_mismatch")
        if value.transaction.public_command_root_id != _exact_string(
            row.get("command_root_id"),
            "command_terminal_receipt_public_root_invalid",
        ):
            raise ValueError("command_terminal_receipt_public_root_mismatch")
        if value.transaction.family.value != _exact_string(
            row.get("family"),
            "command_terminal_receipt_family_invalid",
        ):
            raise ValueError("command_terminal_receipt_family_mismatch")
        if value.transaction.command != _exact_string(
            row.get("command"),
            "command_terminal_receipt_command_invalid",
        ):
            raise ValueError("command_terminal_receipt_command_mismatch")
        if value.transaction.command_id != _exact_string(
            row.get("command_id"),
            "command_terminal_receipt_command_id_invalid",
        ):
            raise ValueError("command_terminal_receipt_command_mismatch")
        matrix_identity = row.get("matrix_identity")
        if not isinstance(matrix_identity, Mapping):
            raise TypeError(
                "command_terminal_receipt_matrix_identity_mapping_required"
            )
        if value.transaction.matrix_identity != (
            MatrixGenerationIdentity.from_mapping(matrix_identity)
        ):
            raise ValueError("command_terminal_receipt_matrix_identity_mismatch")
        if value.transaction.identity_key != _exact_string(
            row.get("identity_key"),
            "command_terminal_receipt_identity_invalid",
        ):
            raise ValueError("command_terminal_receipt_identity_mismatch")
        action_row = row.get("action_transaction")
        if not isinstance(action_row, Mapping):
            raise TypeError("command_terminal_receipt_action_key_mapping_required")
        if value.action_key.as_dict() != dict(action_row):
            raise ValueError("command_terminal_receipt_action_key_mismatch")
        expected_flags = {
            "accepted": value.terminal_status
            in {
                CommandTerminalStatus.ACCEPTED,
                CommandTerminalStatus.CONFIRMED,
                CommandTerminalStatus.SETTLED,
            },
            "confirmed": value.terminal_status
            in {CommandTerminalStatus.CONFIRMED, CommandTerminalStatus.SETTLED},
            "settled": value.terminal_status is CommandTerminalStatus.SETTLED,
        }
        for name, expected in expected_flags.items():
            if _exact_bool(
                row.get(name),
                f"command_terminal_receipt_{name}_bool",
            ) is not expected:
                raise ValueError(
                    f"command_terminal_receipt_{name}_mismatch"
                )
        if value.terminal_status is CommandTerminalStatus.SETTLED:
            value.validate_settlement_proof()
        return value


@dataclass(frozen=True)
class CommandLifecycleProjectionObligation:
    """Exact Command Authority truth that must reach the Core action register."""

    obligation_id: str
    authority_sequence: int
    transaction_root_id: str
    projection_kind: str
    payload_fingerprint: str
    created_at: float
    payload: tuple[Any, ...]

    def typed_receipt(self) -> CommandLifecycle | CommandTerminalReceipt:
        raw = _thaw(self.payload)
        if not isinstance(raw, Mapping):
            raise TypeError("command_projection_payload_mapping_required")
        if self.projection_kind == "lifecycle":
            receipt: CommandLifecycle | CommandTerminalReceipt = (
                CommandLifecycle.from_mapping(dict(raw))
            )
            root_id = receipt.key.root_id
        elif self.projection_kind == "terminal":
            receipt = CommandTerminalReceipt.from_mapping(dict(raw))
            root_id = receipt.root_id
        else:
            raise ValueError("command_projection_kind_invalid")
        if root_id != self.transaction_root_id:
            raise ValueError("command_projection_root_mismatch")
        if _checksum(dict(raw)) != self.payload_fingerprint:
            raise ValueError("command_projection_payload_fingerprint_invalid")
        return receipt

    def as_dict(self) -> dict[str, Any]:
        content = {
            "schema": COMMAND_PROJECTION_OBLIGATION_SCHEMA,
            "owner": COMMAND_AUTHORITY_OWNER,
            "obligation_id": self.obligation_id,
            "authority_sequence": int(self.authority_sequence),
            "transaction_root_id": self.transaction_root_id,
            "projection_kind": self.projection_kind,
            "payload_fingerprint": self.payload_fingerprint,
            "created_at": float(self.created_at),
            "payload": dict(_thaw(self.payload)),
        }
        return {**content, "checksum": _checksum(content)}

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "CommandLifecycleProjectionObligation":
        row = dict(raw or {})
        _exact_fields(
            row,
            frozenset(
                {
                    "schema",
                    "owner",
                    "obligation_id",
                    "authority_sequence",
                    "transaction_root_id",
                    "projection_kind",
                    "payload_fingerprint",
                    "created_at",
                    "payload",
                    "checksum",
                }
            ),
            "command_projection_obligation_fields_invalid",
        )
        if (
            row.get("schema") != COMMAND_PROJECTION_OBLIGATION_SCHEMA
            or row.get("owner") != COMMAND_AUTHORITY_OWNER
            or str(row.get("checksum") or "") != _checksum(row)
        ):
            raise ValueError("command_projection_obligation_contract_invalid")
        value = cls(
            obligation_id=_exact_string(
                row.get("obligation_id"),
                "command_projection_obligation_id_invalid",
            ),
            authority_sequence=_exact_int(
                row.get("authority_sequence"),
                "command_projection_authority_sequence_invalid",
                minimum=1,
            ),
            transaction_root_id=_exact_string(
                row.get("transaction_root_id"),
                "command_projection_root_invalid",
            ),
            projection_kind=_exact_string(
                row.get("projection_kind"),
                "command_projection_kind_invalid",
            ),
            payload_fingerprint=_exact_string(
                row.get("payload_fingerprint"),
                "command_projection_fingerprint_invalid",
            ),
            created_at=_exact_number(
                row.get("created_at"),
                "command_projection_created_at_invalid",
            ),
            payload=_freeze(
                _exact_mapping(
                    row.get("payload"),
                    "command_projection_payload_invalid",
                )
            ),
        )
        value.typed_receipt()
        expected_id = hashlib.sha256(
            (
                f"{value.authority_sequence}|{value.transaction_root_id}|"
                f"{value.projection_kind}|{value.payload_fingerprint}"
            ).encode("utf-8")
        ).hexdigest()
        if value.obligation_id != expected_id:
            raise ValueError("command_projection_obligation_identity_invalid")
        return value


@dataclass(frozen=True)
class CommandLifecycleProjectionAcknowledgement:
    """Identity-bound proof that one obligation reached REGISTER_ACTION."""

    obligation_id: str
    authority_sequence: int
    transaction_root_id: str
    payload_fingerprint: str
    register_sequence: int
    register_signature: str
    acknowledged_at: float

    def as_dict(self) -> dict[str, Any]:
        content = {
            "schema": COMMAND_PROJECTION_ACK_SCHEMA,
            "owner": COMMAND_AUTHORITY_OWNER,
            "obligation_id": self.obligation_id,
            "authority_sequence": int(self.authority_sequence),
            "transaction_root_id": self.transaction_root_id,
            "payload_fingerprint": self.payload_fingerprint,
            "register_sequence": int(self.register_sequence),
            "register_signature": self.register_signature,
            "acknowledged_at": float(self.acknowledged_at),
        }
        return {**content, "checksum": _checksum(content)}

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "CommandLifecycleProjectionAcknowledgement":
        row = dict(raw or {})
        _exact_fields(
            row,
            frozenset(
                {
                    "schema",
                    "owner",
                    "obligation_id",
                    "authority_sequence",
                    "transaction_root_id",
                    "payload_fingerprint",
                    "register_sequence",
                    "register_signature",
                    "acknowledged_at",
                    "checksum",
                }
            ),
            "command_projection_ack_fields_invalid",
        )
        if (
            row.get("schema") != COMMAND_PROJECTION_ACK_SCHEMA
            or row.get("owner") != COMMAND_AUTHORITY_OWNER
            or str(row.get("checksum") or "") != _checksum(row)
        ):
            raise ValueError("command_projection_ack_contract_invalid")
        return cls(
            obligation_id=_exact_string(
                row.get("obligation_id"),
                "command_projection_ack_id_invalid",
            ),
            authority_sequence=_exact_int(
                row.get("authority_sequence"),
                "command_projection_ack_authority_sequence_invalid",
                minimum=1,
            ),
            transaction_root_id=_exact_string(
                row.get("transaction_root_id"),
                "command_projection_ack_root_invalid",
            ),
            payload_fingerprint=_exact_string(
                row.get("payload_fingerprint"),
                "command_projection_ack_fingerprint_invalid",
            ),
            register_sequence=_exact_int(
                row.get("register_sequence"),
                "command_projection_ack_register_sequence_invalid",
                minimum=1,
            ),
            register_signature=_exact_string(
                row.get("register_signature"),
                "command_projection_ack_register_signature_invalid",
                required=False,
            ),
            acknowledged_at=_exact_number(
                row.get("acknowledged_at"),
                "command_projection_ack_timestamp_invalid",
            ),
        )


class CommandAuthorityStateStore:
    """One bounded atomic current-state artifact; never an event journal."""

    def __init__(self, directory: str | Path) -> None:
        self.path = Path(directory) / COMMAND_AUTHORITY_STATE_FILENAME

    def write(self, payload: Mapping[str, Any]) -> None:
        atomic_write_json(
            self.path,
            dict(payload),
            indent=None,
            sort_keys=True,
            trailing_newline=True,
        )

    def read(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError("command_authority_state_mapping_required")
        return value


@dataclass(frozen=True)
class CommandAuthorityStateRecord:
    """One validated current state from the sole command authority."""

    event: str
    sequence: int
    active_cellular: CommandLifecycle | None
    command_roots: tuple[CoreCommandRoot, ...]
    dedupe: tuple[tuple[str, str], ...]
    terminal: tuple[CommandTerminalReceipt, ...]
    terminal_evicted_through_sequence: int
    admission_frozen: bool
    admission_frozen_reason: str
    projection_obligation: CommandLifecycleProjectionObligation | None
    last_projection_ack: CommandLifecycleProjectionAcknowledgement | None

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "CommandAuthorityStateRecord":
        row = dict(raw or {})
        _exact_fields(
            row,
            frozenset(
                {
                    "schema",
                    "owner",
                    "event",
                    "sequence",
                    "active_cellular",
                    "command_roots",
                    "dedupe",
                    "terminal",
                    "terminal_evicted_through_sequence",
                    "admission_frozen",
                    "admission_frozen_reason",
                    "projection_obligation",
                    "last_projection_ack",
                    "checksum",
                }
            ),
            "command_authority_state_fields_invalid",
        )
        if row.get("schema") != COMMAND_AUTHORITY_STATE_SCHEMA:
            raise ValueError("command_authority_state_schema_invalid")
        if row.get("owner") != COMMAND_AUTHORITY_OWNER:
            raise ValueError("command_authority_state_owner_invalid")
        if _exact_string(
            row.get("checksum"),
            "command_authority_checksum_invalid",
        ) != _checksum(row):
            raise ValueError("command_authority_state_checksum_invalid")
        event = _exact_string(
            row.get("event"),
            "command_authority_event_invalid",
        )
        sequence = _exact_int(
            row.get("sequence"),
            "command_authority_sequence_invalid",
            minimum=1,
        )
        active_raw = row.get("active_cellular")
        if active_raw is not None and not isinstance(active_raw, Mapping):
            raise TypeError("command_authority_active_mapping_required")
        active = (
            CommandLifecycle.from_mapping(dict(active_raw))
            if isinstance(active_raw, Mapping)
            else None
        )
        roots_raw = row.get("command_roots")
        if not isinstance(roots_raw, list):
            raise TypeError("command_authority_roots_list_required")
        command_roots = tuple(
            CoreCommandRoot.from_mapping(
                _exact_mapping(
                    item,
                    "command_authority_root_mapping_required",
                )
            )
            for item in roots_raw
        )
        if len({item.request.request_id for item in command_roots}) != len(
            command_roots
        ):
            raise ValueError("command_authority_duplicate_command_root")
        dedupe_raw = row.get("dedupe")
        if not isinstance(dedupe_raw, list):
            raise TypeError("command_authority_dedupe_list_required")
        dedupe: list[tuple[str, str]] = []
        for item in dedupe_raw:
            if not isinstance(item, list) or len(item) != 2:
                raise TypeError("command_authority_dedupe_row_invalid")
            dedupe.append(
                (
                    _exact_string(
                        item[0],
                        "command_authority_dedupe_session_invalid",
                    ),
                    _exact_string(
                        item[1],
                        "command_authority_dedupe_command_invalid",
                    ),
                )
            )
        terminal_raw = row.get("terminal")
        if not isinstance(terminal_raw, list):
            raise TypeError("command_authority_terminal_list_required")
        terminal_values: list[CommandTerminalReceipt] = []
        for item in terminal_raw:
            if not isinstance(item, Mapping):
                raise TypeError("command_authority_terminal_mapping_required")
            terminal_values.append(
                CommandTerminalReceipt.from_mapping(dict(item))
            )
        terminal = tuple(terminal_values)
        evicted_through = _exact_int(
            row.get("terminal_evicted_through_sequence"),
            "command_authority_terminal_evicted_sequence_invalid",
        )
        if terminal and evicted_through >= terminal[0].receipt_sequence:
            raise ValueError("command_authority_terminal_retention_overlap")
        obligation_raw = row.get("projection_obligation")
        if obligation_raw is not None and not isinstance(obligation_raw, Mapping):
            raise TypeError("command_projection_obligation_mapping_required")
        projection_obligation = (
            CommandLifecycleProjectionObligation.from_mapping(
                dict(obligation_raw)
            )
            if isinstance(obligation_raw, Mapping)
            else None
        )
        ack_raw = row.get("last_projection_ack")
        if ack_raw is not None and not isinstance(ack_raw, Mapping):
            raise TypeError("command_projection_ack_mapping_required")
        last_projection_ack = (
            CommandLifecycleProjectionAcknowledgement.from_mapping(dict(ack_raw))
            if isinstance(ack_raw, Mapping)
            else None
        )
        if (
            projection_obligation is not None
            and last_projection_ack is not None
            and last_projection_ack.authority_sequence
            >= projection_obligation.authority_sequence
        ):
            raise ValueError("command_projection_pending_ack_order_invalid")
        return cls(
            event=event,
            sequence=sequence,
            active_cellular=active,
            command_roots=command_roots,
            dedupe=tuple(dedupe),
            terminal=terminal,
            terminal_evicted_through_sequence=evicted_through,
            admission_frozen=_exact_bool(
                row.get("admission_frozen"),
                "command_authority_admission_frozen_bool_required",
            ),
            admission_frozen_reason=_exact_string(
                row.get("admission_frozen_reason"),
                "command_authority_admission_frozen_reason_invalid",
                required=False,
            ),
            projection_obligation=projection_obligation,
            last_projection_ack=last_projection_ack,
        )


@dataclass(frozen=True)
class CommandTransition:
    active: CommandLifecycle | None = None
    terminal: CommandTerminalReceipt | None = None


class CommandAuthority:
    """Durable owner of command admission and physical execution receipts.

    This owner cannot publish GUI presentation or delayed Intelligence grades.
    Core may expose only a read-only projection of these typed receipts.
    """

    def __init__(
        self,
        *,
        persistence_dir: str | Path | None = None,
        max_dedupe_identities: int = MAX_DEDUPE_IDENTITIES,
        max_terminal_receipts: int = MAX_TERMINAL_RECEIPTS,
        max_command_roots: int = MAX_DEDUPE_IDENTITIES,
    ) -> None:
        self._lock = threading.RLock()
        self._max_dedupe_identities = max(8, int(max_dedupe_identities))
        self._max_terminal_receipts = max(8, int(max_terminal_receipts))
        self._max_command_roots = max(8, int(max_command_roots))
        self._dedupe_identities: deque[tuple[str, str]] = deque(
            maxlen=self._max_dedupe_identities
        )
        self._active_cellular: CommandLifecycle | None = None
        self._command_roots: dict[str, CoreCommandRoot] = {}
        self._terminal_receipts: deque[CommandTerminalReceipt] = deque(
            maxlen=self._max_terminal_receipts
        )
        self._terminal_evicted_through_sequence = 0
        self._sequence = 0
        self._admission_frozen = False
        self._admission_frozen_reason = ""
        self._projection_obligation: (
            CommandLifecycleProjectionObligation | None
        ) = None
        self._last_projection_ack: (
            CommandLifecycleProjectionAcknowledgement | None
        ) = None
        self._restart_admission_reopen_reason = ""
        self._restore_error = ""
        self._state_store = (
            CommandAuthorityStateStore(Path(persistence_dir))
            if persistence_dir is not None
            else None
        )
        if self._state_store is not None:
            state_was_present = self._state_store.path.is_file()
            self._restore_from_state()
            self._reissue_projection_after_restart()
            self._reopen_admission_after_restart()
            if not state_was_present:
                self._commit_locked(event="command_authority_initialized")

    def receive_core_command(
        self,
        request: CoreCommandRequest,
        *,
        now: float,
    ) -> CoreCommandRoot:
        if not isinstance(request, CoreCommandRequest):
            raise TypeError("command_root_typed_request_required")
        current = _exact_number(now, "command_root_received_at_invalid")
        request_root_id = core_command_request_root(request)
        with self._lock:
            retained = self._command_roots.get(request.request_id)
            if retained is not None:
                if (
                    retained.request_root_id != request_root_id
                    or retained.request != request
                ):
                    raise RuntimeError(
                        "command_root_same_id_different_payload"
                    )
                return retained
            root = CoreCommandRoot(
                request=request,
                request_root_id=request_root_id,
                state=CommandRootState.RECEIVED,
                received_at=current,
                updated_at=current,
            )
            roots = dict(self._command_roots)
            if len(roots) >= self._max_command_roots:
                terminal_roots = sorted(
                    (
                        retained
                        for retained in roots.values()
                        if retained.state is CommandRootState.TERMINAL
                    ),
                    key=lambda retained: (
                        retained.updated_at,
                        retained.request.request_id,
                    ),
                )
                while (
                    len(roots) >= self._max_command_roots
                    and terminal_roots
                ):
                    evicted = terminal_roots.pop(0)
                    roots.pop(evicted.request.request_id, None)
            if len(roots) >= self._max_command_roots:
                raise RuntimeError(
                    "command_root_retention_capacity_exhausted"
                )
            roots[request.request_id] = root
            self._commit_locked(
                event="core_command_received",
                command_roots=roots,
            )
            return root

    def advance_core_command(
        self,
        *,
        request_id: str,
        expected: CommandRootState,
        target: CommandRootState,
        now: float,
        execution_receipt: CoreCommandExecutionReceipt | None = None,
    ) -> CoreCommandRoot:
        allowed = {
            (CommandRootState.RECEIVED, CommandRootState.VALIDATED),
            (CommandRootState.VALIDATED, CommandRootState.ADMITTED),
            (CommandRootState.ADMITTED, CommandRootState.DISPATCHED),
        }
        if (expected, target) not in allowed:
            raise ValueError("command_root_transition_invalid")
        current = _exact_number(now, "command_root_transition_time_invalid")
        with self._lock:
            root = self._require_core_command_root(request_id)
            if root.state is target:
                return root
            if root.state is not expected:
                raise RuntimeError(
                    f"command_root_{target.value}_from_{root.state.value}"
                )
            physical_root = root.physical_action_root_id
            if target is CommandRootState.DISPATCHED:
                if not isinstance(
                    execution_receipt,
                    CoreCommandExecutionReceipt,
                ):
                    raise TypeError(
                        "command_root_execution_receipt_required"
                    )
                if (
                    not execution_receipt.submitted
                    or execution_receipt.request_id
                    != root.request.request_id
                    or execution_receipt.command_root_id
                    != root.request_root_id
                    or execution_receipt.matrix_identity
                    != root.matrix_identity
                ):
                    raise RuntimeError(
                        "command_root_execution_receipt_mismatch"
                    )
                physical_root = execution_receipt.physical_action_root_id
            updated = replace(
                root,
                state=target,
                updated_at=current,
                physical_action_root_id=physical_root,
            )
            roots = dict(self._command_roots)
            roots[request_id] = updated
            self._commit_locked(
                event=f"core_command_{target.value}",
                command_roots=roots,
            )
            return updated

    def terminalize_core_command(
        self,
        *,
        request_id: str,
        reason: str,
        now: float,
    ) -> CoreCommandRoot:
        current = _exact_number(now, "command_root_terminal_time_invalid")
        terminal_reason = _exact_string(
            reason,
            "command_root_terminal_reason_invalid",
        )
        with self._lock:
            root = self._require_core_command_root(request_id)
            if root.state is CommandRootState.TERMINAL:
                if root.terminal_reason != terminal_reason:
                    raise RuntimeError("command_root_terminal_conflict")
                return root
            updated = replace(
                root,
                state=CommandRootState.TERMINAL,
                updated_at=current,
                terminal_reason=terminal_reason,
            )
            roots = dict(self._command_roots)
            roots[request_id] = updated
            self._commit_locked(
                event="core_command_terminal",
                command_roots=roots,
            )
            return updated

    def core_command_root(self, request_id: str) -> CoreCommandRoot | None:
        identity = _exact_string(
            request_id,
            "command_root_request_id_invalid",
        )
        with self._lock:
            return self._command_roots.get(identity)

    def _require_core_command_root(self, request_id: str) -> CoreCommandRoot:
        identity = _exact_string(
            request_id,
            "command_root_request_id_invalid",
        )
        root = self._command_roots.get(identity)
        if root is None:
            raise RuntimeError("command_root_missing")
        return root

    def freeze_admission(self, *, reason: str = "shutdown") -> dict[str, Any]:
        with self._lock:
            self._admission_frozen = True
            self._admission_frozen_reason = str(reason or "shutdown")
            self._commit_locked(event="command_admission_frozen")
            return {
                "owner": "command_authority",
                "admission_frozen": True,
                "reason": self._admission_frozen_reason,
                "radio_mutation_inflight": self._active_cellular is not None,
            }

    def admission_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "owner": "command_authority",
                "admission_frozen": bool(self._admission_frozen),
                "reason": str(self._admission_frozen_reason or ""),
                "radio_mutation_inflight": self._active_cellular is not None,
            }

    def claim_cellular_action(
        self,
        action: LeverAction,
        *,
        execution_owner: str,
        now: float | None = None,
        recovery_timing: RecoveryTimingProfile | None = None,
    ) -> CellularCommandAdmission:
        """Atomically admit one cellular action and claim its physical lease."""

        if not isinstance(action, LeverAction):
            return CellularCommandAdmission(
                False, (), reason="cellular_action_not_lever_action"
            )
        current = float(time.time() if now is None else now)
        authority = lever_action_authority(action.lever)
        envelope = {
            key: getattr(action, key, None)
            for key in (
                "command_id",
                "dedupe_key",
                "created_ts",
                "expires_after_s",
                "expires_at_ts",
                "replay_policy",
                "source",
                "risk_level",
                "effect",
                "capability_key",
                "requires_modem_write",
                "requires_quiet_window",
                "expected_readback",
                "mutation_window",
            )
        }
        selected_context = selected_action_context_from_action(action)
        envelope.update(
            {
                "command": action.lever.name.lower(),
                "lever": action.lever.name,
                "params": dict(action.params or {}),
                "action_origin": selected_context.origin.kind.value,
            }
        )
        frozen = self._frozen_rejection(envelope)
        if frozen is not None:
            return frozen
        expected = authority.as_payload()
        for field_name in (
            "requires_modem_write",
            "requires_quiet_window",
            "expected_readback",
            "mutation_window",
            "capability_key",
        ):
            if envelope.get(field_name) != expected.get(field_name):
                return self._reject(envelope, f"authority_mismatch:{field_name}")
        metadata_reason, metadata_detail = _command_metadata_rejection(
            envelope,
            now=current,
        )
        if metadata_reason:
            reason = (
                f"{metadata_reason}:{metadata_detail}"
                if metadata_detail
                else metadata_reason
            )
            return self._reject(envelope, reason)
        transaction = build_cellular_transaction(action, now=current, recovery_timing=recovery_timing)
        key = transaction.action_key
        if key is None:
            return self._reject(envelope, "action_transaction_key_missing")
        owner = str(execution_owner or "").strip()
        if not owner:
            raise ValueError("cellular_execution_owner_missing")
        with self._lock:
            command_root = self._command_roots.get(transaction.command_id)
            if command_root is None:
                return self._reject(envelope, "command_root_missing")
            if (
                command_root.state is not CommandRootState.ADMITTED
                or command_root.request_root_id
                != transaction.public_command_root_id
                or command_root.matrix_identity
                != transaction.matrix_identity
            ):
                return self._reject(
                    envelope,
                    "command_root_admission_identity_mismatch",
                )
            if self._projection_obligation is not None:
                return self._reject(
                    envelope,
                    "command_projection_pending:"
                    + self._projection_obligation.obligation_id,
                )
            self._expire_cellular_locked(now=current)
            if self._admission_frozen:
                reason = str(self._admission_frozen_reason or "shutdown")
                return self._reject(
                    envelope, f"command_admission_frozen:{reason}"
                )
            if self._active_cellular is not None:
                return CellularCommandAdmission(
                    False,
                    _freeze(envelope),
                    transaction=transaction,
                    reason="cellular_mutation_inflight",
                )
            dedupe_identity = (
                _runtime_namespace_key(key.matrix_identity),
                key.command_id,
            )
            if dedupe_identity in self._dedupe_identities:
                return self._reject(envelope, "duplicate_command_id")
            dedupe = deque(
                self._dedupe_identities, maxlen=self._max_dedupe_identities
            )
            dedupe.append(dedupe_identity)
            lease = RadioMutationLease(
                transaction=transaction,
                owner=owner,
                acquired_ts=current,
                expires_ts=transaction.expires_at_ts,
            )
            lifecycle = CommandLifecycle(
                lease=lease,
                stage=CommandLifecycleStage.CLAIMED,
                deadline_ts=lease.expires_ts,
                receipts=_freeze({"admission": {"reason": "action_claimed"}}),
            )
            self._commit_locked(
                event="cellular_action_claimed",
                active_cellular=lifecycle,
                dedupe=dedupe,
                projection=lifecycle,
            )
        return CellularCommandAdmission(
            True,
            _freeze(envelope),
            transaction=transaction,
            lease=lease,
        )

    def validate_cellular_execution(
        self,
        action: LeverAction,
        lease: RadioMutationLease | None,
    ) -> tuple[bool, str, RadioMutationLease | None]:
        if not isinstance(lease, RadioMutationLease):
            return False, "cellular_mutation_lease_missing", None
        action_key = action_transaction_key_from_action(action)
        if action_key is None:
            return False, "action_transaction_key_missing", lease
        with self._lock:
            lifecycle = self._active_cellular
            if lifecycle is None:
                return False, "cellular_mutation_lease_not_active", None
            retained = lifecycle.lease
            if retained != lease:
                return False, "cellular_mutation_lease_mismatch", retained
            if retained.root_id != action_key.root_id:
                return False, "cellular_mutation_lease_root_mismatch", retained
            transaction = retained.transaction
            if transaction.command_id != str(
                getattr(action, "command_id", "") or ""
            ):
                return (
                    False,
                    "cellular_mutation_lease_command_mismatch",
                    retained,
                )
            if transaction.command != action.lever.name.lower():
                return False, "cellular_mutation_lease_lever_mismatch", retained
            return True, "", retained

    def mark_dispatched(
        self,
        lease: RadioMutationLease,
        *,
        receipt: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> CommandLifecycle:
        current = float(time.time() if now is None else now)
        with self._lock:
            lifecycle = self._require_active_lease_before_deadline(
                lease,
                now=current,
            )
            if lifecycle.stage is CommandLifecycleStage.DISPATCHED:
                return lifecycle
            if lifecycle.stage is not CommandLifecycleStage.CLAIMED:
                raise RuntimeError(
                    f"command_dispatch_from_{lifecycle.stage.value}"
                )
            updated = replace(
                lifecycle,
                stage=CommandLifecycleStage.DISPATCHED,
                deadline_ts=min(
                    lifecycle.lease.expires_ts,
                    current + DEFAULT_WRITE_RECEIPT_DEADLINE_S,
                ),
                receipts=_merge_receipt(
                    lifecycle.receipts, "dispatch", receipt
                ),
            )
            self._commit_locked(
                event="cellular_dispatched",
                active_cellular=updated,
                projection=updated,
            )
            return updated

    def note_network_stage(
        self, lease: RadioMutationLease, *, stage_kind: str, ordinal: int,
        coordination_profile: str, now: float,
    ) -> CommandLifecycle:
        """Retain actual stage entry without extending or confirming the command."""
        from arch4.core_contracts.network_settings import CoreNetworkSettingsTarget, network_stage_kinds
        if type(now) not in {int, float} or not math.isfinite(now) or now <= 0:
            raise ValueError("network_progress_timestamp_invalid")
        with self._lock:
            lifecycle = self._require_active_lease_before_deadline(lease, now=now)
            target = lifecycle.key.target
            if lifecycle.stage is not CommandLifecycleStage.DISPATCHED or not isinstance(target, CoreNetworkSettingsTarget):
                raise RuntimeError("network_progress_active_dispatch_required")
            stages = network_stage_kinds(target, coordination_profile=coordination_profile)
            previous = dict(_thaw(lifecycle.receipts)).get("network_progress", {})
            if (type(ordinal) is not int or ordinal != previous.get("ordinal", 0) + 1
                    or ordinal > len(stages) or stages[ordinal - 1] != stage_kind
                    or now < previous.get("started_at", lifecycle.lease.acquired_ts)):
                raise RuntimeError("network_progress_stage_order_invalid")
            updated = replace(lifecycle, receipts=_merge_receipt(lifecycle.receipts, "network_progress", {
                "stage_kind": stage_kind, "ordinal": ordinal, "total": len(stages),
                "started_at": now,
            }))
            self._commit_locked(event="network_stage_started", active_cellular=updated, projection=updated)
            return updated

    def acknowledge_modem_result(
        self,
        lease: RadioMutationLease,
        *,
        physical_receipt: PhysicalCommandReceipt,
        now: float | None = None,
    ) -> CommandTransition:
        current = float(time.time() if now is None else now)
        with self._lock:
            lifecycle = self._require_active_lease_before_deadline(
                lease,
                now=current,
            )
            if lifecycle.stage is not CommandLifecycleStage.DISPATCHED:
                raise RuntimeError(
                    f"command_modem_result_from_{lifecycle.stage.value}"
                )
            if not isinstance(physical_receipt, PhysicalCommandReceipt):
                raise TypeError("command_authority_physical_receipt_required")
            if (
                physical_receipt.request_id
                != lifecycle.lease.transaction.command_id
            ):
                raise RuntimeError("physical_receipt_request_mismatch")
            if physical_receipt.transaction_root_id != lifecycle.key.root_id:
                raise RuntimeError("physical_receipt_root_mismatch")
            if physical_receipt.target != lifecycle.key.target:
                raise RuntimeError("physical_receipt_target_mismatch")
            if physical_receipt.transaction_completed_at > current:
                raise RuntimeError("physical_receipt_after_acknowledgement")
            receipts = _merge_receipt(
                lifecycle.receipts,
                "modem",
                physical_receipt.as_dict(),
            )
            if not physical_receipt.accepted:
                terminal = self._terminalize_cellular_locked(
                    replace(lifecycle, receipts=receipts),
                    status=CommandTerminalStatus.FAILED,
                    reason=physical_receipt.reason,
                    now=current,
                )
                return CommandTransition(terminal=terminal)
            accepted_lifecycle = replace(
                lifecycle,
                stage=CommandLifecycleStage.ACCEPTED,
                deadline_ts=min(
                    lifecycle.lease.expires_ts,
                    current + (
                        RecoveryTimingProfile.from_mapping(lifecycle.lease.transaction.authority_dict()["recovery_timing"])
                        .restart_verification_s
                        if lifecycle.lease.transaction.command == "restart" else DEFAULT_TARGET_DEADLINE_S
                    ),
                ),
                accepted_ts=current,
                confirmed_ts=0.0,
                reason=physical_receipt.reason,
                receipts=receipts,
            )
            contract = accepted_lifecycle.completion
            if contract.terminal_on_modem_acceptance:
                terminal = self._terminalize_cellular_locked(
                    accepted_lifecycle,
                    status=CommandTerminalStatus.ACCEPTED,
                    reason=physical_receipt.reason,
                    now=current,
                )
                return CommandTransition(terminal=terminal)
            self._commit_locked(
                event="cellular_transaction_accepted",
                active_cellular=accepted_lifecycle,
                projection=accepted_lifecycle,
            )
            return CommandTransition(active=accepted_lifecycle)

    def acknowledge_target_confirmed(
        self,
        *,
        root_id: str,
        receipt: TargetConfirmationReceipt,
        now: float | None = None,
    ) -> CommandLifecycle:
        current = float(time.time() if now is None else now)
        with self._lock:
            lifecycle = self._require_active_root_before_deadline(
                root_id,
                now=current,
            )
            if not isinstance(receipt, TargetConfirmationReceipt):
                raise TypeError("command_target_typed_receipt_required")
            if receipt.transaction_root_id != lifecycle.key.root_id:
                raise RuntimeError("command_target_receipt_root_mismatch")
            if receipt.request_id != lifecycle.key.command_id:
                raise RuntimeError("command_target_receipt_request_mismatch")
            if (
                receipt.matrix_identity.namespace
                != lifecycle.key.matrix_identity.namespace
            ):
                raise RuntimeError("command_target_receipt_session_mismatch")
            if receipt.target != lifecycle.key.target:
                raise RuntimeError("command_target_receipt_target_mismatch")
            if not lifecycle.completion.target_required:
                raise RuntimeError("command_target_confirmation_not_declared")
            if lifecycle.stage is CommandLifecycleStage.CONFIRMED:
                return lifecycle
            if lifecycle.stage is not CommandLifecycleStage.ACCEPTED:
                raise RuntimeError(
                    f"command_target_from_{lifecycle.stage.value}"
                )
            generation = _exact_int(
                receipt.matrix_identity.matrix_sequence,
                "command_target_generation_invalid",
                minimum=1,
            )
            if generation <= lifecycle.key.matrix_identity.matrix_sequence:
                raise RuntimeError(
                    "command_target_generation_not_after_source"
                )
            updated = replace(
                lifecycle,
                stage=CommandLifecycleStage.CONFIRMED,
                deadline_ts=min(
                    lifecycle.lease.expires_ts,
                    current + DEFAULT_SETTLE_DEADLINE_S,
                ),
                confirmed_ts=current,
                target_generation=generation,
                receipts=_merge_receipt(
                    lifecycle.receipts, "target", receipt.as_mapping()
                ),
            )
            self._commit_locked(
                event="cellular_target_confirmed",
                active_cellular=updated,
                projection=updated,
            )
            return updated

    def latch_stream_continuation(
        self,
        *,
        root_id: str,
        source_generation: int,
        receipt: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> CommandLifecycle:
        current = float(time.time() if now is None else now)
        with self._lock:
            lifecycle = self._require_active_root_before_deadline(
                root_id,
                now=current,
            )
            if lifecycle.stage is not CommandLifecycleStage.CONFIRMED:
                raise RuntimeError(
                    f"command_stream_settle_from_{lifecycle.stage.value}"
                )
            generation = _exact_int(
                source_generation,
                "command_settle_generation_invalid",
                minimum=1,
            )
            if lifecycle.completion.continuation_required:
                if generation <= int(lifecycle.target_generation or 0):
                    raise RuntimeError(
                        "command_settle_requires_later_generation"
                    )
            elif generation <= 0:
                generation = int(lifecycle.target_generation or 0)
            if lifecycle.settle_generation > 0:
                retained = dict(_thaw(lifecycle.receipts)).get("settle")
                if (
                    lifecycle.settle_generation == generation
                    and retained == dict(receipt or {})
                ):
                    return lifecycle
                raise RuntimeError(
                    "command_stream_continuation_already_latched"
                )
            updated = replace(
                lifecycle,
                settle_generation=generation,
                receipts=_merge_receipt(
                    lifecycle.receipts, "settle", receipt
                ),
            )
            self._commit_locked(
                event="cellular_stream_continuation_latched",
                active_cellular=updated,
                projection=updated,
            )
            return updated

    def settle_confirmed_stream(
        self,
        *,
        root_id: str,
        now: float | None = None,
    ) -> CommandTerminalReceipt:
        current = float(time.time() if now is None else now)
        with self._lock:
            lifecycle = self._require_active_root_before_deadline(
                root_id,
                now=current,
            )
            if lifecycle.stage is not CommandLifecycleStage.CONFIRMED:
                raise RuntimeError(
                    f"command_stream_settle_from_{lifecycle.stage.value}"
                )
            if lifecycle.settle_generation <= 0:
                raise RuntimeError(
                    "command_stream_settle_without_continuation"
                )
            receipts = dict(_thaw(lifecycle.receipts))
            if {"reachability", "reachability_unavailable"}.intersection(receipts):
                raise RuntimeError(
                    "command_stream_settle_embedded_reachability_forbidden"
                )
            return self._terminalize_cellular_locked(
                lifecycle,
                status=CommandTerminalStatus.SETTLED,
                reason="target_confirmed_and_stream_settled",
                now=current,
            )

    def acknowledge_reachability(
        self,
        receipt: Any,
    ) -> CommandLifecycle:
        raise RuntimeError("command_reachability_owned_by_action_pause")

    def acknowledge_reachability_unavailable(
        self,
        receipt: Any,
    ) -> CommandLifecycle:
        raise RuntimeError("command_reachability_owned_by_action_pause")

    def acknowledge_reachability_system_fault(
        self,
        receipt: Any,
    ) -> CommandTerminalReceipt:
        raise RuntimeError("command_reachability_owned_by_action_pause")

    def acknowledge_settled(self, **_: Any) -> None:
        raise RuntimeError("command_authority_old_settle_path_illegal")

    def fail_cellular(
        self,
        *,
        root_id: str,
        reason: str,
        receipt: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> CommandTerminalReceipt:
        current = float(time.time() if now is None else now)
        with self._lock:
            lifecycle = self._require_active_root_before_deadline(
                root_id,
                now=current,
            )
            updated = replace(
                lifecycle,
                receipts=_merge_receipt(
                    lifecycle.receipts, "failure", receipt
                ),
            )
            return self._terminalize_cellular_locked(
                updated,
                status=CommandTerminalStatus.FAILED,
                reason=str(reason or "command_failed"),
                now=current,
            )

    def cancel_cellular(
        self,
        *,
        root_id: str,
        reason: str,
        receipt: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> CommandTerminalReceipt:
        current = float(time.time() if now is None else now)
        with self._lock:
            lifecycle = self._require_active_root_before_deadline(
                root_id,
                now=current,
            )
            updated = replace(
                lifecycle,
                receipts=_merge_receipt(
                    lifecycle.receipts, "cancellation", receipt
                ),
            )
            return self._terminalize_cellular_locked(
                updated,
                status=CommandTerminalStatus.CANCELLED,
                reason=str(reason or "command_cancelled"),
                now=current,
            )

    def expire_overdue(
        self, *, now: float | None = None
    ) -> tuple[CommandTerminalReceipt, ...]:
        current = float(time.time() if now is None else now)
        with self._lock:
            cellular = self._expire_cellular_locked(now=current)
            return (cellular,) if cellular is not None else ()

    def active_lifecycle(self) -> CommandLifecycle | None:
        with self._lock:
            return self._active_cellular

    def terminal_receipts(
        self, *, after_sequence: int = 0
    ) -> tuple[CommandTerminalReceipt, ...]:
        with self._lock:
            threshold = _exact_int(
                after_sequence,
                "command_terminal_after_sequence_invalid",
            )
            return tuple(
                receipt
                for receipt in self._terminal_receipts
                if receipt.receipt_sequence > threshold
            )

    def pending_projection_obligation(
        self,
    ) -> CommandLifecycleProjectionObligation | None:
        with self._lock:
            return self._projection_obligation

    def reconcile_runtime_identity(self, session_scope_key: str) -> bool:
        """Remove durable command truth owned by another attached modem."""

        scope = _exact_string(
            session_scope_key,
            "command_runtime_session_scope_key_required",
        )
        with self._lock:
            active = self._active_cellular
            next_active = (
                active
                if active is None
                or active.key.matrix_identity.session_scope_key == scope
                else None
            )
            roots = {
                request_id: root
                for request_id, root in self._command_roots.items()
                if root.matrix_identity.session_scope_key == scope
            }
            terminal = deque(
                (
                    receipt
                    for receipt in self._terminal_receipts
                    if receipt.transaction.matrix_identity.session_scope_key
                    == scope
                ),
                maxlen=self._max_terminal_receipts,
            )
            obligation = self._projection_obligation
            clear_projection = False
            if obligation is not None:
                receipt = obligation.typed_receipt()
                matrix_identity = (
                    receipt.key.matrix_identity
                    if isinstance(receipt, CommandLifecycle)
                    else receipt.transaction.matrix_identity
                )
                clear_projection = matrix_identity.session_scope_key != scope
            changed = bool(
                next_active is not active
                or len(roots) != len(self._command_roots)
                or len(terminal) != len(self._terminal_receipts)
                or clear_projection
            )
            if not changed:
                return False
            self._commit_locked(
                event="command_runtime_identity_reconciled",
                active_cellular=next_active,
                command_roots=roots,
                dedupe=deque(maxlen=self._max_dedupe_identities),
                terminal=terminal,
                clear_projection=clear_projection,
                projection_ack=None,
            )
            return True

    def last_projection_acknowledgement(
        self,
    ) -> CommandLifecycleProjectionAcknowledgement | None:
        with self._lock:
            return self._last_projection_ack

    def acknowledge_projection(
        self,
        *,
        obligation_id: str,
        payload_fingerprint: str,
        register_sequence: int,
        register_signature: str,
        acknowledged_at: float,
    ) -> CommandLifecycleProjectionAcknowledgement:
        """Clear one retained obligation only after exact register proof."""

        with self._lock:
            obligation = self._projection_obligation
            if obligation is None:
                retained = self._last_projection_ack
                if (
                    retained is not None
                    and retained.obligation_id == str(obligation_id or "")
                    and retained.payload_fingerprint
                    == str(payload_fingerprint or "")
                ):
                    return retained
                raise RuntimeError("command_projection_obligation_missing")
            if obligation.obligation_id != str(obligation_id or ""):
                raise RuntimeError("command_projection_obligation_id_mismatch")
            if obligation.payload_fingerprint != str(
                payload_fingerprint or ""
            ):
                raise RuntimeError(
                    "command_projection_obligation_fingerprint_mismatch"
                )
            sequence = _exact_int(
                register_sequence,
                "command_projection_register_sequence_invalid",
                minimum=1,
            )
            current = _exact_number(
                acknowledged_at,
                "command_projection_acknowledged_at_invalid",
            )
            if current < obligation.created_at:
                raise RuntimeError(
                    "command_projection_ack_before_obligation"
                )
            ack = CommandLifecycleProjectionAcknowledgement(
                obligation_id=obligation.obligation_id,
                authority_sequence=obligation.authority_sequence,
                transaction_root_id=obligation.transaction_root_id,
                payload_fingerprint=obligation.payload_fingerprint,
                register_sequence=sequence,
                register_signature=str(register_signature or ""),
                acknowledged_at=current,
            )
            self._commit_locked(
                event="command_projection_acknowledged",
                clear_projection=True,
                projection_ack=ack,
            )
            return ack

    def terminal_receipt_retention(self) -> dict[str, int]:
        """Return CommandAuthority-owned terminal-history retention bounds."""

        with self._lock:
            return {
                "evicted_through_sequence": (
                    self._terminal_evicted_through_sequence
                ),
                "oldest_retained_sequence": (
                    self._terminal_receipts[0].receipt_sequence
                    if self._terminal_receipts
                    else 0
                ),
                "latest_retained_sequence": (
                    self._terminal_receipts[-1].receipt_sequence
                    if self._terminal_receipts
                    else 0
                ),
            }

    def cellular_execution_status(self) -> dict[str, Any]:
        with self._lock:
            lifecycle = self._active_cellular
            if lifecycle is None:
                return {
                    "owner": "command_authority",
                    "active": False,
                    "admission_frozen": bool(self._admission_frozen),
                    "admission_frozen_reason": str(
                        self._admission_frozen_reason or ""
                    ),
                    "durable": self._state_store is not None,
                    "dedupe_count": len(self._dedupe_identities),
                    "terminal_receipt_count": len(self._terminal_receipts),
                    "projection_pending": (
                        self._projection_obligation is not None
                    ),
                    "projection_obligation_id": (
                        self._projection_obligation.obligation_id
                        if self._projection_obligation is not None
                        else ""
                    ),
                    "restore_error": self._restore_error,
                }
            lease = lifecycle.lease
            return {
                "owner": "command_authority",
                "active": True,
                "lease_owner": lease.owner,
                "command": lease.transaction.command,
                "command_id": lease.transaction.command_id,
                "family": lease.transaction.family.value,
                "acquired_ts": lease.acquired_ts,
                "expires_ts": lease.expires_ts,
                "stage": lifecycle.stage.value,
                "deadline_ts": lifecycle.deadline_ts,
                "root_id": lifecycle.key.root_id,
                "proof_id": lifecycle.key.proof_id,
                "opportunity_id": lifecycle.key.opportunity_id,
                "opportunity_set_id": lifecycle.key.opportunity_set_id,
                "decision_id": lifecycle.key.decision_id,
                "decision_receipt_id": lifecycle.key.decision_receipt_id,
                "bandit_selection_receipt_id": (
                    lifecycle.key.bandit_selection_receipt_id
                ),
                "matrix_identity": lifecycle.key.matrix_identity.as_dict(),
                "target_generation": lifecycle.target_generation,
                "settle_generation": lifecycle.settle_generation,
                "completion": lifecycle.completion.as_dict(),
                "admission_frozen": bool(self._admission_frozen),
                "admission_frozen_reason": str(
                    self._admission_frozen_reason or ""
                ),
                "durable": self._state_store is not None,
                "projection_pending": (
                    self._projection_obligation is not None
                ),
                "projection_obligation_id": (
                    self._projection_obligation.obligation_id
                    if self._projection_obligation is not None
                    else ""
                ),
                "restore_error": self._restore_error,
            }

    def _frozen_rejection(
        self, envelope: Mapping[str, Any]
    ) -> CellularCommandAdmission | None:
        with self._lock:
            if not self._admission_frozen:
                return None
            reason = str(self._admission_frozen_reason or "shutdown")
        return self._reject(
            envelope, f"command_admission_frozen:{reason}"
        )

    def _require_active_lease(
        self, lease: RadioMutationLease
    ) -> CommandLifecycle:
        lifecycle = self._active_cellular
        if lifecycle is None:
            raise RuntimeError("cellular_mutation_lease_not_active")
        if lifecycle.lease != lease:
            raise RuntimeError("cellular_mutation_lease_mismatch")
        return lifecycle

    def _require_active_root(self, root_id: str) -> CommandLifecycle:
        lifecycle = self._active_cellular
        if lifecycle is None:
            raise RuntimeError("cellular_command_lifecycle_missing")
        if lifecycle.key.root_id != str(root_id or ""):
            raise RuntimeError("cellular_command_root_mismatch")
        return lifecycle

    def _require_transition_before_deadline(
        self,
        lifecycle: CommandLifecycle,
        *,
        now: float,
    ) -> CommandLifecycle:
        """Expire first so no receipt can revive an overdue command."""

        terminal = self._expire_cellular_locked(now=now)
        if terminal is not None:
            raise RuntimeError(
                "cellular_command_transition_after_deadline:"
                + terminal.reason
            )
        return lifecycle

    def _require_active_lease_before_deadline(
        self,
        lease: RadioMutationLease,
        *,
        now: float,
    ) -> CommandLifecycle:
        return self._require_transition_before_deadline(
            self._require_active_lease(lease),
            now=now,
        )

    def _require_active_root_before_deadline(
        self,
        root_id: str,
        *,
        now: float,
    ) -> CommandLifecycle:
        return self._require_transition_before_deadline(
            self._require_active_root(root_id),
            now=now,
        )

    def _expire_cellular_locked(
        self, *, now: float
    ) -> CommandTerminalReceipt | None:
        lifecycle = self._active_cellular
        if lifecycle is None:
            return None
        if float(now) < float(lifecycle.deadline_ts or 0.0):
            return None
        return self._terminalize_cellular_locked(
            replace(
                lifecycle,
                receipts=_merge_receipt(
                    lifecycle.receipts,
                    "timeout",
                    {
                        "stage": lifecycle.stage.value,
                        "deadline_ts": lifecycle.deadline_ts,
                        "observed_ts": now,
                    },
                ),
            ),
            status=CommandTerminalStatus.TIMED_OUT,
            reason=f"{lifecycle.stage.value}_deadline_expired",
            now=now,
        )

    def _terminalize_cellular_locked(
        self,
        lifecycle: CommandLifecycle,
        *,
        status: CommandTerminalStatus,
        reason: str,
        now: float,
    ) -> CommandTerminalReceipt:
        if self._active_cellular is None:
            raise RuntimeError("cellular_command_terminal_without_active")
        receipt = CommandTerminalReceipt(
            receipt_sequence=int(self._sequence) + 1,
            transaction=lifecycle.lease.transaction,
            terminal_status=status,
            reason=str(reason or status.value),
            terminal_ts=float(now),
            accepted_ts=float(lifecycle.accepted_ts),
            confirmed_ts=float(lifecycle.confirmed_ts),
            settled_ts=(
                float(now)
                if status is CommandTerminalStatus.SETTLED
                else 0.0
            ),
            target_generation=int(lifecycle.target_generation),
            settle_generation=int(lifecycle.settle_generation),
            # This flag proves the Command Authority settlement contract, not
            # an Intelligence/referee grade.  Exact required receipts are
            # validated immediately below.
            proof_ready=status is CommandTerminalStatus.SETTLED,
            receipts=lifecycle.receipts,
        )
        if status is CommandTerminalStatus.SETTLED:
            receipt.validate_settlement_proof()
        terminal = deque(
            self._terminal_receipts, maxlen=self._max_terminal_receipts
        )
        terminal_evicted_through = self._terminal_evicted_through_sequence
        if len(terminal) == self._max_terminal_receipts:
            terminal_evicted_through = max(
                terminal_evicted_through,
                terminal[0].receipt_sequence,
            )
        terminal.append(receipt)
        roots = dict(self._command_roots)
        command_root = roots.get(lifecycle.lease.transaction.command_id)
        if command_root is not None:
            if (
                command_root.request_root_id
                != lifecycle.lease.transaction.public_command_root_id
                or command_root.matrix_identity
                != lifecycle.lease.transaction.matrix_identity
            ):
                raise RuntimeError(
                    "command_terminal_ingress_root_identity_mismatch"
                )
            roots[command_root.request.request_id] = replace(
                command_root,
                state=CommandRootState.TERMINAL,
                updated_at=float(now),
                physical_action_root_id=(
                    command_root.physical_action_root_id
                    or lifecycle.key.root_id
                ),
                terminal_reason=str(reason or status.value),
            )
        self._commit_locked(
            event=f"cellular_terminal_{status.value}",
            active_cellular=None,
            command_roots=roots,
            terminal=terminal,
            terminal_evicted_through=terminal_evicted_through,
            projection=receipt,
        )
        return receipt

    def _reopen_admission_after_restart(self) -> None:
        """Publish the new process's open gate after durable-state recovery."""

        with self._lock:
            if not self._restart_admission_reopen_reason:
                return
            self._admission_frozen = False
            self._admission_frozen_reason = ""
            self._commit_locked(
                event="command_admission_reopened_after_restart"
            )
            self._restart_admission_reopen_reason = ""

    def _reissue_projection_after_restart(self) -> None:
        """Rebuild the in-memory action register from durable authority truth."""

        with self._lock:
            if self._projection_obligation is not None:
                return
            receipt: CommandLifecycle | CommandTerminalReceipt | None
            if self._active_cellular is not None:
                receipt = self._active_cellular
            elif self._terminal_receipts:
                receipt = self._terminal_receipts[-1]
            else:
                receipt = None
            if receipt is None:
                return
            self._commit_locked(
                event="command_projection_reissued_after_restart",
                projection=receipt,
            )

    def _commit_locked(
        self,
        *,
        event: str,
        active_cellular: CommandLifecycle | None | object = ...,
        command_roots: Mapping[str, CoreCommandRoot] | object = ...,
        dedupe: deque[tuple[str, str]] | object = ...,
        terminal: deque[CommandTerminalReceipt] | object = ...,
        terminal_evicted_through: int | object = ...,
        projection: (
            CommandLifecycle | CommandTerminalReceipt | object
        ) = ...,
        clear_projection: bool = False,
        projection_ack: (
            CommandLifecycleProjectionAcknowledgement | object
        ) = ...,
    ) -> None:
        if clear_projection and projection is not ...:
            raise RuntimeError("command_projection_commit_mode_conflict")
        if projection is not ... and self._projection_obligation is not None:
            raise RuntimeError(
                "command_projection_obligation_pending:"
                + self._projection_obligation.obligation_id
            )
        if clear_projection and self._projection_obligation is None:
            raise RuntimeError("command_projection_obligation_missing")
        next_cellular = (
            self._active_cellular
            if active_cellular is ...
            else active_cellular
        )
        next_command_roots = (
            dict(self._command_roots)
            if command_roots is ...
            else dict(command_roots)
        )
        if any(
            not isinstance(root, CoreCommandRoot)
            or request_id != root.request.request_id
            for request_id, root in next_command_roots.items()
        ):
            raise TypeError("command_authority_command_roots_invalid")
        next_dedupe = (
            deque(
                self._dedupe_identities,
                maxlen=self._max_dedupe_identities,
            )
            if dedupe is ...
            else deque(dedupe, maxlen=self._max_dedupe_identities)
        )
        next_terminal = (
            deque(
                self._terminal_receipts,
                maxlen=self._max_terminal_receipts,
            )
            if terminal is ...
            else deque(terminal, maxlen=self._max_terminal_receipts)
        )
        next_terminal_evicted_through = (
            self._terminal_evicted_through_sequence
            if terminal_evicted_through is ...
            else _exact_int(
                terminal_evicted_through,
                "command_terminal_evicted_sequence_invalid",
            )
        )
        sequence = int(self._sequence) + 1
        next_projection_obligation = self._projection_obligation
        next_projection_ack = (
            self._last_projection_ack
            if projection_ack is ...
            else projection_ack
        )
        if projection is not ...:
            if not isinstance(
                projection,
                (CommandLifecycle, CommandTerminalReceipt),
            ):
                raise TypeError("command_projection_typed_receipt_required")
            projection_payload = projection.as_dict()
            projection_kind = (
                "lifecycle"
                if isinstance(projection, CommandLifecycle)
                else "terminal"
            )
            transaction_root_id = (
                projection.key.root_id
                if isinstance(projection, CommandLifecycle)
                else projection.root_id
            )
            fingerprint = _checksum(projection_payload)
            obligation_id = hashlib.sha256(
                (
                    f"{sequence}|{transaction_root_id}|"
                    f"{projection_kind}|{fingerprint}"
                ).encode("utf-8")
            ).hexdigest()
            created_at = (
                max(
                    float(projection.confirmed_ts or 0.0),
                    float(projection.accepted_ts or 0.0),
                    float(projection.lease.acquired_ts or 0.0),
                )
                if isinstance(projection, CommandLifecycle)
                else float(projection.terminal_ts)
            )
            next_projection_obligation = (
                CommandLifecycleProjectionObligation(
                    obligation_id=obligation_id,
                    authority_sequence=sequence,
                    transaction_root_id=transaction_root_id,
                    projection_kind=projection_kind,
                    payload_fingerprint=fingerprint,
                    created_at=created_at,
                    payload=_freeze(projection_payload),
                )
            )
        elif clear_projection:
            next_projection_obligation = None
        payload = {
            "schema": COMMAND_AUTHORITY_STATE_SCHEMA,
            "owner": COMMAND_AUTHORITY_OWNER,
            "event": str(event or "transition"),
            "sequence": sequence,
            "active_cellular": (
                next_cellular.as_dict()
                if isinstance(next_cellular, CommandLifecycle)
                else None
            ),
            "command_roots": [
                next_command_roots[request_id].as_dict()
                for request_id in sorted(next_command_roots)
            ],
            "dedupe": [
                [session, command] for session, command in next_dedupe
            ],
            "terminal": [receipt.as_dict() for receipt in next_terminal],
            "terminal_evicted_through_sequence": (
                next_terminal_evicted_through
            ),
            "admission_frozen": bool(self._admission_frozen),
            "admission_frozen_reason": str(
                self._admission_frozen_reason or ""
            ),
            "projection_obligation": (
                next_projection_obligation.as_dict()
                if next_projection_obligation is not None
                else None
            ),
            "last_projection_ack": (
                next_projection_ack.as_dict()
                if isinstance(
                    next_projection_ack,
                    CommandLifecycleProjectionAcknowledgement,
                )
                else None
            ),
        }
        payload["checksum"] = _checksum(payload)
        # Preparation and projection acknowledgements are in-process state.
        # Persisting them adds file I/O to the physical command lane without
        # improving crash recovery: before dispatch there is no modem effect
        # to recover, while after dispatch the complete authority snapshot is
        # already captured by the one durable Core-root dispatch checkpoint
        # before the execution gate is released.
        if (
            self._state_store is not None
            and _command_transition_persists(event)
        ):
            self._state_store.write(payload)
        self._active_cellular = (
            next_cellular
            if isinstance(next_cellular, CommandLifecycle)
            else None
        )
        self._command_roots = next_command_roots
        self._dedupe_identities = next_dedupe
        self._terminal_receipts = next_terminal
        self._terminal_evicted_through_sequence = (
            next_terminal_evicted_through
        )
        self._projection_obligation = next_projection_obligation
        self._last_projection_ack = (
            next_projection_ack
            if isinstance(
                next_projection_ack,
                CommandLifecycleProjectionAcknowledgement,
            )
            else None
        )
        self._sequence = sequence

    def _restore_from_state(self) -> None:
        assert self._state_store is not None
        try:
            last = self._state_store.read()
            if last is None:
                return
            record = CommandAuthorityStateRecord.from_mapping(last)
        except Exception as exc:
            self._restore_error = (
                "command_authority_state_invalid:"
                + type(exc).__name__
                + ":"
                + str(exc)
            )
            self._admission_frozen = True
            self._admission_frozen_reason = "command_authority_state_invalid"
            return
        self._active_cellular = record.active_cellular
        self._command_roots = {
            root.request.request_id: root
            for root in record.command_roots
        }
        self._dedupe_identities = deque(
            record.dedupe,
            maxlen=self._max_dedupe_identities,
        )
        self._terminal_receipts = deque(
            record.terminal,
            maxlen=self._max_terminal_receipts,
        )
        self._terminal_evicted_through_sequence = (
            record.terminal_evicted_through_sequence
        )
        self._projection_obligation = record.projection_obligation
        self._last_projection_ack = record.last_projection_ack
        self._sequence = record.sequence
        # A shutdown freeze protects only the process performing shutdown.  It
        # is retained in current state, never restored as
        # policy for a new process instance.
        if record.admission_frozen is True:
            self._restart_admission_reopen_reason = (
                record.admission_frozen_reason or "shutdown"
            )

    @staticmethod
    def _reject(
        envelope: Mapping[str, Any], reason: str
    ) -> CellularCommandAdmission:
        return CellularCommandAdmission(
            False, _freeze(envelope), reason=str(reason or "rejected")
        )


def build_cellular_transaction(
    action: LeverAction,
    *,
    now: float,
    recovery_timing: RecoveryTimingProfile | None = None,
) -> CommandTransaction:
    key = action_transaction_key_from_action(action)
    if key is None:
        raise ValueError("cellular_action_transaction_key_missing")
    family = (
        TransactionFamily.CELLULAR_OPERATOR
        if is_human_operator_origin(key.origin_kind)
        else TransactionFamily.CELLULAR_AUTONOMOUS
    )
    descriptor = action_descriptor(action.lever)
    completion = _cellular_completion_contract(descriptor)
    current = float(now)
    if current <= 0.0:
        raise ValueError("cellular_transaction_positive_timestamp_required")
    created_ts = _float(getattr(action, "created_ts", 0.0), current)
    expires_at_ts = current + DEFAULT_LEASE_DEADLINE_S
    if action.lever is LeverType.RESTART:
        if not isinstance(recovery_timing, RecoveryTimingProfile):
            raise TypeError("restart_selected_recovery_timing_required")
        expires_at_ts = current + max(DEFAULT_LEASE_DEADLINE_S,
            DEFAULT_WRITE_RECEIPT_DEADLINE_S + recovery_timing.restart_verification_s + DEFAULT_SETTLE_DEADLINE_S)
    elif recovery_timing is not None:
        raise ValueError("recovery_timing_for_non_restart")
    return CommandTransaction(
        root_id=key.root_id,
        public_command_root_id=str(
            dict(action.params or {}).get("_core_command_root_id") or ""
        ),
        family=family,
        command=action.lever.name.lower(),
        command_id=str(getattr(action, "command_id", "") or ""),
        dedupe_key=str(getattr(action, "dedupe_key", "") or ""),
        source=str(getattr(action, "source", "") or ""),
        origin_kind=key.origin_kind,
        correlation_id=key.correlation_id,
        correlation_source=key.correlation_source,
        risk_level=str(getattr(action, "risk_level", "") or "high"),
        created_ts=created_ts,
        expires_at_ts=expires_at_ts,
        matrix_identity=key.matrix_identity,
        identity_key=key.identity_key,
        params=_freeze(dict(action.params or {})),
        authority=_freeze(
            {
                "effect": str(
                    getattr(action, "effect", "") or "modem_write"
                ),
                "requires_modem_write": True,
                **({"recovery_timing": recovery_timing.as_dict()} if recovery_timing is not None else {}),
            }
        ),
        action_key=key,
        cellular_completion=completion,
    )


def _cellular_completion_contract(descriptor: Any) -> CellularCompletionContract:
    verification = descriptor.verification
    setting_readback = descriptor.lever in {LeverType.WIFI_RADIO, LeverType.MOBILE_DATA}
    target_required = verification in {
        ActionVerification.RADIO_TARGET_AND_FRESH_SAMPLE,
        ActionVerification.MODEM_RECONNECT_AND_FRESH_SAMPLE,
    } or setting_readback
    continuation_required = target_required
    terminal_on_acceptance = verification in {
        ActionVerification.TRAFFIC_READBACK,
        ActionVerification.CAPABILITY_READBACK,
    } and not setting_readback
    completion = (
        "target_then_fresh_generation"
        if target_required
        else "transport_acceptance_or_required_readback"
    )
    return CellularCompletionContract(
        verification=verification.value,
        completion=completion,
        proof=descriptor.proof.value,
        learning=descriptor.learning.value,
        target_required=target_required,
        continuation_required=continuation_required,
        terminal_on_modem_acceptance=terminal_on_acceptance,
        reachability_measurement_enabled=descriptor.requires_cellular_recovery,
    )


def _transaction_to_dict(transaction: CommandTransaction) -> dict[str, Any]:
    return {
        "root_id": transaction.root_id,
        "public_command_root_id": transaction.public_command_root_id,
        "family": transaction.family.value,
        "command": transaction.command,
        "command_id": transaction.command_id,
        "dedupe_key": transaction.dedupe_key,
        "source": transaction.source,
        "origin_kind": transaction.origin_kind,
        "correlation_id": transaction.correlation_id,
        "correlation_source": transaction.correlation_source,
        "risk_level": transaction.risk_level,
        "created_ts": transaction.created_ts,
        "expires_at_ts": transaction.expires_at_ts,
        "matrix_identity": transaction.matrix_identity.as_dict(),
        "identity_key": transaction.identity_key,
        "params": dict(_thaw(transaction.params)),
        "authority": dict(_thaw(transaction.authority)),
        "action_transaction": (
            transaction.action_key.as_dict()
            if transaction.action_key
            else {}
        ),
        "cellular_completion": (
            transaction.cellular_completion.as_dict()
            if transaction.cellular_completion
            else {}
        ),
    }


def _transaction_from_dict(raw: Mapping[str, Any]) -> CommandTransaction:
    row = dict(raw or {})
    _exact_fields(
        row,
        frozenset(
            {
                "root_id",
                "public_command_root_id",
                "family",
                "command",
                "command_id",
                "dedupe_key",
                "source",
                "origin_kind",
                "correlation_id",
                "correlation_source",
                "risk_level",
                "created_ts",
                "expires_at_ts",
                "matrix_identity",
                "identity_key",
                "params",
                "authority",
                "action_transaction",
                "cellular_completion",
            }
        ),
        "command_transaction_fields_invalid",
    )
    key_raw = row.get("action_transaction")
    key = (
        ActionTransactionKey.from_mapping(dict(key_raw))
        if isinstance(key_raw, Mapping) and key_raw
        else None
    )
    completion_raw = row.get("cellular_completion")
    completion = (
        CellularCompletionContract.from_mapping(dict(completion_raw))
        if isinstance(completion_raw, Mapping) and completion_raw
        else None
    )
    if not isinstance(row.get("params"), Mapping):
        raise TypeError("command_transaction_params_mapping_required")
    if not isinstance(row.get("authority"), Mapping):
        raise TypeError("command_transaction_authority_mapping_required")
    return CommandTransaction(
        root_id=_exact_string(row.get("root_id"), "command_transaction_root_invalid"),
        public_command_root_id=_exact_string(
            row.get("public_command_root_id"),
            "command_transaction_public_root_invalid",
        ),
        family=TransactionFamily(
            _exact_string(row.get("family"), "command_transaction_family_invalid")
        ),
        command=_exact_string(
            row.get("command"), "command_transaction_command_invalid"
        ),
        command_id=_exact_string(
            row.get("command_id"), "command_transaction_command_id_invalid"
        ),
        dedupe_key=_exact_string(
            row.get("dedupe_key"),
            "command_transaction_dedupe_invalid",
            required=False,
        ),
        source=_exact_string(
            row.get("source"), "command_transaction_source_invalid"
        ),
        origin_kind=require_action_origin_name(
            _exact_string(
                row.get("origin_kind"),
                "command_transaction_origin_invalid",
            )
        ),
        correlation_id=_exact_string(
            row.get("correlation_id"),
            "command_transaction_correlation_invalid",
        ),
        correlation_source=_exact_string(
            row.get("correlation_source"),
            "command_transaction_correlation_source_invalid",
        ),
        risk_level=_exact_string(
            row.get("risk_level"), "command_transaction_risk_invalid"
        ),
        created_ts=_exact_number(
            row.get("created_ts"), "command_transaction_created_ts_invalid"
        ),
        expires_at_ts=_exact_number(
            row.get("expires_at_ts"),
            "command_transaction_expires_at_invalid",
        ),
        matrix_identity=MatrixGenerationIdentity.from_mapping(
            _exact_mapping(
                row.get("matrix_identity"),
                "command_transaction_matrix_identity_mapping_required",
            )
        ),
        identity_key=_exact_string(
            row.get("identity_key"),
            "command_transaction_identity_invalid",
        ),
        params=_freeze(dict(row.get("params"))),
        authority=_freeze(dict(row.get("authority"))),
        action_key=key,
        cellular_completion=completion,
    )


def _lease_to_dict(lease: RadioMutationLease) -> dict[str, Any]:
    return {
        "owner": lease.owner,
        "acquired_ts": lease.acquired_ts,
        "expires_ts": lease.expires_ts,
        "transaction": _transaction_to_dict(lease.transaction),
    }


def _lease_from_dict(raw: Mapping[str, Any]) -> RadioMutationLease:
    row = dict(raw or {})
    _exact_fields(
        row,
        frozenset({"owner", "acquired_ts", "expires_ts", "transaction"}),
        "radio_mutation_lease_fields_invalid",
    )
    if not isinstance(row.get("transaction"), Mapping):
        raise TypeError("radio_mutation_lease_transaction_mapping_required")
    return RadioMutationLease(
        owner=_exact_string(
            row.get("owner"), "radio_mutation_lease_owner_invalid"
        ),
        acquired_ts=_exact_number(
            row.get("acquired_ts"),
            "radio_mutation_lease_acquired_ts_invalid",
        ),
        expires_ts=_exact_number(
            row.get("expires_ts"),
            "radio_mutation_lease_expires_ts_invalid",
        ),
        transaction=_transaction_from_dict(dict(row.get("transaction"))),
    )


def _merge_receipt(
    receipts: tuple[tuple[str, Any], ...],
    key: str,
    receipt: Mapping[str, Any] | None,
) -> tuple[tuple[str, Any], ...]:
    rows = dict(_thaw(receipts))
    rows[str(key)] = dict(receipt or {})
    return _freeze(rows)


def _float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed


def _checksum(payload: Mapping[str, Any]) -> str:
    canonical = {
        str(key): value
        for key, value in dict(payload or {}).items()
        if str(key) != "checksum"
    }
    return hashlib.sha256(canonical_json_bytes(canonical)).hexdigest()


__all__ = [
    "CellularCommandAdmission",
    "CellularCompletionContract",
    "CommandAuthority",
    "CommandAuthorityStateRecord",
    "CommandAuthorityStateStore",
    "CommandLifecycle",
    "CommandLifecycleStage",
    "CommandRootState",
    "CommandTerminalReceipt",
    "CommandTerminalStatus",
    "CommandTransaction",
    "CommandTransition",
    "CoreCommandRoot",
    "RadioMutationLease",
    "TRANSACTION_POLICIES",
    "TransactionFamily",
    "TransactionPolicy",
    "build_cellular_transaction",
]
