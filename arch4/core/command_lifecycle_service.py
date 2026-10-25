"""Core adapters into the only durable CommandAuthority lifecycle."""

from __future__ import annotations

import logging
import math
import time
from threading import RLock
from typing import Any, Callable, Mapping

from arch4.accepted_modem_generation import AcceptedModemGeneration
from arch4.architecture_contracts.runtime_generation import (
    RuntimeGenerationNamespace,
)
from arch4.command_transactions import (
    CellularCommandAdmission,
    CommandAuthority,
    CommandLifecycle,
    CommandLifecycleProjectionAcknowledgement,
    CommandLifecycleStage,
    CommandTerminalReceipt,
    CommandTransition,
    RadioMutationLease,
)
from arch4.core.action_runtime import (
    publish_command_terminal_evidence,
)
from arch4.core.action_reachability import CoreReachabilityTimeline
from arch4.core.command_status_projection import (
    CommandStatusProjectionReceipt,
    project_core_command_status,
)
from arch4.core_contracts.accepted_generation_root import (
    CoreAcceptedGenerationRoot,
)
from arch4.core_contracts.commands import CoreCommandAdmissionStatus
from arch4.core_contracts.command_targets import CoreMobileDataTarget
from arch4.core_contracts.command_targets import CoreBandSetTarget, CorePBandTarget, CoreNrBandTarget, CoreRadioModeTarget
from arch4.core.action_source_state_authority import ActionSourceStateAuthority
from arch4.core_contracts.emergency_reachability import EmergencyReachabilityDisposition, TAKEOVER_AFTER_S
from arch4.core_contracts.command_physical_status import (
    CoreCommandPhysicalStatus,
)
from arch4.core_contracts.physical_commands import PhysicalCommandReceipt
from arch4.fault_supervisor import FaultBoundaryClass, FaultSeverity
from arch4.modem_live.rtt_register import ModemLiveRttRegisterSnapshot
from arch4.radio_target_contract import (
    TargetConfirmationReceipt,
    match_radio_target,
)


OWNER = "54guc.core.command_lifecycle_service"
log = logging.getLogger(__name__)


def _exact_timestamp(
    value: Any,
    reason: str,
    *,
    positive: bool,
) -> float:
    if type(value) is not float:
        raise TypeError(reason)
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(reason)
    if positive and value <= 0.0:
        raise ValueError(reason)
    return value


def _exact_generation(value: Any, reason: str, *, positive: bool) -> int:
    if type(value) is not int:
        raise TypeError(reason)
    if value < (1 if positive else 0):
        raise ValueError(reason)
    return value


