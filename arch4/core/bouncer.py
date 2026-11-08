"""Independent safety verdicts for actions. Bouncer does not propose or replace actions.
Its single-use receipt is consumed before execution.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import threading
import time
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional
import uuid

from arch4.action_fingerprint import action_semantic_fingerprint
from arch4.canonical_json import canonical_json_bytes
from arch4.action_transaction import (
    ActionTransactionKey,
    action_transaction_key_from_action,
)
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.bouncer_receipt_contract import BouncerReceiptValidation
from arch4.config_views import build_bouncer_config
from arch4.core_contracts.commands import CoreCommandRequest
from arch4.core_contracts.command_safety import (
    CoreCommandOpinion,
    CoreCommandSafetyEvidence,
)
from arch4.core_contracts.accepted_runtime_view import (
    CoreAcceptedRuntimeView,
)
from arch4.interfaces import (
    Arch4Config,
    LeverAction,
    LeverType,
    SystemState,
)


SCHEMA = "bouncer_verdict.v3"
OWNER = "arch4.core.bouncer"
INTENT_PHASE = "intent"
RECEIPT_MAX_AGE_S = 30.0
MAX_INTENT_RECEIPTS = 2048

INTENT_AUTHORITY_DISPOSITIONS: Mapping[str, str] = MappingProxyType({
    "manual": "core_operator_command",
    "restore_ca": "core_runtime_recovery",
    "congested_recovery": "core_runtime_recovery",
    "congested_cell_lock": "core_runtime_recovery",
    "degraded_recovery": "core_runtime_recovery",
    "degraded_cell_lock": "core_runtime_recovery",
    "emergency_recovery": "core_fault_recovery",
    "fault_monitoring": "core_fault_recovery",
    "emergency_restart": "core_fault_recovery",
    "emergency_fast_restart": "core_fault_recovery",
    "emergency_radio_down_recover": "core_fault_recovery",
    "modem_watchdog_recovery": "foundation_automation_recovery",
    "sms_remote_command": "foundation_operator_command",
    "scheduled_band_plan": "foundation_scheduler_command",
    "morning_sequence": "foundation_scheduler_command",
    "night_hold": "foundation_scheduler_command",
    "big_dip": "foundation_scheduler_command",
    "explore": "future_intelligence_policy_boundary",
    "explore_cell_lock": "future_intelligence_policy_boundary",
    "preemptive_congestion": "future_intelligence_policy_boundary",
    "anchor_lock": "future_intelligence_policy_boundary",
    "systematic_probe": "future_intelligence_policy_boundary",
    "systematic_probe_hold": "future_intelligence_policy_boundary",
    "tsar_vector_candidate": "future_intelligence_policy_boundary",
    "smart_loop_candidate": "future_intelligence_policy_boundary",
    "cell_optimization": "future_intelligence_policy_boundary",
    "cell_optimization_bounce": "future_intelligence_policy_boundary",
    "cell_optimization_hook": "future_intelligence_policy_boundary",
})

FUTURE_INTELLIGENCE_INTENTS: frozenset[str] = frozenset(
    intent
    for intent, disposition in INTENT_AUTHORITY_DISPOSITIONS.items()
    if disposition == "future_intelligence_policy_boundary"
)


class BouncerDisposition(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    HOLD = "HOLD"
    HARD_STOP = "HARD_STOP"
    RECOVERY_ONLY = "RECOVERY_ONLY"


def _freeze_details(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(copy.deepcopy(dict(value or {})))


@dataclass(frozen=True)
class BouncerVerdict:
    """Immutable Bouncer observation or single-use intent receipt."""

    disposition: BouncerDisposition
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)
    phase: str = INTENT_PHASE
    receipt_token: str = ""
    action_key: ActionTransactionKey | None = None
    action_fingerprint: str = ""
    latch_epoch: int = 0
    recovery_authorized: bool = False
    issued_at_ts: float = field(default_factory=time.time)
    integrity: str = ""
    schema: str = SCHEMA
    owner: str = OWNER

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", _freeze_details(self.details))
        if self.phase == INTENT_PHASE and self.receipt_token:
            if not isinstance(self.action_key, ActionTransactionKey):
                raise TypeError("bouncer_verdict_action_key_required")
        elif self.action_key is not None and not isinstance(
            self.action_key,
            ActionTransactionKey,
        ):
            raise TypeError("bouncer_verdict_action_key_invalid")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "phase": self.phase,
            "disposition": self.disposition.value,
            "code": self.code,
            "message": self.message,
            "details": copy.deepcopy(dict(self.details)),
            "receipt_token": self.receipt_token,
            "action_key": (
                self.action_key.as_dict()
                if self.action_key is not None
                else None
            ),
            "action_fingerprint": self.action_fingerprint,
            "latch_epoch": int(self.latch_epoch),
            "recovery_authorized": bool(self.recovery_authorized),
            "issued_at_ts": float(self.issued_at_ts),
            "integrity": self.integrity,
        }


@dataclass(frozen=True)
class _HardStopLatch:
    active: bool = False
    reason: str = ""
    identity_key: str = ""
    identity_code: str = ""
    matrix_identity: MatrixGenerationIdentity | None = None
    tripped_at_ts: float = 0.0
    pending_recovery_root_id: str = ""
    safety_available: bool = True


def _verdict_integrity(verdict: BouncerVerdict) -> str:
    payload = verdict.to_dict()
    payload.pop("integrity", None)
    encoded = canonical_json_bytes(payload)
    return hashlib.sha256(encoded).hexdigest()


class CoreBouncer:
    """Own pass, warn, hold, hard-stop, and recovery-only safety results."""

    KNOWN_INTENTS: frozenset[str] = frozenset(INTENT_AUTHORITY_DISPOSITIONS)

    RECOVERY_INTENTS: frozenset[str] = frozenset({
        "emergency_recovery",
        "fault_monitoring",
        "emergency_restart",
        "emergency_fast_restart",
        "emergency_radio_down_recover",
        "modem_watchdog_recovery",
    })

    def __init__(self, cfg: Arch4Config):
        self.cfg = cfg
        self.bouncer_cfg = build_bouncer_config(cfg)
        self._lock = threading.RLock()
        self._runtime_events: list[dict[str, Any]] = []
        self._counts = {
            "intent_total": 0,
            "intent_pass": 0,
            "intent_warn": 0,
            "intent_hold": 0,
            "intent_hard_stop": 0,
            "intent_recovery_only": 0,
            "outcome_total": 0,
            "outcome_pass": 0,
            "outcome_warn": 0,
            "outcome_hold": 0,
            "outcome_hard_stop": 0,
            "receipt_rejected": 0,
        }
        self._last_intent: Optional[BouncerVerdict] = None
        self._last_outcome: Optional[BouncerVerdict] = None
        self._latch = _HardStopLatch()
        self._latch_epoch = 0
        self._intent_receipts: dict[str, dict[str, Any]] = {}
        self._retired_latches: list[dict[str, Any]] = []

    @staticmethod
    def _band_set(values: Any) -> set[int]:
        out: set[int] = set()
        for value in list(values or []):
            try:
                band = int(value)
            except (TypeError, ValueError):
                continue
            if band > 0:
                out.add(band)
        return out

    def refresh_thresholds(self) -> None:
        self.bouncer_cfg = build_bouncer_config(self.cfg)

    def bind_config_generation(
        self,
        config: Arch4Config,
        *,
        generation_id: str,
        config_fingerprint: str,
    ) -> None:
        """Bind the same explicit immutable generation used by Core peers."""

        generation = str(generation_id or "").strip()
        fingerprint = str(config_fingerprint or "").strip()
        if not generation or not fingerprint:
            raise ValueError("bouncer_config_generation_identity_required")
        if str(getattr(config, "generation_id", "") or "") != generation:
            raise RuntimeError("bouncer_config_generation_mismatch")
        if str(getattr(config, "fingerprint", "") or "") != fingerprint:
            raise RuntimeError("bouncer_config_fingerprint_mismatch")
        with self._lock:
            self.cfg = config
            self.bouncer_cfg = build_bouncer_config(config)

    @staticmethod
    def _is_recovery_action(
        action: LeverAction,
    ) -> bool:
        return str(action.intent or "").strip() in CoreBouncer.RECOVERY_INTENTS

    @staticmethod
    def _action_root_id(action: Any) -> str:
        try:
            key = action_transaction_key_from_action(action)
        except Exception:
            return ""
        return str(key.root_id if key is not None else "")

    def _clear_latch(
        self,
        *,
        reason: str,
        action_key: ActionTransactionKey,
    ) -> None:
        if not isinstance(action_key, ActionTransactionKey):
            raise TypeError("bouncer_action_key_required")
        previous = self._latch
        if not previous.active:
            return
        self._latch_epoch += 1
        self._latch = _HardStopLatch()
        self._runtime_events.append({
            "kind": "bouncer_hard_stop_cleared",
            "reason": str(reason or "accepted_recovery_evidence"),
            "previous_reason": previous.reason,
            "action_key": action_key.as_dict(),
            "latch_epoch": self._latch_epoch,
        })

    def _authorize_recovery_root(self, action_root_id: str) -> None:
        if not self._latch.active:
            raise RuntimeError("bouncer_recovery_authorization_without_latch")
        root_id = str(action_root_id or "").strip()
        if not root_id:
            raise ValueError("bouncer_recovery_action_root_missing")
        self._latch = replace(self._latch, pending_recovery_root_id=root_id)

    def _trip_latch(
        self,
        *,
        reason: str,
        identity_key: str,
        identity_code: str,
        matrix_identity: MatrixGenerationIdentity | None,
        safety_available: bool = True,
    ) -> None:
        reason_text = str(reason or "unsafe_outcome")
        identity_text = str(identity_key or "")
        if matrix_identity is not None and not isinstance(
            matrix_identity,
            MatrixGenerationIdentity,
        ):
            raise TypeError("bouncer_latch_matrix_identity_invalid")
        if (
            self._latch.active
            and self._latch.reason == reason_text
            and self._latch.identity_key == identity_text
            and self._latch.identity_code == str(identity_code or "")
            and self._latch.matrix_identity == matrix_identity
        ):
            if not safety_available and self._latch.safety_available:
                self._latch = replace(self._latch, safety_available=False)
            return
        self._latch_epoch += 1
        self._latch = _HardStopLatch(
            active=True,
            reason=reason_text,
            identity_key=identity_text,
            identity_code=str(identity_code or ""),
            matrix_identity=matrix_identity,
            tripped_at_ts=time.time(),
            safety_available=bool(safety_available),
        )
        self._runtime_events.append({
            "kind": "bouncer_hard_stop_latched",
            "reason": self._latch.reason,
            "identity_key": self._latch.identity_key,
            "identity_code": self._latch.identity_code,
            "matrix_identity": (
                self._latch.matrix_identity.as_dict()
                if self._latch.matrix_identity is not None
                else None
            ),
            "latch_epoch": self._latch_epoch,
            "safety_available": self._latch.safety_available,
        })

    def mark_safety_unavailable(
        self,
        *,
        reason: str,
        action_key: ActionTransactionKey,
        accepted_view: CoreAcceptedRuntimeView,
    ) -> None:
        """Latch when the independent outcome observer cannot produce truth."""

        if not isinstance(action_key, ActionTransactionKey):
            raise TypeError("bouncer_action_key_required")
        if not isinstance(accepted_view, CoreAcceptedRuntimeView):
            raise TypeError("bouncer_accepted_runtime_view_required")
        if accepted_view.matrix_identity != action_key.matrix_identity:
            raise RuntimeError("bouncer_action_view_matrix_mismatch")
        with self._lock:
            self._trip_latch(
                reason=str(reason or "bouncer_outcome_evaluation_unavailable"),
                identity_key=action_key.identity_key,
                identity_code=accepted_view.identity_code,
                matrix_identity=action_key.matrix_identity,
                safety_available=False,
            )

    def mark_checkpoint_safety_unavailable(self, *, reason: str) -> None:
        """Fail closed when durable safety state cannot be interpreted."""

        with self._lock:
            self._trip_latch(
                reason=str(reason or "bouncer_checkpoint_safety_unavailable"),
                identity_key="checkpoint_unknown",
                identity_code="",
                matrix_identity=None,
                safety_available=False,
            )

    def _issue_verdict(
        self,
        *,
        disposition: BouncerDisposition,
        code: str,
        message: str,
        details: Mapping[str, Any],
        phase: str,
        action: Optional[LeverAction] = None,
        recovery_authorized: bool = False,
    ) -> BouncerVerdict:
        token = uuid.uuid4().hex if phase == INTENT_PHASE and action is not None else ""
        action_key = (
            action_transaction_key_from_action(action)
            if action is not None
            else None
        )
        verdict = BouncerVerdict(
            disposition=disposition,
            code=str(code or ""),
            message=str(message or ""),
            details=details,
            phase=str(phase or ""),
            receipt_token=token,
            action_key=action_key,
            action_fingerprint=action_semantic_fingerprint(action) if action is not None else "",
            latch_epoch=self._latch_epoch,
            recovery_authorized=bool(recovery_authorized),
        )
        verdict = replace(verdict, integrity=_verdict_integrity(verdict))
        if token:
            self._intent_receipts[token] = {
                "integrity": verdict.integrity,
                "issued_at_ts": verdict.issued_at_ts,
            }
            while len(self._intent_receipts) > MAX_INTENT_RECEIPTS:
                self._intent_receipts.pop(next(iter(self._intent_receipts)))
        return verdict

    def _finish_intent(
        self,
        verdict: BouncerVerdict,
        *,
        action_present: bool,
        accepted_view: CoreAcceptedRuntimeView | None = None,
    ) -> BouncerVerdict:
        if (
            action_present
            and verdict.disposition is BouncerDisposition.HARD_STOP
            and not self._latch.active
        ):
            if not isinstance(accepted_view, CoreAcceptedRuntimeView):
                raise RuntimeError(
                    "bouncer_hard_stop_accepted_view_required"
                )
            if (
                verdict.action_key is None
                or verdict.action_key.matrix_identity
                != accepted_view.matrix_identity
            ):
                raise RuntimeError("bouncer_hard_stop_matrix_mismatch")
            self._trip_latch(
                reason=verdict.code,
                identity_key=(
                    verdict.action_key.identity_key
                    if verdict.action_key is not None
                    else ""
                ),
                identity_code=accepted_view.identity_code,
                matrix_identity=(
                    verdict.action_key.matrix_identity
                    if verdict.action_key is not None
                    else None
                ),
            )
            verdict = replace(
                verdict,
                latch_epoch=self._latch_epoch,
                integrity="",
            )
            verdict = replace(verdict, integrity=_verdict_integrity(verdict))
            if verdict.receipt_token:
                self._intent_receipts[verdict.receipt_token] = {
                    "integrity": verdict.integrity,
                    "issued_at_ts": verdict.issued_at_ts,
                }
        if action_present:
            self._counts["intent_total"] += 1
            self._counts[f"intent_{verdict.disposition.value.lower()}"] += 1
        self._last_intent = verdict
        self._runtime_events.append({"kind": "bouncer_intent", "verdict": verdict.to_dict()})
        return verdict

    def _finish_outcome(self, verdict: BouncerVerdict, *, action_present: bool) -> BouncerVerdict:
        if action_present:
            self._counts["outcome_total"] += 1
            key = f"outcome_{verdict.disposition.value.lower()}"
            if key in self._counts:
                self._counts[key] += 1
        self._last_outcome = verdict
        self._runtime_events.append({"kind": "bouncer_outcome", "verdict": verdict.to_dict()})
        return verdict

    def evaluate_intent(
        self,
        *,
        request: CoreCommandRequest | None,
        action: Optional[LeverAction],
        action_key: ActionTransactionKey | None,
        accepted_view: CoreAcceptedRuntimeView | None,
        observe_only: bool,
        safety_evidence: CoreCommandSafetyEvidence | None,
    ) -> BouncerVerdict:
        raise RuntimeError(
            "bouncer_routine_intent_path_deleted:use_core_command_opinion"
        )

    def evaluate_core_opinion(
        self,
        opinion: CoreCommandOpinion,
        *,
        action_key: ActionTransactionKey | None = None,
    ) -> BouncerVerdict:
        """Own the final autopilot pass/stop after Core states its opinion."""

        if not isinstance(opinion, CoreCommandOpinion):
            raise TypeError("bouncer_core_opinion_required")
        if action_key is not None:
            if not isinstance(action_key, ActionTransactionKey):
                raise TypeError("bouncer_core_opinion_action_key_invalid")
            if (
                opinion.request_id != action_key.command_id
                or opinion.matrix_identity != action_key.matrix_identity
            ):
                raise RuntimeError("bouncer_core_opinion_scope_mismatch")
        with self._lock:
            emergency_stop = bool(
                opinion.allowed is False and opinion.critical is True
            )
            details: dict[str, Any] = {
                "core_opinion_reason": opinion.reason,
                "critical": opinion.critical,
                "request_id": opinion.request_id,
                "matrix_identity": opinion.matrix_identity.as_dict(),
            }
            if action_key is not None:
                details["action_key"] = action_key.as_dict()
            return self._finish_outcome(
                self._issue_verdict(
                    disposition=(
                        BouncerDisposition.HARD_STOP
                        if emergency_stop
                        else BouncerDisposition.PASS
                    ),
                    code=(
                        "autopilot_emergency_stop"
                        if emergency_stop
                        else "autopilot_core_opinion_observed"
                    ),
                    message=(
                        "Core reports a critical emergency; this command is stopped"
                        if emergency_stop
                        else "Core opinion recorded; no emergency stop is required"
                    ),
                    details=details,
                    phase="core_opinion",
                ),
                action_present=True,
            )

    def _validate_intent_receipt(
        self,
        verdict: Any,
        *,
        action: Any,
        action_key: ActionTransactionKey,
        consume: bool,
        now: float,
    ) -> BouncerReceiptValidation:
        raise RuntimeError(
            "bouncer_intent_receipt_path_deleted:use_core_command_opinion"
        )

    def validate_intent_receipt(
        self,
        verdict: Any,
        *,
        action: Any,
        action_key: ActionTransactionKey,
        now: float | None = None,
    ) -> BouncerReceiptValidation:
        raise RuntimeError(
            "bouncer_intent_receipt_path_deleted:use_core_command_opinion"
        )

    def consume_intent_receipt(
        self,
        verdict: Any,
        *,
        action: Any,
        action_key: ActionTransactionKey,
        now: float | None = None,
    ) -> BouncerReceiptValidation:
        raise RuntimeError(
            "bouncer_intent_receipt_path_deleted:use_core_command_opinion"
        )

    def evaluate_outcome(self, *args: Any, **kwargs: Any) -> BouncerVerdict:
        raise RuntimeError(
            "bouncer_legacy_outcome_path_deleted_use_core_command_outcome_stream"
        )

    def drain_runtime_events(self) -> list[dict]:
        with self._lock:
            out = list(self._runtime_events)
            self._runtime_events.clear()
            return out

    def get_runtime_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "schema": SCHEMA,
                "owner": OWNER,
                "counts": dict(self._counts),
                "last_intent": self._last_intent.to_dict() if self._last_intent else None,
                "last_outcome": self._last_outcome.to_dict() if self._last_outcome else None,
                "hard_stop_latch": {
                    "active": self._latch.active,
                    "reason": self._latch.reason,
                    "identity_key": self._latch.identity_key,
                    "identity_code": self._latch.identity_code,
                    "matrix_identity": (
                        self._latch.matrix_identity.as_dict()
                        if self._latch.matrix_identity is not None
                        else None
                    ),
                    "tripped_at_ts": self._latch.tripped_at_ts,
                    "epoch": self._latch_epoch,
                    "pending_recovery_root_id": self._latch.pending_recovery_root_id,
                    "safety_available": self._latch.safety_available,
                },
                "retired_hard_stop_latches": copy.deepcopy(
                    self._retired_latches[-16:]
                ),
                "pending_intent_receipts": len(self._intent_receipts),
            }

    def checkpoint_payload(self) -> Dict[str, Any]:
        """Persist only durable fuse state; one-shot receipts never survive restart."""

        with self._lock:
            return {
                "schema": "BouncerSafetyState.v3",
                "owner": OWNER,
                "latch_epoch": int(self._latch_epoch),
                "hard_stop_latch": {
                    "active": bool(self._latch.active),
                    "reason": self._latch.reason,
                    "identity_key": self._latch.identity_key,
                    "identity_code": self._latch.identity_code,
                    "matrix_identity": (
                        self._latch.matrix_identity.as_dict()
                        if self._latch.matrix_identity is not None
                        else None
                    ),
                    "tripped_at_ts": float(self._latch.tripped_at_ts),
                    "pending_recovery_root_id": self._latch.pending_recovery_root_id,
                    "safety_available": bool(self._latch.safety_available),
                },
                "retired_hard_stop_latches": copy.deepcopy(
                    self._retired_latches[-16:]
                ),
            }

    def restore_checkpoint(self, payload: Mapping[str, Any]) -> bool:
        with self._lock:
            if not isinstance(payload, Mapping):
                raise TypeError("bouncer_safety_checkpoint_mapping_required")
            row = dict(payload)
            if set(row) != {
                "schema",
                "owner",
                "latch_epoch",
                "hard_stop_latch",
                "retired_hard_stop_latches",
            }:
                raise ValueError("bouncer_safety_checkpoint_fields_invalid")
            if row.get("schema") != "BouncerSafetyState.v3" or row.get("owner") != OWNER:
                raise ValueError("bouncer_safety_checkpoint_contract_invalid")
            latch_epoch = row.get("latch_epoch")
            if type(latch_epoch) is not int or latch_epoch < 0:
                raise TypeError("bouncer_safety_checkpoint_epoch_invalid")
            latch_value = row.get("hard_stop_latch")
            if not isinstance(latch_value, Mapping):
                raise TypeError("bouncer_safety_checkpoint_latch_mapping_required")
            latch = dict(latch_value)
            if set(latch) != {
                "active",
                "reason",
                "identity_key",
                "identity_code",
                "matrix_identity",
                "tripped_at_ts",
                "pending_recovery_root_id",
                "safety_available",
            }:
                raise ValueError("bouncer_safety_checkpoint_latch_fields_invalid")
            for name in (
                "reason",
                "identity_key",
                "identity_code",
                "pending_recovery_root_id",
            ):
                if not isinstance(latch.get(name), str):
                    raise TypeError(
                        f"bouncer_safety_checkpoint_{name}_string_required"
                    )
            if type(latch.get("active")) is not bool or type(
                latch.get("safety_available")
            ) is not bool:
                raise TypeError("bouncer_safety_checkpoint_bool_required")
            matrix_payload = latch.get("matrix_identity")
            if matrix_payload is None:
                matrix_identity = None
            elif isinstance(matrix_payload, Mapping):
                matrix_identity = MatrixGenerationIdentity.from_mapping(
                    matrix_payload
                )
            else:
                raise TypeError(
                    "bouncer_safety_checkpoint_matrix_identity_invalid"
                )
            if (
                latch.get("active") is True
                and matrix_identity is None
                and latch.get("identity_key") != "checkpoint_unknown"
            ):
                raise ValueError(
                    "bouncer_safety_checkpoint_active_matrix_required"
                )
            tripped_at_ts = latch.get("tripped_at_ts")
            if (
                isinstance(tripped_at_ts, bool)
                or not isinstance(tripped_at_ts, (int, float))
                or float(tripped_at_ts) < 0.0
            ):
                raise TypeError("bouncer_safety_checkpoint_timestamp_invalid")
            retired_value = row.get("retired_hard_stop_latches")
            if not isinstance(retired_value, list) or any(
                not isinstance(item, Mapping) for item in retired_value
            ):
                raise TypeError(
                    "bouncer_safety_checkpoint_retired_latches_invalid"
                )
            self._latch_epoch = latch_epoch
            self._latch = _HardStopLatch(
                active=latch["active"],
                reason=latch["reason"],
                identity_key=latch["identity_key"],
                identity_code=latch["identity_code"],
                matrix_identity=matrix_identity,
                tripped_at_ts=float(tripped_at_ts),
                pending_recovery_root_id=latch["pending_recovery_root_id"],
                safety_available=latch["safety_available"],
            )
            self._retired_latches = [
                dict(item)
                for item in retired_value[-16:]
            ]
            self._intent_receipts.clear()
            return True

__all__ = [
    "CoreBouncer",
    "BouncerDisposition",
    "BouncerVerdict",
    "FUTURE_INTELLIGENCE_INTENTS",
    "INTENT_AUTHORITY_DISPOSITIONS",
    "OWNER",
    "SCHEMA",
]
