"""Sole two-bank Product Matrix for the New Horizon Intelligence customer."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from secrets import token_hex
from threading import Condition, Event, RLock, Thread
from time import monotonic, time
from typing import TYPE_CHECKING, Any

from arch4.architecture_contracts.application_lifecycle import (
    ProductStartupReceipt,
)
from arch4.command_transactions import COMMAND_TERMINAL_RECEIPT_SCHEMA
from arch4.core.command_gateway import require_core_command_gateway
from arch4.core.command_outcome_stream import CORE_COMMAND_OUTCOME_HOOK_ID
from arch4.core.command_request_builder import build_core_command_request
from arch4.core.runtime_snapshot import require_core_runtime_snapshot_publisher
from arch4.core_contracts.clients import CoreClientManifest, CoreClientRole
from arch4.core_contracts.connected_settlement import ConnectedSettlement
from arch4.core_contracts.command_targets import core_command_target_from_payload
from arch4.core_contracts.commands import CoreCommandOrigin
from arch4.core_contracts.hooks import CoreHookAttachmentMode
from arch4.core_contracts.intelligence_boundary import (
    INTELLIGENCE_CLIENT_ID,
    INTELLIGENCE_CONTROL_AUTOPILOT,
    INTELLIGENCE_CONTROL_OBSERVE,
    INTELLIGENCE_CONTROL_STANDALONE,
    INTELLIGENCE_MATRIX_OWNER,
    INTELLIGENCE_PRODUCT_CONTROL_SCHEMA,
    INTELLIGENCE_PROVIDER_POLICY_SCHEMA,
    IntelligenceHealthSnapshot,
    IntelligenceMatrixInputSnapshot,
    IntelligenceMatrixObservation,
    IntelligenceMatrixOutputSnapshot,
    IntelligenceOperationalConstraintView,
    IntelligencePresentationSnapshot,
    IntelligenceProposalDispatchResult,
    IntelligenceSignalBaselineSnapshot,
    intelligence_signal_scope_key,
)
from arch4.core_contracts.intelligence_proposals import BrainCommandProposal
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.runtime_snapshot import (
    CORE_RUNTIME_SNAPSHOT_HOOK_ID,
    CORE_RUNTIME_SNAPSHOT_SCHEMA,
    NGUIExtensionFrame,
)
from arch4.intelligence_presentation_contract import (
    INTELLIGENCE_NGUI_EXTENSION_HOOK_ID,
    INTELLIGENCE_NGUI_EXTENSION_SCHEMA,
    INTELLIGENCE_NGUI_SOURCE_IDS,
    NEW_HORIZON_NGUI_SOURCE_IDS,
)
from arch4.customer_presentation_extension import (
    CustomerNGUIExtensionPublisher,
)
from arch4.operational_service_contracts import (
    IntelligenceAutopilotControlRequest,
    IntelligenceExploreControlRequest,
    OperationalDisposition,
    OperationalKind,
    OperationalServiceReceipt,
    ProductOperationalEffectPermit,
    require_product_operational_effect_permit,
)

if TYPE_CHECKING:
    from arch4.architecture_contracts.runtime_generation import (
        RuntimeGenerationNamespace,
    )
    from arch4.product_supervisor import ProductSupervisor


_RECOVERY_INTERVAL_S = 0.1
_WINDOW_LIMIT = 64
_CGF_STANDALONE = INTELLIGENCE_CONTROL_STANDALONE
_INTELLIGENCE_OBSERVE = INTELLIGENCE_CONTROL_OBSERVE
_INTELLIGENCE_AUTOPILOT = INTELLIGENCE_CONTROL_AUTOPILOT
_QUALIFICATION_INTERVAL_S = 300.0
_SUPERVISION_SKEW_GRACE_S = 5.0
_EXPLORE_DURATION_S = 3.0 * 60.0 * 60.0
_ADAPTER_HOOK_IDS = (
    CORE_RUNTIME_SNAPSHOT_HOOK_ID,
    CORE_COMMAND_OUTCOME_HOOK_ID,
)
_MANIFEST = CoreClientManifest(
    client_id=INTELLIGENCE_CLIENT_ID,
    role=CoreClientRole.PRODUCT_BOUNDARY,
    contract_versions=(
        CORE_RUNTIME_SNAPSHOT_SCHEMA,
        COMMAND_TERMINAL_RECEIPT_SCHEMA,
    ),
    queue_budget=_WINDOW_LIMIT,
    required=False,
)


@dataclass(frozen=True, slots=True)
class _SignalBaselineApplication:
    status: str
    reason: str

    def __post_init__(self) -> None:
        if self.status not in {
            "applied",
            "stale",
            "transient",
        }:
            raise ValueError("signal_baseline_application_status_invalid")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("signal_baseline_application_reason_required")

    @property
    def terminal(self) -> bool:
        return self.status in {"applied", "stale"}


class _CGFInputBank:
    """CGF-owned cells; independent from every Intelligence-output lock."""

    def __init__(self) -> None:
        self._changed = Condition(RLock())
        self._revision = 0
        self._published_at = 0.0
        self._observation: IntelligenceMatrixObservation | None = None
        self._outcomes = deque(maxlen=_WINDOW_LIMIT)
        self._outcome_keys: set[str] = set()
        self._outcome_evicted_through = 0
        self._proposal_results = deque(maxlen=_WINDOW_LIMIT)
        self._proposal_result_ids: set[int] = set()
        self._proposal_result_evicted_through = 0

    def _advance_locked(self, now: float) -> None:
        self._revision += 1
        self._published_at = now
        self._changed.notify_all()

    def publish_observation(
        self,
        observation: IntelligenceMatrixObservation,
    ) -> bool:
        if not isinstance(observation, IntelligenceMatrixObservation):
            raise TypeError("intelligence_matrix_observation_required")
        with self._changed:
            current = self._observation
            if current is not None and current.key == observation.key:
                return False
            if (
                current is not None
                and current.root.matrix_identity.namespace
                != observation.root.matrix_identity.namespace
            ):
                self._outcomes.clear()
                self._outcome_keys.clear()
                self._outcome_evicted_through = 0
                # Proposal results are dispatch receipts, not current modem
                # facts. Keep them until the Intelligence consumer advances
                # its exact result sequence, even when a new modem session is
                # published meanwhile.
            self._observation = observation
            self._advance_locked(observation.published_at)
            return True

    def publish_outcome(self, record: Any) -> bool:
        from arch4.core_contracts.outcomes import CoreCommandOutcomeRecord

        if not isinstance(record, CoreCommandOutcomeRecord):
            raise TypeError("intelligence_matrix_outcome_required")
        with self._changed:
            if record.key in self._outcome_keys:
                return False
            if len(self._outcomes) == _WINDOW_LIMIT:
                evicted = self._outcomes.popleft()
                self._outcome_keys.discard(evicted.key)
                self._outcome_evicted_through = max(
                    self._outcome_evicted_through,
                    evicted.receipt.receipt_sequence,
                )
            self._outcomes.append(record)
            self._outcome_keys.add(record.key)
            self._advance_locked(time())
            return True

    def publish_proposal_result(
        self,
        result: IntelligenceProposalDispatchResult,
    ) -> IntelligenceProposalDispatchResult:
        if not isinstance(result, IntelligenceProposalDispatchResult):
            raise TypeError("intelligence_matrix_proposal_result_required")
        with self._changed:
            if result.sequence in self._proposal_result_ids:
                retained = next(
                    item
                    for item in self._proposal_results
                    if item.sequence == result.sequence
                )
                if retained != result:
                    raise ValueError(
                        "intelligence_matrix_proposal_result_sequence_collision"
                    )
                return retained
            if len(self._proposal_results) == _WINDOW_LIMIT:
                evicted = self._proposal_results.popleft()
                self._proposal_result_ids.discard(evicted.sequence)
                self._proposal_result_evicted_through = max(
                    self._proposal_result_evicted_through,
                    evicted.sequence,
                )
            self._proposal_results.append(result)
            self._proposal_result_ids.add(result.sequence)
            self._advance_locked(result.published_at)
            return result

    def snapshot(self) -> IntelligenceMatrixInputSnapshot:
        with self._changed:
            return IntelligenceMatrixInputSnapshot(
                revision=self._revision,
                published_at=self._published_at,
                observation=self._observation,
                outcomes=tuple(self._outcomes),
                proposal_results=tuple(self._proposal_results),
                outcome_evicted_through_sequence=(
                    self._outcome_evicted_through
                ),
                proposal_result_evicted_through_sequence=(
                    self._proposal_result_evicted_through
                ),
            )

    def wait_after(
        self,
        revision: int,
        *,
        timeout_s: float,
    ) -> IntelligenceMatrixInputSnapshot:
        if type(revision) is not int or revision < 0:
            raise TypeError("intelligence_matrix_input_revision_required")
        timeout = max(0.0, float(timeout_s))
        with self._changed:
            if self._revision <= revision:
                self._changed.wait(timeout)
            return self.snapshot()

    def wake(self) -> None:
        with self._changed:
            self._changed.notify_all()


class _IntelligenceOutputBank:
    """Intelligence-owned cells; independent from every CGF-input lock."""

    def __init__(self) -> None:
        self._changed = Condition(RLock())
        self._revision = 0
        self._published_at = 0.0
        self._presentation: IntelligencePresentationSnapshot | None = None
        self._proposal: BrainCommandProposal | None = None
        self._dispatching_proposal_id = ""
        self._proposal_result_outbox: IntelligenceProposalDispatchResult | None = None
        self._health: IntelligenceHealthSnapshot | None = None
        self._signal_baselines: IntelligenceSignalBaselineSnapshot | None = None
        self._applied_signal_baseline_key: tuple[object, ...] = ()

    def _advance_locked(self, now: float) -> None:
        self._revision += 1
        self._published_at = now
        self._changed.notify_all()

    def publish_presentation(
        self,
        frame: IntelligencePresentationSnapshot,
    ) -> None:
        if not isinstance(frame, IntelligencePresentationSnapshot):
            raise TypeError("intelligence_matrix_presentation_required")
        with self._changed:
            current = self._presentation
            if current is not None and (
                current.namespace == frame.namespace
                and frame.sequence < current.sequence
            ):
                raise ValueError("intelligence_matrix_presentation_regressed")
            if current is not None and (
                current.namespace == frame.namespace
                and current.values == frame.values
                and current.validity == frame.validity
                and current.freshness == frame.freshness
                and current.reasons == frame.reasons
            ):
                return
            self._presentation = frame
            self._advance_locked(frame.published_at)

    def publish_proposal(self, proposal: BrainCommandProposal) -> None:
        if not isinstance(proposal, BrainCommandProposal):
            raise TypeError("intelligence_matrix_typed_proposal_required")
        with self._changed:
            if self._proposal is not None:
                if self._proposal.proposal_id == proposal.proposal_id:
                    return
                raise RuntimeError("intelligence_matrix_proposal_cell_busy")
            self._proposal = proposal
            self._advance_locked(proposal.created_at)

    def publish_health(self, health: IntelligenceHealthSnapshot) -> None:
        if not isinstance(health, IntelligenceHealthSnapshot):
            raise TypeError("intelligence_matrix_health_required")
        with self._changed:
            current = self._health
            if current is not None and (
                current.state == health.state
                and current.fault_count == health.fault_count
                and current.last_error == health.last_error
                and current.input_revision == health.input_revision
                and current.control_risk == health.control_risk
                and current.explore_active == health.explore_active
                and current.explore_session_id == health.explore_session_id
                and current.explore_stop_requested
                == health.explore_stop_requested
                and current.explore_reason == health.explore_reason
            ):
                return
            self._health = health
            self._advance_locked(health.observed_at)

    def publish_signal_baselines(
        self,
        snapshot: IntelligenceSignalBaselineSnapshot,
    ) -> None:
        if not isinstance(snapshot, IntelligenceSignalBaselineSnapshot):
            raise TypeError("intelligence_matrix_signal_baselines_required")
        with self._changed:
            if (
                self._signal_baselines is not None
                and self._signal_baselines.key == snapshot.key
            ):
                return
            self._signal_baselines = snapshot
            self._advance_locked(snapshot.published_at)

    def claim_signal_baselines(
        self,
    ) -> IntelligenceSignalBaselineSnapshot | None:
        with self._changed:
            snapshot = self._signal_baselines
            if snapshot is None or snapshot.key == self._applied_signal_baseline_key:
                return None
            return snapshot

    def complete_signal_baselines(
        self,
        snapshot: IntelligenceSignalBaselineSnapshot,
    ) -> None:
        with self._changed:
            if self._signal_baselines is not snapshot:
                return
            self._applied_signal_baseline_key = snapshot.key

    def claim_proposal(self) -> BrainCommandProposal | None:
        with self._changed:
            proposal = self._proposal
            if (
                proposal is None
                or self._dispatching_proposal_id
                or self._proposal_result_outbox is not None
            ):
                return None
            self._dispatching_proposal_id = proposal.proposal_id
            return proposal

    def stage_proposal_result(
        self,
        result: IntelligenceProposalDispatchResult,
    ) -> None:
        with self._changed:
            if not isinstance(result, IntelligenceProposalDispatchResult):
                raise TypeError("intelligence_proposal_result_outbox_typed_required")
            if self._dispatching_proposal_id != result.proposal_id:
                raise RuntimeError("intelligence_matrix_proposal_claim_mismatch")
            if (
                self._proposal is None
                or self._proposal.proposal_id != result.proposal_id
            ):
                raise RuntimeError("intelligence_matrix_proposal_cell_mismatch")
            if self._proposal_result_outbox is not None:
                if self._proposal_result_outbox != result:
                    raise RuntimeError(
                        "intelligence_proposal_result_outbox_collision"
                    )
                return
            self._proposal_result_outbox = result
            self._advance_locked(result.published_at)

    def claim_proposal_result(
        self,
    ) -> IntelligenceProposalDispatchResult | None:
        with self._changed:
            return self._proposal_result_outbox

    def claim_undispatched_proposal_for_rejection(
        self,
    ) -> BrainCommandProposal | None:
        """Claim a proposal only when Core dispatch never started."""

        with self._changed:
            if self._proposal_result_outbox is not None:
                return None
            if self._proposal is None:
                return None
            if self._dispatching_proposal_id:
                raise RuntimeError(
                    "intelligence_matrix_claimed_proposal_result_missing"
                )
            self._dispatching_proposal_id = self._proposal.proposal_id
            return self._proposal

    def acknowledge_proposal_result(
        self,
        acknowledgement: IntelligenceProposalDispatchResult,
    ) -> None:
        with self._changed:
            if (
                self._proposal_result_outbox is None
                or acknowledgement != self._proposal_result_outbox
            ):
                raise RuntimeError(
                    "intelligence_proposal_result_acknowledgement_mismatch"
                )
            proposal_id = acknowledgement.proposal_id
            if self._dispatching_proposal_id != proposal_id:
                raise RuntimeError("intelligence_matrix_proposal_claim_mismatch")
            if self._proposal is None or self._proposal.proposal_id != proposal_id:
                raise RuntimeError("intelligence_matrix_proposal_cell_mismatch")
            self._proposal = None
            self._dispatching_proposal_id = ""
            self._proposal_result_outbox = None
            self._advance_locked(time())

    def snapshot(self) -> IntelligenceMatrixOutputSnapshot:
        with self._changed:
            return IntelligenceMatrixOutputSnapshot(
                revision=self._revision,
                published_at=self._published_at,
                presentation=self._presentation,
                proposal=self._proposal,
                health=self._health,
                signal_baselines=self._signal_baselines,
            )

    def wait_for_output(self, *, timeout_s: float) -> None:
        with self._changed:
            if (
                self._proposal is None
                and (
                    self._signal_baselines is None
                    or self._signal_baselines.key
                    == self._applied_signal_baseline_key
                )
            ):
                self._changed.wait(max(0.0, float(timeout_s)))

    def clear(self) -> None:
        with self._changed:
            if (
                self._proposal is not None
                or self._dispatching_proposal_id
                or self._proposal_result_outbox is not None
            ):
                raise RuntimeError(
                    "intelligence_matrix_unsettled_proposal_clear_forbidden"
                )
            self._presentation = None
            self._health = None
            self._signal_baselines = None
            self._applied_signal_baseline_key = ()
            self._advance_locked(time())

    def wake(self) -> None:
        with self._changed:
            self._changed.notify_all()


class IntelligenceMatrixCustomerPort:
    """Narrow customer view; intentionally exposes no CGF owner object."""

    __slots__ = ("__matrix",)

    def __init__(self, matrix: "ProductIntelligenceMatrix") -> None:
        self.__matrix = matrix

    def attach(self, root: ProductStartupReceipt) -> None:
        self.__matrix.attach_customer(root)

    def detach(self) -> None:
        self.__matrix.detach_customer()

    def read_inputs(
        self,
        *,
        after_revision: int,
        timeout_s: float,
    ) -> IntelligenceMatrixInputSnapshot:
        return self.__matrix.read_inputs(
            after_revision=after_revision,
            timeout_s=timeout_s,
        )

    def publish_presentation(
        self,
        frame: IntelligencePresentationSnapshot,
    ) -> None:
        self.__matrix.publish_presentation(frame)

    def publish_proposal(self, proposal: BrainCommandProposal) -> None:
        self.__matrix.publish_proposal(proposal)

    def publish_health(self, health: IntelligenceHealthSnapshot) -> None:
        self.__matrix.publish_health(health)

    def publish_signal_baselines(
        self,
        snapshot: IntelligenceSignalBaselineSnapshot,
    ) -> None:
        self.__matrix.publish_signal_baselines(snapshot)


class ProductIntelligenceMatrix:
    """One Product boundary with two independently serviced one-way banks."""

    def __init__(self, product: "ProductSupervisor") -> None:
        self._product = product
        self._lifecycle = RLock()
        self._input = _CGFInputBank()
        self._output = _IntelligenceOutputBank()
        self._customer = IntelligenceMatrixCustomerPort(self)
        self._presentation_publisher = CustomerNGUIExtensionPublisher(
            hook_id=INTELLIGENCE_NGUI_EXTENSION_HOOK_ID,
            payload_schema=INTELLIGENCE_NGUI_EXTENSION_SCHEMA,
            provider_id=INTELLIGENCE_MATRIX_OWNER,
            source_ids=INTELLIGENCE_NGUI_SOURCE_IDS,
        )
        self._presentation_key: tuple[object, ...] = ()
        self._root: ProductStartupReceipt | None = None
        self._stop = Event()
        self._input_thread: Thread | None = None
        self._output_thread: Thread | None = None
        self._autonomous_lease = ""
        self._issuer_active = False
        self._control_mode = _CGF_STANDALONE
        self._autopilot_requested = True
        self._explore_requested = False
        self._explore_session_id = ""
        self._explore_started_at = 0.0
        self._explore_deadline_at = 0.0
        self._explore_reason = "not_requested"
        self._control_available = False
        self._control_generation = 0
        self._control_reason = "intelligence_unattached"
        self._qualification_started_at = 0.0
        self._qualification_lost_at = 0.0
        self._qualification_complete = False
        self._connected_settlement = ConnectedSettlement()
        self._learning_settled = False
        self._critical_failure = ""
        self._presentation_namespace = None
        self._last_fault_at = 0.0
        self._dispatch_sequence = 0
        self._fault_count = 0
        self._last_error = ""

    def customer_port(self) -> IntelligenceMatrixCustomerPort:
        return self._customer

    def _record_fault(self, operation: str, error: BaseException) -> None:
        with self._lifecycle:
            self._fault_count += 1
            self._last_fault_at = time()
            self._last_error = (
                f"{operation}:{type(error).__name__}:{error}"
            )
        self._reconcile_control_mode()

    def _record_critical_failure(
        self,
        operation: str,
        error: BaseException,
    ) -> None:
        """Stop autonomous output when an owner invariant is broken."""

        with self._lifecycle:
            self._fault_count += 1
            self._last_fault_at = time()
            self._last_error = f"{operation}:{type(error).__name__}:{error}"
            self._critical_failure = self._last_error
        self._stop.set()
        self._input.wake()
        self._output.wake()
        self._reconcile_control_mode()

    def _reconcile_control_mode(self) -> None:
        input_state = self._input.snapshot()
        output_state = self._output.snapshot()
        health = output_state.health
        current = monotonic()
        wall_now = time()
        with self._lifecycle:
            attached = self._root is not None
            workers_running = bool(
                self._input_thread is not None
                and self._input_thread.is_alive()
                and self._output_thread is not None
                and self._output_thread.is_alive()
            )
            namespace_matches = bool(
                input_state.observation is not None
                and self._root is not None
                and input_state.observation.root.matrix_identity.namespace
                == self._root.matrix_readiness.matrix_identity.namespace
            )
            synchronized = bool(
                namespace_matches
                and health is not None
                # New Horizon owns its semantic intake and output health.
                # Product Intelligence Matrix must not independently require
                # equality with a newer, continuously advancing input cell.
                # One successfully acknowledged input proves attachment;
                # health/risk below owns whether processing remains usable.
                and health.input_revision > 0
                and health.input_revision <= input_state.revision
            )
            healthy = bool(
                health is not None
                and (
                    health.state == "running"
                    or (
                        self._qualification_complete
                        and health.state == "degraded"
                        and health.control_risk == "recoverable"
                    )
                )
                and health.control_risk != "critical"
            )
            critical = bool(
                self._critical_failure
                or (
                    health is not None
                    and health.control_risk == "critical"
                )
            )
            if (
                self._explore_requested
                and health is not None
                and health.explore_stop_requested
                and health.explore_session_id == self._explore_session_id
            ):
                self._clear_explore_locked(
                    health.explore_reason or "intelligence_requested_stop"
                )
            elif (
                self._explore_requested
                and health is not None
                and health.explore_active
                and health.explore_session_id == self._explore_session_id
                and health.explore_reason
                and health.explore_reason != self._explore_reason
            ):
                self._explore_reason = health.explore_reason
                self._control_generation += 1
            eligible = bool(
                attached
                and workers_running
                and synchronized
                and healthy
                and not critical
            )

            if critical or not attached:
                self._qualification_started_at = 0.0
                self._qualification_lost_at = 0.0
                self._qualification_complete = False
            elif eligible:
                if self._qualification_lost_at:
                    lost_for = max(0.0, current - self._qualification_lost_at)
                    if (
                        not self._qualification_complete
                        and self._qualification_started_at
                    ):
                        self._qualification_started_at += lost_for
                    self._qualification_lost_at = 0.0
                if not self._qualification_started_at:
                    self._qualification_started_at = current
                if (
                    not self._qualification_complete
                    and current - self._qualification_started_at
                    >= _QUALIFICATION_INTERVAL_S
                ):
                    self._qualification_complete = True
            else:
                if not self._qualification_lost_at:
                    self._qualification_lost_at = current
                elif (
                    current - self._qualification_lost_at
                    > _SUPERVISION_SKEW_GRACE_S
                ):
                    self._qualification_started_at = 0.0
                    self._qualification_complete = False

            qualified = bool(
                attached
                and self._qualification_complete
                and not critical
                and (
                    eligible
                    or (
                        self._qualification_lost_at
                        and current - self._qualification_lost_at
                        <= _SUPERVISION_SKEW_GRACE_S
                    )
                )
            )
            if self._explore_requested and (
                wall_now >= self._explore_deadline_at
                or not qualified
                or critical
            ):
                self._clear_explore_locked(
                    "deadline_reached"
                    if wall_now >= self._explore_deadline_at
                    else "autopilot_unavailable"
                )
            self._control_available = qualified
            desired = (
                _INTELLIGENCE_AUTOPILOT
                if qualified
                and (self._autopilot_requested or self._explore_requested)
                else _INTELLIGENCE_OBSERVE
                if attached and not critical
                else _CGF_STANDALONE
            )
            if not attached:
                reason = (
                    "intelligence_runtime_unavailable"
                    if self._critical_failure
                    else "intelligence_unattached"
                )
            elif critical:
                reason = "intelligence_critical_off"
            elif self._explore_requested:
                reason = "user_authorized_explore"
            elif not self._autopilot_requested:
                reason = "user_disabled_autopilot"
            elif not workers_running:
                reason = "intelligence_workers_not_running"
            elif not namespace_matches:
                reason = "intelligence_namespace_not_synchronized"
            elif not synchronized:
                reason = "intelligence_matrix_not_synchronized"
            elif not healthy:
                reason = "intelligence_health_not_running"
            elif not qualified:
                reason = "intelligence_qualifying"
            else:
                reason = "intelligence_supervision_ready"
            if desired == self._control_mode:
                self._control_reason = reason
                return
            gateway = require_core_command_gateway(self._product._core)
            if desired == _INTELLIGENCE_AUTOPILOT:
                gateway.activate_autonomous_issuer(
                    client_id=INTELLIGENCE_CLIENT_ID,
                    lease=self._autonomous_lease,
                )
                self._issuer_active = True
            elif self._issuer_active:
                gateway.deactivate_autonomous_issuer(
                    client_id=INTELLIGENCE_CLIENT_ID,
                )
                self._issuer_active = False
            self._control_mode = desired
            self._control_generation += 1
            self._control_reason = reason

    def _require_attached(self) -> ProductStartupReceipt:
        root = self._root
        if root is None or not self._product._admission.admits(root):
            raise RuntimeError("product_intelligence_matrix_not_attached")
        return root

    def attach_customer(self, root: ProductStartupReceipt) -> None:
        if not isinstance(root, ProductStartupReceipt):
            raise TypeError("product_intelligence_matrix_startup_root_required")
        attached: list[str] = []
        started: list[Thread] = []
        lease = "intelligence-matrix:" + token_hex(16)
        setup_started = False
        try:
            with self._lifecycle:
                if self._root is not None:
                    raise RuntimeError(
                        "product_intelligence_matrix_already_attached"
                    )
                if not self._product._admission.admits(root):
                    raise RuntimeError(
                        "product_intelligence_matrix_admission_closed"
                    )
                setup_started = True
                for hook_id in _ADAPTER_HOOK_IDS:
                    self._product._core.extension_hooks.attach(
                        _MANIFEST,
                        hook_id=hook_id,
                        mode=CoreHookAttachmentMode.SUBSCRIBER,
                    )
                    attached.append(hook_id)
                self._root = root
                self._autonomous_lease = lease
                self._critical_failure = ""
                self._qualification_started_at = 0.0
                self._qualification_lost_at = 0.0
                self._qualification_complete = False
                self._control_available = False
                self._presentation_namespace = (
                    root.matrix_readiness.matrix_identity.namespace
                )
                self._presentation_publisher.bind_runtime_namespace(
                    self._presentation_namespace
                )
                self._presentation_key = ()
                self._stop.clear()
                self._input_thread = Thread(
                    target=self._run_input_adapter,
                    name="54guc-intelligence-matrix-input",
                    daemon=True,
                )
                self._output_thread = Thread(
                    target=self._run_output_adapter,
                    name="54guc-intelligence-matrix-output",
                    daemon=True,
                )
                self._input_thread.start()
                started.append(self._input_thread)
                self._output_thread.start()
                started.append(self._output_thread)
            self._reconcile_control_mode()
        except BaseException as error:
            if not setup_started:
                raise
            self._stop.set()
            self._input.wake()
            self._output.wake()
            for thread in started:
                thread.join(timeout=2.0)
            survivors = tuple(
                thread.name for thread in started if thread.is_alive()
            )
            if survivors:
                raise RuntimeError(
                    "product_intelligence_matrix_startup_thread_survived:"
                    + ",".join(survivors)
                ) from error
            self._settle_output_before_clear(
                reason="intelligence_startup_failed_before_dispatch"
            )
            with self._lifecycle:
                if self._issuer_active:
                    require_core_command_gateway(
                        self._product._core
                    ).deactivate_autonomous_issuer(
                        client_id=INTELLIGENCE_CLIENT_ID,
                    )
                    self._issuer_active = False
                self._root = None
                self._autonomous_lease = ""
                self._input_thread = None
                self._output_thread = None
                for hook_id in reversed(attached):
                    self._product._core.extension_hooks.detach(
                        INTELLIGENCE_CLIENT_ID,
                        hook_id=hook_id,
                    )
            self._output.clear()
            raise

    def report_customer_start_failure(
        self,
        root: ProductStartupReceipt,
        error: BaseException,
    ) -> None:
        """Publish required-customer failure without degrading standalone CGF."""

        if not isinstance(root, ProductStartupReceipt):
            raise TypeError(
                "product_intelligence_failure_startup_root_required"
            )
        if not isinstance(error, BaseException):
            raise TypeError("product_intelligence_failure_error_required")
        namespace = root.matrix_readiness.matrix_identity.namespace
        with self._lifecycle:
            if self._root is not None:
                raise RuntimeError(
                    "product_intelligence_failure_while_customer_attached"
                )
            self._presentation_namespace = namespace
            self._presentation_publisher.bind_runtime_namespace(namespace)
            self._critical_failure = (
                f"{type(error).__name__}:{error}"
            )
            self._fault_count += 1
            self._last_fault_at = time()
            self._last_error = "customer_start:" + self._critical_failure
            self._qualification_started_at = 0.0
            self._qualification_lost_at = 0.0
            self._qualification_complete = False
            self._control_available = False
        self._reconcile_control_mode()

    def detach_customer(self) -> None:
        with self._lifecycle:
            if self._root is None:
                raise RuntimeError("product_intelligence_matrix_not_attached")
            self._stop.set()
            input_thread = self._input_thread
            output_thread = self._output_thread
        self._input.wake()
        self._output.wake()
        for thread in (input_thread, output_thread):
            if thread is not None:
                thread.join(timeout=2.0)
        survivors = tuple(
            thread.name
            for thread in (input_thread, output_thread)
            if thread is not None and thread.is_alive()
        )
        if survivors:
            raise RuntimeError(
                "product_intelligence_matrix_thread_survived:"
                + ",".join(survivors)
            )
        self._settle_output_before_clear(
            reason="intelligence_detached_before_dispatch"
        )
        with self._lifecycle:
            if self._issuer_active:
                require_core_command_gateway(
                    self._product._core
                ).deactivate_autonomous_issuer(
                    client_id=INTELLIGENCE_CLIENT_ID,
                )
                self._issuer_active = False
            for hook_id in reversed(_ADAPTER_HOOK_IDS):
                self._product._core.extension_hooks.detach(
                    INTELLIGENCE_CLIENT_ID,
                    hook_id=hook_id,
                )
            self._root = None
            self._autonomous_lease = ""
            self._input_thread = None
            self._output_thread = None
            if self._control_mode != _CGF_STANDALONE:
                self._control_mode = _CGF_STANDALONE
                self._control_generation += 1
            self._control_reason = "intelligence_unattached"
            self._control_available = False
            self._qualification_started_at = 0.0
            self._qualification_lost_at = 0.0
            self._qualification_complete = False
            self._clear_explore_locked("intelligence_detached")
        self._output.clear()

    def _refresh_observation(self) -> bool:
        root = self._product._core.accepted_root_outbox.current()
        if root is None:
            return False
        current = self._input.snapshot().observation
        measurements_available = self._product._core.current_health_view().allows_measurement(
            root.commit.matrix_source_time
        )
        schedule = self._product._foundation.schedule.state()
        with self._lifecycle:
            settlement = self._connected_settlement.evaluate(
                connected=measurements_available and self._available_fact(root, "health.radio_connected") is True,
                source_at=root.commit.matrix_source_time,
                source_sequence=root.matrix_identity.matrix_sequence,
                namespace=root.matrix_identity.namespace)
            self._learning_settled = settlement["stable_connected"]
        product_control = self._product_control_view()
        published_at = time()
        core_reachability = self._product._core.current_reachability_view(
            now=published_at,
        )
        core_reachability_evidence = (
            self._product._core.current_reachability_evidence_view(
                now=published_at,
            )
        )
        core_emergency_reachability = (
            self._product._core.current_emergency_reachability_view(
                now=published_at,
            )
        )
        key = (
            "intelligence-observation:"
            + root.root_fingerprint
            + ":measurements=" + str(int(measurements_available))
            + ":control="
            + str(product_control["control_generation"])
            + ":schedule="
            + str(int(schedule.get("revision", 0) or 0))
            + ":reachability="
            + str(core_reachability_evidence.register_sequence)
            + ":reachability-state="
            + core_reachability.disposition.value
            + ":"
            + core_reachability.reason
            + ":emergency="
            + core_emergency_reachability.incident_id
            + ":"
            + core_emergency_reachability.disposition.value
            + ":"
            + str(core_emergency_reachability.verification_round_count)
        )
        if current is not None and current.key == key:
            return False
        if (
            current is None
            or current.root.matrix_identity != root.matrix_identity
        ):
            causes = ("matrix_root",)
        else:
            cause_set: set[str] = set()
            prior = current.constraints
            if (
                prior.core_reachability_evidence.register_sequence,
                prior.core_reachability.disposition,
                prior.core_reachability.latest_probe_at,
                prior.core_reachability.reason,
            ) != (
                core_reachability_evidence.register_sequence,
                core_reachability.disposition,
                core_reachability.latest_probe_at,
                core_reachability.reason,
            ):
                cause_set.add("reachability")
            prior_emergency = prior.core_emergency_reachability
            if (
                prior_emergency.incident_id,
                prior_emergency.disposition,
                prior_emergency.verification_round_count,
                prior_emergency.failed_round_count,
                prior_emergency.successful_round_streak,
            ) != (
                core_emergency_reachability.incident_id,
                core_emergency_reachability.disposition,
                core_emergency_reachability.verification_round_count,
                core_emergency_reachability.failed_round_count,
                core_emergency_reachability.successful_round_streak,
            ):
                cause_set.add("emergency")
            if (
                prior.schedule_revision
                != int(schedule.get("revision", 0) or 0)
                or prior.product_control.to_mapping() != product_control
                or prior.core_snapshot.current_health.allows_measurement(
                    current.root.commit.matrix_source_time) != measurements_available
            ):
                cause_set.add("constraint")
            causes = tuple(
                cause
                for cause in (
                    "reachability",
                    "emergency",
                    "constraint",
                )
                if cause in cause_set
            )
            if not causes:
                raise RuntimeError(
                    "product_intelligence_observation_change_cause_missing"
                )
        snapshot = require_core_runtime_snapshot_publisher(
            self._product._core
        ).snapshot_for(
            INTELLIGENCE_CLIENT_ID,
            accepted_root=root,
            published_at=published_at,
        )
        provider_policy = self._provider_band_policy(root)
        constraints = IntelligenceOperationalConstraintView(
            matrix_identity=root.matrix_identity,
            accepted_root_fingerprint=root.root_fingerprint,
            core_snapshot=snapshot,
            core_reachability=core_reachability,
            core_reachability_evidence=core_reachability_evidence,
            core_emergency_reachability=core_emergency_reachability,
            foundation_schedule=CoreFrozenPayload.from_mapping(schedule),
            foundation_band_policy=CoreFrozenPayload.from_mapping(
                provider_policy
            ),
            product_control=CoreFrozenPayload.from_mapping(product_control),
            schedule_revision=int(schedule.get("revision", 0) or 0),
            schedule_observed_at=float(schedule.get("ts", 0.0) or 0.0),
        )
        return self._input.publish_observation(
            IntelligenceMatrixObservation(
                key=key,
                root=root,
                constraints=constraints,
                causes=causes,
                published_at=published_at,
            )
        )

    def _provider_band_policy(self, root) -> dict[str, Any]:
        """Project Foundation policy without teaching Intelligence Foundation APIs."""

        provider_plmn = ""
        try:
            fact = root.current_facts.exact("identity.provider_plmn")
            if fact.disposition == "available":
                provider_plmn = str(
                    fact.value.to_mapping().get("value") or ""
                ).strip()
        except KeyError:
            pass
        policy: dict[str, Any] = {}
        reason = "provider_identity_unavailable"
        if provider_plmn:
            authority = self._product._foundation.provider_band_profiles
            try:
                policy = dict(
                    authority.resolve_contract(
                        domain="lte_allocated_auto",
                        provider_plmn=provider_plmn,
                        runtime_view=root.runtime_view,
                    )
                )
                reason = ""
            except (LookupError, ValueError) as exc:
                reason = str(exc) or "provider_band_policy_unavailable"
        return {
            "schema": INTELLIGENCE_PROVIDER_POLICY_SCHEMA,
            "owner": INTELLIGENCE_MATRIX_OWNER,
            "disposition": "available" if policy else "unavailable",
            "policy": policy,
            "reason": reason,
        }

    def _product_control_view(self) -> dict[str, Any]:
        """Return PIM-owned control truth for Brain's next deliberation."""

        with self._lifecycle:
            return {
                "schema": INTELLIGENCE_PRODUCT_CONTROL_SCHEMA,
                "owner": INTELLIGENCE_MATRIX_OWNER,
                "control_mode": self._control_mode,
                "control_available": self._control_available,
                "connected_settled": self._learning_settled,
                "autopilot_requested": self._autopilot_requested,
                "explore_requested": self._explore_requested,
                "explore_session_id": self._explore_session_id,
                "explore_started_at": self._explore_started_at,
                "explore_deadline_at": self._explore_deadline_at,
                "explore_reason": self._explore_reason,
                "control_generation": self._control_generation,
                "control_reason": self._control_reason,
            }

    def _refresh_outcomes(self) -> bool:
        observation = self._input.snapshot().observation
        if observation is None:
            return False
        progressed = False
        namespace = observation.root.matrix_identity.namespace
        while not self._stop.is_set():
            record = self._product._core.command_outcome_stream.claim_next(
                INTELLIGENCE_CLIENT_ID,
                runtime_namespace=namespace,
            )
            if record is None:
                return progressed
            try:
                self._input.publish_outcome(record)
                self._product._core.command_outcome_stream.acknowledge(
                    INTELLIGENCE_CLIENT_ID,
                    runtime_namespace=namespace,
                    key=record.key,
                )
                progressed = True
            except BaseException as exc:
                self._product._core.command_outcome_stream.release(
                    INTELLIGENCE_CLIENT_ID,
                    runtime_namespace=namespace,
                    key=record.key,
                    error=exc,
                )
                raise

    def _run_input_adapter(self) -> None:
        while not self._stop.is_set():
            try:
                progressed = self._refresh_observation()
                progressed = self._refresh_outcomes() or progressed
                self._reconcile_control_mode()
            except BaseException as exc:
                self._record_fault("input", exc)
            self._stop.wait(_RECOVERY_INTERVAL_S)

    def _dispatch(self, proposal: BrainCommandProposal) -> dict[str, Any]:
        with self._lifecycle:
            self._require_attached()
            lease = self._autonomous_lease
            mode = self._control_mode
            explore_requested = self._explore_requested
            control_available = self._control_available
        explore = proposal.opportunity_id.startswith("explore:")
        if (
            mode != _INTELLIGENCE_AUTOPILOT
            or (explore and (not explore_requested or not control_available))
        ):
            return {
                "submitted": False,
                "reason": "intelligence_autopilot_not_ready",
                "control_mode": mode,
            }
        if not lease:
            raise RuntimeError("product_intelligence_matrix_lease_missing")
        target = core_command_target_from_payload(
            proposal.action_target.target.to_mapping()
        )
        request = build_core_command_request(
            proposal.command_context,
            request_id=proposal.proposal_id,
            correlation_id=proposal.proposal_id,
            client_id=INTELLIGENCE_CLIENT_ID,
            client_role=CoreClientRole.AUTONOMOUS_INTELLIGENCE,
            origin=CoreCommandOrigin.AUTONOMOUS_INTELLIGENCE,
            command_kind=proposal.action_target.command_kind,
            target=target,
            parameters={
                "reason": "Brain selected " + proposal.opportunity_id,
                "intent": proposal.execution_intent,
                "objective_id": proposal.proposal_id,
                "autonomous_source": "explore" if explore else "smart_loop",
                "action_parameters": {"sequential": False},
            },
            capability_reference=proposal.capability_reference,
            created_at=proposal.created_at,
            expires_at=proposal.expires_at,
            confirmed=False,
            safety_context={
                "proposal_schema": proposal.schema,
                "opportunity_id": proposal.opportunity_id,
                "profile_id": proposal.profile_id,
                "confidence": proposal.confidence,
                "evidence_refs": list(proposal.evidence_refs),
                "predicted_grades": proposal.predicted_grades.to_payload(),
                "verification_requirement": proposal.verification_requirement,
                "rollback_requirement": proposal.rollback_requirement,
            },
            autonomous_lease=lease,
        )
        return require_core_command_gateway(self._product._core).submit(request)

    def _publish_staged_proposal_result_once(self) -> bool:
        staged_result = self._output.claim_proposal_result()
        if staged_result is None:
            return False
        acknowledgement = self._input.publish_proposal_result(staged_result)
        self._output.acknowledge_proposal_result(acknowledgement)
        return True

    def _settle_output_before_clear(self, *, reason: str) -> None:
        """Close or deliver every proposal before output cells are cleared."""

        self._publish_staged_proposal_result_once()
        proposal = self._output.claim_undispatched_proposal_for_rejection()
        if proposal is None:
            return
        with self._lifecycle:
            self._dispatch_sequence += 1
            sequence = self._dispatch_sequence
        self._output.stage_proposal_result(
            IntelligenceProposalDispatchResult(
                sequence=sequence,
                proposal_id=proposal.proposal_id,
                proposal=proposal,
                submitted=False,
                reason=reason,
                result=CoreFrozenPayload.from_mapping(
                    {"submitted": False, "reason": reason}
                ),
                published_at=time(),
            )
        )
        self._publish_staged_proposal_result_once()

    def _run_output_adapter(self) -> None:
        while not self._stop.is_set():
            # A Core dispatch result is already causal and cannot wait behind
            # an optional learned-baseline retry.
            staged_result = self._output.claim_proposal_result()
            if staged_result is not None:
                try:
                    self._publish_staged_proposal_result_once()
                except BaseException as exc:
                    # The Core dispatch has already happened. Keep this exact
                    # result in the outbox and retry only the publication.
                    self._record_fault("proposal_result", exc)
                    self._stop.wait(_RECOVERY_INTERVAL_S)
                continue

            baselines = self._output.claim_signal_baselines()
            if baselines is not None:
                try:
                    result = self._apply_signal_baselines(baselines)
                except BaseException as exc:
                    # Context availability is represented by a typed transient
                    # result. An exception here is an invariant/programming
                    # fault and must not become an endless quiet retry.
                    self._record_critical_failure("signal_baselines", exc)
                    return
                if not isinstance(result, _SignalBaselineApplication):
                    self._record_critical_failure(
                        "signal_baselines",
                        TypeError("signal_baseline_application_result_required"),
                    )
                    return
                if result.terminal:
                    self._output.complete_signal_baselines(baselines)
                else:
                    self._stop.wait(_RECOVERY_INTERVAL_S)

            proposal = self._output.claim_proposal()
            if proposal is None:
                self._output.wait_for_output(
                    timeout_s=_RECOVERY_INTERVAL_S,
                )
                continue
            try:
                result = dict(self._dispatch(proposal))
                raw_submitted = result.get("submitted")
                if type(raw_submitted) is not bool:
                    raise TypeError(
                        "intelligence_proposal_submitted_bool_required"
                    )
                submitted = raw_submitted
                reason = str(result.get("reason", "") or "")
            except BaseException as exc:
                submitted = False
                reason = (
                    "proposal_submission_error:"
                    + type(exc).__name__
                    + ":"
                    + str(exc)
                )
                result = {"submitted": False, "reason": reason}
                self._record_fault("proposal", exc)
            with self._lifecycle:
                self._dispatch_sequence += 1
                sequence = self._dispatch_sequence
            try:
                self._output.stage_proposal_result(
                    IntelligenceProposalDispatchResult(
                        sequence=sequence,
                        proposal_id=proposal.proposal_id,
                        proposal=proposal,
                        submitted=submitted,
                        reason=reason,
                        result=CoreFrozenPayload.from_mapping(result),
                        published_at=time(),
                    ),
                )
            except BaseException as exc:
                # Staging is a local owner invariant. Retrying dispatch or
                # waiting forever would hide an already-executed Core action.
                self._record_critical_failure("proposal_result_stage", exc)
                return

    @staticmethod
    def _available_fact(root: Any, path: str) -> Any | None:
        fact = root.current_facts.exact(path)
        if fact.disposition != "available":
            return None
        return fact.value.to_mapping()["value"]

    def _apply_signal_baselines(
        self,
        snapshot: IntelligenceSignalBaselineSnapshot,
    ) -> _SignalBaselineApplication:
        """Validate current Matrix context before serialized signal admission."""

        root = self._product._core.accepted_root_outbox.current()
        if root is None:
            return _SignalBaselineApplication(
                status="transient",
                reason="accepted_root_unavailable",
            )
        if snapshot.namespace != root.matrix_identity.namespace:
            return _SignalBaselineApplication(
                status="stale",
                reason="namespace_replaced",
            )
        sim = self._available_fact(root, "identity.sim_network")
        subscription = sim.get("subscription_key") if isinstance(sim, Mapping) else None
        if not subscription:
            return _SignalBaselineApplication(status="transient", reason="current_subscription_missing")
        if subscription != snapshot.subscription_key:
            return _SignalBaselineApplication(status="stale", reason="subscription_replaced")
        bandwidth_fact = root.current_facts.exact("slow.dl_bandwidth_mhz")
        bandwidth_state = bandwidth_fact.disposition
        bandwidth_mhz = (
            float(bandwidth_fact.value.to_mapping()["value"])
            if bandwidth_state == "available"
            else 0.0
        )
        pool = self._available_fact(root, "radio.configured_band_pool")
        current = {
            "identity_code": root.observation_frame.identity_code,
            "profile_id": self._available_fact(root, "identity.profile_id"),
            "provider_plmn": self._available_fact(root, "identity.provider_plmn"),
            "network_type": self._available_fact(root, "radio.network_type"),
            "serving_band": self._available_fact(root, "radio.serving_band"),
            "configured_band_pool": (
                tuple(sorted(set(pool))) if isinstance(pool, (list, tuple)) else ()
            ),
            "cell_id": self._available_fact(root, "rf.cell_id"),
            "earfcn": self._available_fact(root, "rf.earfcn_dl"),
            "pci": self._available_fact(root, "rf.pci"),
            "bandwidth_state": bandwidth_state,
            "bandwidth_mhz": bandwidth_mhz,
        }
        if any(current[name] is None for name in (
            "profile_id",
            "provider_plmn",
            "network_type",
            "serving_band",
            "cell_id",
            "earfcn",
            "pci",
        )) or not current["configured_band_pool"]:
            return _SignalBaselineApplication(
                status="transient",
                reason="current_context_incomplete",
            )
        if intelligence_signal_scope_key(**current) != snapshot.signal_scope_key:
            return _SignalBaselineApplication(
                status="stale",
                reason="scope_replaced",
            )
        self._product._core.signals.admit_learned_signal_baselines(
            serving_band=current["serving_band"],
            subscription_key=subscription,
            network_plmn=current["provider_plmn"],
            cell_id=current["cell_id"],
            earfcn=current["earfcn"],
            bandwidth_mhz=current["bandwidth_mhz"],
            rsrq_baseline_db=snapshot.rsrq_baseline_db,
            rtt_baseline_ms=snapshot.rtt_baseline_ms,
            learned_at=snapshot.learned_at,
        )
        return _SignalBaselineApplication(
            status="applied",
            reason="signal_baseline_admitted",
        )

    def read_inputs(
        self,
        *,
        after_revision: int,
        timeout_s: float,
    ) -> IntelligenceMatrixInputSnapshot:
        with self._lifecycle:
            self._require_attached()
        return self._input.wait_after(
            after_revision,
            timeout_s=timeout_s,
        )

    def publish_presentation(
        self,
        frame: IntelligencePresentationSnapshot,
    ) -> None:
        with self._lifecycle:
            self._require_attached()
        self._output.publish_presentation(frame)

    def _autopilot_receipt(
        self,
        request: IntelligenceAutopilotControlRequest,
    ) -> OperationalServiceReceipt:
        with self._lifecycle:
            requested = self._autopilot_requested
            available = self._control_available
            mode = self._control_mode
            reason = self._control_reason
            generation = self._control_generation
            explore_requested = self._explore_requested
        return OperationalServiceReceipt(
            request_id=request.context.request_id,
            kind=OperationalKind.SET_INTELLIGENCE_AUTOPILOT,
            owner=INTELLIGENCE_MATRIX_OWNER,
            disposition=OperationalDisposition.CONFIRMED,
            message=(
                "Brain enabled."
                if requested and available
                else "Brain requested; it is still qualifying."
                if requested
                else "Routine Brain actions disabled; Explore remains active."
                if explore_requested and mode == _INTELLIGENCE_AUTOPILOT
                else "Brain is observing; automatic actions are disabled."
                if mode == _INTELLIGENCE_OBSERVE
                else "Brain disabled; CGF standalone control is active."
            ),
            occurred_at=time(),
            runtime_namespace=request.context.runtime_namespace,
            matrix_identity=request.context.matrix_identity,
            detail=CoreFrozenPayload.from_mapping(
                {
                    "requested": requested,
                    "control_available": available,
                    "control_mode": mode,
                    "control_reason": reason,
                    "control_generation": generation,
                }
            ),
        )

    def _clear_explore_locked(self, reason: str) -> bool:
        changed = bool(
            self._explore_requested
            or self._explore_session_id
            or self._explore_started_at
            or self._explore_deadline_at
        )
        self._explore_requested = False
        self._explore_session_id = ""
        self._explore_started_at = 0.0
        self._explore_deadline_at = 0.0
        self._explore_reason = str(reason or "not_requested")
        if changed:
            self._control_generation += 1
        return changed

    def _explore_receipt(
        self,
        request: IntelligenceExploreControlRequest,
    ) -> OperationalServiceReceipt:
        with self._lifecycle:
            requested = self._explore_requested
            session_id = self._explore_session_id
            started_at = self._explore_started_at
            deadline_at = self._explore_deadline_at
            reason = self._explore_reason
            generation = self._control_generation
        return OperationalServiceReceipt(
            request_id=request.context.request_id,
            kind=OperationalKind.SET_INTELLIGENCE_EXPLORE,
            owner=INTELLIGENCE_MATRIX_OWNER,
            disposition=OperationalDisposition.CONFIRMED,
            message=(
                "Explore activated for up to three hours."
                if requested
                else "Explore stopped."
            ),
            occurred_at=time(),
            runtime_namespace=request.context.runtime_namespace,
            matrix_identity=request.context.matrix_identity,
            detail=CoreFrozenPayload.from_mapping(
                {
                    "requested": requested,
                    "session_id": session_id,
                    "started_at": started_at,
                    "deadline_at": deadline_at,
                    "reason": reason,
                    "control_generation": generation,
                }
            ),
        )

    @staticmethod
    def _control_status_message(
        *,
        requested: bool,
        control_mode: str,
        control_reason: str,
    ) -> str:
        """Describe Product-owned Autopilot state without GUI inference."""

        if control_mode == _INTELLIGENCE_AUTOPILOT:
            return "BRAIN ACTIVATED"
        if not requested:
            return "BRAIN DEACTIVATED"
        if control_mode == _INTELLIGENCE_OBSERVE:
            return "BRAIN OBSERVING — waiting for stable qualification."
        if control_reason in {
            "intelligence_runtime_unavailable",
            "intelligence_critical_off",
        }:
            return "BRAIN STOPPED — it requires attention."
        return "BRAIN UNAVAILABLE"

    def set_autopilot_requested(
        self,
        request: IntelligenceAutopilotControlRequest,
        *,
        permit: ProductOperationalEffectPermit,
    ) -> OperationalServiceReceipt:
        require_product_operational_effect_permit(
            permit,
            request,
            expected_kind=OperationalKind.SET_INTELLIGENCE_AUTOPILOT,
        )
        with self._lifecycle:
            # Safe OFF is unconditional. Qualification is required only when
            # enabling the autonomous issuer.
            if request.enabled and not self._control_available:
                raise RuntimeError(
                    "intelligence_autopilot_control_unavailable"
                )
            if self._autopilot_requested is not request.enabled:
                self._autopilot_requested = request.enabled
                self._control_generation += 1
        self._reconcile_control_mode()
        self._refresh_observation()
        return self._autopilot_receipt(request)

    def reconcile_autopilot_request(
        self,
        request: IntelligenceAutopilotControlRequest,
        *,
        permit: ProductOperationalEffectPermit,
    ) -> OperationalServiceReceipt | None:
        require_product_operational_effect_permit(
            permit,
            request,
            expected_kind=OperationalKind.SET_INTELLIGENCE_AUTOPILOT,
        )
        with self._lifecycle:
            if self._autopilot_requested is not request.enabled:
                return None
        return self._autopilot_receipt(request)

    def set_explore_requested(
        self,
        request: IntelligenceExploreControlRequest,
        *,
        permit: ProductOperationalEffectPermit,
    ) -> OperationalServiceReceipt:
        require_product_operational_effect_permit(
            permit,
            request,
            expected_kind=OperationalKind.SET_INTELLIGENCE_EXPLORE,
        )
        now = time()
        with self._lifecycle:
            if request.enabled:
                if not self._control_available:
                    raise RuntimeError(
                        "intelligence_explore_control_unavailable"
                    )
                if not self._explore_requested:
                    self._explore_requested = True
                    self._explore_session_id = request.context.request_id
                    self._explore_started_at = now
                    self._explore_deadline_at = now + _EXPLORE_DURATION_S
                    self._explore_reason = "user_authorized"
                    self._control_generation += 1
            else:
                self._clear_explore_locked("user_stopped")
        self._reconcile_control_mode()
        self._refresh_observation()
        return self._explore_receipt(request)

    def reconcile_explore_request(
        self,
        request: IntelligenceExploreControlRequest,
        *,
        permit: ProductOperationalEffectPermit,
    ) -> OperationalServiceReceipt | None:
        require_product_operational_effect_permit(
            permit,
            request,
            expected_kind=OperationalKind.SET_INTELLIGENCE_EXPLORE,
        )
        with self._lifecycle:
            if self._explore_requested is not request.enabled:
                return None
        return self._explore_receipt(request)

    def publish_proposal(self, proposal: BrainCommandProposal) -> None:
        with self._lifecycle:
            self._require_attached()
        self._output.publish_proposal(proposal)

    def publish_health(self, health: IntelligenceHealthSnapshot) -> None:
        with self._lifecycle:
            self._require_attached()
        self._output.publish_health(health)
        self._reconcile_control_mode()

    def publish_signal_baselines(
        self,
        snapshot: IntelligenceSignalBaselineSnapshot,
    ) -> None:
        with self._lifecycle:
            self._require_attached()
        self._output.publish_signal_baselines(snapshot)

    def latest_presentation(
        self,
        namespace: "RuntimeGenerationNamespace",
    ) -> NGUIExtensionFrame | None:
        output = self._output.snapshot()
        source = output.presentation
        with self._lifecycle:
            if self._presentation_namespace != namespace:
                return None
        if source is not None and source.namespace != namespace:
            return None
        bouncer = self._product._core.bouncer.get_runtime_status()
        latch = bouncer.get("hard_stop_latch", {})
        latch = latch if isinstance(latch, dict) else {}
        bouncer_text = (
            "HARD STOP: " + str(latch.get("reason") or "active")
            if latch.get("active") is True
            else "READY"
        )
        with self._lifecycle:
            requested = self._autopilot_requested
            explore_requested = self._explore_requested
            explore_session_id = self._explore_session_id
            explore_deadline_at = self._explore_deadline_at
            explore_reason = self._explore_reason
            control_available = self._control_available
            control_mode = self._control_mode
            control_reason = self._control_reason
            control_generation = self._control_generation
            qualification_remaining_s = (
                0.0
                if self._qualification_complete
                else _QUALIFICATION_INTERVAL_S
                if not self._qualification_started_at
                else max(
                    0.0,
                    _QUALIFICATION_INTERVAL_S
                    - (monotonic() - self._qualification_started_at),
                )
            )
            qualification_display_s = float(
                int(qualification_remaining_s + 0.999)
            )
            projection_key = (
                namespace,
                source.sequence if source is not None else 0,
                control_generation,
                requested,
                control_available,
                control_mode,
                control_reason,
                qualification_display_s,
                bouncer_text,
            )
            if projection_key == self._presentation_key:
                return self._presentation_publisher.latest_for(namespace)
        now = time()
        # This adapter combines the latest Intelligence owner frame with
        # Product-owned control facts. A Windows wall-clock correction can
        # make this fresh read a little earlier than the already accepted
        # owner frame. Keep the source timestamps exact and date the combined
        # frame no earlier than the newest input it contains.
        causal_published_at = max(
            now,
            source.published_at if source is not None else 0.0,
        )
        if source is None:
            values = {
                source_id: None
                for source_id in NEW_HORIZON_NGUI_SOURCE_IDS
            }
            validity = {
                source_id: False
                for source_id in NEW_HORIZON_NGUI_SOURCE_IDS
            }
            freshness = {
                source_id: "unknown"
                for source_id in NEW_HORIZON_NGUI_SOURCE_IDS
            }
            reasons = {
                source_id: control_reason
                for source_id in NEW_HORIZON_NGUI_SOURCE_IDS
            }
            sampled_at = {
                source_id: 0.0
                for source_id in NEW_HORIZON_NGUI_SOURCE_IDS
            }
            sample_sequences = {
                source_id: 0
                for source_id in NEW_HORIZON_NGUI_SOURCE_IDS
            }
        else:
            values = source.values.to_mapping()
            validity = source.validity.to_mapping()
            freshness = source.freshness.to_mapping()
            reasons = source.reasons.to_mapping()
            sampled_at = source.sampled_at.to_mapping()
            sample_sequences = source.sample_sequences.to_mapping()
        values.update(
            {
                "intelligence.autopilot_requested": (
                    "ON" if requested else "OFF"
                ),
                "intelligence.explore_requested": (
                    "ON" if explore_requested else "OFF"
                ),
                "intelligence.explore_session_id": explore_session_id,
                "intelligence.explore_deadline_at": explore_deadline_at,
                "intelligence.explore_reason": explore_reason,
                "intelligence.control_available": control_available,
                "intelligence.control_mode": control_mode,
                "intelligence.control_reason": control_reason,
                "intelligence.control_status_message": (
                    self._control_status_message(
                        requested=requested,
                        control_mode=control_mode,
                        control_reason=control_reason,
                    )
                ),
                "intelligence.qualification_remaining_s": (
                    qualification_display_s
                ),
                "intelligence.supervision": (
                    "READY"
                    if control_available
                    else "CRITICAL"
                    if control_reason in {
                        "intelligence_runtime_unavailable",
                        "intelligence_critical_off",
                    }
                    else "QUALIFYING"
                    if control_mode == _INTELLIGENCE_OBSERVE
                    else "UNATTACHED"
                ),
                "intelligence.bouncer_status": bouncer_text,
            }
        )
        for source_id in INTELLIGENCE_NGUI_SOURCE_IDS - set(validity):
            validity[source_id] = True
            freshness[source_id] = "fresh"
            reasons[source_id] = ""
            sampled_at[source_id] = now
            sample_sequences[source_id] = max(1, control_generation + 1)
        frame = self._presentation_publisher.publish(
            values,
            namespace=namespace,
            validity=validity,
            freshness=freshness,
            reasons=reasons,
            sampled_at=sampled_at,
            sample_sequences=sample_sequences,
            published_at=causal_published_at,
        )
        with self._lifecycle:
            self._presentation_key = projection_key
        return frame

    def status(self) -> CoreFrozenPayload:
        with self._lifecycle:
            attached = self._root is not None
            faults = self._fault_count
            error = self._last_error
            control_mode = self._control_mode
            control_generation = self._control_generation
            control_reason = self._control_reason
            autopilot_requested = self._autopilot_requested
            explore_requested = self._explore_requested
            explore_session_id = self._explore_session_id
            explore_deadline_at = self._explore_deadline_at
            explore_reason = self._explore_reason
            control_available = self._control_available
            qualification_started_at = self._qualification_started_at
            qualification_complete = self._qualification_complete
            critical_failure = self._critical_failure
            qualification_remaining_s = (
                0.0
                if qualification_complete
                else _QUALIFICATION_INTERVAL_S
                if not qualification_started_at
                else max(
                    0.0,
                    _QUALIFICATION_INTERVAL_S
                    - (monotonic() - qualification_started_at),
                )
            )
        input_state = self._input.snapshot()
        output_state = self._output.snapshot()
        return CoreFrozenPayload.from_mapping(
            {
                "schema": "ProductIntelligenceMatrixStatus.v1",
                "owner": INTELLIGENCE_MATRIX_OWNER,
                "attached": attached,
                "input_revision": input_state.revision,
                "output_revision": output_state.revision,
                "fault_count": faults,
                "last_error": error,
                "control_mode": control_mode,
                "control_generation": control_generation,
                "control_reason": control_reason,
                "autopilot_requested": autopilot_requested,
                "explore_requested": explore_requested,
                "explore_session_id": explore_session_id,
                "explore_deadline_at": explore_deadline_at,
                "explore_reason": explore_reason,
                "control_available": control_available,
                "qualification_complete": qualification_complete,
                "qualification_remaining_s": qualification_remaining_s,
                "critical_failure": critical_failure,
            }
        )


__all__ = (
    "IntelligenceMatrixCustomerPort",
    "ProductIntelligenceMatrix",
)
