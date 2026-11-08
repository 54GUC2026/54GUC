"""Canonical action description, origin, and fail-closed admission owner.

This module owns the mechanical contract shared by every ``LeverAction``
origin.  It does not rank autonomous candidates and it never talks to a modem.
CommandAuthority remains the physical execution-lease owner; MutationAuthority
may execute only an action carrying a receipt issued here.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, replace
from enum import Enum
import threading
import time
from types import MappingProxyType
from typing import Any, Mapping

from arch4.action_fingerprint import action_semantic_fingerprint
from arch4.action_origin import (
    ActionAuthority,
    ActionOriginKind,
    HUMAN_OPERATOR_ORIGINS,
    action_authority_for_origin,
)
from arch4.action_transaction import (
    ActionTransactionKey,
    action_transaction_key_from_action,
    bind_action_transaction_key,
    bound_core_command_target_from_action,
)
from arch4.core_contracts.accepted_runtime_view import (
    CoreAcceptedRuntimeView,
)
from arch4.core_contracts.emergency_reachability import (
    CoreEmergencyRestartPermit,
)
from arch4.core_contracts.command_safety import CoreCommandSafetyEvidence
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.interfaces import LeverAction, LeverType
from arch4.core.band_action_policy import (
    BandActionPolicyDecision,
    is_band_control_action,
    normalized_name,
)


SCHEMA = "action_admission.v1"
OWNER = "arch4.action_admission"
AUTHORITY_SCHEMA = "action_admission_authority.v1"
AUTHORITY_ATTR = "_action_admission_authority"
ORIGIN_ATTR = "_action_origin"
SELECTED_CONTEXT_ATTR = "_selected_action_context"
AUTHORITY_LEDGER_ATTR = "_action_admission_authority_records"
AUTHORITY_LOCK_ATTR = "_action_admission_authority_lock"
MAX_AUTHORITY_RECEIPTS = 2048
IDENTITY_MAX_AGE_S = 30.0


class ActionFamily(str, Enum):
    CELLULAR_RADIO = "cellular_radio"
    CELLULAR_SESSION = "cellular_session"
    TRAFFIC_POLICY = "traffic_policy"


class ActionExecutionBoundary(str, Enum):
    CELLULAR_COMMAND = "cellular_command"


class ActionRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionVerification(str, Enum):
    RADIO_TARGET_AND_FRESH_SAMPLE = "radio_target_and_fresh_sample"
    MODEM_RECONNECT_AND_FRESH_SAMPLE = "modem_reconnect_and_fresh_sample"
    CAPABILITY_READBACK = "capability_readback"
    TRAFFIC_READBACK = "traffic_readback"


class ActionProof(str, Enum):
    DELAYED_REFEREE = "delayed_referee"
    EXECUTION_ONLY = "execution_only"


class ActionLearning(str, Enum):
    GRADED_ALL_ORIGINS = "graded_all_origins"
    DIAGNOSTIC_ONLY = "diagnostic_only"
    PROHIBITED = "prohibited"


@dataclass(frozen=True)
class ActionDescriptor:
    lever: LeverType
    family: ActionFamily
    execution_boundary: ActionExecutionBoundary
    mutates_cellular_radio: bool
    requires_modem_write: bool
    required_capability: str
    risk: ActionRisk
    settling_s: float
    verification: ActionVerification
    proof: ActionProof
    learning: ActionLearning
    allowed_origins: frozenset[ActionOriginKind]

    @property
    def requires_cellular_recovery(self) -> bool:
        """Cellular writes whose completion requires the service to return."""
        return self.mutates_cellular_radio and self.verification in {
            ActionVerification.RADIO_TARGET_AND_FRESH_SAMPLE,
            ActionVerification.MODEM_RECONNECT_AND_FRESH_SAMPLE,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "lever": self.lever.name,
            "family": self.family.value,
            "execution_boundary": self.execution_boundary.value,
            "mutates_cellular_radio": self.mutates_cellular_radio,
            "requires_modem_write": self.requires_modem_write,
            "required_capability": self.required_capability,
            "risk": self.risk.value,
            "settling_s": float(self.settling_s),
            "verification": self.verification.value,
            "proof": self.proof.value,
            "learning": self.learning.value,
            "allowed_origins": sorted(origin.value for origin in self.allowed_origins),
        }


_RADIO_ORIGINS = frozenset(
    {
        ActionOriginKind.SMART_LOOP,
        ActionOriginKind.EXPLORE,
        ActionOriginKind.FAULT_RECOVERY,
        ActionOriginKind.OPERATOR_NGUI,
        ActionOriginKind.OPERATOR_SMS,
        ActionOriginKind.SCHEDULE,
    }
)
ACTION_DESCRIPTORS: Mapping[LeverType, ActionDescriptor] = MappingProxyType({
    LeverType.RADIO_MODE: ActionDescriptor(
        LeverType.RADIO_MODE, ActionFamily.CELLULAR_RADIO,
        ActionExecutionBoundary.CELLULAR_COMMAND, True, True,
        "radio_mode", ActionRisk.HIGH, 60.0,
        ActionVerification.RADIO_TARGET_AND_FRESH_SAMPLE,
        ActionProof.EXECUTION_ONLY, ActionLearning.PROHIBITED,
        frozenset({ActionOriginKind.SMART_LOOP, ActionOriginKind.OPERATOR_NGUI}),
    ),
    LeverType.BAND_SELECTION: ActionDescriptor(
        LeverType.BAND_SELECTION,
        ActionFamily.CELLULAR_RADIO,
        ActionExecutionBoundary.CELLULAR_COMMAND,
        True,
        True,
        "band_selection",
        ActionRisk.HIGH,
        60.0,
        ActionVerification.RADIO_TARGET_AND_FRESH_SAMPLE,
        ActionProof.DELAYED_REFEREE,
        ActionLearning.GRADED_ALL_ORIGINS,
        _RADIO_ORIGINS,
    ),
    LeverType.BAND_LOCK: ActionDescriptor(
        LeverType.BAND_LOCK,
        ActionFamily.CELLULAR_RADIO,
        ActionExecutionBoundary.CELLULAR_COMMAND,
        True,
        True,
        "band_lock",
        ActionRisk.HIGH,
        60.0,
        ActionVerification.RADIO_TARGET_AND_FRESH_SAMPLE,
        ActionProof.DELAYED_REFEREE,
        ActionLearning.GRADED_ALL_ORIGINS,
        _RADIO_ORIGINS,
    ),
    LeverType.CELL_LOCK: ActionDescriptor(
        LeverType.CELL_LOCK,
        ActionFamily.CELLULAR_RADIO,
        ActionExecutionBoundary.CELLULAR_COMMAND,
        True,
        True,
        "cell_lock",
        ActionRisk.HIGH,
        60.0,
        ActionVerification.RADIO_TARGET_AND_FRESH_SAMPLE,
        ActionProof.DELAYED_REFEREE,
        ActionLearning.GRADED_ALL_ORIGINS,
        _RADIO_ORIGINS | {ActionOriginKind.LEASE_EXPIRY},
    ),
    LeverType.TRAFFIC_PRESENCE: ActionDescriptor(
        LeverType.TRAFFIC_PRESENCE,
        ActionFamily.TRAFFIC_POLICY,
        ActionExecutionBoundary.CELLULAR_COMMAND,
        False,
        True,
        "traffic_presence",
        ActionRisk.MEDIUM,
        0.0,
        ActionVerification.TRAFFIC_READBACK,
        ActionProof.EXECUTION_ONLY,
        ActionLearning.DIAGNOSTIC_ONLY,
        frozenset(
            {
                ActionOriginKind.SMART_LOOP,
                ActionOriginKind.FAULT_RECOVERY,
                ActionOriginKind.OPERATOR_NGUI,
                ActionOriginKind.OPERATOR_SMS,
                ActionOriginKind.SCHEDULE,
            }
        ),
    ),
    LeverType.TRAFFIC_VOLUME: ActionDescriptor(
        LeverType.TRAFFIC_VOLUME,
        ActionFamily.TRAFFIC_POLICY,
        ActionExecutionBoundary.CELLULAR_COMMAND,
        False,
        True,
        "traffic_volume",
        ActionRisk.MEDIUM,
        0.0,
        ActionVerification.TRAFFIC_READBACK,
        ActionProof.EXECUTION_ONLY,
        ActionLearning.DIAGNOSTIC_ONLY,
        HUMAN_OPERATOR_ORIGINS | {ActionOriginKind.SCHEDULE},
    ),
    LeverType.UE_CAPABILITY: ActionDescriptor(
        LeverType.UE_CAPABILITY,
        ActionFamily.CELLULAR_SESSION,
        ActionExecutionBoundary.CELLULAR_COMMAND,
        True,
        True,
        "ue_capability",
        ActionRisk.CRITICAL,
        60.0,
        ActionVerification.CAPABILITY_READBACK,
        ActionProof.EXECUTION_ONLY,
        ActionLearning.PROHIBITED,
        _RADIO_ORIGINS,
    ),
    LeverType.RESTART: ActionDescriptor(
        LeverType.RESTART,
        ActionFamily.CELLULAR_SESSION,
        ActionExecutionBoundary.CELLULAR_COMMAND,
        True,
        True,
        "restart",
        ActionRisk.CRITICAL,
        90.0,
        ActionVerification.MODEM_RECONNECT_AND_FRESH_SAMPLE,
        ActionProof.DELAYED_REFEREE,
        ActionLearning.GRADED_ALL_ORIGINS,
        frozenset(
            {
                ActionOriginKind.FAULT_RECOVERY,
                ActionOriginKind.OPERATOR_NGUI,
                ActionOriginKind.OPERATOR_SMS,
                ActionOriginKind.WATCHDOG,
            }
        ),
    ),
    LeverType.PBAND: ActionDescriptor(
        LeverType.PBAND,
        ActionFamily.CELLULAR_RADIO,
        ActionExecutionBoundary.CELLULAR_COMMAND,
        True,
        True,
        "pband",
        ActionRisk.HIGH,
        60.0,
        ActionVerification.RADIO_TARGET_AND_FRESH_SAMPLE,
        ActionProof.DELAYED_REFEREE,
        ActionLearning.GRADED_ALL_ORIGINS,
        _RADIO_ORIGINS,
    ),
    LeverType.NR_BAND_LOCK: ActionDescriptor(
        LeverType.NR_BAND_LOCK,
        ActionFamily.CELLULAR_RADIO,
        ActionExecutionBoundary.CELLULAR_COMMAND,
        True,
        True,
        "nr_band_lock",
        ActionRisk.HIGH,
        60.0,
        ActionVerification.RADIO_TARGET_AND_FRESH_SAMPLE,
        ActionProof.DELAYED_REFEREE,
        ActionLearning.GRADED_ALL_ORIGINS,
        HUMAN_OPERATOR_ORIGINS,
    ),
    LeverType.NETWORK_SETTINGS: ActionDescriptor(
        LeverType.NETWORK_SETTINGS,
        ActionFamily.CELLULAR_SESSION,
        ActionExecutionBoundary.CELLULAR_COMMAND,
        True,
        True,
        "network_settings",
        ActionRisk.HIGH,
        15.0,
        ActionVerification.RADIO_TARGET_AND_FRESH_SAMPLE,
        ActionProof.EXECUTION_ONLY,
        ActionLearning.PROHIBITED,
        HUMAN_OPERATOR_ORIGINS,
    ),
    LeverType.MOBILE_DATA: ActionDescriptor(
        LeverType.MOBILE_DATA,
        ActionFamily.CELLULAR_SESSION,
        ActionExecutionBoundary.CELLULAR_COMMAND,
        False,
        True,
        "mobile_data",
        ActionRisk.HIGH,
        15.0,
        ActionVerification.CAPABILITY_READBACK,
        ActionProof.EXECUTION_ONLY,
        ActionLearning.PROHIBITED,
        HUMAN_OPERATOR_ORIGINS,
    ),
    LeverType.WIFI_RADIO: ActionDescriptor(
        LeverType.WIFI_RADIO,
        ActionFamily.CELLULAR_SESSION,
        ActionExecutionBoundary.CELLULAR_COMMAND,
        False,
        True,
        "wifi_control",
        ActionRisk.MEDIUM,
        0.0,
        ActionVerification.CAPABILITY_READBACK,
        ActionProof.EXECUTION_ONLY,
        ActionLearning.PROHIBITED,
        HUMAN_OPERATOR_ORIGINS,
    ),
})


def action_descriptor(lever: LeverType) -> ActionDescriptor:
    descriptor = ACTION_DESCRIPTORS.get(lever)
    if descriptor is None:
        raise KeyError(f"action_descriptor_missing:{getattr(lever, 'name', lever)}")
    return descriptor


def action_expects_modem_interruption(action: LeverAction) -> bool:
    """Project action semantics, including the explicit UE restart request."""
    descriptor = action_descriptor(action.lever)
    return descriptor.requires_cellular_recovery or bool(
        descriptor.lever is LeverType.UE_CAPABILITY
        and isinstance(action.params, Mapping)
        and action.params.get("restart") is True
    )


def assert_action_descriptor_coverage() -> None:
    missing = sorted(lever.name for lever in set(LeverType) - set(ACTION_DESCRIPTORS))
    extra = sorted(str(lever) for lever in set(ACTION_DESCRIPTORS) - set(LeverType))
    if missing or extra:
        raise RuntimeError(f"action_descriptor_coverage:missing={missing}:extra={extra}")


@dataclass(frozen=True)
class ActionOrigin:
    kind: ActionOriginKind
    authority: ActionAuthority
    source: str
    command_id: str
    issued_at_ts: float
    expected_identity_key: str = ""

    def __post_init__(self) -> None:
        if self.authority is not action_authority_for_origin(self.kind):
            raise ValueError(
                f"action_origin_authority_mismatch:{self.kind.value}:{self.authority.value}"
            )
        if not str(self.source or "").strip():
            raise ValueError("action_origin_source_missing")
        if not str(self.command_id or "").strip():
            raise ValueError("action_origin_command_id_missing")

    def as_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["kind"] = self.kind.value
        row["authority"] = self.authority.value
        return row


@dataclass(frozen=True, slots=True)
class SelectedActionContext:
    """Immutable provenance and policy context for one selected action."""

    origin: ActionOrigin
    descriptor: ActionDescriptor
    action_fingerprint: str
    objective_id: str
    opportunity_id: str
    requested_target: Mapping[str, Any]
    selection_evidence: Mapping[str, Any]
    decision_trace_id: str = ""
    admission_receipt: Mapping[str, Any] | None = None

    @property
    def operator_initiated(self) -> bool:
        return self.origin.kind in HUMAN_OPERATOR_ORIGINS

    @property
    def autonomous(self) -> bool:
        return not self.operator_initiated

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "selected_action_context.v1",
            "owner": OWNER,
            "origin": self.origin.as_dict(),
            "descriptor": self.descriptor.as_dict(),
            "action_fingerprint": self.action_fingerprint,
            "objective_id": self.objective_id,
            "opportunity_id": self.opportunity_id,
            "requested_target": copy.deepcopy(dict(self.requested_target)),
            "selection_evidence": copy.deepcopy(dict(self.selection_evidence)),
            "decision_trace_id": self.decision_trace_id,
            "admission_receipt": copy.deepcopy(dict(self.admission_receipt or {})),
        }


def bind_selected_action_context(
    action: LeverAction,
    *,
    origin_kind: ActionOriginKind,
    selection_evidence: Mapping[str, Any],
    opportunity_id: str = "",
    decision_trace_id: str = "",
    now: float | None = None,
) -> SelectedActionContext:
    """Bind typed origin once; origin reconstruction after this boundary is illegal."""

    if not isinstance(action, LeverAction):
        raise TypeError("selected_action_context_requires_lever_action")
    existing = getattr(action, SELECTED_CONTEXT_ATTR, None)
    if isinstance(existing, SelectedActionContext):
        if existing.origin.kind is not origin_kind:
            raise RuntimeError("selected_action_context_origin_conflict")
        return existing
    current = float(time.time() if now is None else now)
    command_id = ensure_action_command_id(action, now=current)
    origin = build_action_origin(
        kind=origin_kind,
        source=str(getattr(action, "source", "") or origin_kind.value),
        command_id=command_id,
        issued_at_ts=current,
    )
    descriptor = action_descriptor(action.lever)
    context = SelectedActionContext(
        origin=origin,
        descriptor=descriptor,
        action_fingerprint=action_semantic_fingerprint(action),
        objective_id=str(action.objective_id or ""),
        opportunity_id=str(opportunity_id or ""),
        requested_target=MappingProxyType(
            copy.deepcopy(
                bound_core_command_target_from_action(action).to_mapping()
            )
        ),
        selection_evidence=MappingProxyType(copy.deepcopy(dict(selection_evidence))),
        decision_trace_id=str(decision_trace_id or ""),
    )
    setattr(action, SELECTED_CONTEXT_ATTR, context)
    setattr(action, ORIGIN_ATTR, origin)
    return context


def selected_action_context_from_action(action: Any) -> SelectedActionContext:
    context = getattr(action, SELECTED_CONTEXT_ATTR, None)
    if not isinstance(context, SelectedActionContext):
        raise RuntimeError("selected_action_context_missing")
    try:
        fingerprint = action_semantic_fingerprint(action)
    except Exception as exc:
        raise RuntimeError("selected_action_changed_after_selection") from exc
    if context.action_fingerprint != fingerprint:
        raise RuntimeError("selected_action_changed_after_selection")
    return context


def extend_selected_action_context_evidence(
    action: LeverAction,
    evidence: Mapping[str, Any],
) -> SelectedActionContext:
    """Replace immutable selection context before admission adds its receipt."""

    context = getattr(action, SELECTED_CONTEXT_ATTR, None)
    if not isinstance(context, SelectedActionContext):
        raise RuntimeError("selected_action_context_missing")
    if context.admission_receipt:
        raise RuntimeError("selected_action_context_already_admitted")
    merged = dict(context.selection_evidence)
    merged.update(copy.deepcopy(dict(evidence)))
    updated = replace(
        context,
        action_fingerprint=action_semantic_fingerprint(action),
        selection_evidence=MappingProxyType(merged),
    )
    setattr(action, SELECTED_CONTEXT_ATTR, updated)
    return updated


def build_action_origin(
    *,
    kind: ActionOriginKind,
    source: str,
    command_id: str,
    issued_at_ts: float,
    expected_identity_key: str = "",
) -> ActionOrigin:
    return ActionOrigin(
        kind=kind,
        authority=action_authority_for_origin(kind),
        source=str(source or ""),
        command_id=str(command_id or ""),
        issued_at_ts=float(issued_at_ts),
        expected_identity_key=str(expected_identity_key or ""),
    )


@dataclass(frozen=True)
class ActionAdmissionContext:
    now_ts: float
    identity_key: str
    identity_verified_at_ts: float
    identity_source: str
    execution_boundary: ActionExecutionBoundary
    mutation_active: bool
    execution_lease_active: bool
    physical_capability_enabled: bool | None = None
    physical_capability_reason: str = ""
    profile_id: str = ""
    band_policy_decision: BandActionPolicyDecision | None = None
    hard_stop_reason: str = ""
    safety_checks_complete: bool = False
    command_safety_evidence: CoreCommandSafetyEvidence | None = None
    matrix_identity: MatrixGenerationIdentity | None = None
    emergency_restart_permit: CoreEmergencyRestartPermit | None = None


@dataclass(frozen=True)
class ActionAdmissionDecision:
    accepted: bool
    reason: str
    descriptor: ActionDescriptor | None
    origin: ActionOrigin | None
    identity_key: str = ""
    transaction_key: ActionTransactionKey | None = None
    emergency_restart_permit: CoreEmergencyRestartPermit | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            "accepted": bool(self.accepted),
            "reason": str(self.reason or ""),
            "descriptor": self.descriptor.as_dict() if self.descriptor else {},
            "origin": self.origin.as_dict() if self.origin else {},
            "identity_key": str(self.identity_key or ""),
            "action_transaction": (
                self.transaction_key.as_dict() if self.transaction_key else {}
            ),
            "emergency_restart_permit": (
                self.emergency_restart_permit.as_dict()
                if self.emergency_restart_permit is not None
                else {}
            ),
        }


def runtime_action_unsupported_reason(lever: LeverType, *, profile_id: str = "") -> str:
    """Product restriction, distinct from a backend's protocol capability."""
    if lever is LeverType.NR_BAND_LOCK and profile_id != "tplink_nx520_srba1_250902_dev2":
        return "nr_observation_only_action_unsupported"
    return ""


