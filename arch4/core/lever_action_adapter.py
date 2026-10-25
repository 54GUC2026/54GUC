"""Convert CoreCommandRequest to the LeverAction used by Modem Live. This adapter validates
and builds the internal request shape. It owns no queue, policy, lifecycle, lease, or
modem I/O.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from arch4.action_admission import (
    ACTION_DESCRIPTORS,
    ActionExecutionBoundary,
    ActionFamily,
    ActionVerification,
    bind_selected_action_context,
)
from arch4.action_origin import ActionOriginKind
from arch4.action_shape_legality import validate_action_shape
from arch4.core_contracts.commands import (
    CoreCommandOrigin,
    CoreCommandRequest,
    core_command_request_root,
)
from arch4.core_contracts.command_targets import (
    CoreBandAutoTarget,
    CoreBandSetTarget,
    CoreNrBandTarget,
    core_command_target_payload,
)
from arch4.interfaces import LeverAction, LeverType


LEVER_ACTION_AUTHORITY_FIELDS = (
    "action_kind",
    "effect",
    "capability_key",
    "expected_readback",
    "mutation_window",
    "requires_modem_write",
    "requires_quiet_window",
    "allowed_while_mutating",
    "allowed_while_degraded",
)
_TARGET_VOCABULARY = frozenset(
    {
        "bands",
        "auto",
        "release_manual",
        "domain",
        "pband",
        "ca_pool",
        "dl_bands",
        "pci",
        "earfcn",
        "band",
        "unlock",
        "nr_bands",
        "restart",
        "emergency_fast",
        "keepalive_interval_s",
        "background_mbps",
        "sqm_limit_mbps",
        "dl_256qam",
        "ul_64qam",
        "enabled",
        "subscription_key", "roaming_enabled", "selection", "target_plmn", "apn",
        "mode", "expected_mode", "lte_bands", "band_selection_mode", "profile_definition_hash", "retry_delay_s",
    }
)


@dataclass(frozen=True, slots=True)
class LeverActionAuthority:
    lever: str
    effect: str = "modem_write"
    requires_modem_write: bool = True
    requires_quiet_window: bool = False
    allowed_while_mutating: bool = False
    allowed_while_degraded: bool = False
    capability_key: str = ""
    expected_readback: str = ""
    mutation_window: str = "none"

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


def _project_lever_action_authority() -> dict[str, LeverActionAuthority]:
    readback = {
        ActionVerification.RADIO_TARGET_AND_FRESH_SAMPLE: "radio_lock_confirmation",
        ActionVerification.MODEM_RECONNECT_AND_FRESH_SAMPLE: "modem_reconnect",
        ActionVerification.CAPABILITY_READBACK: "capability_probe",
        ActionVerification.TRAFFIC_READBACK: "",
    }
    mutation_window = {
        ActionFamily.CELLULAR_RADIO: "radio_lock",
        ActionFamily.CELLULAR_SESSION: "cellular_session",
        ActionFamily.TRAFFIC_POLICY: "traffic",
    }
    projected: dict[str, LeverActionAuthority] = {}
    for lever, descriptor in ACTION_DESCRIPTORS.items():
        if descriptor.execution_boundary is not ActionExecutionBoundary.CELLULAR_COMMAND:
            continue
        window = mutation_window.get(descriptor.family, "none")
        if lever is LeverType.RESTART:
            window = "restart"
        elif lever is LeverType.UE_CAPABILITY:
            window = "ue_capability"
        traffic_policy = descriptor.family is ActionFamily.TRAFFIC_POLICY
        projected[lever.name] = LeverActionAuthority(
            lever=lever.name,
            effect="modem_write" if descriptor.requires_modem_write else "none",
            requires_modem_write=descriptor.requires_modem_write,
            requires_quiet_window=descriptor.settling_s > 0.0,
            allowed_while_mutating=traffic_policy,
            allowed_while_degraded=traffic_policy,
            capability_key=descriptor.required_capability,
            expected_readback=readback.get(
                descriptor.verification,
                descriptor.verification.value,
            ),
            mutation_window=window,
        )
    return projected


LEVER_ACTION_AUTHORITY: Mapping[str, LeverActionAuthority] = (
    _project_lever_action_authority()
)


def lever_action_authority(lever: LeverType | str) -> LeverActionAuthority:
    name = lever.name if isinstance(lever, LeverType) else str(lever or "").strip().upper()
    authority = LEVER_ACTION_AUTHORITY.get(name)
    if authority is not None:
        return authority
    return LeverActionAuthority(
        lever=name,
        effect="unknown",
        requires_modem_write=False,
        allowed_while_mutating=False,
        allowed_while_degraded=False,
        mutation_window="unknown",
    )


def lever_action_authority_metadata(lever: LeverType | str) -> dict[str, Any]:
    return lever_action_authority(lever).as_payload()


def validate_lever_action_command_authority(
    action: LeverAction,
) -> tuple[bool, str, str]:
    if not isinstance(action, LeverAction):
        return False, "action_not_lever_action", ""
    authority = lever_action_authority(action.lever)
    if authority.effect == "unknown":
        return False, "unknown_lever_authority", authority.lever
    data = {
        field: getattr(action, field, None)
        for field in LEVER_ACTION_AUTHORITY_FIELDS
    }
    if str(data.get("action_kind") or "").strip().lower() != "lever_action":
        return False, "missing_action_authority", "action_kind"
    expected = authority.as_payload()
    for field in (
        "effect",
        "capability_key",
        "expected_readback",
        "mutation_window",
    ):
        if data.get(field) != expected.get(field):
            return False, "action_authority_mismatch", field
    for field in (
        "requires_modem_write",
        "requires_quiet_window",
        "allowed_while_mutating",
        "allowed_while_degraded",
    ):
        if type(data.get(field)) is not bool:
            return False, "missing_action_authority", field
        if data[field] is not expected[field]:
            return False, "action_authority_mismatch", field
    return True, "", ""


def lever_action_initiator_provenance(action: LeverAction) -> dict[str, object]:
    """Project command attribution from the one selected typed origin."""

    from arch4.action_admission import selected_action_context_from_action

    selected_context = selected_action_context_from_action(action)
    origin_kind = selected_context.origin.kind.value
    gui_action_id = str(getattr(action, "_gui_action_id", "") or "").strip()
    gui_action_source = str(
        getattr(action, "_gui_action_source", "") or ""
    ).strip()
    command_id = str(getattr(action, "command_id", "") or "").strip()
    source = str(getattr(action, "source", "") or "").strip()
    request_source = str(
        selected_context.origin.source
        or gui_action_source
        or source
        or origin_kind
    )
    if origin_kind in {"schedule", "lease_expiry"}:
        interaction_family = "scheduled_automation"
    elif origin_kind in {"operator_ngui", "operator_sms"}:
        interaction_family = "operator_path"
    elif origin_kind in {"smart_loop", "explore"}:
        interaction_family = "autonomous_optimization"
    elif origin_kind in {"fault_recovery", "watchdog"}:
        interaction_family = "recovery_automation"
    else:
        raise RuntimeError(f"initiator_origin_unhandled:{origin_kind}")
    return {
        "action_origin": origin_kind,
        "interaction_family": interaction_family,
        "request_source": request_source,
        "gui_action_id": gui_action_id,
        "gui_action_source": gui_action_source,
        "command_id": command_id,
        "origin_kind": origin_kind,
        "origin_authority": selected_context.origin.authority.value,
    }


def _origin_kind(request: CoreCommandRequest) -> ActionOriginKind:
    if request.origin is CoreCommandOrigin.OPERATOR:
        return ActionOriginKind.OPERATOR_NGUI
    if request.origin is CoreCommandOrigin.AUTONOMOUS_INTELLIGENCE:
        autonomous_source = str(
            request.parameters.to_mapping().get("autonomous_source") or ""
        ).strip().lower()
        autonomous_mapping = {
            "smart_loop": ActionOriginKind.SMART_LOOP,
            "explore": ActionOriginKind.EXPLORE,
            "fault_recovery": ActionOriginKind.FAULT_RECOVERY,
        }
        kind = autonomous_mapping.get(autonomous_source)
        if kind is None:
            raise ValueError("core_command_autonomous_source_invalid")
        return kind
    automation = str(
        request.parameters.to_mapping().get("automation_source") or ""
    ).strip().lower()
    mapping = {
        "sms": ActionOriginKind.OPERATOR_SMS,
        "schedule": ActionOriginKind.SCHEDULE,
        "watchdog": ActionOriginKind.WATCHDOG,
    }
    kind = mapping.get(automation)
    if kind is None:
        raise ValueError("core_command_automation_source_invalid")
    return kind


class CoreLeverActionAdapter:
    """Stateless request/action boundary; never an execution path."""

    def materialize(
        self,
        request: CoreCommandRequest,
        *,
        resolved_auto_target: CoreBandSetTarget | CoreNrBandTarget | None = None,
        admitted_auto_evidence: Mapping[str, Any] | None = None,
    ) -> LeverAction:
        if not isinstance(request, CoreCommandRequest):
            raise TypeError("core_lever_adapter_typed_request_required")
        physical_target = request.target
        if isinstance(request.target, CoreBandAutoTarget):
            if not isinstance(admitted_auto_evidence, Mapping):
                raise TypeError("core_auto_resolution_evidence_required")
            if request.target.domain == "lte_allocated_auto":
                valid = (isinstance(resolved_auto_target, CoreBandSetTarget)
                         and resolved_auto_target.command_kind == "BAND_SELECTION")
            else:
                valid = isinstance(resolved_auto_target, CoreNrBandTarget)
            if not valid:
                raise ValueError("core_auto_resolution_target_mismatch")
            if not resolved_auto_target.release_manual:
                raise ValueError("core_auto_native_release_required")
            physical_target = resolved_auto_target
        elif resolved_auto_target is not None or admitted_auto_evidence is not None:
            raise ValueError("core_explicit_command_resolution_forbidden")
        elif isinstance(physical_target, (CoreBandSetTarget, CoreNrBandTarget)) and physical_target.release_manual:
            raise ValueError("core_explicit_auto_release_forbidden")
        command_name = physical_target.command_kind
        try:
            lever = LeverType[command_name]
        except KeyError as exc:
            raise ValueError(
                f"core_command_lever_unknown:{command_name or 'missing'}"
            ) from exc
        target = physical_target.to_mapping()
        declared_lever = str(target.pop("lever", "") or "").strip().upper()
        if declared_lever != lever.name:
            raise ValueError("core_command_target_lever_mismatch")
        metadata = request.parameters.to_mapping()
        extra_params = metadata.get("action_parameters", {})
        if not isinstance(extra_params, Mapping):
            raise TypeError("core_command_action_parameters_mapping_required")
        if admitted_auto_evidence is not None:
            if set(extra_params).intersection(admitted_auto_evidence):
                raise ValueError("core_auto_resolution_evidence_overlap")
            extra_params = {**extra_params, **admitted_auto_evidence}
        overlap = set(target).intersection(extra_params)
        if overlap:
            raise ValueError(
                "core_command_action_parameter_overlap:"
                + ",".join(sorted(str(value) for value in overlap))
            )
        alternate_target_fields = _TARGET_VOCABULARY.intersection(extra_params)
        if alternate_target_fields:
            raise ValueError(
                "core_command_alternate_target_parameter_forbidden:"
                + ",".join(sorted(alternate_target_fields))
            )
        params = {**target, **dict(extra_params)}
        params.update(
            {
                "_core_command_request_id": request.request_id,
                "_core_command_request_schema": request.schema,
                "_core_command_request_owner": request.owner,
                "_core_command_root_id": core_command_request_root(request),
                "_core_command_target": core_command_target_payload(
                    physical_target
                ),
            }
        )
        action = LeverAction(
            lever=lever,
            params=params,
            reason=str(metadata.get("reason") or request.command_kind),
            intent=str(metadata.get("intent") or "").strip(),
            objective_id=str(
                metadata.get("objective_id") or request.correlation_id
            ),
        )
        if not action.intent:
            raise ValueError("core_command_intent_required")
        shape = validate_action_shape(action.lever, action.params)
        if not shape.legal:
            raise ValueError(
                f"core_command_action_shape_invalid:{shape.code}:{shape.reason}"
            )
        authority = lever_action_authority(lever)
        if request.capability_reference != authority.capability_key:
            raise ValueError("core_command_capability_reference_mismatch")
        attributes = {
            "command_id": request.request_id,
            "created_ts": request.created_at,
            "created_at": request.created_at,
            "expires_after_s": request.expires_at - request.created_at,
            "expires_at_ts": request.expires_at,
            "dedupe_key": request.request_id,
            "replay_policy": "reject_duplicate",
            "source": request.client_id,
            "risk_level": "high" if authority.requires_modem_write else "medium",
            "action_kind": "lever_action",
            "_gui_action_id": request.correlation_id,
            "_gui_action_source": request.client_id,
            **{
                key: value
                for key, value in authority.as_payload().items()
                if key != "lever"
            },
        }
        for field, value in attributes.items():
            setattr(action, field, value)
        bind_selected_action_context(
            action,
            origin_kind=_origin_kind(request),
            selection_evidence={
                "schema": request.schema,
                "owner": request.owner,
                "request_id": request.request_id,
                "client_id": request.client_id,
                "origin": request.origin.value,
                "identity_code": request.identity_code,
                "session_id": request.session_id,
                "source_epoch": request.source_epoch,
                "source_sequence": request.source_sequence,
                "confirmed": request.confirmed,
            },
            opportunity_id=request.request_id,
            decision_trace_id=request.correlation_id,
            now=request.created_at,
        )
        ok, reason, detail = validate_lever_action_command_authority(action)
        if not ok:
            raise RuntimeError(f"core_lever_authority_invalid:{reason}:{detail}")
        return action


__all__ = (
    "CoreLeverActionAdapter",
    "LEVER_ACTION_AUTHORITY",
    "LEVER_ACTION_AUTHORITY_FIELDS",
    "LeverActionAuthority",
    "lever_action_authority",
    "lever_action_authority_metadata",
    "lever_action_initiator_provenance",
    "validate_lever_action_command_authority",
)
