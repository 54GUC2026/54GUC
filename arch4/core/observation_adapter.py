"""Boundary from one accepted generation to one Core observation."""

from __future__ import annotations

import math
from dataclasses import asdict, is_dataclass
from types import MappingProxyType
from typing import Any, Mapping

from arch4.accepted_modem_generation import AcceptedModemGeneration
from arch4.core_contracts.observation_identity import (
    CoreObservationIdentityContext,
)
from arch4.core_contracts.accepted_runtime_view import (
    CORE_ACTION_READBACK_TO_CAPABILITY,
    CoreAcceptedRuntimeView,
)
from arch4.core_contracts.accepted_current_facts import (
    CoreAcceptedCurrentFact,
    CoreAcceptedCurrentFacts,
)
from arch4.core_contracts.observations import CoreObservationFrame
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.controller_frames import (
    RadioTransitionFrame,
    radio_transition_frame_to_mapping,
)
from arch4.radio_target_contract import (
    RadioTargetFacts,
    build_radio_target_facts_at_accepted_matrix_boundary,
)
from arch4.runtime_capability import (
    capability_contract_action_enabled,
    capability_contract_action_meta,
)
from arch4.modem_live.compatibility_identity import (
    EXACT_STATUS,
    IDENTITY_SCHEMA,
    OWNER as MODEM_LIVE_IDENTITY_OWNER,
    PROVISIONAL_STATUS,
    UNKNOWN_IDENTITY_CODE,
    UNKNOWN_STATUS,
)
from arch4.modem_live.session_band_capability import SessionBandCapability
from arch4.modem_live.truth_matrix import (
    CELL_AVAILABLE,
    MODEM_LIVE_TRUTH_FIELD_CATALOG,
)


_CAPABILITY_FIELDS = (
    "access_type_contract",
    "access_type_id",
    "access_type_fingerprint",
    "modem_live_access_profile",
    "capability_profile",
    "static_modem_truth",
    "resolved_identity_card",
    "profile_id",
    "backend",
    "catalog_version",
    "catalog_checksum",
    "provider_name",
    "provider_plmn",
    "manufacturer",
    "model",
    "hardware_sku",
    "firmware_version",
    "serial",
    "imei",
    "imsi",
)


def build_core_accepted_current_facts(
    accepted: AcceptedModemGeneration,
) -> CoreAcceptedCurrentFacts:
    """Mechanically project the complete finite Matrix cell denominator once."""

    if not isinstance(accepted, AcceptedModemGeneration):
        raise TypeError("accepted_current_facts_generation_required")
    reader = accepted.raw.truth_matrix_reader()
    facts: list[CoreAcceptedCurrentFact] = []
    for line, fields in MODEM_LIVE_TRUTH_FIELD_CATALOG.items():
        for field in fields:
            cell = reader.cell(line, field)
            facts.append(
                CoreAcceptedCurrentFact(
                    line=line,
                    field=field,
                    disposition=cell.state,
                    value=CoreFrozenPayload.from_mapping(
                        {
                            "present": cell.state == CELL_AVAILABLE,
                            "value": (
                                cell.value
                                if cell.state == CELL_AVAILABLE
                                else None
                            ),
                        }
                    ),
                    reason=cell.reason,
                    sampled_at=cell.sampled_at,
                    acquired_at=cell.acquired_at,
                    acquisition_attempt_id=cell.acquisition_attempt_id,
                    source_lane=cell.source_lane,
                    acquisition_namespace=cell.acquisition_namespace,
                    retention=(
                        CoreFrozenPayload.from_mapping(cell.retention)
                        if cell.retention is not None
                        else None
                    ),
                    line_sequence=cell.line_sequence,
                    source_sequence=cell.source_sequence,
                    source=cell.source,
                )
            )
    return CoreAcceptedCurrentFacts.prepare(
        matrix_identity=accepted.raw.matrix_identity,
        facts=tuple(facts),
    )

def _strict_accepted_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        return CoreFrozenPayload.from_mapping(
            {"value": dict(value)}
        ).to_mapping()["value"]
    if type(value) is list:
        return CoreFrozenPayload.from_mapping(
            {"value": value}
        ).to_mapping()["value"]
    if value is None or type(value) in {bool, int, float, str}:
        return CoreFrozenPayload.from_mapping(
            {"value": value}
        ).to_mapping()["value"]
    raise TypeError(
        f"core_observation_accepted_value_invalid:{type(value).__name__}"
    )