class ActionAdmission:
    """One fail-closed mechanical admission contract for all action origins."""

    @staticmethod
    def evaluate(
        action: Any,
        *,
        origin: ActionOrigin | None,
        context: ActionAdmissionContext,
    ) -> ActionAdmissionDecision:
        if not isinstance(action, LeverAction):
            return ActionAdmissionDecision(False, "action_not_lever_action", None, origin)
        try:
            descriptor = action_descriptor(action.lever)
        except Exception:
            return ActionAdmissionDecision(False, "action_descriptor_missing", None, origin)
        unsupported_reason = runtime_action_unsupported_reason(
            action.lever, profile_id=context.profile_id if context is not None else ""
        )
        if unsupported_reason:
            return ActionAdmissionDecision(
                False,
                unsupported_reason,
                descriptor,
                origin,
            )
        if not isinstance(origin, ActionOrigin):
            return ActionAdmissionDecision(False, "action_origin_missing", descriptor, None)
        if origin.kind not in descriptor.allowed_origins:
            return ActionAdmissionDecision(False, "action_origin_not_allowed", descriptor, origin)
        if origin.authority is not action_authority_for_origin(origin.kind):
            return ActionAdmissionDecision(False, "action_origin_authority_invalid", descriptor, origin)
        if descriptor.execution_boundary is not context.execution_boundary:
            return ActionAdmissionDecision(False, "action_execution_boundary_mismatch", descriptor, origin)
        action_command_id = str(getattr(action, "command_id", "") or "")
        if action_command_id and action_command_id != origin.command_id:
            return ActionAdmissionDecision(False, "action_origin_command_mismatch", descriptor, origin)
        if not context.safety_checks_complete:
            return ActionAdmissionDecision(False, "action_safety_unavailable", descriptor, origin)
        if context.hard_stop_reason:
            return ActionAdmissionDecision(
                False,
                f"action_hard_stop:{context.hard_stop_reason}",
                descriptor,
                origin,
            )
        if descriptor.requires_modem_write:
            if context.physical_capability_enabled is not True:
                return ActionAdmissionDecision(
                    False,
                    str(
                        context.physical_capability_reason
                        or f"action_capability_unavailable:{descriptor.required_capability}"
                    ),
                    descriptor,
                    origin,
                )
            identity_key = str(context.identity_key or "").strip()
            identity_source = str(context.identity_source or "").strip()
            if (
                not identity_key
                or identity_source != "core_accepted_runtime_view"
            ):
                return ActionAdmissionDecision(False, "action_identity_unverified", descriptor, origin)
            permit = context.emergency_restart_permit
            permit_allows_stale_identity = False
            if permit is not None:
                if not isinstance(permit, CoreEmergencyRestartPermit):
                    return ActionAdmissionDecision(
                        False,
                        "emergency_restart_permit_untyped",
                        descriptor,
                        origin,
                    )
                if (
                    action.lever is not LeverType.RESTART
                    or origin.kind is not ActionOriginKind.WATCHDOG
                ):
                    return ActionAdmissionDecision(
                        False,
                        "emergency_restart_permit_action_forbidden",
                        descriptor,
                        origin,
                    )
                if (
                    permit.request_id != origin.command_id
                    or permit.request_id != action_command_id
                    or permit.matrix_identity != context.matrix_identity
                    or permit.matrix_identity.session_scope_key != identity_key
                ):
                    return ActionAdmissionDecision(
                        False,
                        "emergency_restart_permit_binding_mismatch",
                        descriptor,
                        origin,
                    )
                permit_allows_stale_identity = True
            age_s = max(0.0, float(context.now_ts) - float(context.identity_verified_at_ts or 0.0))
            if (
                context.identity_verified_at_ts <= 0.0
                or (
                    age_s > IDENTITY_MAX_AGE_S
                    and not permit_allows_stale_identity
                )
            ):
                return ActionAdmissionDecision(False, "action_identity_stale", descriptor, origin)
            if origin.expected_identity_key and origin.expected_identity_key != identity_key:
                return ActionAdmissionDecision(False, "action_identity_mismatch", descriptor, origin)
            if context.execution_lease_active:
                return ActionAdmissionDecision(False, "cellular_mutation_inflight", descriptor, origin)
        if is_band_control_action(action.lever, action.params):
            band_policy = context.band_policy_decision
            if not isinstance(band_policy, BandActionPolicyDecision):
                return ActionAdmissionDecision(
                    False, "band_action_policy_unavailable", descriptor, origin
                )
            if (
                band_policy.lever != action.lever.name
                or band_policy.origin != normalized_name(origin.kind)
            ):
                return ActionAdmissionDecision(
                    False, "band_action_policy_mismatch", descriptor, origin
                )
            if not band_policy.allowed:
                return ActionAdmissionDecision(
                    False, band_policy.reason, descriptor, origin
                )
        if descriptor.mutates_cellular_radio and context.mutation_active:
            return ActionAdmissionDecision(False, "mutating_action_inflight", descriptor, origin)
        if not isinstance(context.matrix_identity, MatrixGenerationIdentity):
            return ActionAdmissionDecision(
                False,
                "action_matrix_identity_missing",
                descriptor,
                origin,
            )
        safety = context.command_safety_evidence
        if not isinstance(safety, CoreCommandSafetyEvidence):
            return ActionAdmissionDecision(
                False,
                "core_command_safety_evidence_missing",
                descriptor,
                origin,
            )
        safety_error = ""
        if safety.request_id != origin.command_id:
            safety_error = "core_command_safety_request_mismatch"
        elif safety.matrix_identity != context.matrix_identity:
            safety_error = "core_command_safety_matrix_mismatch"
        elif safety.identity_key != str(context.identity_key or ""):
            safety_error = "core_command_safety_identity_key_mismatch"
        if safety_error:
            return ActionAdmissionDecision(
                False,
                safety_error,
                descriptor,
                origin,
            )
        recovery_origin = origin.kind in {
            ActionOriginKind.FAULT_RECOVERY,
            ActionOriginKind.WATCHDOG,
            ActionOriginKind.LEASE_EXPIRY,
        }
        if safety.modem_health_state != "ok" and not recovery_origin:
            return ActionAdmissionDecision(
                False,
                f"modem_health_{safety.modem_health_state}",
                descriptor,
                origin,
            )
        try:
            transaction_key = action_transaction_key_from_action(action)
        except Exception:
            transaction_key = None
        if not isinstance(transaction_key, ActionTransactionKey):
            return ActionAdmissionDecision(
                False,
                "action_transaction_root_not_prebound",
                descriptor,
                origin,
            )
        if transaction_key.command_id != origin.command_id:
            return ActionAdmissionDecision(
                False,
                "action_transaction_command_mismatch",
                descriptor,
                origin,
            )
        if transaction_key.identity_key != str(context.identity_key or ""):
            return ActionAdmissionDecision(
                False,
                "action_transaction_identity_mismatch",
                descriptor,
                origin,
            )
        if transaction_key.matrix_identity != context.matrix_identity:
            return ActionAdmissionDecision(
                False,
                "action_transaction_matrix_mismatch",
                descriptor,
                origin,
            )
        if (
            context.emergency_restart_permit is not None
            and context.emergency_restart_permit.matrix_identity
            != transaction_key.matrix_identity
        ):
            return ActionAdmissionDecision(
                False,
                "emergency_restart_permit_transaction_mismatch",
                descriptor,
                origin,
            )
        return ActionAdmissionDecision(
            True,
            "action_admitted",
            descriptor,
            origin,
            identity_key=str(context.identity_key or ""),
            transaction_key=transaction_key,
            emergency_restart_permit=context.emergency_restart_permit,
        )


