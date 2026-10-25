"""The only Core command ingress, validation, deduplication and correlation port."""

from __future__ import annotations

import logging
import math
from threading import Condition, RLock
from time import monotonic, time
from typing import Any, Callable, Mapping, NoReturn

from arch4.core.band_admission import CoreBandAdmission, CoreBandAdmissionError
from arch4.core.extension_hooks import CoreExtensionHookRegistry
from arch4.core.lever_action_adapter import CoreLeverActionAdapter
from arch4.core.accepted_root_outbox import CoreAcceptedRootOutbox
from arch4.core_contracts.commands import (
    CoreCommandAdmissionStatus,
    CoreCommandExecutionAbortReceipt,
    CoreCommandExecutionGate,
    CoreCommandExecutionReceipt,
    CoreCommandGatewayShutdownReceipt,
    CoreCommandOrigin,
    CoreCommandRequest,
    core_command_request_root,
)
from arch4.core_contracts.accepted_runtime_view import (
    CoreAcceptedRuntimeView,
)
from arch4.core_contracts.command_safety import CoreCommandOpinion
from arch4.core_contracts.command_targets import (
    CoreBandSetTarget,
    CoreNrBandTarget,
    CorePBandTarget,
    CoreRestartTarget,
)
from arch4.core_contracts.emergency_reachability import (
    CORE_EMERGENCY_REACHABILITY_OWNER,
    CORE_EMERGENCY_REACHABILITY_SCHEMA,
    CoreEmergencyRestartPermit,
)
from arch4.command_transactions import (
    CommandAuthority,
    CoreCommandRoot,
    CommandRootState,
)
from arch4.fault_supervisor import FaultBoundaryClass, FaultSeverity


CORE_COMMAND_GATEWAY_OWNER = "54guc.core.command_gateway"
log = logging.getLogger(__name__)


def _exact_identity(value: Any, reason: str) -> str:
    if not isinstance(value, str):
        raise TypeError(reason)
    if not value or value != value.strip():
        raise ValueError(reason)
    return value