def _facts_line(
    current_facts: CoreAcceptedCurrentFacts,
    line: str,
) -> Mapping[str, Any]:
    output: dict[str, Any] = {}
    for field in MODEM_LIVE_TRUTH_FIELD_CATALOG[line]:
        fact = current_facts.exact(f"{line}.{field}")
        envelope = fact.value.to_mapping()
        if envelope["present"] is True:
            output[field] = envelope["value"]
    return output


def _validated_identity_context(
    accepted: AcceptedModemGeneration,
    current_facts: CoreAcceptedCurrentFacts,
) -> CoreObservationIdentityContext:
    publication = _facts_line(current_facts, "identity")
    validated = dict(publication)
    if (
        type(validated.get("schema")) is not str
        or validated.get("schema") != IDENTITY_SCHEMA
        or type(validated.get("owner")) is not str
        or validated.get("owner") != MODEM_LIVE_IDENTITY_OWNER
    ):
        raise ValueError("core_observation_modem_live_identity_owner_schema_invalid")
    source_status = validated.get("identity_status")
    identity_code = validated.get("identity_code")
    if type(source_status) is not str:
        raise TypeError("core_observation_source_identity_status_invalid")
    if type(identity_code) is not str:
        raise TypeError("core_observation_identity_code_invalid")
    if source_status == EXACT_STATUS:
        identity_status = "confirmed"
        if not identity_code or identity_code == UNKNOWN_IDENTITY_CODE:
            raise ValueError("core_observation_exact_identity_code_required")
    elif source_status in {PROVISIONAL_STATUS, UNKNOWN_STATUS}:
        identity_status = "provisional"
        if identity_code != UNKNOWN_IDENTITY_CODE:
            raise ValueError("core_observation_provisional_identity_code_invalid")
    else:
        raise ValueError("core_observation_source_identity_status_invalid")
    access_type = validated.get("access_type")
    if (
        type(access_type) is not str
        or not access_type
        or access_type != access_type.strip()
    ):
        raise ValueError("core_observation_access_type_required")
    capabilities = {
        key: _strict_accepted_value(validated[key])
        for key in _CAPABILITY_FIELDS
        if key in validated and validated[key] not in (None, "")
    }
    capabilities["identity_status"] = source_status
    capabilities["identity_code"] = identity_code
    return CoreObservationIdentityContext(
        session_id=accepted.raw.session_scope_key,
        identity_code=identity_code,
        identity_status=identity_status,
        access_type=access_type,
        source_identity_status=source_status,
        source_schema=IDENTITY_SCHEMA,
        source_owner=MODEM_LIVE_IDENTITY_OWNER,
        capability_facts=CoreFrozenPayload.from_mapping(capabilities),
    )


def _validated_session_band_capability(
    accepted: AcceptedModemGeneration,
    current_facts: CoreAcceptedCurrentFacts,
) -> dict[str, Any]:
    payload = _facts_line(current_facts, "capability")
    publication = payload.get("session_band_capability")
    if isinstance(publication, MappingProxyType):
        raise TypeError("core_observation_mappingproxy_capability_forbidden")
    if not isinstance(publication, Mapping):
        raise ValueError("core_observation_session_band_capability_required")
    capability = SessionBandCapability.from_mapping(publication)
    if capability.session_scope_key != accepted.raw.session_scope_key:
        raise ValueError("core_observation_session_band_capability_scope_mismatch")
    return capability.as_dict()