def _authority_ledger(engine: Any) -> dict[str, dict[str, Any]]:
    ledger = getattr(engine, AUTHORITY_LEDGER_ATTR, None)
    if not isinstance(ledger, dict):
        ledger = {}
        setattr(engine, AUTHORITY_LEDGER_ATTR, ledger)
    return ledger


def _authority_lock(engine: Any) -> threading.RLock:
    lock = getattr(engine, AUTHORITY_LOCK_ATTR, None)
    if lock is None:
        lock = threading.RLock()
        setattr(engine, AUTHORITY_LOCK_ATTR, lock)
    return lock


def authorize_admitted_action(
    engine: Any,
    action: LeverAction,
    *,
    decision: ActionAdmissionDecision,
    decision_trace_id: str = "",
    lanes: Mapping[str, Any] | None = None,
) -> LeverAction:
    if not decision.accepted or decision.origin is None or decision.descriptor is None:
        raise RuntimeError(f"cannot_authorize_rejected_action:{decision.reason}")
    if decision.transaction_key is None:
        raise RuntimeError("cannot_authorize_action_without_transaction_key")
    selected_context = selected_action_context_from_action(action)
    if selected_context.origin != decision.origin:
        raise RuntimeError("selected_action_admission_origin_mismatch")
    bind_action_transaction_key(action, decision.transaction_key)
    with _authority_lock(engine):
        counter = int(getattr(engine, "_action_admission_authority_counter", 0) or 0) + 1
        setattr(engine, "_action_admission_authority_counter", counter)
        token = f"admission:{int(time.time() * 1000)}:{counter}:{action.lever.name}"
        record = {
            "schema": AUTHORITY_SCHEMA,
            "issuer": OWNER,
            "token": token,
            "lever": action.lever.name,
            "command_id": decision.origin.command_id,
            "decision_trace_id": str(decision_trace_id or ""),
            "issued_ts": time.time(),
            "identity_key": str(decision.identity_key or ""),
            "origin": decision.origin.as_dict(),
            "descriptor": decision.descriptor.as_dict(),
            "action_transaction": decision.transaction_key.as_dict(),
            "emergency_restart_permit": (
                decision.emergency_restart_permit.as_dict()
                if decision.emergency_restart_permit is not None
                else {}
            ),
            "lanes": dict(lanes or {}),
            "action_fingerprint": action_semantic_fingerprint(action),
        }
        setattr(action, AUTHORITY_ATTR, copy.deepcopy(record))
        setattr(action, ORIGIN_ATTR, decision.origin)
        setattr(
            action,
            SELECTED_CONTEXT_ATTR,
            replace(
                selected_context,
                decision_trace_id=str(decision_trace_id or selected_context.decision_trace_id),
                admission_receipt=MappingProxyType(copy.deepcopy(record)),
            ),
        )
        if not str(getattr(action, "command_id", "") or ""):
            setattr(action, "command_id", decision.origin.command_id)
        ledger = _authority_ledger(engine)
        ledger[token] = copy.deepcopy(record)
        while len(ledger) > MAX_AUTHORITY_RECEIPTS:
            ledger.pop(next(iter(ledger)))
    return action


