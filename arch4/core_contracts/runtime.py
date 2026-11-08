"""Direct structural contract for the permanent Core runtime owner."""

from __future__ import annotations

from typing import Any, Protocol

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)

from arch4.core_contracts.accepted_root_outbox import (
    CoreAcceptedRootOutboxPort,
)
from arch4.core_contracts.current_health import CoreCurrentHealthView
from arch4.core_contracts.action_reachability import (
    CoreCurrentReachabilityView,
    CoreReachabilityEvidenceView,
)
from arch4.core_contracts.emergency_reachability import (
    CoreEmergencyReachabilityView,
    CoreEmergencyRestartPermit,
    RecoveryTimingProfile,
)
from arch4.core_contracts.commands import (
    CoreCommandExecutionAbortReceipt,
    CoreCommandExecutionGate,
    CoreCommandExecutionReceipt,
    CoreCommandRequest,
)
from arch4.core_contracts.runtime_snapshot import CoreRuntimeSnapshotPort


class CoreRuntimePort(Protocol):
    """Core-only runtime surface; customer branches are intentionally absent."""

    cfg: Any
    input_module: Any
    signals: Any
    capabilities: Any
    constraints: Any
    diagnostics: Any
    register_bank: Any
    accepted_root_outbox: CoreAcceptedRootOutboxPort
    runtime_snapshot_publisher: CoreRuntimeSnapshotPort
    modem_sample_publication: Any
    clock: Any
    clock_service: Any
    clock_job_workers: Any
    command_authority: Any
    command_lifecycle: Any
    command_gateway: Any
    command_outcome_stream: Any
    persistence: Any
    active_configuration: Any
    bouncer: Any
    action_source_state_authority: Any
    radio_transition_authority: Any
    fault_supervisor: Any
    worker_health: Any
    observe_only: bool
    check_only: bool
    probe_mode: bool

    def bind_provider_pool_policy(self, resolver: Any) -> None: ...

    def current_recovery_timing(self) -> RecoveryTimingProfile: ...

    def execute_core_cellular_command(
        self,
        request: CoreCommandRequest,
        action: Any,
        **kwargs: Any,
    ) -> CoreCommandExecutionReceipt: ...

    def abort_core_cellular_command(
        self,
        request: CoreCommandRequest,
        *,
        execution_gate: CoreCommandExecutionGate,
        reason: str,
    ) -> CoreCommandExecutionAbortReceipt: ...

    def current_health_view(self) -> CoreCurrentHealthView: ...

    def current_reachability_view(
        self,
        *,
        now: float,
    ) -> CoreCurrentReachabilityView: ...

    def current_reachability_evidence_view(
        self,
        *,
        now: float,
    ) -> CoreReachabilityEvidenceView: ...

    def current_emergency_reachability_view(
        self,
        *,
        now: float,
    ) -> CoreEmergencyReachabilityView: ...

    def verify_emergency_reachability(
        self,
        *,
        now: float,
    ) -> CoreEmergencyReachabilityView: ...

    def restart_emergency_reachability_verification(
        self,
        *,
        incident_id: str,
        now: float,
    ) -> CoreEmergencyReachabilityView: ...

    def issue_emergency_restart_permit(
        self,
        *,
        request_id: str,
        matrix_identity: MatrixGenerationIdentity,
        identity_code: str,
        incident_id: str,
        now: float,
    ) -> CoreEmergencyRestartPermit: ...

    def validate_emergency_restart_permit(
        self,
        permit: CoreEmergencyRestartPermit,
        *,
        now: float,
    ) -> bool: ...

    def current_device_recovery_state(self) -> Any: ...

    def _advance_runtime_clock(self) -> None: ...

__all__ = ("CoreRuntimePort",)