def build_core_observation_frame(
    accepted: AcceptedModemGeneration,
    *,
    accepted_commit_id: str,
    published_at: float,
    radio_transition: RadioTransitionFrame,
    current_facts: CoreAcceptedCurrentFacts,
) -> CoreObservationFrame:
    """Project accepted truth without recompute, identity inference or clock read."""

    if not isinstance(accepted, AcceptedModemGeneration):
        raise TypeError("core_observation_accepted_generation_required")
    if not isinstance(radio_transition, RadioTransitionFrame):
        raise TypeError("core_observation_typed_radio_transition_required")
    if (
        type(accepted_commit_id) is not str
        or not accepted_commit_id
        or accepted_commit_id != accepted_commit_id.strip()
    ):
        raise TypeError("core_observation_commit_id_invalid")
    if type(published_at) is not float:
        raise TypeError("core_observation_published_at_invalid")
    boundary_published_at = published_at
    if (
        not math.isfinite(boundary_published_at)
        or boundary_published_at < accepted.accepted_at
    ):
        raise ValueError("core_observation_published_at_invalid")
    if (
        not isinstance(current_facts, CoreAcceptedCurrentFacts)
        or current_facts.matrix_identity != accepted.raw.matrix_identity
    ):
        raise TypeError("core_observation_current_facts_required")
    identity = _validated_identity_context(accepted, current_facts)
    session_band_capability = _validated_session_band_capability(
        accepted,
        current_facts,
    )
    raw = accepted.raw
    if (
        radio_transition.schema != "radio_transition.v1"
        or radio_transition.owner
        != "arch4.radio_transition_authority.RadioTransitionAuthority"
        or type(radio_transition.source_generation) is not int
        or radio_transition.source_generation != raw.source_sequence
        or radio_transition.matrix_identity != raw.matrix_identity
    ):
        raise ValueError("core_observation_radio_transition_contract_invalid")
    accepted_health = _facts_line(current_facts, "health")
    radio_connected_value = accepted_health.get("radio_connected")
    if "radio_connected" in accepted_health and type(radio_connected_value) is not bool:
        raise TypeError(
            "core_observation_radio_connected_boolean_required"
        )
    radio_connected = radio_connected_value
    radio_target = build_radio_target_facts_at_accepted_matrix_boundary(
        current_facts
    )
    derived_facts = CoreFrozenPayload.from_mapping(
        {
            "signal_compute": _strict_accepted_value(
                accepted.computed_copy()
            ),
            "radio_target_facts": radio_target.as_mapping(),
        }
    )
    capability_facts = identity.capability_facts.to_mapping()
    capability_facts["session_band_capability"] = _strict_accepted_value(
        session_band_capability
    )
    return CoreObservationFrame(
        accepted_commit_id=accepted_commit_id,
        identity_code=identity.identity_code,
        identity_status=identity.identity_status,
        access_type=identity.access_type,
        matrix_identity=raw.matrix_identity,
        sampled_at=raw.sampled_at,
        accepted_at=accepted.accepted_at,
        published_at=boundary_published_at,
        matrix_rtt=accepted.matrix_rtt,
        radio_connected=radio_connected,
        derived_facts=derived_facts,
        capability_facts=CoreFrozenPayload.from_mapping(capability_facts),
        lower_controller_change=CoreFrozenPayload.from_mapping(
            _strict_accepted_value(
                radio_transition_frame_to_mapping(radio_transition)
            )
        ),
    )