def action_admission_authority_record(action: Any) -> dict[str, Any]:
    if not isinstance(action, LeverAction):
        return {}
    payload = getattr(action, AUTHORITY_ATTR, None)
    return copy.deepcopy(dict(payload or {})) if isinstance(payload, Mapping) else {}


def action_origin_from_action(action: Any) -> ActionOrigin:
    """Read the exact admitted origin retained on one action."""

    origin = getattr(action, ORIGIN_ATTR, None)
    if not isinstance(origin, ActionOrigin):
        raise RuntimeError("action_origin_missing")
    return origin


def consume_action_admission_authority(
    engine: Any,
    action: Any,
) -> tuple[bool, str, dict[str, Any]]:
    if not isinstance(action, LeverAction):
        return False, "action_not_lever_action", {}
    record = action_admission_authority_record(action)
    if not record:
        return False, "action_admission_authority_missing", {}
    if record.get("schema") != AUTHORITY_SCHEMA or record.get("issuer") != OWNER:
        return False, "action_admission_authority_invalid", record
    token = str(record.get("token") or "")
    with _authority_lock(engine):
        ledger = _authority_ledger(engine)
        if not token or token not in ledger:
            return False, "action_admission_authority_unknown_token", record
        retained = dict(ledger.pop(token))
        if retained != record:
            return False, "action_admission_authority_record_changed", record
        if str(record.get("lever") or "") != action.lever.name:
            return False, "action_admission_authority_lever_mismatch", record
        if str(record.get("command_id") or "") != str(getattr(action, "command_id", "") or ""):
            return False, "action_admission_authority_command_mismatch", record
        origin = getattr(action, ORIGIN_ATTR, None)
        if not isinstance(origin, ActionOrigin):
            return False, "action_origin_missing", record
        try:
            selected_context = selected_action_context_from_action(action)
        except Exception as exc:
            if str(exc) == "selected_action_changed_after_selection":
                return False, "action_changed_after_admission", record
            return False, f"selected_action_context_invalid:{type(exc).__name__}", record
        if selected_context.origin != origin:
            return False, "selected_action_context_origin_mismatch", record
        if origin.as_dict() != dict(record.get("origin") or {}):
            return False, "action_origin_record_mismatch", record
        if str(record.get("action_fingerprint") or "") != action_semantic_fingerprint(action):
            return False, "action_changed_after_admission", record
        try:
            action_key = action_transaction_key_from_action(action)
        except Exception:
            return False, "action_transaction_key_invalid", record
        if action_key is None:
            return False, "action_transaction_key_missing", record
        if action_key.as_dict() != dict(record.get("action_transaction") or {}):
            return False, "action_transaction_key_changed", record
        permit_payload = record.get("emergency_restart_permit")
        if not isinstance(permit_payload, Mapping):
            return False, "emergency_restart_permit_record_invalid", record
        permit: CoreEmergencyRestartPermit | None = None
        if permit_payload:
            try:
                permit = CoreEmergencyRestartPermit.from_mapping(
                    dict(permit_payload)
                )
            except Exception:
                return False, "emergency_restart_permit_record_invalid", record
            if (
                action.lever is not LeverType.RESTART
                or origin.kind is not ActionOriginKind.WATCHDOG
                or permit.request_id != action_key.command_id
                or permit.matrix_identity != action_key.matrix_identity
                or permit.matrix_identity.session_scope_key
                != str(record.get("identity_key") or "")
            ):
                return False, "emergency_restart_permit_record_mismatch", record
        current_identity = current_engine_identity(engine, now=time.time())
        if str(current_identity.get("identity_key") or "") != str(record.get("identity_key") or ""):
            return False, "action_identity_changed_before_execution", record
        if permit is not None:
            validate = getattr(
                engine,
                "validate_emergency_restart_permit",
                None,
            )
            if not callable(validate) or not bool(
                validate(permit, now=time.time())
            ):
                return False, "emergency_restart_permit_invalid_before_execution", record
        elif not bool(current_identity.get("fresh", False)):
            return False, "action_identity_stale_before_execution", record
        return True, "", record


