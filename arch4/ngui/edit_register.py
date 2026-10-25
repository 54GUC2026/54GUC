"""Own NGUI operator drafts and accepted readback without a GUI toolkit."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from time import monotonic
from types import MappingProxyType
from typing import Any, Callable, Mapping

from arch4.operational_service_contracts import (
    DOWNSTREAM_HEALTH_INTERVAL_MAX_S,
    DOWNSTREAM_HEALTH_INTERVAL_MIN_S,
)

from .front_register import PresentationState, PresentationValue


EDIT_PENDING_TIMEOUT_S = 90.0
TRAFFIC_PLAN_GROUP = "traffic_plan"
PING_GROUP = "ping"
SMS_GROUP = "sms"
SMS_SENDER_GROUP = "sms_sender"


@dataclass(frozen=True, slots=True)
class EditFieldSpec:
    field_id: str
    group: str
    payload_key: str
    default: Any
    value_kind: str = "number"
    minimum: float | None = None
    maximum: float | None = None
    tolerance: float = 0.0
    integer: bool = False
    source_field_id: str = ""


EDIT_FIELD_SPECS: Mapping[str, EditFieldSpec] = MappingProxyType(
    {
        spec.field_id: spec
        for spec in (
            EditFieldSpec(
                "traffic.plan_limit_gb",
                TRAFFIC_PLAN_GROUP,
                "limit_gb",
                1000.0,
                minimum=0.0,
                maximum=100_000.0,
            ),
            EditFieldSpec(
                "traffic.plan_cycle_day",
                TRAFFIC_PLAN_GROUP,
                "cycle_day",
                1.0,
                minimum=1.0,
                maximum=31.0,
                integer=True,
            ),
            EditFieldSpec(
                "command.ping_enabled",
                PING_GROUP,
                "enabled",
                False,
                value_kind="bool",
                source_field_id="command.ping_enabled",
            ),
            EditFieldSpec(
                "command.ping_host",
                PING_GROUP,
                "host",
                "",
                value_kind="text",
                source_field_id="command.ping_host",
            ),
            EditFieldSpec(
                "command.ping_count",
                PING_GROUP,
                "count",
                1.0,
                minimum=1.0,
                maximum=10.0,
                integer=True,
                source_field_id="command.ping_count",
            ),
            EditFieldSpec(
                "command.ping_every_s",
                PING_GROUP,
                "every_s",
                30.0,
                minimum=DOWNSTREAM_HEALTH_INTERVAL_MIN_S,
                maximum=DOWNSTREAM_HEALTH_INTERVAL_MAX_S,
                integer=True,
                source_field_id="command.ping_every_s",
            ),
            EditFieldSpec(
                "command.sms_enabled",
                SMS_GROUP,
                "enabled",
                False,
                value_kind="bool",
                source_field_id="command.sms_enabled",
            ),
            EditFieldSpec(
                "command.sms_keyword",
                SMS_GROUP,
                "keyword",
                "reboot",
                value_kind="text",
                source_field_id="command.sms_keyword",
            ),
            EditFieldSpec(
                "command.sms_every_s",
                SMS_GROUP,
                "every_s",
                121.0,
                minimum=121.0,
                maximum=3600.0,
                integer=True,
                source_field_id="command.sms_every_s",
            ),
            EditFieldSpec(
                "command.sms_sender",
                SMS_SENDER_GROUP,
                "sender",
                "",
                value_kind="text",
            ),
        )
    }
)


@dataclass(frozen=True, slots=True)
class PendingEdit:
    request_id: str
    group: str
    submitted_at: float
    expected: Mapping[str, Any] | None
    base_sequences: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ExternalPendingEdit:
    request_id: str
    group: str
    scope_id: str
    submitted_at: float
    expected: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EditTerminalReceipt:
    request_id: str
    group: str
    state: str
    requested_at: float
    completed_at: float
    reason: str

    def __post_init__(self) -> None:
        if not str(self.request_id or "").strip():
            raise ValueError("edit terminal request identity required")
        if not str(self.group or "").strip():
            raise ValueError("edit terminal group required")
        if self.state not in {"accepted", "failed", "cancelled"}:
            raise ValueError("edit terminal state invalid")
        if self.requested_at <= 0 or self.completed_at < self.requested_at:
            raise ValueError("edit terminal timestamps invalid")
        if not str(self.reason or "").strip():
            raise ValueError("edit terminal reason required")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "NGUIEditTerminalReceipt.v1",
            "owner": "54guc.ngui.edit_register",
            "request_id": self.request_id,
            "group": self.group,
            "state": self.state,
            "requested_at": self.requested_at,
            "completed_at": self.completed_at,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class EditRegisterSnapshot:
    generation: int
    visible: Mapping[str, Any]
    accepted: Mapping[str, Any]
    dirty_fields: frozenset[str]
    invalid_fields: Mapping[str, str]
    value_origins: Mapping[str, str]
    pending_groups: frozenset[str]
    scopes: Mapping[str, str]
    terminal_receipts: Mapping[str, EditTerminalReceipt]


@dataclass(frozen=True, slots=True)
class NGUIEditReconcileCandidate:
    """Prepared Edit state that does not change the live owner."""

    previous: EditRegisterSnapshot
    snapshot: EditRegisterSnapshot
    previous_revision: int
    revision: int
    accepted: Mapping[str, Any]
    accepted_sequences: Mapping[str, int]
    drafts: Mapping[str, Any]
    invalid_drafts: Mapping[str, str]
    pending: Mapping[str, PendingEdit]
    external_pending: Mapping[str, ExternalPendingEdit]
    scopes: Mapping[str, str]
    terminal_receipts: Mapping[str, EditTerminalReceipt]
    request_sequence: int
    _issuer: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.previous, EditRegisterSnapshot) or not isinstance(
            self.snapshot,
            EditRegisterSnapshot,
        ):
            raise TypeError("ngui_edit_reconcile_candidate_snapshot_required")
        if (
            type(self.previous_revision) is not int
            or self.previous_revision < 0
            or type(self.revision) is not int
            or self.revision != self.previous_revision + 1
        ):
            raise ValueError(
                "ngui_edit_reconcile_candidate_revision_invalid"
            )
        for name in (
            "accepted",
            "accepted_sequences",
            "drafts",
            "invalid_drafts",
            "pending",
            "external_pending",
            "scopes",
            "terminal_receipts",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise TypeError(
                    f"ngui_edit_reconcile_candidate_{name}_mapping_required"
                )
            object.__setattr__(
                self,
                name,
                MappingProxyType(dict(value)),
            )
        if type(self.request_sequence) is not int or self.request_sequence < 0:
            raise ValueError(
                "ngui_edit_reconcile_candidate_request_sequence_invalid"
            )


class NGUIEditRegister:
    """Own drafts until settings or runtime readback accepts them."""

    def __init__(
        self,
        *,
        pending_timeout_s: float = EDIT_PENDING_TIMEOUT_S,
        clock: Callable[[], float] = monotonic,
        lock: Any | None = None,
    ) -> None:
        if pending_timeout_s <= 0:
            raise ValueError("edit pending timeout must be positive")
        if not callable(clock):
            raise TypeError("edit register clock must be callable")
        self._pending_timeout_s = float(pending_timeout_s)
        self._clock = clock
        self._lock = lock if lock is not None else RLock()
        self._generation = 0
        self._revision = 0
        self._accepted: dict[str, Any] = {}
        self._accepted_sequences: dict[str, int] = {}
        self._drafts: dict[str, Any] = {}
        self._invalid_drafts: dict[str, str] = {}
        self._pending: dict[str, PendingEdit] = {}
        self._external_pending: dict[str, ExternalPendingEdit] = {}
        self._scopes: dict[str, str] = {}
        self._terminal_receipts: dict[str, EditTerminalReceipt] = {}
        self._request_sequence = 0
        self._candidate_issuer = object()

    def set_draft(self, field_id: str, value: Any) -> EditRegisterSnapshot:
        spec = self._spec(field_id)
        normalized = self._normalize(spec, value)
        with self._lock:
            changed = self._invalid_drafts.pop(field_id, None) is not None
            if self._drafts.get(field_id) != normalized:
                self._drafts[field_id] = normalized
                changed = True
            if changed:
                self._advance_generation_unlocked()
            return self._snapshot_unlocked()

    def reset_runtime_state(self) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """Clear drafts, accepted readback, and late completion IDs."""

        with self._lock:
            prior = self._reset_observation_unlocked()
            self._generation = 0
            self._revision += 1
            self._accepted = {}
            self._accepted_sequences = {}
            self._drafts = {}
            self._invalid_drafts = {}
            self._pending = {}
            self._external_pending = {}
            self._scopes = {}
            self._terminal_receipts = {}
            self._request_sequence = 0
            self._candidate_issuer = object()
            result = self._reset_observation_unlocked()
            return MappingProxyType(prior), MappingProxyType(result)

    def set_invalid_draft(
        self,
        field_id: str,
        *,
        reason: str,
    ) -> EditRegisterSnapshot:
        """Keep an invalid editor state instead of reusing an older value."""

        self._spec(field_id)
        detail = str(reason or "").strip()
        if not detail:
            raise ValueError("invalid edit draft requires reason")
        with self._lock:
            changed = self._invalid_drafts.get(field_id) != detail
            changed = self._drafts.pop(field_id, None) is not None or changed
            self._invalid_drafts[field_id] = detail
            if changed:
                self._advance_generation_unlocked()
            return self._snapshot_unlocked()

    def bind_scope(
        self,
        group: str,
        *,
        scope_id: str,
        accepted: Mapping[str, Any],
    ) -> EditRegisterSnapshot:
        """Adopt a settings scope and clear drafts from the old scope."""

        scope = str(scope_id or "").strip()
        if not scope:
            raise ValueError(f"{group} edit scope is required")
        specs = self._group_specs(group)
        expected_fields = {spec.field_id for spec in specs}
        if set(accepted) != expected_fields:
            raise ValueError(
                f"{group} scope seed must exactly match declared fields: "
                f"missing={sorted(expected_fields - set(accepted))} "
                f"extra={sorted(set(accepted) - expected_fields)}"
            )
        normalized = {
            spec.field_id: self._normalize(spec, accepted[spec.field_id])
            for spec in specs
        }
        with self._lock:
            if self._scopes.get(group) == scope:
                return self._snapshot_unlocked()
            now = self._now_unlocked()
            for spec in specs:
                self._accepted[spec.field_id] = normalized[spec.field_id]
                self._accepted_sequences.pop(spec.field_id, None)
                self._drafts.pop(spec.field_id, None)
                self._invalid_drafts.pop(spec.field_id, None)
            self._cancel_pending_unlocked(
                group,
                completed_at=now,
                reason="edit_scope_replaced",
            )
            self._scopes[group] = scope
            self._advance_generation_unlocked()
            return self._snapshot_unlocked()

    def scope(self, group: str) -> str:
        self._group_specs(group)
        with self._lock:
            return self._scopes.get(group, "")

    def clear_scope(self, group: str) -> EditRegisterSnapshot:
        """Reset a draft group to neutral defaults between sessions."""

        specs = self._group_specs(group)
        with self._lock:
            now = self._now_unlocked()
            changed = bool(self._scopes.pop(group, ""))
            changed = self._cancel_pending_unlocked(
                group,
                completed_at=now,
                reason="edit_scope_cleared",
            ) or changed
            for spec in specs:
                changed = self._accepted.pop(spec.field_id, None) is not None or changed
                self._accepted_sequences.pop(spec.field_id, None)
                changed = self._drafts.pop(spec.field_id, None) is not None or changed
                changed = self._invalid_drafts.pop(spec.field_id, None) is not None or changed
            if changed:
                self._advance_generation_unlocked()
            return self._snapshot_unlocked()

    def payload(self, group: str) -> Mapping[str, Any]:
        specs = self._group_specs(group)
        with self._lock:
            invalid = {
                spec.field_id: self._invalid_drafts[spec.field_id]
                for spec in specs
                if spec.field_id in self._invalid_drafts
            }
            if invalid:
                raise ValueError(
                    f"edit group {group} has invalid draft: "
                    + ",".join(
                        f"{field_id}={reason}"
                        for field_id, reason in sorted(invalid.items())
                    )
                )
            return self._payload_unlocked(group, specs)

    def request_apply(self, group: str) -> Mapping[str, Any]:
        specs = self._group_specs(group)
        readback_specs = tuple(spec for spec in specs if spec.source_field_id)
        if not readback_specs:
            raise RuntimeError(f"edit group {group} has no canonical readback")
        with self._lock:
            now = self._now_unlocked()
            payload = self._payload_unlocked(group, specs)
            visible = self._visible_unlocked()
            request_id = self._next_request_id_unlocked(group)
            self._pending[group] = PendingEdit(
                request_id=request_id,
                group=group,
                submitted_at=float(now),
                expected=MappingProxyType(
                    {spec.field_id: visible[spec.field_id] for spec in readback_specs}
                ),
                base_sequences=MappingProxyType(
                    {
                        spec.field_id: self._accepted_sequences.get(spec.field_id, 0)
                        for spec in readback_specs
                    }
                ),
            )
            self._advance_generation_unlocked()
            return payload

    def request_external_apply(
        self,
        group: str,
    ) -> Mapping[str, Any]:
        """Keep a settings draft until its owner returns a receipt."""

        specs = self._group_specs(group)
        if any(spec.source_field_id for spec in specs):
            raise RuntimeError(
                f"edit group {group} requires runtime readback, not external receipt"
            )
        with self._lock:
            now = self._now_unlocked()
            scope_id = self._scopes.get(group, "")
            if not scope_id:
                raise RuntimeError(f"external edit group {group} has no active scope")
            if group in self._external_pending:
                raise RuntimeError(
                    f"external edit group {group} already has a pending request"
                )
            visible = self._visible_unlocked()
            expected = {
                spec.field_id: self._normalize(spec, visible[spec.field_id])
                for spec in specs
            }
            self._external_pending[group] = ExternalPendingEdit(
                request_id=self._next_request_id_unlocked(group),
                group=group,
                scope_id=scope_id,
                submitted_at=float(now),
                expected=MappingProxyType(expected),
            )
            self._advance_generation_unlocked()
            return self._payload_unlocked(group, specs)

    def accept_external(
        self,
        group: str,
        *,
        scope_id: str,
        accepted: Mapping[str, Any],
        completed_at: float,
    ) -> EditRegisterSnapshot:
        """Accept only the matching final receipt for a pending draft."""

        specs = self._group_specs(group)
        expected_fields = {spec.field_id for spec in specs}
        if set(accepted) != expected_fields:
            raise ValueError(
                f"{group} external receipt fields mismatch: "
                f"missing={sorted(expected_fields - set(accepted))} "
                f"extra={sorted(set(accepted) - expected_fields)}"
            )
        normalized = {
            spec.field_id: self._normalize(spec, accepted[spec.field_id])
            for spec in specs
        }
        scope = str(scope_id or "").strip()
        with self._lock:
            pending = self._external_pending.get(group)
            if pending is None:
                raise RuntimeError(f"{group} external receipt has no pending request")
            if scope != pending.scope_id or scope != self._scopes.get(group, ""):
                raise RuntimeError(f"{group} external receipt scope mismatch")
            for spec in specs:
                if not self._matches(
                    spec,
                    normalized[spec.field_id],
                    pending.expected[spec.field_id],
                ):
                    raise RuntimeError(f"{group} external receipt value mismatch")
            for spec in specs:
                value = normalized[spec.field_id]
                self._accepted[spec.field_id] = value
                self._accepted_sequences.pop(spec.field_id, None)
                if (
                    spec.field_id in self._drafts
                    and self._matches(spec, self._drafts[spec.field_id], value)
                ):
                    self._drafts.pop(spec.field_id, None)
            self._external_pending.pop(group, None)
            self._terminal_receipts[group] = EditTerminalReceipt(
                request_id=pending.request_id,
                group=group,
                state="accepted",
                requested_at=pending.submitted_at,
                completed_at=float(completed_at),
                reason="canonical_owner_receipt",
            )
            self._advance_generation_unlocked()
            return self._snapshot_unlocked()

    def reject_external(
        self,
        group: str,
        *,
        completed_at: float,
        reason: str,
    ) -> EditRegisterSnapshot:
        """End a failed external request without clearing the user's draft."""

        self._group_specs(group)
        with self._lock:
            pending = self._external_pending.pop(group, None)
            if pending is not None:
                self._terminal_receipts[group] = EditTerminalReceipt(
                    request_id=pending.request_id,
                    group=group,
                    state="failed",
                    requested_at=pending.submitted_at,
                    completed_at=float(completed_at),
                    reason=str(reason or "").strip(),
                )
                self._advance_generation_unlocked()
            return self._snapshot_unlocked()

    def reconcile(
        self,
        fields: Mapping[str, PresentationValue],
    ) -> EditRegisterSnapshot:
        candidate = self.prepare_reconcile(fields)
        return self.commit_reconcile(candidate)

    def prepare_reconcile(
        self,
        fields: Mapping[str, PresentationValue],
    ) -> NGUIEditReconcileCandidate:
        """Prepare readback reconciliation on an isolated owner copy."""

        if not isinstance(fields, Mapping):
            raise TypeError("ngui_edit_reconcile_fields_mapping_required")
        with self._lock:
            previous = self._snapshot_unlocked()
            previous_revision = self._revision
            now = self._now_unlocked()
            candidate_owner = NGUIEditRegister(
                pending_timeout_s=self._pending_timeout_s,
                clock=lambda: now,
            )
            candidate_owner._generation = self._generation
            candidate_owner._revision = self._revision
            candidate_owner._accepted = dict(self._accepted)
            candidate_owner._accepted_sequences = dict(
                self._accepted_sequences
            )
            candidate_owner._drafts = dict(self._drafts)
            candidate_owner._invalid_drafts = dict(
                self._invalid_drafts
            )
            candidate_owner._pending = dict(self._pending)
            candidate_owner._external_pending = dict(
                self._external_pending
            )
            candidate_owner._scopes = dict(self._scopes)
            candidate_owner._terminal_receipts = dict(
                self._terminal_receipts
            )
            candidate_owner._request_sequence = self._request_sequence
        snapshot = candidate_owner._reconcile_live(fields)
        candidate_owner._revision = previous_revision + 1
        return NGUIEditReconcileCandidate(
            previous=previous,
            snapshot=snapshot,
            previous_revision=previous_revision,
            revision=candidate_owner._revision,
            accepted=candidate_owner._accepted,
            accepted_sequences=candidate_owner._accepted_sequences,
            drafts=candidate_owner._drafts,
            invalid_drafts=candidate_owner._invalid_drafts,
            pending=candidate_owner._pending,
            external_pending=candidate_owner._external_pending,
            scopes=candidate_owner._scopes,
            terminal_receipts=candidate_owner._terminal_receipts,
            request_sequence=candidate_owner._request_sequence,
            _issuer=self._candidate_issuer,
        )

    def commit_reconcile(
        self,
        candidate: NGUIEditReconcileCandidate,
    ) -> EditRegisterSnapshot:
        """Install prepared reconciliation made from the current base."""

        if not isinstance(candidate, NGUIEditReconcileCandidate):
            raise TypeError("ngui_edit_reconcile_candidate_required")
        with self._lock:
            if candidate._issuer is not self._candidate_issuer:
                raise RuntimeError(
                    "ngui_edit_reconcile_candidate_foreign_owner"
                )
            if (
                self._revision != candidate.previous_revision
                or self._snapshot_unlocked() != candidate.previous
            ):
                raise RuntimeError("ngui_edit_reconcile_candidate_stale")
            candidate_owner = NGUIEditRegister(
                pending_timeout_s=self._pending_timeout_s,
                clock=self._clock,
            )
            candidate_owner._generation = candidate.snapshot.generation
            candidate_owner._revision = candidate.revision
            candidate_owner._accepted = dict(candidate.accepted)
            candidate_owner._accepted_sequences = dict(
                candidate.accepted_sequences
            )
            candidate_owner._drafts = dict(candidate.drafts)
            candidate_owner._invalid_drafts = dict(
                candidate.invalid_drafts
            )
            candidate_owner._pending = dict(candidate.pending)
            candidate_owner._external_pending = dict(
                candidate.external_pending
            )
            candidate_owner._scopes = dict(candidate.scopes)
            candidate_owner._terminal_receipts = dict(
                candidate.terminal_receipts
            )
            candidate_owner._request_sequence = candidate.request_sequence
            if candidate_owner._snapshot_unlocked() != candidate.snapshot:
                raise RuntimeError(
                    "ngui_edit_reconcile_candidate_changed"
                )
            self._generation = candidate.snapshot.generation
            self._revision = candidate.revision
            self._accepted = dict(candidate.accepted)
            self._accepted_sequences = dict(
                candidate.accepted_sequences
            )
            self._drafts = dict(candidate.drafts)
            self._invalid_drafts = dict(candidate.invalid_drafts)
            self._pending = dict(candidate.pending)
            self._external_pending = dict(candidate.external_pending)
            self._scopes = dict(candidate.scopes)
            self._terminal_receipts = dict(
                candidate.terminal_receipts
            )
            self._request_sequence = candidate.request_sequence
            return candidate.snapshot

    def _reconcile_live(
        self,
        fields: Mapping[str, PresentationValue],
    ) -> EditRegisterSnapshot:
        changed = False
        with self._lock:
            now = self._now_unlocked()
            for field_id, spec in EDIT_FIELD_SPECS.items():
                if not spec.source_field_id:
                    continue
                field = fields.get(spec.source_field_id)
                if field is None or field.state not in {
                    PresentationState.VALID,
                    PresentationState.HELD,
                }:
                    continue
                try:
                    normalized = self._normalize(spec, field.value)
                except (TypeError, ValueError):
                    continue
                sequence = int(field.source_sequence or 0)
                if (
                    self._accepted.get(field_id) != normalized
                    or self._accepted_sequences.get(field_id) != sequence
                ):
                    self._accepted[field_id] = normalized
                    self._accepted_sequences[field_id] = sequence
                    changed = True

            for group, pending in tuple(self._pending.items()):
                if self._pending_complete_unlocked(pending):
                    self._complete_pending_unlocked(group, pending, now=now)
                    changed = True
                    continue
                if now - pending.submitted_at >= self._pending_timeout_s:
                    self._timeout_pending_unlocked(group, pending, now=now)
                    changed = True
            for group, pending in tuple(self._external_pending.items()):
                if now - pending.submitted_at >= self._pending_timeout_s:
                    self._external_pending.pop(group, None)
                    self._terminal_receipts[group] = EditTerminalReceipt(
                        request_id=pending.request_id,
                        group=group,
                        state="failed",
                        requested_at=pending.submitted_at,
                        completed_at=float(now),
                        reason="canonical_owner_receipt_timeout",
                    )
                    changed = True
            if changed:
                self._advance_generation_unlocked()
            return self._snapshot_unlocked()

    def snapshot(self) -> EditRegisterSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    def lifecycle_payload(self) -> Mapping[str, Any]:
        """Return typed edit lifecycle data for PageTruth."""

        with self._lock:
            return self.lifecycle_payload_from_snapshot(
                self._snapshot_unlocked()
            )

    @staticmethod
    def lifecycle_payload_from_snapshot(
        snapshot: EditRegisterSnapshot,
    ) -> Mapping[str, Any]:
        """Build lifecycle data from an Edit snapshot without changing it."""

        if not isinstance(snapshot, EditRegisterSnapshot):
            raise TypeError("ngui_edit_lifecycle_snapshot_required")
        groups: dict[str, dict[str, Any]] = {}
        declared_groups = sorted(
            {spec.group for spec in EDIT_FIELD_SPECS.values()}
        )
        for group in declared_groups:
            group_fields = {
                spec.field_id
                for spec in EDIT_FIELD_SPECS.values()
                if spec.group == group
            }
            invalid = {
                field_id: reason
                for field_id, reason in snapshot.invalid_fields.items()
                if field_id in group_fields
            }
            receipt = snapshot.terminal_receipts.get(group)
            if invalid:
                state = "invalid"
                message = "Check the highlighted entry."
            elif group in snapshot.pending_groups:
                state = "pending"
                message = "Waiting for confirmation."
            elif receipt is not None:
                state = receipt.state
                message = {
                    "accepted": "Saved.",
                    "failed": "Not saved.",
                    "cancelled": "Cancelled.",
                }[receipt.state]
            else:
                state = "draft"
                message = ""
            groups[group] = {
                "state": state,
                "message": message,
                "invalid_fields": dict(invalid),
                "terminal": receipt.as_dict() if receipt is not None else {},
            }
        return MappingProxyType(
            {
                "schema": "NGUIEditLifecycle.v1",
                "owner": "54guc.ngui.edit_register",
                "generation": snapshot.generation,
                "groups": groups,
            }
        )

    def _payload_unlocked(
        self,
        group: str,
        specs: tuple[EditFieldSpec, ...],
    ) -> Mapping[str, Any]:
        invalid = {
            spec.field_id: self._invalid_drafts[spec.field_id]
            for spec in specs
            if spec.field_id in self._invalid_drafts
        }
        if invalid:
            raise ValueError(
                f"edit group {group} has invalid draft: "
                + ",".join(
                    f"{field_id}={reason}"
                    for field_id, reason in sorted(invalid.items())
                )
            )
        visible = self._visible_unlocked()
        missing = [spec.field_id for spec in specs if spec.field_id not in visible]
        if missing:
            raise RuntimeError(f"edit group {group} has no accepted or draft values: {missing}")
        return MappingProxyType(
            {
                spec.payload_key: self._normalize(spec, visible[spec.field_id])
                for spec in specs
            }
        )

    def _pending_complete_unlocked(self, pending: PendingEdit) -> bool:
        specs = tuple(
            spec
            for spec in self._group_specs(pending.group)
            if spec.source_field_id
        )
        if pending.expected is None:
            return all(
                self._accepted_sequences.get(spec.field_id, 0)
                > pending.base_sequences.get(spec.field_id, 0)
                for spec in specs
            )
        return all(
            self._accepted_sequences.get(spec.field_id, 0)
            > pending.base_sequences.get(spec.field_id, 0)
            and spec.field_id in self._accepted
            and self._matches(
                spec,
                self._accepted[spec.field_id],
                pending.expected[spec.field_id],
            )
            for spec in specs
        )

    def _complete_pending_unlocked(
        self,
        group: str,
        pending: PendingEdit,
        *,
        now: float,
    ) -> None:
        self._pending.pop(group, None)
        self._terminal_receipts[group] = EditTerminalReceipt(
            request_id=pending.request_id,
            group=group,
            state="accepted",
            requested_at=pending.submitted_at,
            completed_at=float(now),
            reason="canonical_runtime_readback",
        )
        if pending.expected is None:
            return
        for spec in self._group_specs(group):
            if spec.field_id not in self._drafts or spec.field_id not in pending.expected:
                continue
            if self._matches(
                spec,
                self._drafts[spec.field_id],
                pending.expected[spec.field_id],
            ):
                self._drafts.pop(spec.field_id, None)

    def _timeout_pending_unlocked(
        self,
        group: str,
        pending: PendingEdit,
        *,
        now: float,
    ) -> None:
        self._pending.pop(group, None)
        self._terminal_receipts[group] = EditTerminalReceipt(
            request_id=pending.request_id,
            group=group,
            state="failed",
            requested_at=pending.submitted_at,
            completed_at=float(now),
            reason="canonical_runtime_readback_timeout",
        )

    def _cancel_pending_unlocked(
        self,
        group: str,
        *,
        completed_at: float,
        reason: str,
    ) -> bool:
        pending = self._pending.pop(group, None)
        external = self._external_pending.pop(group, None)
        record = external or pending
        if record is None:
            return False
        terminal_at = max(float(completed_at), float(record.submitted_at))
        self._terminal_receipts[group] = EditTerminalReceipt(
            request_id=record.request_id,
            group=group,
            state="cancelled",
            requested_at=record.submitted_at,
            completed_at=terminal_at,
            reason=reason,
        )
        return True

    def _next_request_id_unlocked(self, group: str) -> str:
        self._request_sequence += 1
        return f"ngui-edit-{group}-{self._request_sequence}"

    def _advance_generation_unlocked(self) -> None:
        self._generation += 1
        self._revision += 1

    def _now_unlocked(self) -> float:
        now = float(self._clock())
        if now <= 0:
            raise ValueError("edit register clock must return a positive timestamp")
        return now

    def _visible_unlocked(self) -> dict[str, Any]:
        visible = {
            field_id: spec.default for field_id, spec in EDIT_FIELD_SPECS.items()
        }
        visible.update(self._accepted)
        for pending in self._pending.values():
            if pending.expected is not None:
                visible.update(pending.expected)
        visible.update(self._drafts)
        return visible

    def _value_origins_unlocked(self) -> dict[str, str]:
        origins = {
            field_id: "default_draft"
            for field_id in EDIT_FIELD_SPECS
        }
        origins.update(
            {field_id: "accepted_truth" for field_id in self._accepted}
        )
        for pending in self._pending.values():
            if pending.expected is not None:
                origins.update(
                    {field_id: "pending_request" for field_id in pending.expected}
                )
        origins.update({field_id: "user_draft" for field_id in self._drafts})
        origins.update(
            {field_id: "invalid_draft" for field_id in self._invalid_drafts}
        )
        return origins

    def _snapshot_unlocked(self) -> EditRegisterSnapshot:
        return EditRegisterSnapshot(
            generation=self._generation,
            visible=MappingProxyType(self._visible_unlocked()),
            accepted=MappingProxyType(dict(self._accepted)),
            dirty_fields=frozenset(self._drafts),
            invalid_fields=MappingProxyType(dict(self._invalid_drafts)),
            value_origins=MappingProxyType(self._value_origins_unlocked()),
            pending_groups=frozenset(self._pending) | frozenset(self._external_pending),
            scopes=MappingProxyType(dict(self._scopes)),
            terminal_receipts=MappingProxyType(dict(self._terminal_receipts)),
        )

    def _reset_observation_unlocked(self) -> dict[str, Any]:
        return {
            "generation": self._generation,
            "accepted": dict(sorted(self._accepted.items())),
            "accepted_sequences": dict(
                sorted(self._accepted_sequences.items())
            ),
            "drafts": dict(sorted(self._drafts.items())),
            "invalid_drafts": dict(sorted(self._invalid_drafts.items())),
            "pending": dict(sorted(self._pending.items())),
            "external_pending": dict(
                sorted(self._external_pending.items())
            ),
            "scopes": dict(sorted(self._scopes.items())),
            "terminal_receipts": dict(
                sorted(self._terminal_receipts.items())
            ),
            "request_sequence": self._request_sequence,
        }

    @staticmethod
    def _spec(field_id: str) -> EditFieldSpec:
        try:
            return EDIT_FIELD_SPECS[field_id]
        except KeyError as exc:
            raise KeyError(f"undeclared NGUI edit field: {field_id}") from exc

    @staticmethod
    def _group_specs(group: str) -> tuple[EditFieldSpec, ...]:
        specs = tuple(
            spec for spec in EDIT_FIELD_SPECS.values() if spec.group == group
        )
        if not specs:
            raise KeyError(f"undeclared NGUI edit group: {group}")
        return specs

    @staticmethod
    def _normalize(spec: EditFieldSpec, value: Any) -> Any:
        if spec.value_kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value in {0, 1}:
                return bool(value)
            text = str(value or "").strip().lower()
            if text in {"true", "yes", "on", "enabled", "1"}:
                return True
            if text in {"false", "no", "off", "disabled", "0", ""}:
                return False
            raise ValueError(f"{spec.field_id} requires a boolean")
        if spec.value_kind == "text":
            text = str(value or "").strip()
            if len(text) > 256:
                raise ValueError(f"{spec.field_id} text exceeds 256 characters")
            return text
        numeric = float(value)
        if spec.minimum is not None and numeric < spec.minimum:
            raise ValueError(
                f"{spec.field_id} value {numeric} outside "
                f"{spec.minimum}..{spec.maximum}"
            )
        if spec.maximum is not None and numeric > spec.maximum:
            raise ValueError(
                f"{spec.field_id} value {numeric} outside "
                f"{spec.minimum}..{spec.maximum}"
            )
        return int(round(numeric)) if spec.integer else numeric

    @staticmethod
    def _matches(spec: EditFieldSpec, accepted: Any, expected: Any) -> bool:
        if spec.value_kind != "number":
            return accepted == expected
        return abs(float(accepted) - float(expected)) <= spec.tolerance


__all__ = [
    "EDIT_FIELD_SPECS",
    "EDIT_PENDING_TIMEOUT_S",
    "EditTerminalReceipt",
    "EditRegisterSnapshot",
    "ExternalPendingEdit",
    "NGUIEditRegister",
    "NGUIEditReconcileCandidate",
    "PING_GROUP",
    "SMS_GROUP",
    "SMS_SENDER_GROUP",
    "TRAFFIC_PLAN_GROUP",
]