class CoreCommandGateway:
    """Boundary adapter; owns no physical command lifecycle."""

    def __init__(
        self,
        runtime_owner: Any,
        *,
        extension_hooks: CoreExtensionHookRegistry,
        accepted_root_outbox: CoreAcceptedRootOutbox,
        fault_reporter: Callable[..., Any],
        adapter: CoreLeverActionAdapter | None = None,
    ) -> None:
        if runtime_owner is None:
            raise TypeError("core_command_gateway_runtime_owner_required")
        if not isinstance(extension_hooks, CoreExtensionHookRegistry):
            raise TypeError("core_command_gateway_extension_hooks_required")
        if not isinstance(accepted_root_outbox, CoreAcceptedRootOutbox):
            raise TypeError("core_command_gateway_accepted_root_outbox_required")
        if not callable(fault_reporter):
            raise TypeError("core_command_gateway_fault_reporter_required")
        self._runtime_owner = runtime_owner
        authority = getattr(runtime_owner, "command_authority", None)
        if not isinstance(authority, CommandAuthority):
            raise TypeError("core_command_gateway_command_authority_required")
        self._command_authority = authority
        self._extension_hooks = extension_hooks
        self._accepted_root_outbox = accepted_root_outbox
        self._fault_reporter = fault_reporter
        self._adapter = adapter or CoreLeverActionAdapter()
        self._band_admission = CoreBandAdmission(self._adapter)
        self._lock = RLock()
        self._admission_changed = Condition(self._lock)
        self._autonomous_client_id = ""
        self._autonomous_lease = ""
        self._closed = False
        self._inflight_submissions = 0
        self._waiting_submissions = 0
        self._active_request_ids: set[str] = set()
        self._admission_health_provider: (
            Callable[[], CoreCommandAdmissionStatus] | None
        ) = None

    def bind_admission_health_provider(
        self,
        provider: Callable[[], CoreCommandAdmissionStatus],
    ) -> None:
        if not callable(provider):
            raise TypeError("core_command_admission_health_provider_required")
        with self._lock:
            if self._admission_health_provider is not None:
                raise RuntimeError("core_command_admission_health_already_bound")
            self._admission_health_provider = provider

    def bind_band_auto_policy_provider(
        self,
        provider: Callable[..., Mapping[str, Any]],
    ) -> None:
        self._band_admission.bind_provider_policy(provider)

    def activate_autonomous_issuer(self, *, client_id: str, lease: str) -> None:
        identity = _exact_identity(
            client_id,
            "core_autonomous_issuer_client_id_invalid",
        )
        lease_id = _exact_identity(
            lease,
            "core_autonomous_issuer_lease_invalid",
        )
        with self._lock:
            if self._autonomous_client_id and (
                self._autonomous_client_id != identity
                or self._autonomous_lease != lease_id
            ):
                raise RuntimeError("core_autonomous_issuer_already_active")
            self._autonomous_client_id = identity
            self._autonomous_lease = lease_id

    def deactivate_autonomous_issuer(self, *, client_id: str) -> None:
        identity = _exact_identity(
            client_id,
            "core_autonomous_issuer_client_id_invalid",
        )
        with self._lock:
            if identity != self._autonomous_client_id:
                raise PermissionError("core_autonomous_issuer_mismatch")
            self._autonomous_client_id = ""
            self._autonomous_lease = ""

    def _admission_health(self) -> CoreCommandAdmissionStatus:
        with self._lock:
            provider = self._admission_health_provider
        if provider is None:
            raise RuntimeError("core_command_lifecycle_health_unbound")
        value = provider()
        if not isinstance(value, CoreCommandAdmissionStatus):
            raise RuntimeError("core_command_lifecycle_health_invalid")
        return value

    def _claim_submission(self, request_id: str) -> None:
        with self._admission_changed:
            self._waiting_submissions += 1
            try:
                while (
                    request_id in self._active_request_ids
                    and not self._closed
                ):
                    self._admission_changed.wait()
                if self._closed:
                    raise RuntimeError("core_command_gateway_closed")
                self._active_request_ids.add(request_id)
                self._inflight_submissions += 1
            finally:
                self._waiting_submissions -= 1
                if self._waiting_submissions < 0:
                    raise RuntimeError(
                        "core_command_gateway_waiting_underflow"
                    )
                self._admission_changed.notify_all()

    def _release_submission(self, request_id: str) -> None:
        with self._admission_changed:
            if request_id not in self._active_request_ids:
                raise RuntimeError(
                    "core_command_gateway_request_claim_missing"
                )
            self._active_request_ids.remove(request_id)
            self._inflight_submissions -= 1
            if self._inflight_submissions < 0:
                raise RuntimeError(
                    "core_command_gateway_inflight_underflow"
                )
            self._admission_changed.notify_all()

    def _raise_system_fault(
        self,
        *,
        request: CoreCommandRequest,
        command_root_state: CommandRootState,
        operation: str,
        terminal_reason: str,
        error: BaseException,
        now: float,
    ) -> NoReturn:
        # Finish the command record before running the optional diagnostic
        # observer.
        if command_root_state is not CommandRootState.TERMINAL:
            self._command_authority.terminalize_core_command(
                request_id=request.request_id,
                reason=terminal_reason,
                now=now,
            )
        try:
            self._fault_reporter(
                boundary=FaultBoundaryClass.PROCESS,
                owner=CORE_COMMAND_GATEWAY_OWNER,
                operation=operation,
                generation=request.source_sequence,
                severity=FaultSeverity.ERROR,
                retryable=False,
                degraded_behavior=(
                    "command_root_terminal_diagnostic_publication_fail_soft"
                ),
                error=error,
                suppression_budget=0,
            )
        except Exception as report_error:
            log.error(
                "Core command fault publication failed for %s/%s: %s: %s",
                request.request_id,
                operation,
                type(report_error).__name__,
                report_error,
            )
        raise RuntimeError(terminal_reason) from error

    def _abort_execution(
        self,
        *,
        request: CoreCommandRequest,
        execution_gate: CoreCommandExecutionGate,
        reason: str,
    ) -> CoreCommandExecutionAbortReceipt:
        abort = getattr(
            self._runtime_owner,
            "abort_core_cellular_command",
            None,
        )
        if not callable(abort):
            raise RuntimeError(
                "core_cellular_execution_abort_port_unavailable"
            )
        receipt = abort(
            request,
            execution_gate=execution_gate,
            reason=reason,
        )
        if not isinstance(receipt, CoreCommandExecutionAbortReceipt):
            raise TypeError(
                "core_cellular_typed_execution_abort_receipt_required"
            )
        if (
            receipt.request_id != request.request_id
            or receipt.matrix_identity != request.matrix_identity
        ):
            raise RuntimeError(
                "core_cellular_execution_abort_receipt_identity_mismatch"
            )
        if not receipt.terminated:
            raise RuntimeError(
                "core_cellular_execution_abort_survivor"
            )
        return receipt

    def _origin_error(self, request: CoreCommandRequest) -> str:
        if request.origin in {
            CoreCommandOrigin.OPERATOR,
            CoreCommandOrigin.OPERATOR_AUTOMATION,
        }:
            if request.confirmed is not True:
                return "core_command_confirmation_required"
            return (
                "core_command_operator_autonomous_lease_forbidden"
                if request.autonomous_lease
                else ""
            )
        with self._lock:
            if not self._autonomous_client_id:
                return "core_autonomous_issuer_unattached"
            if request.client_id != self._autonomous_client_id:
                return "core_autonomous_issuer_mismatch"
            if request.autonomous_lease != self._autonomous_lease:
                return "core_autonomous_lease_mismatch"
        return ""

    def _request_view(
        self,
        request: CoreCommandRequest,
    ) -> tuple[CoreAcceptedRuntimeView | None, str]:
        current_record, request_record = (
            self._accepted_root_outbox.latest_and_exact(
            matrix_identity=request.matrix_identity,
        )
        )
        current = (
            current_record.root.runtime_view
            if current_record is not None
            else None
        )
        if not isinstance(current, CoreAcceptedRuntimeView):
            raise RuntimeError("core_command_accepted_generation_unavailable")
        current_identity = current.matrix_identity
        if request.session_id != current_identity.session_scope_key:
            return None, "core_command_session_mismatch"
        if request.identity_code != current.identity_code:
            return None, "core_command_identity_mismatch"
        if request.source_epoch != current_identity.source_epoch:
            return None, "core_command_source_epoch_mismatch"
        if (
            request.lifecycle_generation
            != current_identity.lifecycle_generation
        ):
            return None, "core_command_lifecycle_generation_mismatch"
        if (
            request.source_sequence
            > current_identity.matrix_sequence
        ):
            return None, "core_command_source_sequence_not_yet_published"
        view = (
            request_record.root.runtime_view
            if request_record is not None
            else None
        )
        if not isinstance(view, CoreAcceptedRuntimeView):
            if request.source_sequence < current_identity.matrix_sequence:
                return None, "core_command_source_sequence_not_retained"
            if request.source_sequence == current_identity.matrix_sequence:
                return None, "core_command_source_revision_mismatch"
            raise RuntimeError("core_command_source_sequence_state_invalid")
        if (
            request.origin is CoreCommandOrigin.AUTONOMOUS_INTELLIGENCE
            and request.matrix_identity != current_identity
            and not self._autonomous_source_unchanged(
                request=request,
                source=view,
                current=current,
            )
        ):
            return None, "core_autonomous_source_context_changed"
        return view, ""

    @staticmethod
    def _autonomous_source_unchanged(
        *,
        request: CoreCommandRequest,
        source: CoreAcceptedRuntimeView,
        current: CoreAcceptedRuntimeView,
    ) -> bool:
        """Revalidate semantic source truth without coupling to sample cadence."""

        before = source.radio_target
        now = current.radio_target
        if request.parameters.to_mapping().get("intent") == "emergency_radio_down_recover":
            # Outer admission already binds the device/session. Serving radio
            # identity may disappear during an outage; current emergency and
            # the one-shot rescue slot are rechecked by command execution.
            previous_provider = source.capability_facts.to_mapping().get("provider_plmn")
            return bool(previous_provider and previous_provider == current.capability_facts.to_mapping().get("provider_plmn"))
        if not before.connected or not now.connected:
            return False
        if isinstance(request.target, (CorePBandTarget, CoreBandSetTarget)):
            return bool(
                before.serving_band_available
                and now.serving_band_available
                and before.primary_band == now.primary_band
                and before.selected_bands_available
                and now.selected_bands_available
                and before.selected_bands == now.selected_bands
                and before.pci == now.pci
                and before.earfcn == now.earfcn
            )
        if isinstance(request.target, CoreNrBandTarget):
            return before.nr_bands == now.nr_bands
        return False

    def _record_autopilot_core_rejection(
        self,
        request: CoreCommandRequest,
        *,
        reason: str,
        now: float,
    ) -> str:
        """Report a routine Core rejection without turning it into an emergency."""

        if request.origin is not CoreCommandOrigin.AUTONOMOUS_INTELLIGENCE:
            return reason
        bouncer = getattr(self._runtime_owner, "bouncer", None)
        evaluate = getattr(bouncer, "evaluate_core_opinion", None)
        if not callable(evaluate):
            raise RuntimeError("core_bouncer_opinion_port_unavailable")
        verdict = evaluate(
            CoreCommandOpinion(
                request_id=request.request_id,
                matrix_identity=request.matrix_identity,
                allowed=False,
                reason=reason,
                observed_at=float(now),
                critical=False,
            )
        )
        disposition = str(
            getattr(getattr(verdict, "disposition", None), "value", "")
        )
        if disposition != "PASS":
            raise RuntimeError("core_bouncer_non_emergency_stop_forbidden")
        return reason

    def _issue_emergency_restart_permit(
        self,
        request: CoreCommandRequest,
        *,
        now: float,
    ) -> CoreEmergencyRestartPermit | None:
        """Issue the named age exception only for Foundation's exact restart."""

        metadata = request.parameters.to_mapping()
        automation_source = str(
            metadata.get("automation_source") or ""
        ).strip().lower()
        if automation_source != "watchdog":
            return None
        evidence = metadata.get("action_parameters")
        if (
            request.origin is not CoreCommandOrigin.OPERATOR_AUTOMATION
            or request.client_id != "foundation.internet_emergency"
            or request.command_kind != "RESTART"
            or not isinstance(request.target, CoreRestartTarget)
            or request.target.emergency_fast is not False
            or str(metadata.get("intent") or "").strip()
            != "modem_watchdog_recovery"
            or str(metadata.get("objective_id") or "").strip()
            != request.request_id
            or not isinstance(evidence, Mapping)
            or evidence.get("schema") != CORE_EMERGENCY_REACHABILITY_SCHEMA
            or evidence.get("owner") != CORE_EMERGENCY_REACHABILITY_OWNER
            or evidence.get("confirmed") is not True
            or evidence.get("disposition") != "confirmed"
            or str(evidence.get("session_scope_key") or "").strip()
            != request.matrix_identity.session_scope_key
            or str(evidence.get("identity_code") or "").strip()
            != request.identity_code
            or not str(evidence.get("incident_id") or "").strip()
        ):
            raise RuntimeError("core_emergency_restart_request_invalid")
        issue = getattr(
            self._runtime_owner,
            "issue_emergency_restart_permit",
            None,
        )
        if not callable(issue):
            raise RuntimeError("core_emergency_restart_permit_port_unavailable")
        permit = issue(
            request_id=request.request_id,
            matrix_identity=request.matrix_identity,
            identity_code=request.identity_code,
            incident_id=str(evidence["incident_id"]),
            now=float(now),
        )
        if not isinstance(permit, CoreEmergencyRestartPermit):
            raise TypeError("core_emergency_restart_typed_permit_required")
        return permit

    def submit(
        self,
        request: CoreCommandRequest,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(request, CoreCommandRequest):
            raise TypeError("core_command_gateway_typed_request_required")
        current = float(time() if now is None else now)
        root_id = core_command_request_root(request)
        self._claim_submission(request.request_id)
        try:
            command_root = self._command_authority.receive_core_command(
                request,
                now=current,
            )
            if command_root.state is CommandRootState.TERMINAL:
                return {
                    "submitted": False,
                    "reason": command_root.terminal_reason,
                    "root_id": root_id,
                    "command_state": command_root.state.value,
                }
            if command_root.state is CommandRootState.DISPATCHED:
                return {
                    "submitted": True,
                    "reason": "core_command_dispatch_already_retained",
                    "root_id": root_id,
                    "physical_action_root_id": (
                        command_root.physical_action_root_id
                    ),
                    "command_state": command_root.state.value,
                }
            if command_root.state is CommandRootState.RECEIVED:
                try:
                    health = self._admission_health()
                except Exception as exc:
                    self._raise_system_fault(
                        request=request,
                        command_root_state=command_root.state,
                        operation="resolve_command_admission_status",
                        terminal_reason=(
                            "core_command_admission_status_system_fault"
                        ),
                        error=exc,
                        now=current,
                    )
                if health.available is not True:
                    return {
                        "submitted": False,
                        "reason": "core_command_lifecycle_unavailable",
                        "root_id": root_id,
                        "error": health.reason,
                        "command_state": command_root.state.value,
                    }
                reason = (
                    "core_command_expired"
                    if current > request.expires_at
                    else self._origin_error(request)
                )
                if reason:
                    command_root = (
                        self._command_authority.terminalize_core_command(
                            request_id=request.request_id,
                            reason=reason,
                            now=current,
                        )
                    )
                    return {
                        "submitted": False,
                        "reason": reason,
                        "root_id": root_id,
                        "command_state": command_root.state.value,
                    }
                command_root = self._command_authority.advance_core_command(
                    request_id=request.request_id,
                    expected=CommandRootState.RECEIVED,
                    target=CommandRootState.VALIDATED,
                    now=current,
                )
            view = None
            if command_root.state in {
                CommandRootState.VALIDATED,
                CommandRootState.ADMITTED,
            }:
                try:
                    view, reason = self._request_view(request)
                except Exception as exc:
                    self._raise_system_fault(
                        request=request,
                        command_root_state=command_root.state,
                        operation="resolve_command_request_view",
                        terminal_reason=(
                            "core_command_context_system_fault"
                        ),
                        error=exc,
                        now=current,
                    )
                if reason:
                    command_root = (
                        self._command_authority.terminalize_core_command(
                            request_id=request.request_id,
                            reason=reason,
                            now=current,
                        )
                    )
                    return {
                        "submitted": False,
                        "reason": reason,
                        "root_id": root_id,
                        "command_state": command_root.state.value,
                    }
                try:
                    admission = self._band_admission.admit(request, view)
                except CoreBandAdmissionError as exc:
                    terminal_reason = self._record_autopilot_core_rejection(
                        request,
                        reason=exc.code,
                        now=current,
                    )
                    command_root = (
                        self._command_authority.terminalize_core_command(
                            request_id=request.request_id,
                            reason=terminal_reason,
                            now=current,
                        )
                    )
                    return {
                        "submitted": False,
                        "reason": terminal_reason,
                        "root_id": root_id,
                        "band_admission": dict(exc.detail),
                        "command_state": command_root.state.value,
                    }
                except Exception as exc:
                    self._raise_system_fault(
                        request=request,
                        command_root_state=command_root.state,
                        operation="admit_command_band_policy",
                        terminal_reason=(
                            "core_command_band_admission_system_fault"
                        ),
                        error=exc,
                        now=current,
                    )
                if command_root.state is CommandRootState.VALIDATED:
                    command_root = (
                        self._command_authority.advance_core_command(
                            request_id=request.request_id,
                            expected=CommandRootState.VALIDATED,
                            target=CommandRootState.ADMITTED,
                            now=current,
                        )
                    )
            else:
                raise RuntimeError(
                    "core_command_root_state_unhandled:"
                    + command_root.state.value
                )
            try:
                emergency_restart_permit = (
                    self._issue_emergency_restart_permit(
                        request,
                        now=current,
                    )
                )
            except Exception as exc:
                reason = str(exc).strip() or (
                    "core_emergency_restart_permit_rejected:"
                    + type(exc).__name__
                )
                command_root = (
                    self._command_authority.terminalize_core_command(
                        request_id=request.request_id,
                        reason=reason,
                        now=current,
                    )
                )
                return {
                    "submitted": False,
                    "reason": reason,
                    "root_id": root_id,
                    "command_state": command_root.state.value,
                }
            execution_gate = CoreCommandExecutionGate(
                request_id=request.request_id,
                matrix_identity=request.matrix_identity,
                emergency_restart_permit=emergency_restart_permit,
            )
            try:
                execute = getattr(
                    self._runtime_owner,
                    "execute_core_cellular_command",
                    None,
                )
                if not callable(execute):
                    raise RuntimeError(
                        "core_cellular_execution_port_unavailable"
                    )
                receipt = execute(
                    request,
                    admission.action,
                    now=current,
                    execution_gate=execution_gate,
                    core_runtime_view=view,
                )
                if not isinstance(receipt, CoreCommandExecutionReceipt):
                    raise TypeError(
                        "core_cellular_typed_execution_receipt_required"
                    )
                if (
                    receipt.request_id != request.request_id
                    or receipt.command_root_id != root_id
                    or receipt.matrix_identity != request.matrix_identity
                ):
                    raise RuntimeError(
                        "core_cellular_execution_receipt_identity_mismatch"
                    )
                if receipt.submitted:
                    command_root = (
                        self._command_authority.advance_core_command(
                            request_id=request.request_id,
                            expected=CommandRootState.ADMITTED,
                            target=CommandRootState.DISPATCHED,
                            now=receipt.occurred_at,
                            execution_receipt=receipt,
                        )
                    )
                    execution_gate.commit()
                else:
                    command_root = (
                        self._command_authority.terminalize_core_command(
                            request_id=request.request_id,
                            reason=receipt.reason,
                            now=receipt.occurred_at,
                        )
                    )
            except Exception as exc:
                failure: BaseException = exc
                try:
                    self._abort_execution(
                        request=request,
                        execution_gate=execution_gate,
                        reason="core_command_execution_system_fault",
                    )
                except Exception as abort_exc:
                    failure = RuntimeError(
                        "core_command_execution_abort_unproven:"
                        + type(abort_exc).__name__
                        + ":"
                        + str(abort_exc)
                        + ":origin="
                        + type(exc).__name__
                        + ":"
                        + str(exc)
                    )
                self._raise_system_fault(
                    request=request,
                    command_root_state=command_root.state,
                    operation="execute_core_cellular_command",
                    terminal_reason="core_command_execution_system_fault",
                    error=failure,
                    now=current,
                )
            return {
                "submitted": receipt.submitted,
                "reason": receipt.reason,
                "occurred_at": receipt.occurred_at,
                "root_id": root_id,
                "physical_action_root_id": receipt.physical_action_root_id,
                "execution_receipt": receipt.to_payload(),
                "band_admission": dict(admission.detail),
                "command_state": command_root.state.value,
            }
        finally:
            self._release_submission(request.request_id)

    def owns_request(
        self,
        request_id: str,
        *,
        command_root_id: str = "",
        physical_action_root_id: str = "",
    ) -> bool:
        identity = str(request_id or "").strip()
        root = self._command_authority.core_command_root(identity)
        if root is None:
            return False
        if command_root_id and root.request_root_id != command_root_id:
            return False
        if (
            physical_action_root_id
            and root.physical_action_root_id != physical_action_root_id
        ):
            return False
        return True

    def request_root(self, request_id: str) -> CoreCommandRoot | None:
        """Return durable command correlation without exposing command ownership."""

        return self._command_authority.core_command_root(
            str(request_id or "").strip()
        )

    def command_root_id(self, request_id: str) -> str:
        root = self._command_authority.core_command_root(
            str(request_id or "").strip()
        )
        if root is None:
            raise KeyError("core_command_request_root_unknown")
        return root.request_root_id

    def physical_action_root_id(self, request_id: str) -> str:
        root = self._command_authority.core_command_root(
            str(request_id or "").strip()
        )
        if root is None or not root.physical_action_root_id:
            raise KeyError("core_command_physical_action_root_unknown")
        return root.physical_action_root_id

    def publish_execution_state(self, **_: Any) -> None:
        raise RuntimeError("core_command_gateway_lifecycle_publication_illegal")

    def events(self) -> tuple[()]:
        raise RuntimeError("core_command_gateway_lifecycle_events_illegal")

    def last_terminal_receipt_sequence(self) -> int:
        raise RuntimeError("core_command_gateway_terminal_cursor_illegal")

    def correlation_status(self) -> dict[str, Any]:
        return {
            "owner": CORE_COMMAND_GATEWAY_OWNER,
            "persistence_owner": "arch4.command_transactions.CommandAuthority",
        }

    def close(
        self,
        *,
        timeout_s: float = 6.0,
    ) -> CoreCommandGatewayShutdownReceipt:
        if (
            type(timeout_s) is not float
            or not math.isfinite(timeout_s)
            or timeout_s < 0.0
        ):
            raise TypeError("core_command_gateway_shutdown_timeout_invalid")
        deadline = monotonic() + timeout_s
        with self._admission_changed:
            already_closed = self._closed
            self._closed = True
            self._admission_changed.notify_all()
            while (
                self._inflight_submissions
                or self._waiting_submissions
            ):
                remaining = deadline - monotonic()
                if remaining <= 0.0:
                    break
                self._admission_changed.wait(timeout=remaining)
            active = self._inflight_submissions
            waiting = self._waiting_submissions
        return CoreCommandGatewayShutdownReceipt(
            active_submissions=active,
            waiting_submissions=waiting,
            already_closed=already_closed,
        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            provider_bound = self._admission_health_provider is not None
            return {
                "schema": "CoreCommandGatewayStatus.v5",
                "owner": CORE_COMMAND_GATEWAY_OWNER,
                "lifecycle_events": 0,
                "lifecycle_persistent": False,
                "persistence_owner": (
                    "arch4.command_transactions.CommandAuthority"
                ),
                "admission_health_bound": provider_bound,
                "autonomous_issuer_active": bool(
                    self._autonomous_client_id
                    and self._autonomous_lease
                ),
                "closed": self._closed,
                "active_submissions": self._inflight_submissions,
                "waiting_submissions": self._waiting_submissions,
            }


def require_core_command_gateway(port: Any) -> CoreCommandGateway:
    gateway = getattr(port, "command_gateway", None)
    if not isinstance(gateway, CoreCommandGateway):
        raise RuntimeError("core_command_gateway_unavailable")
    return gateway


__all__ = (
    "CORE_COMMAND_GATEWAY_OWNER",
    "CoreCommandGateway",
    "require_core_command_gateway",
)