def current_engine_identity(
    engine: Any,
    *,
    now: float,
    accepted_view: CoreAcceptedRuntimeView | None = None,
) -> dict[str, Any]:
    view = accepted_view
    if view is None:
        publication = getattr(engine, "modem_sample_publication", None)
        if publication is None:
            raise RuntimeError(
                "current_engine_identity_modem_sample_publication_required"
            )
        outbox = getattr(publication, "accepted_root_outbox", None)
        current = getattr(outbox, "current", None)
        if not callable(current):
            raise RuntimeError(
                "current_engine_identity_accepted_root_outbox_required"
            )
        root = current()
        view = getattr(root, "runtime_view", None)
    if not isinstance(view, CoreAcceptedRuntimeView):
        if view is not None:
            raise TypeError(
                "current_engine_identity_requires_accepted_runtime_view"
            )
        return {
            "identity_key": "",
            "verified_at_ts": 0.0,
            "identity_source": "",
            "fresh": False,
        }
    age_s = max(0.0, float(now) - view.sampled_at)
    return {
        "identity_key": view.matrix_identity.session_scope_key,
        "verified_at_ts": view.sampled_at,
        "identity_source": "core_accepted_runtime_view",
        "fresh": bool(
            view.matrix_identity.matrix_sequence > 0
            and age_s <= IDENTITY_MAX_AGE_S
        ),
    }