def _accepted_action_readback(
    accepted: AcceptedModemGeneration,
    current_facts: CoreAcceptedCurrentFacts,
) -> tuple[CoreFrozenPayload, CoreFrozenPayload]:
    """Build command readback once at the accepted-Matrix boundary."""

    pool_cell = current_facts.exact("radio.configured_band_pool")
    pool_source_cell = current_facts.exact(
        "radio.configured_band_pool_source"
    )
    pool_value = pool_cell.value.to_mapping()["value"]
    if pool_cell.disposition == CELL_AVAILABLE:
        if type(pool_value) is not list or any(
            type(item) is not int or item <= 0 for item in pool_value
        ):
            raise TypeError(
                "core_action_readback_configured_pool_invalid"
            )
        if pool_value != sorted(set(pool_value)):
            raise ValueError(
                "core_action_readback_configured_pool_not_canonical"
            )
        configured_pool = list(pool_value)
    else:
        configured_pool = []
    pool_source_value = pool_source_cell.value.to_mapping()["value"]
    if (
        pool_source_cell.disposition == CELL_AVAILABLE
        and type(pool_source_value) is not str
    ):
        raise TypeError("core_action_readback_pool_source_invalid")
    configured_source = (
        pool_source_value
        if pool_source_cell.disposition == CELL_AVAILABLE
        else ""
    )
    cell_lock = current_facts.exact("radio.cell_locked")
    capability_cell = current_facts.exact(
        "capability.runtime_capabilities"
    )
    capability_value = capability_cell.value.to_mapping()["value"]
    runtime_capabilities = (
        dict(capability_value)
        if (
            capability_cell.disposition == CELL_AVAILABLE
            and isinstance(capability_value, Mapping)
        )
        else {}
    )
    action_capabilities: dict[str, dict[str, Any]] = {}
    for action_name, capability_key in (
        CORE_ACTION_READBACK_TO_CAPABILITY
    ):
        enabled = (
            capability_contract_action_enabled(
                runtime_capabilities,
                capability_key,
            )
            if runtime_capabilities
            else None
        )
        metadata = (
            capability_contract_action_meta(
                runtime_capabilities,
                capability_key,
            )
            if runtime_capabilities
            else None
        )
        metadata = metadata if isinstance(metadata, Mapping) else {}
        valid = type(enabled) is bool
        action_capabilities[action_name] = {
            "enabled": enabled if valid else None,
            "valid": valid,
            "reason": str(
                metadata.get("reason")
                or metadata.get("write_block_reason")
                or capability_cell.reason
                or (
                    ""
                    if valid
                    else "runtime_capability_readback_unavailable"
                )
            ),
        }
    band_lock_valid = pool_cell.disposition == CELL_AVAILABLE
    cell_lock_valid = cell_lock.disposition == CELL_AVAILABLE
    cell_lock_value = cell_lock.value.to_mapping()["value"]
    if cell_lock_valid and type(cell_lock_value) is not bool:
        raise TypeError("core_action_readback_cell_lock_invalid")
    readback: dict[str, Any] = {
        "readback_source_sequence": accepted.source_sequence,
        "readback_sampled_at": accepted.raw.sampled_at,
        "band_locked": (
            len(configured_pool) == 1 if band_lock_valid else False
        ),
        "band_locked_valid": band_lock_valid,
        "band_locked_reason": (
            ""
            if band_lock_valid
            else (
                pool_cell.reason
                or "configured_band_pool_readback_unavailable"
            )
        ),
        "cell_locked": (
            cell_lock_value if cell_lock_valid else None
        ),
        "cell_locked_valid": cell_lock_valid,
        "cell_locked_reason": (
            ""
            if cell_lock_valid
            else cell_lock.reason or "cell_lock_readback_unavailable"
        ),
        "action_capabilities": {
            "schema": "CoreActionCapabilities.v1",
            "owner": "54guc.core.action_status",
            "source_sequence": accepted.source_sequence,
            "ready": all(
                bool(row["valid"])
                for row in action_capabilities.values()
            ),
            "actions": action_capabilities,
        },
    }
    if band_lock_valid:
        readback["band_lock_readback"] = {
            "configured_band_pool": list(configured_pool),
            "configured_band_pool_source": configured_source,
            "source_sequence": accepted.source_sequence,
        }
    return (
        CoreFrozenPayload.from_mapping(runtime_capabilities),
        CoreFrozenPayload.from_mapping(readback),
    )


def build_core_accepted_runtime_view(
    accepted: AcceptedModemGeneration,
    *,
    frame: CoreObservationFrame,
    current_facts: CoreAcceptedCurrentFacts,
) -> CoreAcceptedRuntimeView:
    """Create the only typed Core interpretation of an accepted Matrix."""

    if not isinstance(accepted, AcceptedModemGeneration):
        raise TypeError("core_accepted_view_generation_required")
    if not isinstance(frame, CoreObservationFrame):
        raise TypeError("core_accepted_view_frame_required")
    if frame.matrix_identity != accepted.raw.matrix_identity:
        raise ValueError("core_accepted_view_frame_identity_mismatch")
    radio_target_payload = frame.derived_facts.to_mapping().get(
        "radio_target_facts"
    )
    if not isinstance(radio_target_payload, Mapping):
        raise ValueError("core_accepted_view_radio_target_missing")
    radio_target = RadioTargetFacts.from_mapping(radio_target_payload)
    if (
        not isinstance(current_facts, CoreAcceptedCurrentFacts)
        or current_facts.matrix_identity != frame.matrix_identity
    ):
        raise TypeError("core_accepted_view_current_facts_required")
    runtime_capabilities, action_readback = _accepted_action_readback(
        accepted,
        current_facts,
    )
    capability_facts = frame.capability_facts.to_mapping()
    return CoreAcceptedRuntimeView(
        matrix_identity=frame.matrix_identity,
        accepted_commit_id=frame.accepted_commit_id,
        matrix_rtt=accepted.matrix_rtt,
        identity_code=frame.identity_code,
        identity_status=frame.identity_status,
        access_type=frame.access_type,
        sampled_at=frame.sampled_at,
        accepted_at=frame.accepted_at,
        published_at=frame.published_at,
        radio_connected=frame.radio_connected,
        radio_target=radio_target,
        capability_facts=frame.capability_facts,
        runtime_capabilities=runtime_capabilities,
        action_readback=action_readback,
    )


__all__ = (
    "build_core_accepted_current_facts",
    "build_core_accepted_runtime_view",
    "build_core_observation_frame",
)
