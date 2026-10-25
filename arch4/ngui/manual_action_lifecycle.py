"""Correlate NGUI manual actions with Core CommandAuthority results."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.command_transactions import (
    COMMAND_AUTHORITY_OWNER,
    COMMAND_AUTHORITY_SCHEMA,
    COMMAND_TERMINAL_RECEIPT_SCHEMA,
    CommandLifecycle,
    CommandTerminalReceipt,
    CommandTerminalStatus,
)
from arch4.core_contracts.physical_commands import (
    PhysicalCommandCoordinationTarget,
    PhysicalCommandReceipt,
)
from arch4.core.command_status_projection import (
    CORE_COMMAND_STATUS_OWNER,
    CORE_COMMAND_STATUS_SCHEMA,
)
from arch4.interfaces import LeverAction
from arch4.radio_target_contract import TargetConfirmationReceipt
from arch4.ngui.millisecond_text import format_elapsed_milliseconds

from .action_lifecycle import (
    NGUIActionCorrelation,
    NGUIActionCorrelationFactory,
)


_AUTHORITY_OWNER = COMMAND_AUTHORITY_OWNER
_FAILURE_STAGES = frozenset({"failed", "rejected", "timed_out", "cancelled", "transport_failed"})
MANUAL_USER_TIMING_SUMMARY_SCHEMA = "NGUIManualUserTimingSummary.v2"


@dataclass(frozen=True, slots=True)
class _BoundCommandIdentity:
    request_id: str
    command_root_id: str
    physical_action_root_id: str
    session_id: str
    identity_key: str
    source_sequence: int

    @classmethod
    def from_status(
        cls,
        status: Mapping[str, Any],
    ) -> "_BoundCommandIdentity":
        if status.get("schema") != CORE_COMMAND_STATUS_SCHEMA:
            raise RuntimeError("ngui_action_core_status_schema_invalid")
        if status.get("owner") != CORE_COMMAND_STATUS_OWNER:
            raise RuntimeError("ngui_action_core_status_owner_invalid")
        required_strings = {
            "command_id",
            "command_root_id",
            "physical_action_root_id",
            "identity_key",
        }
        for field_name in required_strings:
            value = status.get(field_name)
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
            ):
                raise RuntimeError(
                    f"ngui_action_status_{field_name}_invalid"
                )
        try:
            matrix_identity = MatrixGenerationIdentity.from_mapping(
                status.get("matrix_identity")
            )
        except Exception as exc:
            raise RuntimeError(
                "ngui_action_status_matrix_identity_invalid"
            ) from exc
        return cls(
            request_id=status["command_id"],
            command_root_id=status["command_root_id"],
            physical_action_root_id=status["physical_action_root_id"],
            session_id=matrix_identity.session_scope_key,
            identity_key=status["identity_key"],
            source_sequence=matrix_identity.matrix_sequence,
        )

    def admit_status(
        self,
        status: Mapping[str, Any],
    ) -> bool:
        observed = type(self).from_status(status)
        if observed.request_id != self.request_id:
            return False
        if observed.command_root_id != self.command_root_id:
            raise RuntimeError("ngui_action_status_public_root_mismatch")
        if observed.physical_action_root_id != self.physical_action_root_id:
            raise RuntimeError("ngui_action_status_physical_root_mismatch")
        if observed.session_id != self.session_id:
            raise RuntimeError("ngui_action_status_session_mismatch")
        if observed.identity_key != self.identity_key:
            raise RuntimeError("ngui_action_status_identity_mismatch")
        if observed.source_sequence != self.source_sequence:
            raise RuntimeError("ngui_action_status_source_generation_mismatch")
        return True


@dataclass(frozen=True, slots=True)
class _DecodedAuthorityReceipt:
    """Decoded view of the two allowed authority contracts."""

    raw: Mapping[str, Any]
    command_id: str
    physical_action_root_id: str
    session_scope_key: str
    identity_key: str
    source_generation: int
    terminal_status: CommandTerminalStatus | None


@dataclass(frozen=True, slots=True)
class NGUIActionLifecycleProjectionState:
    """Mutable projection state used for an atomic owner commit."""

    command_identity: _BoundCommandIdentity | None
    chart_evidence: Mapping[str, Any] | None
    connectivity_stream_evidence: Mapping[str, float] | None
    last_surface: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in (
            "chart_evidence",
            "connectivity_stream_evidence",
            "last_surface",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    MappingProxyType(dict(value)),
                )


@dataclass(frozen=True, slots=True)
class NGUIActionLifecycleProjectionCandidate:
    """Prepared manual lifecycle state waiting for owner commit."""

    previous: NGUIActionLifecycleProjectionState
    state: NGUIActionLifecycleProjectionState
    surface: Mapping[str, Any]
    _issuer: object

    def __post_init__(self) -> None:
        if not isinstance(
            self.previous,
            NGUIActionLifecycleProjectionState,
        ) or not isinstance(self.state, NGUIActionLifecycleProjectionState):
            raise TypeError("ngui_action_lifecycle_candidate_state_required")
        object.__setattr__(
            self,
            "surface",
            MappingProxyType(dict(self.surface)),
        )


def _decode_authority_receipt(
    receipt: Mapping[str, Any],
) -> _DecodedAuthorityReceipt:
    raw = dict(receipt)
    schema = str(raw.get("schema") or "")
    try:
        if schema == COMMAND_AUTHORITY_SCHEMA:
            lifecycle = CommandLifecycle.from_mapping(raw)
            transaction = lifecycle.lease.transaction
            key = lifecycle.key
            terminal_status = None
        elif schema == COMMAND_TERMINAL_RECEIPT_SCHEMA:
            terminal = CommandTerminalReceipt.from_mapping(raw)
            transaction = terminal.transaction
            key = terminal.action_key
            terminal_status = terminal.terminal_status
        else:
            raise RuntimeError("ngui_action_authority_receipt_schema_invalid")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            "ngui_action_authority_receipt_contract_invalid"
        ) from exc
    return _DecodedAuthorityReceipt(
        raw=raw,
        command_id=str(key.command_id),
        physical_action_root_id=str(transaction.root_id),
        session_scope_key=str(
            transaction.matrix_identity.session_scope_key
        ),
        identity_key=str(transaction.identity_key),
        source_generation=int(
            transaction.matrix_identity.matrix_sequence
        ),
        terminal_status=terminal_status,
    )


class NGUIActionLifecycleProjection:
    """Correlate and show receipts without owning command transitions."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._correlations = NGUIActionCorrelationFactory()
        self._current: NGUIActionCorrelation | None = None
        self._command_identity: _BoundCommandIdentity | None = None
        self._chart_evidence: dict[str, Any] | None = None
        self._connectivity_stream_evidence: dict[str, float] | None = None
        self._last_surface = self._idle_surface()
        self._candidate_issuer = object()

    def request(
        self,
        action: LeverAction,
        *,
        requested_ts: float,
        requested_monotonic: float,
    ) -> Mapping[str, Any]:
        correlation = self._correlations.bind(
            action,
            requested_ts=requested_ts,
            requested_monotonic=requested_monotonic,
        )
        return self._start_correlation(correlation)

    def reset_runtime_state(
        self,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        with self._lock:
            prior = {
                "current": self._current,
                "command_identity": self._command_identity,
                "chart_evidence": self._chart_evidence,
                "connectivity_stream_evidence": (
                    self._connectivity_stream_evidence
                ),
                "last_surface": dict(self._last_surface),
            }
            self._current = None
            self._command_identity = None
            self._chart_evidence = None
            self._connectivity_stream_evidence = None
            self._last_surface = self._idle_surface()
            result = {
                "current": None,
                "command_identity": None,
                "chart_evidence": None,
                "connectivity_stream_evidence": None,
                "last_surface": dict(self._last_surface),
            }
            return prior, result

    def request_semantic(
        self,
        *,
        lever: str,
        source: str,
        requested_ts: float,
        requested_monotonic: float,
    ) -> Mapping[str, Any]:
        correlation = self._correlations.bind_semantic(
            lever=lever,
            source=source,
            requested_ts=requested_ts,
            requested_monotonic=requested_monotonic,
        )
        return self._start_correlation(correlation)

    def _start_correlation(
        self,
        correlation: NGUIActionCorrelation,
    ) -> Mapping[str, Any]:
        with self._lock:
            self._current = correlation
            self._command_identity = None
            self._chart_evidence = None
            self._connectivity_stream_evidence = None
            self._last_surface = {
                "schema_version": "ngui_action_intent_projection.v2",
                "owner": "ngui.action_transport_adapter",
                "action_id": correlation.correlation_id,
                "source": correlation.source,
                "lever": correlation.lever,
                "status": "submitting",
                "message": "Submitting the confirmed action.",
                "action_pending": True,
                "requested_ts": correlation.requested_ts,
                "core_received_ms": None,
                "modem_work_started_ms": None,
                "pband_command_sent_ms": None,
                "ca_pool_command_sent_ms": None,
                "target_first_verified_ms": None,
                "next_chart_sample_ms": None,
                "target_sample_sequence": None,
                "chart_sample_sequence": None,
                "chart_updated": False,
                "connectivity_stream_continued": False,
                "user_timing_summary": _idle_user_timing_summary(),
            }
            return dict(self._last_surface)

    def submitted(self, action_id: str) -> Mapping[str, Any]:
        with self._lock:
            if self._matches(action_id) and self._last_surface.get("status") == "submitting":
                self._last_surface = {
                    **self._last_surface,
                    "status": "submitted_to_transport",
                    "message": "Action received; waiting for modem execution.",
                }
            return dict(self._last_surface)

    def network_settings_unchanged(self, action_id: str) -> Mapping[str, Any]:
        """Close an unsubmitted settings intent without inventing a Core receipt."""
        with self._lock:
            if (not self._matches(action_id) or self._command_identity is not None
                    or self._last_surface.get('lever') != 'NETWORK_SETTINGS'
                    or self._last_surface.get('status') != 'submitting'):
                raise RuntimeError('network_settings_unchanged_requires_unsubmitted_intent')
            self._last_surface = {
                **self._last_surface, 'status': 'unchanged', 'action_pending': False,
                'message': 'Already using these settings. No change was sent.',
            }
            return dict(self._last_surface)

    def submission_failed(
        self,
        action_id: str,
        reason: str,
        *,
        now: float | None = None,
        user_message: str | None = None,
    ) -> Mapping[str, Any]:
        with self._lock:
            if not self._matches(action_id):
                return dict(self._last_surface)
            self._last_surface = {
                **self._last_surface,
                "status": "transport_failed",
                "message": _stage_message("transport_failed", {
                    "reason": reason, "user_message": user_message,
                }, lever=self._current.lever),
                "error": str(reason or "submission_failed"),
                "action_pending": False,
            }
            self._last_surface["user_timing_summary"] = _user_timing_summary(
                self._last_surface,
            )
            return dict(self._last_surface)

    def observe(
        self,
        *,
        transaction: Mapping[str, Any] | None,
        chart_status: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> Mapping[str, Any]:
        with self._lock:
            correlation = self._current
            if correlation is None:
                return dict(self._last_surface)
            row = dict(transaction or {}) if isinstance(transaction, Mapping) else {}
            if str(row.get("gui_action_id") or "") != correlation.correlation_id:
                return dict(self._last_surface)
            if row.get("schema") != CORE_COMMAND_STATUS_SCHEMA:
                raise RuntimeError("ngui_action_core_status_schema_invalid")
            if row.get("owner") != CORE_COMMAND_STATUS_OWNER:
                raise RuntimeError("ngui_action_core_status_owner_invalid")
            command_identity = self._command_identity
            if command_identity is None:
                command_identity = _BoundCommandIdentity.from_status(row)
                self._command_identity = command_identity
            if not command_identity.admit_status(row):
                return dict(self._last_surface)
            authority_receipt = row.get("command_authority_receipt")
            if not isinstance(authority_receipt, Mapping):
                raise RuntimeError("ngui_action_projection_missing_authority_receipt")
            receipt = dict(authority_receipt)
            if receipt.get("owner") != _AUTHORITY_OWNER:
                raise RuntimeError("ngui_action_projection_wrong_owner")
            decoded = _decode_authority_receipt(receipt)
            if decoded.command_id != command_identity.request_id:
                raise RuntimeError("ngui_action_projection_command_identity_mismatch")
            if decoded.physical_action_root_id != command_identity.physical_action_root_id:
                raise RuntimeError("ngui_action_projection_physical_root_mismatch")
            if decoded.session_scope_key != command_identity.session_id:
                raise RuntimeError("ngui_action_projection_session_mismatch")
            if decoded.identity_key != str(row.get("identity_key") or ""):
                raise RuntimeError("ngui_action_projection_modem_identity_mismatch")
            if decoded.source_generation != command_identity.source_sequence:
                raise RuntimeError("ngui_action_projection_source_generation_mismatch")
            surface = self._surface_from_receipt(
                correlation,
                row,
                decoded.raw,
                terminal_status=decoded.terminal_status,
                chart_status=dict(chart_status or {}),
                now=float(now or 0.0),
            )
            self._last_surface = surface
            return dict(surface)

    def prepare_observe(
        self,
        *,
        transaction: Mapping[str, Any] | None,
        chart_status: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> NGUIActionLifecycleProjectionCandidate:
        """Prepare a complete observation without changing the live owner."""

        with self._lock:
            previous = self._state_unlocked()
            candidate_owner = object.__new__(type(self))
            candidate_owner._lock = RLock()
            candidate_owner._correlations = self._correlations
            candidate_owner._current = self._current
            candidate_owner._command_identity = previous.command_identity
            candidate_owner._chart_evidence = (
                dict(previous.chart_evidence)
                if previous.chart_evidence is not None
                else None
            )
            candidate_owner._connectivity_stream_evidence = (
                dict(previous.connectivity_stream_evidence)
                if previous.connectivity_stream_evidence is not None
                else None
            )
            candidate_owner._last_surface = dict(previous.last_surface)
            candidate_owner._candidate_issuer = object()
        surface = candidate_owner.observe(
            transaction=transaction,
            chart_status=chart_status,
            now=now,
        )
        return NGUIActionLifecycleProjectionCandidate(
            previous=previous,
            state=candidate_owner._state_unlocked(),
            surface=surface,
            _issuer=self._candidate_issuer,
        )

    def prepare_projection_failure(
        self,
        *,
        pending: bool,
        error: str,
    ) -> NGUIActionLifecycleProjectionCandidate:
        """Prepare a manual-action failure from the current state."""

        detail = str(error or "manual_action_projection_failed").strip()
        with self._lock:
            previous = self._state_unlocked()
        failure = {
            **dict(previous.last_surface),
            "status": "presentation_failed",
            "message": (
                "Action timing is unavailable; the modem action is still "
                "being handled."
                if pending
                else "Action timing is unavailable; check Recent Events "
                "for the action result."
            ),
            "error": detail,
            "action_pending": bool(pending),
        }
        state = NGUIActionLifecycleProjectionState(
            command_identity=previous.command_identity,
            chart_evidence=previous.chart_evidence,
            connectivity_stream_evidence=(
                previous.connectivity_stream_evidence
            ),
            last_surface=failure,
        )
        return NGUIActionLifecycleProjectionCandidate(
            previous=previous,
            state=state,
            surface=failure,
            _issuer=self._candidate_issuer,
        )

    def commit_observe(
        self,
        candidate: NGUIActionLifecycleProjectionCandidate,
    ) -> Mapping[str, Any]:
        """Install only a candidate bound to its issuer and current base."""

        if not isinstance(
            candidate,
            NGUIActionLifecycleProjectionCandidate,
        ):
            raise TypeError("ngui_action_lifecycle_candidate_required")
        with self._lock:
            if candidate._issuer is not self._candidate_issuer:
                raise RuntimeError(
                    "ngui_action_lifecycle_candidate_foreign_issuer"
                )
            if self._state_unlocked() != candidate.previous:
                raise RuntimeError("ngui_action_lifecycle_candidate_stale")
            self._command_identity = candidate.state.command_identity
            self._chart_evidence = (
                dict(candidate.state.chart_evidence)
                if candidate.state.chart_evidence is not None
                else None
            )
            self._connectivity_stream_evidence = (
                dict(candidate.state.connectivity_stream_evidence)
                if candidate.state.connectivity_stream_evidence is not None
                else None
            )
            self._last_surface = dict(candidate.state.last_surface)
            return dict(candidate.surface)

    def _state_unlocked(self) -> NGUIActionLifecycleProjectionState:
        return NGUIActionLifecycleProjectionState(
            command_identity=self._command_identity,
            chart_evidence=self._chart_evidence,
            connectivity_stream_evidence=(
                self._connectivity_stream_evidence
            ),
            last_surface=self._last_surface,
        )

    def snapshot(self) -> Mapping[str, Any]:
        with self._lock:
            return dict(self._last_surface)

    def reset_for_session(self) -> Mapping[str, Any]:
        """Clear presentation correlation when Core changes modem scope."""

        with self._lock:
            self._current = None
            self._command_identity = None
            self._chart_evidence = None
            self._connectivity_stream_evidence = None
            self._last_surface = self._idle_surface()
            return dict(self._last_surface)

    def _surface_from_receipt(
        self,
        correlation: NGUIActionCorrelation,
        transaction: Mapping[str, Any],
        receipt: Mapping[str, Any],
        *,
        terminal_status: CommandTerminalStatus | None,
        chart_status: Mapping[str, Any],
        now: float,
    ) -> dict[str, Any]:
        stage = (
            terminal_status.value
            if terminal_status is not None
            else str(transaction.get("status") or "").strip().lower()
        )
        action_pending = bool(transaction.get("action_pending", False))
        target_ts = float(transaction.get("confirmed_ts", 0.0) or 0.0)
        transaction_completed_ts = max(
            float(transaction.get("accepted_ts", 0.0) or 0.0),
            float(transaction.get("terminal_ts", 0.0) or 0.0),
            float(receipt.get("terminal_ts", 0.0) or 0.0),
        )
        physical = _physical_stage_projection(
            receipt,
            transaction_completed_ts=transaction_completed_ts,
            physical_receipt_required=(
                terminal_status
                in {
                    CommandTerminalStatus.ACCEPTED,
                    CommandTerminalStatus.CONFIRMED,
                    CommandTerminalStatus.SETTLED,
                }
                if terminal_status is not None
                else float(transaction.get("accepted_ts", 0.0) or 0.0) > 0.0
            ),
        )
        target_generation = int(transaction.get("target_generation") or 0)
        history_source_sequence = int(
            chart_status.get("history_source_sequence") or 0
        )
        history_append_ts = float(
            chart_status.get("history_append_ts") or 0.0
        )
        if self._chart_evidence is None and bool(
            stage
            in {
                "target_confirmed",
                "confirmed",
                "settled",
            }
            and target_generation > 0
            and target_ts > 0.0
            and history_source_sequence > target_generation
            and history_append_ts > correlation.requested_ts
            and history_append_ts > target_ts
        ):
            self._chart_evidence = {
                "updated_ts": history_append_ts,
                "sample_sequence": history_source_sequence,
            }
        chart_evidence = dict(self._chart_evidence or {})
        chart_ts = float(chart_evidence.get("updated_ts", 0.0) or 0.0)
        chart_updated = chart_ts > 0.0
        terminal_ts = float(
            transaction.get("terminal_ts")
            or receipt.get("terminal_ts")
            or 0.0
        )
        message = _stage_message(stage, receipt, lever=correlation.lever)
        if not action_pending and terminal_ts > 0.0:
            message = _retained_manual_action_message(
                message,
                completed_ts=terminal_ts,
                now=now,
            )
        self._admit_connectivity_stream(
            correlation,
            chart_status,
            terminal_ts=terminal_ts,
        )
        surface = {
            "schema_version": "ngui_action_lifecycle_projection.v3",
            "owner": "ngui.action_lifecycle_projection",
            "source_authority": _AUTHORITY_OWNER,
            "action_id": correlation.correlation_id,
            "source": correlation.source,
            "lever": correlation.lever,
            "status": stage,
            "message": message,
            "error": _stage_error(stage, receipt),
            "action_pending": action_pending,
            "requested_ts": correlation.requested_ts,
            "core_received_ts": self._last_surface.get("core_received_ts"),
            "modem_work_started_ts": self._last_surface.get(
                "modem_work_started_ts"
            ),
            "core_received_ms": self._last_surface.get("core_received_ms"),
            "modem_work_started_ms": self._last_surface.get(
                "modem_work_started_ms"
            ),
            "pband_command_sent_ts": physical["pband_sent_at"],
            "ca_pool_command_sent_ts": physical["ca_pool_sent_at"],
            "physical_stage_status": physical["status"],
            "physical_stage_error": physical["reason"],
            "target_first_verified_ts": target_ts if target_ts > 0.0 else None,
            "pband_command_sent_ms": _optional_elapsed_ms(
                correlation.requested_ts,
                float(physical["pband_sent_at"] or 0.0),
            ),
            "ca_pool_command_sent_ms": _optional_elapsed_ms(
                correlation.requested_ts,
                float(physical["ca_pool_sent_at"] or 0.0),
            ),
            "first_physical_command_sent_ms": _optional_elapsed_ms(
                correlation.requested_ts,
                float(physical["first_command_sent_at"] or 0.0),
            ),
            "target_first_verified_ms": _optional_elapsed_ms(
                correlation.requested_ts, target_ts
            ),
            "target_sample_sequence": target_generation or None,
            "terminal_ts": terminal_ts if terminal_ts > 0.0 else None,
            "chart_updated": chart_updated,
            "connectivity_stream_continued": bool(
                self._connectivity_stream_evidence
            ),
            "next_chart_sample_ts": chart_ts if chart_ts > 0.0 else None,
            "next_chart_sample_ms": _optional_elapsed_ms(
                correlation.requested_ts, chart_ts
            ),
            "chart_sample_sequence": (
                int(chart_evidence.get("sample_sequence", 0) or 0)
                if chart_updated
                else None
            ),
            "canonical_receipt": dict(receipt),
        }
        surface["user_timing_summary"] = _user_timing_summary(
            surface,
        )
        return surface

    def _admit_connectivity_stream(
        self,
        correlation: NGUIActionCorrelation,
        chart_status: Mapping[str, Any],
        *,
        terminal_ts: float,
    ) -> None:
        """Keep one accepted history sample after the final command result."""

        if self._connectivity_stream_evidence is not None:
            return
        if terminal_ts <= 0.0 or not chart_status:
            return
        try:
            history_source_sequence = int(
                chart_status.get("history_source_sequence", 0) or 0
            )
            history_append_ts = float(
                chart_status.get("history_append_ts", 0.0) or 0.0
            )
        except (TypeError, ValueError):
            return
        if (
            history_source_sequence <= 0
            or history_append_ts <= terminal_ts
            or history_append_ts <= correlation.requested_ts
        ):
            return
        self._connectivity_stream_evidence = {
            "updated_ts": history_append_ts,
            "sample_sequence": float(history_source_sequence),
        }

    def _matches(self, action_id: str) -> bool:
        return bool(
            self._current is not None
            and self._current.correlation_id == str(action_id or "")
        )

    @staticmethod
    def _idle_surface() -> dict[str, Any]:
        return {
            "schema_version": "ngui_action_lifecycle_projection.v3",
            "owner": "ngui.action_lifecycle_projection",
            "source_authority": _AUTHORITY_OWNER,
            "status": "ready",
            "message": "Ready",
            "action_pending": False,
            "chart_updated": False,
            "connectivity_stream_continued": False,
            "core_received_ms": None,
            "modem_work_started_ms": None,
            "pband_command_sent_ms": None,
            "ca_pool_command_sent_ms": None,
            "target_first_verified_ms": None,
            "next_chart_sample_ms": None,
            "target_sample_sequence": None,
            "chart_sample_sequence": None,
            "user_timing_summary": _idle_user_timing_summary(),
        }


def _elapsed_text(elapsed_ms: float | None, *, missing_text: str = "---") -> str:
    return format_elapsed_milliseconds(elapsed_ms, missing_text=missing_text)


def _idle_user_timing_summary() -> dict[str, Any]:
    return {
        "schema": MANUAL_USER_TIMING_SUMMARY_SCHEMA,
        "owner": "ngui.action_lifecycle_projection",
        "rows": {
            "command_received": {
                "text": "---",
                "value_ms": None,
                "state": "idle",
                "reason": "no_manual_action",
            },
        },
    }


def _user_timing_summary(
    surface: Mapping[str, Any],
) -> dict[str, Any]:
    sent_ms = surface.get("first_physical_command_sent_ms")
    first_sent = (
        float(sent_ms)
        if isinstance(sent_ms, (int, float)) and float(sent_ms) >= 0.0
        else None
    )
    return {
        "schema": MANUAL_USER_TIMING_SUMMARY_SCHEMA,
        "owner": "ngui.action_lifecycle_projection",
        "rows": {
            "command_received": {
                "text": _elapsed_text(first_sent),
                "value_ms": first_sent,
                "state": "valid" if first_sent is not None else "missing",
                "reason": "" if first_sent is not None else "command_not_sent",
            },
        },
    }


def _physical_stage_projection(
    receipt: Mapping[str, Any],
    *,
    transaction_completed_ts: float,
    physical_receipt_required: bool,
) -> dict[str, Any]:
    empty = {
        "status": "pending",
        "reason": "",
        "pband_sent_at": None,
        "ca_pool_sent_at": None,
        "first_command_sent_at": None,
    }
    receipts = receipt.get("receipts")
    if not isinstance(receipts, Mapping):
        if physical_receipt_required:
            raise RuntimeError("ngui_action_physical_receipts_missing")
        return empty
    modem = receipts.get("modem")
    if not isinstance(modem, Mapping):
        if physical_receipt_required:
            raise RuntimeError("ngui_action_modem_receipt_missing")
        return empty
    try:
        physical = PhysicalCommandReceipt.from_mapping(modem)
    except Exception as exc:
        raise RuntimeError(
            "ngui_action_physical_receipt_contract_invalid"
        ) from exc
    if transaction_completed_ts <= 0.0:
        raise RuntimeError("ngui_action_physical_evidence_before_completion")
    if not physical.stages:
        if (not physical_receipt_required and not physical.accepted and physical.failure is not None
                and physical.failure.phase == "pre_dispatch"
                and physical.failure.after_stage_ordinal == 0):
            return {**empty, "status": "not_sent", "reason": physical.reason}
        raise RuntimeError("ngui_action_physical_stages_missing")
    parsed: dict[str, float] = {}
    first_sent_at: float | None = None
    for stage in physical.stages:
        kind = stage.stage_kind
        sent_at = stage.request_dispatched_at
        if isinstance(stage.target, PhysicalCommandCoordinationTarget):
            continue
        first_sent_at = sent_at if first_sent_at is None else min(first_sent_at, sent_at)
        display_kind = (
            "pband"
            if kind == "pband_anchor"
            else "ca_pool"
            if kind in {"ca_pool", "band_selection"}
            else kind
        )
        parsed[display_kind] = sent_at
    return {
        "status": "verified",
        "reason": "",
        "pband_sent_at": parsed.get("pband"),
        "ca_pool_sent_at": parsed.get("ca_pool"),
        "first_command_sent_at": first_sent_at,
    }


def _stage_message(stage: str, receipt: Mapping[str, Any], *, lever: str) -> str:
    if stage in {"target_confirmed", "confirmed", "settled"}:
        target_receipt = receipt.get("receipts", {}).get("target", {})
        if lever == "PBAND" and str(target_receipt.get("reason", "")).startswith("pband_best_effort:"):
            confirmed = TargetConfirmationReceipt.from_mapping(target_receipt)
            requested = confirmed.target.pband
            actual = receipt.get("receipts", {}).get("settle", {}).get("pband", confirmed.observed.primary_band)
            if type(actual) is not int or actual <= 0:
                raise ValueError("manual_pband_settlement_band_required")
            return (f"Requested B{requested}; settled on B{actual}." if stage == "settled"
                    else f"Requested B{requested}; modem selected B{actual}. Awaiting settlement.")
        labels = {
            "PBAND": "LTE band selection", "BAND_SELECTION": "LTE band selection",
            "NR_BAND_LOCK": "NR band selection", "BAND_LOCK": "Band lock",
            "CELL_LOCK": "Cell lock", "RESTART": "Router restart",
            "MOBILE_DATA": "Mobile connection", "NETWORK_SETTINGS": "Network settings",
            "TRAFFIC_PRESENCE": "Keepalive settings",
            "TRAFFIC_VOLUME": "Traffic settings", "UE_CAPABILITY": "Radio capabilities",
        }
        if lever not in labels:
            raise ValueError("manual_verified_action_label_required")
        return labels[lever] + (" verified; awaiting a fresh sample."
            if stage == "target_confirmed" else " verified.")
    messages = {
        "selected": "Action selected.",
        "selection_claimed": "Action received.",
        "admitted": "Action approved for modem execution.",
        "queued": "Waiting for modem execution.",
        "executing": "Sending the change to the modem.",
        "controller_receipt": "Modem change accepted.",
        "modem_accepted": "Modem write complete; verifying the requested change.",
        "accepted": "Modem accepted the requested command.",
    }
    if stage in _FAILURE_STAGES:
        # Explicit boundary for specific SIM/Network field-validation guidance.
        # Arbitrary transport exceptions remain machine evidence, never UI text.
        if stage == "transport_failed" and receipt.get("user_message") is not None:
            text = receipt["user_message"]
            if not isinstance(text, str) or not text.strip():
                raise TypeError("manual_validation_user_message_required")
            return text.strip()
        reason = _stage_error(stage, receipt)
        if reason == "accepted_deadline_expired":
            return "Command accepted; change not confirmed. Check current settings."
        if "effective_ceiling_unavailable" in reason:
            return "The action was not sent because 54GUC could not verify which bands this device supports. Use Check router to collect details. Band changes remain blocked until the supported bands can be verified."
        if reason == "core_band_target_unsupported_by_effective_ceiling":
            return "The action was not sent because one or more selected bands are not supported for this device. Choose supported bands and try again."
        if "supported_band" in reason:
            return "The action was not sent because the selected target is not supported or its support has not been verified. Check the bands allowed for this device."
        if "not_supported" in reason or "unsupported" in reason:
            return "The action was refused because this device does not support the requested operation. Details are recorded in the support log."
        if "identity" in reason or "stale" in reason:
            return "The action was not confirmed because the device identity or current readings changed. Wait for fresh readings before retrying."
        return {
            "transport_failed": "The action was not sent. Check the device connection and requested settings before retrying. Details are recorded in the support log.",
            "rejected": "The action was refused. Check the requested settings; details are recorded in the support log.",
            "failed": "The action failed. Check the device connection and current settings; details are recorded in the support log.",
            "timed_out": "The action did not finish before the time limit. Check the current settings before retrying.",
            "cancelled": "The action was cancelled.",
        }[stage]
    return messages.get(stage, stage.replace("_", " ").title() or "Ready")


def _retained_manual_action_message(
    message: str,
    *,
    completed_ts: float,
    now: float,
) -> str:
    """Keep only the final result in the user-facing manual box."""

    del completed_ts, now
    return str(message or "").strip()


def _stage_error(stage: str, receipt: Mapping[str, Any]) -> str:
    if stage not in _FAILURE_STAGES:
        return ""
    direct = str(receipt.get("reason") or "")
    if direct:
        return direct
    evidence = dict(receipt.get("evidence") or {})
    terminal = dict(evidence.get("command_terminal_receipt") or {})
    return str(terminal.get("reason") or evidence.get("reason") or stage)


def _optional_elapsed_ms(start: float, end: float) -> float | None:
    if start <= 0.0 or end <= 0.0 or end < start:
        return None
    return round((end - start) * 1000.0, 1)


__all__ = ("NGUIActionLifecycleProjection",)