def execution_lease_active(engine: Any) -> bool | None:
    authority = getattr(engine, "command_authority", None)
    status = getattr(authority, "cellular_execution_status", None)
    if not callable(status):
        return None
    try:
        row = status()
    except Exception:
        return None
    return bool(row.get("active", False)) if isinstance(row, Mapping) else None


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def ensure_action_command_id(action: LeverAction, *, now: float | None = None) -> str:
    """Assign the command identity before policy verdicts bind to an action."""
    command_id = str(getattr(action, "command_id", "") or "").strip()
    if command_id:
        return command_id
    objective = str(getattr(action, "objective_id", "") or "").strip()
    current = float(time.time() if now is None else now)
    command_id = objective or f"action:{int(current * 1000)}:{action.lever.name.lower()}"
    setattr(action, "command_id", command_id)
    return command_id


assert_action_descriptor_coverage()


__all__ = [
    "ACTION_DESCRIPTORS",
    "AUTHORITY_ATTR",
    "ActionAdmission",
    "ActionAdmissionContext",
    "ActionAdmissionDecision",
    "ActionDescriptor",
    "ActionExecutionBoundary",
    "ActionFamily",
    "ActionLearning",
    "ActionOrigin",
    "ActionProof",
    "ActionRisk",
    "SelectedActionContext",
    "ActionVerification",
    "action_admission_authority_record",
    "action_descriptor",
    "action_origin_from_action",
    "action_semantic_fingerprint",
    "assert_action_descriptor_coverage",
    "authorize_admitted_action",
    "bind_selected_action_context",
    "build_action_origin",
    "current_engine_identity",
    "ensure_action_command_id",
    "execution_lease_active",
    "runtime_action_unsupported_reason",
    "extend_selected_action_context_evidence",
    "selected_action_context_from_action",
    "consume_action_admission_authority",
]