class CoreCommandLifecycleService:
    """Feed committed Matrix command truth into CommandAuthority."""

    def __init__(self, core: Any) -> None:
        authority = getattr(core, "command_authority", None)
        if not isinstance(authority, CommandAuthority):
            raise TypeError("core_command_lifecycle_authority_required")
        self._core = core
        self._authority = authority
        reachability = getattr(core, "reachability_timeline", None)
        if not isinstance(reachability, CoreReachabilityTimeline):
            raise TypeError(
                "core_command_lifecycle_reachability_timeline_required"
            )
        self._reachability_timeline = reachability
        input_module = getattr(core, "input_module", None)
        reader = getattr(
            input_module,
            "rtt_observation_register_snapshot",
            None,
        )
        self._rtt_register_reader = reader if callable(reader) else None
        register_bank = getattr(core, "register_bank", None)
        reachability_writer = getattr(
            register_bank,
            "write_internet_reachability",
            None,
        )
        if not callable(reachability_writer):
            raise TypeError("core_reachability_register_writer_required")
        self._register_bank = register_bank
        self._reachability_projection_signature: tuple[object, ...] = ()
        self._last_check_diagnostic_key = None
        self._last_confirmation_diagnostic_key = None
        self._last_activity_projection_at = 0.0
        self._lock = RLock()
        self._projection_error = ""
        self._projected_obligation_id = ""
        self._projected_obligation_fingerprint = ""
        self._projected_receipt: CommandStatusProjectionReceipt | None = None
        self._terminal_evidence_obligation_id = ""
        self._terminal_interruption_obligation_id = ""

    def _apply_authority_transition(
        self,
        operation: Callable[[], Any],
        *,
        missing_projection_reason: str,
    ) -> tuple[Any, CommandLifecycleProjectionAcknowledgement]:
        """Project owner truth even when a late transition expires itself."""

        try:
            result = operation()
        except BaseException:
            if self._authority.pending_projection_obligation() is not None:
                self.project_pending_authority_truth()
            raise
        acknowledgement = self.project_pending_authority_truth()
        if acknowledgement is None:
            raise RuntimeError(missing_projection_reason)
        return result, acknowledgement

    def reconcile_startup_identity(self, session_scope_key: str) -> bool:
        """Reconcile durable Command Authority truth before Core publication."""

        with self._lock:
            self.project_pending_authority_truth()
            self._reconcile_expected_interruption(now=time.time())
            active = self._authority.active_lifecycle()
            if (
                active is not None
                and active.key.matrix_identity.session_scope_key
                != session_scope_key
            ):
                self._apply_authority_transition(
                    lambda: self._authority.cancel_cellular(
                        root_id=active.key.root_id,
                        reason="command_runtime_identity_replaced",
                        receipt={
                            "owner": OWNER,
                            "replacement_session_scope_key": session_scope_key,
                        },
                        now=time.time(),
                    ),
                    missing_projection_reason=(
                        "command_identity_replacement_projection_ack_missing"
                    ),
                )
            changed = self._authority.reconcile_runtime_identity(
                session_scope_key
            )
            if changed:
                self._projected_obligation_id = ""
                self._projected_obligation_fingerprint = ""
                self._projected_receipt = None
                self._terminal_evidence_obligation_id = ""
                self._terminal_interruption_obligation_id = ""
            self._reconcile_expected_interruption(now=time.time())
            return changed

    def _reconcile_expected_interruption(self, *, now: float) -> str:
        """Close an orphan only from the exact retained Core terminal root."""

        reader = getattr(
            self._core.input_module,
            "expected_interruption_status_payload",
            None,
        )
        if not callable(reader):
            return "interruption_status_port_unavailable"
        status = reader(now=now)
        if not isinstance(status, Mapping):
            raise TypeError("expected_interruption_status_mapping_required")
        active_flag = status.get("active")
        if type(active_flag) is not bool:
            raise TypeError("expected_interruption_active_bool_required")
        if not active_flag:
            return "no_expected_interruption"
        root_id = str(status.get("operation_id") or "").strip()
        if not root_id:
            raise ValueError("expected_interruption_operation_id_required")
        active = self._authority.active_lifecycle()
        if active is not None and active.key.root_id == root_id:
            return "active_command_retained"
        terminal = next(
            (
                receipt
                for receipt in reversed(self._authority.terminal_receipts())
                if receipt.root_id == root_id
            ),
            None,
        )
        if terminal is None:
            return "awaiting_exact_terminal_root"
        closer = getattr(
            self._core.input_module,
            "close_expected_interruption_from_command_terminal",
            None,
        )
        if not callable(closer):
            raise RuntimeError(
                "core_command_terminal_modem_live_closer_required"
            )
        closer(
            operation_id=terminal.root_id,
            terminal_reason=terminal.reason,
            now=terminal.terminal_ts,
        )
        return "closed_from_exact_terminal_root"

    def admission_status(self) -> CoreCommandAdmissionStatus:
        with self._lock:
            authority = self._authority.admission_status()
            if not isinstance(authority, Mapping) or set(authority) != {
                "owner",
                "admission_frozen",
                "reason",
                "radio_mutation_inflight",
            }:
                raise TypeError(
                    "core_command_authority_admission_status_contract_invalid"
                )
            if authority["owner"] != "command_authority":
                raise ValueError(
                    "core_command_authority_admission_status_owner_invalid"
                )
            frozen = authority["admission_frozen"]
            if type(frozen) is not bool:
                raise TypeError(
                    "core_command_authority_admission_frozen_bool_required"
                )
            if type(authority["radio_mutation_inflight"]) is not bool:
                raise TypeError(
                    "core_command_authority_inflight_bool_required"
                )
            authority_reason = authority["reason"]
            if (
                not isinstance(authority_reason, str)
                or authority_reason != authority_reason.strip()
            ):
                raise TypeError(
                    "core_command_authority_admission_reason_invalid"
                )
            if frozen is not (authority_reason != ""):
                raise ValueError(
                    "core_command_authority_admission_reason_mismatch"
                )
            pending = self._authority.pending_projection_obligation()
            reason = self._projection_error or (
                (
                    "command_projection_pending:" + pending.obligation_id
                )
                if pending is not None
                else
                authority_reason if frozen else ""
            )
            command_protection_active = (
                frozen
                and authority_reason.startswith("command_protection:")
            )
            return CoreCommandAdmissionStatus(
                available=reason == "",
                reason=reason,
                hard_blocked=False,
                projection_retry_pending=self._projection_error != "",
                projection_obligation_pending=pending is not None,
                command_protection_active=command_protection_active,
                command_protection_reason=(
                    authority_reason if command_protection_active else ""
                ),
            )

    def physical_status(
        self,
        *,
        request_id: str,
        command_root_id: str,
        physical_action_root_id: str,
    ) -> CoreCommandPhysicalStatus:
        """Return only exact Command Authority / Modem Live correlation."""

        identity = str(request_id or "").strip()
        command_root = str(command_root_id or "").strip()
        physical_root = str(physical_action_root_id or "").strip()
        if not identity or not command_root or not physical_root:
            raise ValueError("core_command_physical_status_identity_required")
        with self._lock:
            root = self._authority.core_command_root(identity)
            if root is None:
                raise KeyError("core_command_physical_status_request_unknown")
            if root.request_root_id != command_root:
                raise RuntimeError("core_command_physical_status_command_root_mismatch")
            if root.physical_action_root_id != physical_root:
                raise RuntimeError("core_command_physical_status_physical_root_mismatch")

            active = self._authority.active_lifecycle()
            terminal = None
            if active is not None and active.key.root_id == physical_root:
                if active.key.command_id != identity:
                    raise RuntimeError(
                        "core_command_physical_status_active_identity_mismatch"
                    )
                raw_receipt = dict(active.as_dict()["receipts"]).get("modem")
            else:
                terminal = next(
                    (
                        item
                        for item in reversed(self._authority.terminal_receipts())
                        if item.transaction.command_id == identity
                        and item.root_id == physical_root
                    ),
                    None,
                )
                raw_receipt = (
                    dict(terminal.as_dict()["receipts"]).get("modem")
                    if terminal is not None
                    else None
                )
            if raw_receipt is not None:
                receipt = PhysicalCommandReceipt.from_mapping(raw_receipt)
                return CoreCommandPhysicalStatus(
                    request_id=identity,
                    command_root_id=command_root,
                    physical_action_root_id=physical_root,
                    disposition=(
                        "physical_accepted"
                        if receipt.accepted
                        else "physical_rejected"
                    ),
                    receipt=receipt,
                )
            if terminal is not None:
                return CoreCommandPhysicalStatus(
                    request_id=identity,
                    command_root_id=command_root,
                    physical_action_root_id=physical_root,
                    disposition="terminal_without_physical_receipt",
                    terminal_reason=terminal.reason,
                )
            if active is None:
                raise RuntimeError(
                    "core_command_physical_status_lifecycle_unavailable"
                )
            return CoreCommandPhysicalStatus(
                request_id=identity,
                command_root_id=command_root,
                physical_action_root_id=physical_root,
                disposition="pending_physical_dispatch",
            )

    def claim_cellular_action(
        self,
        action: Any,
        *,
        execution_owner: str,
        now: float,
    ) -> CellularCommandAdmission:
        """Claim and project one cellular lifecycle under one owner lock."""

        with self._lock:
            admission = self._authority.claim_cellular_action(
                action,
                execution_owner=execution_owner,
                now=now,
                recovery_timing=(self._core.current_recovery_timing()
                    if action.lever.name == "RESTART" else None),
            )
            if admission.accepted:
                acknowledgement = self.project_pending_authority_truth()
                if acknowledgement is None:
                    raise RuntimeError(
                        "command_claim_projection_ack_missing"
                    )
            return admission

    def note_network_stage(self, lease, *, plan, coordination_profile, now):
        """Adapt Modem Live stage entry to the existing leased projection."""
        from arch4.modem_live.physical_command import ModemLivePhysicalStagePlan
        if (not isinstance(plan, ModemLivePhysicalStagePlan)
                or plan.request_id != lease.transaction.command_id
                or plan.transaction_root_id != lease.root_id
                or plan.target != lease.transaction.action_key.target):
            raise RuntimeError("network_progress_command_identity_mismatch")
        with self._lock:
            lifecycle, _ = self._apply_authority_transition(
                lambda: self._authority.note_network_stage(
                    lease, stage_kind=plan.stage_kind, ordinal=plan.ordinal,
                    coordination_profile=coordination_profile, now=now),
                missing_projection_reason="network_progress_projection_ack_missing",
            )
            return lifecycle

    def acknowledge_modem_result(
        self,
        lease: RadioMutationLease,
        *,
        physical_receipt: PhysicalCommandReceipt,
        now: float,
    ) -> CommandTransition:
        """Apply physical evidence and project its lifecycle transition."""

        with self._lock:
            transition, _acknowledgement = self._apply_authority_transition(
                lambda: self._authority.acknowledge_modem_result(
                    lease,
                    physical_receipt=physical_receipt,
                    now=now,
                ),
                missing_projection_reason=(
                    "command_modem_result_projection_ack_missing"
                ),
            )
            return transition

    def mark_dispatched(
        self,
        lease: RadioMutationLease,
        *,
        receipt: Mapping[str, Any] | None,
        now: float,
    ) -> CommandLifecycle:
        """Mark physical dispatch and publish it before releasing the lock."""

        with self._lock:
            lifecycle, _acknowledgement = self._apply_authority_transition(
                lambda: self._authority.mark_dispatched(
                    lease,
                    receipt=receipt,
                    now=now,
                ),
                missing_projection_reason=(
                    "command_dispatch_projection_ack_missing"
                ),
            )
            return lifecycle

    def fail_cellular(
        self,
        *,
        root_id: str,
        reason: str,
        receipt: Mapping[str, Any] | None,
        now: float,
    ) -> CommandTerminalReceipt:
        """Fail and project one cellular lifecycle atomically."""

        with self._lock:
            terminal, _acknowledgement = self._apply_authority_transition(
                lambda: self._authority.fail_cellular(
                    root_id=root_id,
                    reason=reason,
                    receipt=receipt,
                    now=now,
                ),
                missing_projection_reason=(
                    "command_failure_projection_ack_missing"
                ),
            )
            return terminal

    def rearm_undispatched_emergency_rescue(self, *, incident_id: str, now: float) -> bool:
        """A failed preflight consumes no write, but retry needs new device contact."""
        with self._lock:
            state = self._core.action_source_state_authority
            slot = state.emergency_band_rescue_status(now=now)
            selected = slot["emergency_band_rescue_selected_command_id"]
            if (not selected or slot["emergency_band_rescue_incident_id"] != incident_id
                    or now >= slot["emergency_band_rescue_deadline_at"]):
                return False
            view = self._core.current_emergency_reachability_view(now=now)
            if not view.confirmed or view.incident_id != incident_id:
                return False
            terminal = next((row for row in reversed(self._authority.terminal_receipts())
                             if row.transaction.command_id == selected), None)
            if terminal is None:
                return False
            raw = terminal.as_dict()["receipts"].get("modem")
            if not isinstance(raw, Mapping):
                return False
            receipt = PhysicalCommandReceipt.from_mapping(raw)
            if receipt.accepted or receipt.stages or receipt.failure is None or receipt.failure.phase != "pre_dispatch":
                return False
            health = self._core.current_health_view()
            if (health.namespace.session_scope_key != view.session_scope_key or health.facts is None
                    or health.facts.last_success_ts <= max(terminal.terminal_ts, health.facts.last_failure_ts)):
                return False
            return state.rearm_undispatched_emergency_rescue(incident_id=incident_id,
                command_id=selected, observed_at=health.facts.last_success_ts)

    def prepare_emergency_band_rescue(self, *, incident_id: str, now: float) -> bool:
        """Supersede only a finished radio write, using current outage truth."""
        from arch4.core.command_execution import _core_command_worker_running

        with self._lock:
            view = self._core.current_emergency_reachability_view(now=now)
            if (view.incident_id != incident_id
                    or view.disposition is not EmergencyReachabilityDisposition.CONFIRMED
                    or now - view.triggered_at < TAKEOVER_AFTER_S):
                return False
            if _core_command_worker_running(self._core):
                return False
            active = self._authority.active_lifecycle()
            if active is None:
                return True
            if (active.key.matrix_identity.session_scope_key != view.session_scope_key
                    or not isinstance(active.key.target, (CoreBandSetTarget, CorePBandTarget, CoreNrBandTarget))
                    or active.stage not in {CommandLifecycleStage.ACCEPTED, CommandLifecycleStage.CONFIRMED}):
                return False
            self.cancel_cellular(root_id=active.key.root_id,
                                 reason="superseded_by_confirmed_emergency_recovery",
                                 receipt={"owner": OWNER, "incident_id": incident_id}, now=now)
            return True

    def cancel_cellular(
        self,
        *,
        root_id: str,
        reason: str,
        receipt: Mapping[str, Any] | None,
        now: float,
    ) -> CommandTerminalReceipt:
        """Cancel and project one cellular lifecycle atomically."""

        with self._lock:
            terminal, _acknowledgement = self._apply_authority_transition(
                lambda: self._authority.cancel_cellular(
                    root_id=root_id,
                    reason=reason,
                    receipt=receipt,
                    now=now,
                ),
                missing_projection_reason=(
                    "command_cancellation_projection_ack_missing"
                ),
            )
            return terminal

    def abort_pre_worker_execution(
        self,
        *,
        request_id: str,
        matrix_identity: Any,
        reason: str,
        now: float,
    ) -> bool:
        """Clean a prepared command when its worker was never started."""

        with self._lock:
            pending = self._authority.pending_projection_obligation()
            if pending is not None:
                self.project_pending_authority_truth()
            active = self._authority.active_lifecycle()
            if active is None:
                return self._authority.pending_projection_obligation() is None
            if (
                active.key.command_id != request_id
                or active.key.matrix_identity != matrix_identity
            ):
                return False
            _terminal, acknowledgement = self._apply_authority_transition(
                lambda: self._authority.cancel_cellular(
                    root_id=active.key.root_id,
                    reason=reason,
                    receipt={
                        "owner": OWNER,
                        "stage": "pre_worker_abort",
                    },
                    now=now,
                ),
                missing_projection_reason=(
                    "command_pre_worker_cancel_projection_ack_missing"
                ),
            )
            return (
                acknowledgement is not None
                and self._authority.active_lifecycle() is None
                and self._authority.pending_projection_obligation() is None
            )

    def fail_active_cellular_if_matching(
        self,
        *,
        root_id: str,
        reason: str,
        receipt: Mapping[str, Any] | None,
        now: float,
    ) -> bool:
        """Fail the named active lifecycle, if it still exists, under one lock."""

        with self._lock:
            if self._authority.pending_projection_obligation() is not None:
                self.project_pending_authority_truth()
            active = self._authority.active_lifecycle()
            if active is None:
                return False
            if active.key.root_id != root_id:
                return False
            _terminal, _acknowledgement = self._apply_authority_transition(
                lambda: self._authority.fail_cellular(
                    root_id=root_id,
                    reason=reason,
                    receipt=receipt,
                    now=now,
                ),
                missing_projection_reason=(
                    "command_failure_projection_ack_missing"
                ),
            )
            return True

    def project_pending_authority_truth(
        self,
    ) -> CommandLifecycleProjectionAcknowledgement | None:
        """Deliver and acknowledge the pending command-owner update."""

        with self._lock:
            obligation = self._authority.pending_projection_obligation()
            if obligation is None:
                return None
            if (
                self._projected_receipt is not None
                and self._projected_obligation_id == obligation.obligation_id
                and self._projected_obligation_fingerprint
                == obligation.payload_fingerprint
            ):
                projection = self._projected_receipt
            else:
                projection = project_core_command_status(self._core, obligation)
                self._projected_obligation_id = obligation.obligation_id
                self._projected_obligation_fingerprint = (
                    obligation.payload_fingerprint
                )
                self._projected_receipt = projection
            typed = obligation.typed_receipt()
            if (
                isinstance(typed, CommandTerminalReceipt)
                and self._terminal_interruption_obligation_id
                != obligation.obligation_id
            ):
                closer = getattr(
                    self._core.input_module,
                    "close_expected_interruption_from_command_terminal",
                    None,
                )
                if not callable(closer):
                    raise RuntimeError(
                        "core_command_terminal_modem_live_closer_required"
                    )
                closer(
                    operation_id=typed.root_id,
                    terminal_reason=typed.reason,
                    now=typed.terminal_ts,
                )
                self._terminal_interruption_obligation_id = (
                    obligation.obligation_id
                )
            if (
                isinstance(typed, CommandTerminalReceipt)
                and self._terminal_evidence_obligation_id
                != obligation.obligation_id
            ):
                publish_command_terminal_evidence(
                    self._core,
                    typed,
                    now=typed.terminal_ts,
                )
                self._terminal_evidence_obligation_id = obligation.obligation_id
            acknowledged_at = max(
                _exact_timestamp(
                    projection.projected_at,
                    "command_projection_projected_at_invalid",
                    positive=True,
                ),
                _exact_timestamp(
                    obligation.created_at,
                    "command_projection_created_at_invalid",
                    positive=True,
                ),
            )
            acknowledgement = self._authority.acknowledge_projection(
                obligation_id=projection.obligation_id,
                payload_fingerprint=projection.payload_fingerprint,
                register_sequence=projection.register_sequence,
                register_signature=projection.register_signature,
                acknowledged_at=acknowledged_at,
            )
            self._projected_obligation_id = ""
            self._projected_obligation_fingerprint = ""
            self._projected_receipt = None
            return acknowledgement

    def observe_accepted_generation(
        self,
        accepted: AcceptedModemGeneration,
    ) -> dict[str, Any]:
        if not isinstance(accepted, AcceptedModemGeneration):
            raise TypeError("core_command_lifecycle_accepted_generation_required")
        raise RuntimeError("precommit_accepted_generation_lifecycle_illegal")

    def observe_committed_generation(
        self,
        root: CoreAcceptedGenerationRoot,
    ) -> dict[str, Any]:
        if not isinstance(root, CoreAcceptedGenerationRoot):
            raise TypeError("core_command_lifecycle_accepted_root_required")
        commit = root.commit
        view = root.runtime_view
        sequence = _exact_generation(
            commit.matrix_sequence,
            "core_command_lifecycle_commit_sequence_invalid",
            positive=True,
        )
        sampled_at = _exact_timestamp(
            commit.matrix_source_time,
            "core_command_lifecycle_sampled_at_invalid",
            positive=True,
        )
        observed_at = _exact_timestamp(
            commit.committed_at,
            "core_command_lifecycle_committed_at_invalid",
            positive=True,
        )
        if (
            view.matrix_identity != commit.matrix_identity
            or view.accepted_commit_id != commit.commit_id
        ):
            raise ValueError("core_command_lifecycle_commit_view_mismatch")
        self._reachability_timeline.observe_traffic_context(root)
        mode_owner = getattr(self._core, "action_source_state_authority", None)
        if isinstance(mode_owner, ActionSourceStateAuthority):
            rescue_target, rescue_command_id = self._owned_lte_rescue_readback(
                root, mode_owner=mode_owner)
            mode_owner.reconcile_radio_mode_configuration(
                identity_key=commit.matrix_identity.session_scope_key,
                observation=view.radio_target.radio_mode.to_mapping() if view.radio_target.radio_mode is not None else None,
                acquired_at=view.radio_target.radio_mode_acquired_at,
                own_rescue_target=rescue_target, own_rescue_command_id=rescue_command_id)
        with self._lock:
            projection_ack = self.project_pending_authority_truth()
            lifecycle = self._authority.active_lifecycle()
            if lifecycle is None:
                return self._result(
                    sequence,
                    "idle",
                    "no_active_command",
                    fully_projected=True,
                    projection_ack=projection_ack,
                )
            key = lifecycle.key
            if commit.matrix_identity.namespace != key.matrix_identity.namespace:
                return self._result(
                    sequence,
                    lifecycle.stage.value,
                    "accepted_generation_session_mismatch",
                    key.root_id,
                )
            _exact_generation(
                lifecycle.target_generation,
                "core_command_lifecycle_target_generation_invalid",
                positive=False,
            )
            for field_name in ("confirmed_ts", "accepted_ts"):
                _exact_timestamp(
                    getattr(lifecycle, field_name),
                    f"core_command_lifecycle_{field_name}_invalid",
                    positive=False,
                )
            required_after_generation = (
                lifecycle.target_generation
                if lifecycle.stage is CommandLifecycleStage.CONFIRMED
                else key.matrix_identity.matrix_sequence
            )
            required_after_ts = (
                lifecycle.confirmed_ts
                if lifecycle.stage is CommandLifecycleStage.CONFIRMED
                else lifecycle.accepted_ts
            )
            if lifecycle.stage not in {
                CommandLifecycleStage.ACCEPTED,
                CommandLifecycleStage.CONFIRMED,
            }:
                return self._result(
                    sequence,
                    lifecycle.stage.value,
                    "command_not_waiting_for_committed_generation",
                    key.root_id,
                )
            if sequence <= required_after_generation:
                return self._result(
                    sequence,
                    lifecycle.stage.value,
                    "accepted_generation_not_after_command_stage",
                    key.root_id,
                )
            if sampled_at <= required_after_ts:
                return self._result(
                    sequence,
                    lifecycle.stage.value,
                    "accepted_generation_sampled_before_command_stage",
                    key.root_id,
                )
            facts = view.radio_target
            transition_at = max(sampled_at, observed_at)
            if lifecycle.stage is CommandLifecycleStage.CONFIRMED:
                if lifecycle.settle_generation <= 0:
                    if isinstance(key.target, (CoreMobileDataTarget, CorePBandTarget, CoreRadioModeTarget)):
                        setting = match_radio_target(
                            target=key.target,
                            facts=facts,
                            before_source_generation=key.matrix_identity.matrix_sequence,
                            expected_root_id=key.root_id,
                            required_after_ts=lifecycle.accepted_ts,
                        )
                        if not setting.matched:
                            return self._result(
                                sequence, lifecycle.stage.value,
                                setting.reason, key.root_id,
                            )
                    elif not facts.connected:
                        return self._result(
                            sequence,
                            lifecycle.stage.value,
                            "accepted_stream_not_connected",
                            key.root_id,
                        )
                    lifecycle, projection_ack = self._apply_authority_transition(
                        lambda: self._authority.latch_stream_continuation(
                            root_id=key.root_id,
                            source_generation=sequence,
                            receipt={
                                "owner": OWNER,
                                "accepted_generation_commit_id": commit.commit_id,
                                "sampled_at": sampled_at,
                                "identity_code": view.identity_code,
                                **({"pband": facts.primary_band} if isinstance(key.target, CorePBandTarget) else {}),
                            },
                            now=transition_at,
                        ),
                        missing_projection_reason=(
                            "command_stream_projection_ack_missing"
                        ),
                    )
                terminal = self._resolve_latched_stream(
                    lifecycle,
                    now=transition_at,
                )
                if terminal is None:
                    return self._result(
                        sequence,
                        lifecycle.stage.value,
                        "reachability_evidence_pending",
                        key.root_id,
                    )
                return self._result(
                    sequence,
                    "completed",
                    terminal.reason,
                    key.root_id,
                    settled=True,
                )
            target = match_radio_target(
                target=key.target,
                facts=facts,
                before_source_generation=(
                    key.matrix_identity.matrix_sequence
                ),
                expected_root_id=key.root_id,
                required_after_ts=required_after_ts,
            )
            if not target.matched:
                return self._result(
                    sequence,
                    lifecycle.stage.value,
                    target.reason,
                    key.root_id,
                )
            confirmed, projection_ack = self._apply_authority_transition(
                lambda: self._authority.acknowledge_target_confirmed(
                    root_id=key.root_id,
                    receipt=TargetConfirmationReceipt(
                        request_id=key.command_id,
                        transaction_root_id=key.root_id,
                        matrix_identity=commit.matrix_identity,
                        target=key.target,
                        observed=facts,
                        committed_at=observed_at,
                        reason=target.reason,
                    ),
                    now=transition_at,
                ),
                missing_projection_reason=(
                    "command_target_projection_ack_missing"
                ),
            )
            return self._result(
                sequence,
                confirmed.stage.value,
                target.reason,
                key.root_id,
                target_confirmed=True,
                fully_projected=True,
                projection_ack=projection_ack,
            )

    def _owned_lte_rescue_readback(self, root, *, mode_owner):
        """Bind a reserved own rescue to its native acceptance and fresh target."""
        if mode_owner.radio_mode_lease_status()["state"] not in {"holding_lte", "trial"}:
            return None, ""
        lifecycle = self._authority.active_lifecycle()
        candidates = []
        if lifecycle is not None and lifecycle.stage in {
                CommandLifecycleStage.ACCEPTED, CommandLifecycleStage.CONFIRMED}:
            candidates.append((lifecycle.key, lifecycle.accepted_ts, lifecycle.as_dict()["receipts"]))
        # Selected LTE-pool confirmation may precede the slower mode/configuration
        # read. Its already-settled command remains the same owned rescue.
        candidates.extend((row.action_key, row.accepted_ts, row.as_dict()["receipts"])
            for row in reversed(self._authority.terminal_receipts()))
        for key, accepted_at, receipts in candidates:
            command = self._authority.core_command_root(key.command_id)
            if (not isinstance(key.target, CoreBandSetTarget)
                    or key.origin_kind not in {"smart_loop", "fault_recovery"}
                    or not mode_owner.owns_emergency_band_rescue(key.command_id)
                    or key.matrix_identity.namespace != root.commit.matrix_identity.namespace
                    or command is None
                    or command.request.parameters.get("intent") != "emergency_radio_down_recover"):
                continue
            raw = receipts.get("modem")
            if not isinstance(raw, Mapping):
                return None, ""
            native = PhysicalCommandReceipt.from_mapping(raw)
            facts = root.runtime_view.radio_target
            if (not native.accepted or not native.stages
                    or any(stage.outcome != "accepted" for stage in native.stages)
                    or facts.radio_mode_acquired_at <= max(accepted_at, native.transaction_completed_at)
                    or root.commit.matrix_source_time <= accepted_at):
                return None, ""
            target = match_radio_target(target=key.target, facts=facts,
                before_source_generation=key.matrix_identity.matrix_sequence,
                expected_root_id=key.root_id, required_after_ts=accepted_at)
            return (key.target, key.command_id) if target.matched else (None, "")
        return None, ""

    def cancel_replaced_session(
        self,
        *,
        previous_namespace: RuntimeGenerationNamespace,
        replacement_namespace: RuntimeGenerationNamespace,
        now: float,
    ) -> CommandTerminalReceipt | None:
        if not isinstance(previous_namespace, RuntimeGenerationNamespace):
            raise TypeError("command_previous_runtime_namespace_required")
        if not isinstance(replacement_namespace, RuntimeGenerationNamespace):
            raise TypeError("command_replacement_runtime_namespace_required")
        if previous_namespace == replacement_namespace:
            raise ValueError("command_session_replacement_scopes_invalid")
        with self._lock:
            lifecycle = self._authority.active_lifecycle()
            if (
                lifecycle is None
                or lifecycle.key.matrix_identity.namespace
                == replacement_namespace
            ):
                return None
            if (
                lifecycle.key.matrix_identity.namespace
                != previous_namespace
            ):
                raise RuntimeError("active_command_session_transition_mismatch")
            terminal, projection_ack = self._apply_authority_transition(
                lambda: self._authority.cancel_cellular(
                    root_id=lifecycle.key.root_id,
                    reason="modem_session_replaced",
                    receipt={
                        "owner": OWNER,
                        "previous_runtime_namespace": (
                            previous_namespace.as_dict()
                        ),
                        "replacement_runtime_namespace": (
                            replacement_namespace.as_dict()
                        ),
                    },
                    now=_exact_timestamp(
                        now,
                        "core_command_session_replacement_time_invalid",
                        positive=True,
                    ),
                ),
                missing_projection_reason=(
                    "command_cancel_projection_ack_missing"
                ),
            )
            return terminal

    @staticmethod
    def _result(
        sequence: int,
        status: str,
        reason: str,
        root_id: str = "",
        *,
        target_confirmed: bool = False,
        settled: bool = False,
        fully_projected: bool = True,
        projection_ack: (
            CommandLifecycleProjectionAcknowledgement | None
        ) = None,
    ) -> dict[str, Any]:
        return {
            "owner": OWNER,
            "source_sequence": sequence,
            "status": status,
            "reason": reason,
            "active_root_id": root_id,
            "target_confirmed": target_confirmed,
            "settled": settled,
            "fully_projected": fully_projected,
            "projection_obligation_id": (
                projection_ack.obligation_id
                if projection_ack is not None
                else ""
            ),
            "projection_ack_register_sequence": (
                projection_ack.register_sequence
                if projection_ack is not None
                else 0
            ),
        }

    def service_once(self, *, now: float) -> dict[str, Any]:
        observed_at = _exact_timestamp(
            now,
            "core_command_lifecycle_positive_timestamp_required",
            positive=True,
        )
        with self._lock:
            expired: tuple[CommandTerminalReceipt, ...] = ()
            projected = 0
            interruption_reconciliation = "not_run"
            try:
                self._advance_reachability_timeline(now=observed_at)
            except Exception as exc:
                self._report_reachability_fault(
                    operation="advance_reachability_timeline",
                    error=exc,
                )
            try:
                if self.project_pending_authority_truth() is not None:
                    projected += 1
                interruption_reconciliation = (
                    self._reconcile_expected_interruption(now=observed_at)
                )
                active = self._authority.active_lifecycle()
                if (
                    active is not None
                    and active.stage is CommandLifecycleStage.CONFIRMED
                    and active.settle_generation > 0
                ):
                    self._resolve_latched_stream(
                        active,
                        now=observed_at,
                    )
                expired = self._authority.expire_overdue(now=observed_at)
                if expired and self.project_pending_authority_truth() is not None:
                    projected += 1
                self._projection_error = ""
            except Exception as exc:
                self._projection_error = f"{type(exc).__name__}: {exc}"
            active = self._authority.active_lifecycle()
            return {
                "owner": OWNER,
                "observed_at": observed_at,
                "expired_count": len(expired),
                "projected_count": projected,
                "active": active is not None,
                "active_root_id": active.key.root_id if active else "",
                "projection_pending": (
                    self._authority.pending_projection_obligation() is not None
                ),
                "expected_interruption_reconciliation": (
                    interruption_reconciliation
                ),
                "reachability_timeline": self._reachability_timeline.status(),
                **self.admission_status().to_payload(),
                "last_report_error": "",
            }

    def status(self) -> dict[str, Any]:
        active = self._authority.active_lifecycle()
        return {
            "owner": OWNER,
            "active": active is not None,
            "active_root_id": active.key.root_id if active else "",
            "projection_pending": (
                self._authority.pending_projection_obligation() is not None
            ),
            "reachability_timeline": self._reachability_timeline.status(),
            **self.admission_status().to_payload(),
            "last_report_error": "",
        }

    def _advance_reachability_timeline(self, *, now: float) -> None:
        if self._rtt_register_reader is None:
            raise RuntimeError(
                "core_reachability_timeline_rtt_register_port_unavailable"
            )
        snapshot = self._rtt_register_reader(now=now)
        if not isinstance(snapshot, ModemLiveRttRegisterSnapshot):
            raise TypeError(
                "core_reachability_timeline_rtt_register_snapshot_required"
            )
        if snapshot.session_scope_key and snapshot.identity_code:
            self._reachability_timeline.observe_modem_health(
                self._register_bank.read_current_health(
                    accepted_namespace=snapshot.observation.namespace,
                )
            )
        self._reachability_timeline.observe(snapshot)
        view = self._reachability_timeline.current_view(now=now)
        if self._reachability_timeline.needs_service_confirmation(now=now):
            self._core.input_module.request_service_confirmation(
                namespace=snapshot.observation.namespace, requested_at=now,
            )
        probe = snapshot.observation
        probe_key = (probe.namespace, probe.probe_sequence)
        if probe.probe_status in {"failed", "unknown"} and probe_key != self._last_check_diagnostic_key:
            self._core.diagnostics.log_event(
                "core_internet_check_diagnostic", ts=now,
                probe=probe.as_dict(), service=view.as_dict(),
            )
            self._last_check_diagnostic_key = probe_key
        confirmation = snapshot.confirmation
        if confirmation is not None and confirmation.request_id != self._last_confirmation_diagnostic_key:
            self._core.diagnostics.log_event(
                "core_internet_service_confirmation", ts=now,
                confirmation=confirmation.as_dict(), service=view.as_dict(),
            )
            self._last_confirmation_diagnostic_key = confirmation.request_id
        signature = (
            view.session_scope_key,
            view.identity_code,
            view.register_sequence,
            view.disposition.value,
            view.reason,
            (
                view.latest_completed_outage.started_at,
                view.latest_completed_outage.restored_at,
            )
            if view.latest_completed_outage is not None
            else (),
            (view.latest_coverage_gap.last_probe_at, view.latest_coverage_gap.resumed_at)
            if view.latest_coverage_gap is not None else (),
        )
        activity_at = self._reachability_timeline.status()["latest_activity_at"]
        if signature != self._reachability_projection_signature or activity_at != self._last_activity_projection_at:
            previous = self._reachability_projection_signature
            self._register_bank.write_internet_reachability(view)
            self._reachability_projection_signature = signature
            self._last_activity_projection_at = activity_at
            if previous and previous[:2] == signature[:2] and (
                previous[3:] != signature[3:]
            ):
                # Preserve the one owner's bounded probe history on failure,
                # unmeasured gaps and restoration, in the existing event log.
                # Normal successful samples do not create disk event traffic.
                self._core.diagnostics.log_event(
                    "core_internet_timeline_change", ts=now,
                    event_owner=self._reachability_timeline.owner,
                    current=view.as_dict(),
                    probe=snapshot.observation.as_dict(),
                    evidence=self._reachability_timeline.evidence_view(now=now).as_dict(),
                )

    def _resolve_latched_stream(
        self,
        lifecycle: CommandLifecycle,
        *,
        now: float,
    ) -> CommandTerminalReceipt | None:
        if lifecycle.stage is not CommandLifecycleStage.CONFIRMED:
            raise RuntimeError(
                "core_stream_resolution_requires_confirmed_command"
            )
        if lifecycle.settle_generation <= 0:
            raise RuntimeError(
                "core_stream_resolution_requires_latched_continuation"
            )
        receipts = lifecycle.as_dict().get("receipts")
        settle = receipts.get("settle") if isinstance(receipts, dict) else None
        if not isinstance(settle, dict):
            raise RuntimeError(
                "core_stream_resolution_settle_receipt_missing"
            )
        _exact_timestamp(
            settle.get("sampled_at"),
            "core_stream_resolution_settle_sampled_at_invalid",
            positive=True,
        )
        terminal, _projection_ack = self._apply_authority_transition(
            lambda: self._authority.settle_confirmed_stream(
                root_id=lifecycle.key.root_id,
                now=now,
            ),
            missing_projection_reason=(
                "command_terminal_projection_ack_missing"
            ),
        )
        return terminal

    def _report_reachability_fault(
        self,
        *,
        operation: str,
        error: BaseException,
    ) -> None:
        self._reachability_timeline.note_failure(
            operation=operation,
            error=error,
        )
        try:
            active = self._authority.active_lifecycle()
            self._core.fault_supervisor.record(
                boundary=FaultBoundaryClass.CODEC,
                owner=OWNER,
                operation=operation,
                generation=(
                    active.key.matrix_identity.matrix_sequence
                    if active is not None
                    else 0
                ),
                severity=FaultSeverity.ERROR,
                retryable=False,
                degraded_behavior=(
                    "active_command_terminalized_as_system_fault"
                    if active is not None
                    else "reachability_service_fault_visible"
                ),
                error=error,
                suppression_budget=3,
            )
        except Exception as report_error:
            log.error(
                "Core reachability fault reporting failed: %s: %s",
                type(report_error).__name__,
                report_error,
            )


__all__ = ("CoreCommandLifecycleService", "OWNER")
