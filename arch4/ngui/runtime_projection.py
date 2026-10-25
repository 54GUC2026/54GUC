"""Project RuntimeRegisterBank state into NGUI input owners."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from arch4.architecture_contracts.runtime_generation import (
    ModemLiveReadyReceipt,
)
from arch4.core_contracts.current_state import (
    CoreCurrentFieldState,
    core_current_field_state,
)
from arch4.modem_live.truth_matrix import MODEM_LIVE_NON_MEASUREMENT_CELL_ADDRESSES
from arch4.core_contracts.runtime_snapshot import (
    CORE_RUNTIME_SNAPSHOT_REGISTER_NAMES,
    FOUNDATION_NGUI_EXTENSION_HOOK_ID,
    FOUNDATION_NGUI_SOURCE_IDS,
    CoreRuntimeSnapshot,
    NGUIExtensionFrame,
)
from arch4.intelligence_presentation_contract import (
    INTELLIGENCE_NGUI_EXTENSION_HOOK_ID,
    INTELLIGENCE_NGUI_SOURCE_IDS,
)
from arch4.operational_service_status_contract import normalize_operational_service_status
from arch4.runtime_register_contract_names import (
    REGISTER_ACTION,
    REGISTER_ACTION_AUXILIARY,
    REGISTER_CA,
    REGISTER_OPERATIONAL_STATUS,
    REGISTER_DOWNSTREAM_HEALTH,
    REGISTER_HEALTH,
    REGISTER_INTERNET_REACHABILITY,
    REGISTER_LATENCY,
    REGISTER_MUTATION,
    REGISTER_QUALITY,
    REGISTER_RUNTIME_CLOCK,
)
from .adapters.owner_register import adapt_owner_register
from .input_registers import (
    InputValue,
    OwnerInputBatch,
    owner_input_batch_fingerprint,
)
from .modem_live_matrix_view import (
    MODEM_LIVE_NGUI_CELL_ADDRESSES,
)
from .registry import NGUI_INPUT_SOURCE_REGISTRY
from .runtime_ipc import RuntimeInputFrame, runtime_frame_identity
from .millisecond_text import format_elapsed_milliseconds
from .types import FreshnessState, SourceOwner


class RuntimeValueTransform(str, Enum):
    """Allowed unit conversions at the runtime-to-NGUI boundary."""

    IDENTITY = "identity"
    FRACTION_TO_PERCENT = "fraction_to_percent"
    INTERNET_PAUSE_TEXT = "internet_pause_text"
    REACHABILITY_DISPOSITION_TO_BOOL = "reachability_disposition_to_bool"
    MODEM_HEALTH_ALIVE = "modem_health_alive"
    MODEM_HEALTH_CONNECTION = "modem_health_connection"
    LAST_REQUEST_TEXT = "last_request_text"


@dataclass(frozen=True, slots=True)
class RuntimeInputBinding:
    source_id: str
    owner: SourceOwner
    register: str
    path: tuple[str, ...]
    valid_path: tuple[str, ...] = ()
    sampled_at_path: tuple[str, ...] = ()
    sample_sequence_path: tuple[str, ...] = ()
    freshness_path: tuple[str, ...] = ()
    reason_path: tuple[str, ...] = ()
    state_path: tuple[str, ...] = ()
    metadata_key: str = ""
    transform: RuntimeValueTransform = RuntimeValueTransform.IDENTITY


@dataclass(frozen=True, slots=True)
class RuntimeExtensionBinding:
    source_id: str
    owner: SourceOwner
    hook_id: str


def _binding(
    source_id: str,
    owner: SourceOwner,
    register: str,
    path: str,
    *,
    valid: str = "",
    sampled_at: str = "",
    sample_sequence: str = "",
    freshness: str = "",
    reason: str = "",
    state: str = "",
    metadata: str = "",
    transform: RuntimeValueTransform = RuntimeValueTransform.IDENTITY,
) -> RuntimeInputBinding:
    return RuntimeInputBinding(
        source_id=source_id,
        owner=owner,
        register=register,
        path=tuple(part for part in path.split(".") if part),
        valid_path=tuple(part for part in valid.split(".") if part),
        sampled_at_path=tuple(part for part in sampled_at.split(".") if part),
        sample_sequence_path=tuple(
            part for part in sample_sequence.split(".") if part
        ),
        freshness_path=tuple(part for part in freshness.split(".") if part),
        reason_path=tuple(part for part in reason.split(".") if part),
        state_path=tuple(part for part in state.split(".") if part),
        metadata_key=metadata,
        transform=transform,
    )


L = SourceOwner.LATENCY
Q = SourceOwner.QUALITY
C = SourceOwner.CA
H = SourceOwner.HEALTH
RC = SourceOwner.RUNTIME_CLOCK
IR = SourceOwner.CORE_REACHABILITY
FOUNDATION = SourceOwner.FOUNDATION_SERVICE
CMD = SourceOwner.COMMAND

COMPLETE_RUNTIME_OWNER_ORDER = (
    SourceOwner.MODEM_MATRIX,
    SourceOwner.LATENCY,
    SourceOwner.QUALITY,
    SourceOwner.CA,
    SourceOwner.HEALTH,
    SourceOwner.RUNTIME_CLOCK,
    SourceOwner.CORE_REACHABILITY,
    SourceOwner.COMMAND,
    SourceOwner.FOUNDATION_PERSISTENCE,
    SourceOwner.FOUNDATION_RUNTIME_HEALTH,
    SourceOwner.FOUNDATION_SERVICE,
)


def runtime_owner_order_for_extensions(
    extensions: tuple[NGUIExtensionFrame, ...],
) -> tuple[SourceOwner, ...]:
    order = COMPLETE_RUNTIME_OWNER_ORDER
    if any(
        item.hook_id == INTELLIGENCE_NGUI_EXTENSION_HOOK_ID
        for item in extensions
    ):
        order += (SourceOwner.INTELLIGENCE,)
    return order


_ALL_REGISTER_BINDINGS = (
    # Core owns the runtime clock. Raw Modem Live values are read from the
    # complete Matrix and do not pass through this projection.
    _binding("runtime.uptime_s", RC, REGISTER_RUNTIME_CLOCK, "uptime_s"),
    _binding(
        "quality.internet_reachable",
        IR,
        REGISTER_INTERNET_REACHABILITY,
        "disposition",
        reason="reason",
        transform=RuntimeValueTransform.REACHABILITY_DISPOSITION_TO_BOOL,
    ),
    _binding(
        "quality.internet_outage_started_at", IR,
        REGISTER_INTERNET_REACHABILITY, "outage_started_at",
    ),
    _binding(
        "quality.internet_outage_event",
        IR,
        REGISTER_INTERNET_REACHABILITY,
        "latest_completed_outage",
    ),
    _binding(
        "quality.internet_coverage_event", IR,
        REGISTER_INTERNET_REACHABILITY, "latest_coverage_gap",
    ),

    # Latency processor authority.
    _binding(
        "quality.rtt_ms",
        L,
        REGISTER_LATENCY,
        "rtt_raw_ms",
        valid="rtt_raw_available",
        sampled_at="rtt_raw_observed_at",
        sample_sequence="rtt_raw_sample_sequence",
        freshness="rtt_raw_freshness",
        reason="rtt_raw_failure_reason",
    ),
    _binding("quality.rtt_average_ms", L, REGISTER_LATENCY, "rtt_sliding_avg"),
    _binding(
        "quality.rtt_delta_ms",
        L,
        REGISTER_LATENCY,
        "rtt_delta_display_ms",
        valid="rtt_delta_display_valid",
        reason="rtt_delta_display_reason",
        state="rtt_delta_display_state",
    ),
    _binding("quality.rtt_reference_ms", L, REGISTER_LATENCY, "rtt_effective_baseline"),
    _binding("quality.rtt_observed_at", L, REGISTER_LATENCY, "rtt_raw_observed_at"),
    _binding("quality.rtt_freshness", L, REGISTER_LATENCY, "rtt_raw_freshness"),
    _binding("quality.rtt_probe_status", L, REGISTER_LATENCY, "rtt_raw_probe_status"),
    _binding("quality.rtt_failure_reason", L, REGISTER_LATENCY, "rtt_raw_failure_reason"),
    _binding(
        "diagnostic.rtt_compute_probe_ok",
        L,
        REGISTER_LATENCY,
        "rtt_probe_ok",
    ),
    _binding(
        "quality.jitter_trimmed_ms",
        L,
        REGISTER_LATENCY,
        "rtt_dl_jitter_trimmed_ms",
        valid="rtt_dl_jitter_valid",
        reason="rtt_dl_jitter_reason",
    ),
    _binding("tuning.jitter_scale_ms", L, REGISTER_LATENCY, "jitter_formula.pct_scale_ms"),
    _binding("tuning.jitter_gain", L, REGISTER_LATENCY, "jitter_formula.visible_scale"),

    # Quality/congestion/scheduler authority.
    _binding(
        "connection.quality_pct",
        Q,
        REGISTER_QUALITY,
        "cq_cell",
        valid="cq_cell_valid",
        transform=RuntimeValueTransform.FRACTION_TO_PERCENT,
    ),
    _binding("radio.ri", Q, REGISTER_QUALITY, "ri", valid="ri_valid"),
    _binding(
        "quality.nr_secondary_active",
        Q,
        REGISTER_QUALITY,
        "nr_secondary_active",
    ),
    _binding(
        "quality.nr_transition_state",
        Q,
        REGISTER_QUALITY,
        "nr_transition_state",
    ),
    _binding("quality.rsrq_delta_db", Q, REGISTER_QUALITY, "rsrq_delta_display_db", valid="rsrq_delta_display_valid"),
    _binding("quality.rsrq_reference_db", Q, REGISTER_QUALITY, "rsrq_baseline_display_db", valid="rsrq_baseline_display_valid"),
    _binding(
        "quality.congestion_pct",
        Q,
        REGISTER_QUALITY,
        "congestion_display_pct",
        valid="congestion_display_valid",
    ),
    _binding("quality.congestion_trend", Q, REGISTER_QUALITY, "congestion_trend_display"),
    _binding("quality.congestion_bucket", Q, REGISTER_QUALITY, "congestion_bucket"),
    _binding("quality.congestion_source", Q, REGISTER_QUALITY, "congestion_source"),
    _binding(
        "diagnostic.congestion_trusted_rtt",
        Q,
        REGISTER_QUALITY,
        "congestion_trusted_rtt",
    ),
    _binding(
        "diagnostic.congestion_rtt_base_pct",
        Q,
        REGISTER_QUALITY,
        "congestion_rtt_base_pct",
    ),
    _binding(
        "diagnostic.congestion_jitter_addition_pct",
        Q,
        REGISTER_QUALITY,
        "congestion_active_jitter_pct",
    ),
    _binding("quality.congestion_rsrq_available", Q, REGISTER_QUALITY, "congestion_rsrq_available"),
    _binding("quality.congestion_rsrq_contributes", Q, REGISTER_QUALITY, "congestion_rsrq_contributes"),
    _binding("quality.congestion_rsrq_reason", Q, REGISTER_QUALITY, "congestion_rsrq_reason"),
    _binding("quality.congestion_rsrq_pct", Q, REGISTER_QUALITY, "congestion_rsrq_pct"),
    _binding("quality.congestion_rsrq_gap_db", Q, REGISTER_QUALITY, "congestion_rsrq_gap_db"),
    _binding("quality.congestion_rsrq_gate_db", Q, REGISTER_QUALITY, "congestion_rsrq_gap_threshold_db"),
    _binding("quality.congestion_bad_now", Q, REGISTER_QUALITY, "congestion_bad_now"),
    _binding("quality.congestion_pressure_moderate", Q, REGISTER_QUALITY, "congestion_pressure_moderate"),
    _binding("quality.congestion_pressure_high", Q, REGISTER_QUALITY, "congestion_pressure_high"),
    _binding("quality.congestion_qam_gap", Q, REGISTER_QUALITY, "congestion_qam_gap"),
    _binding("tuning.congestion_rsrq_weight", Q, REGISTER_QUALITY, "congestion_weights.rsrq_weight"),
    _binding("tuning.congestion_rtt_weight", Q, REGISTER_QUALITY, "congestion_weights.rtt_weight"),
    _binding(
        "quality.cq_pct",
        Q,
        REGISTER_QUALITY,
        "cq_cell",
        valid="cq_cell_valid",
        transform=RuntimeValueTransform.FRACTION_TO_PERCENT,
    ),
    _binding("quality.ri_stability_pct", Q, REGISTER_QUALITY, "ri_stability_pct", valid="ri_stability_valid"),
    _binding("quality.ri_activity", Q, REGISTER_QUALITY, "ri_activity_per_hour", valid="ri_stability_valid"),
    *(
        _binding(f"scheduler.{target}", Q, REGISTER_QUALITY, source)
        for target, source in (
            ("cqi0", "cqi0"), ("cqi1", "cqi1"),
            ("mcs_dl0", "mcs_dl0"), ("mcs_dl1", "mcs_dl1"), ("mcs_ul", "mcs_ul"),
            ("me0", "me0"), ("me1", "me1"), ("me_ul", "me_ul"),
            ("qam_dl0", "qam_dl0"), ("qam_dl1", "qam_dl1"), ("qam_ul", "qam_ul"),
            ("mcs_collapse", "mcs_collapse"),
        )
    ),

    # Only computed CA quality is here. Physical CA state comes from Matrix.
    _binding("quality.ca_ld_pct", C, REGISTER_CA, "ca_stat_ld_pct", valid="ca_stat_ld_valid"),
    _binding("quality.cart", C, REGISTER_CA, "cart"),
    _binding("quality.cast", C, REGISTER_CA, "cast"),
    _binding(
        "quality.cart_ld",
        C,
        REGISTER_CA,
        "cart_ld",
        state="cart_ld_state",
        reason="cart_ld_reason",
    ),
    _binding("quality.cart_activity", C, REGISTER_CA, "cart_activity_per_hour"),
    _binding("quality.ca_ld_activity", C, REGISTER_CA, "ca_ld_change_per_hour"),
    _binding("quality.cart_ld_activity", C, REGISTER_CA, "cart_ld_activity"),

    _binding("runtime.state", H, REGISTER_HEALTH, "facts.state"),
    _binding(
        "connection.modem_comm_alive",
        H,
        REGISTER_HEALTH,
        "facts",
        transform=RuntimeValueTransform.MODEM_HEALTH_ALIVE,
    ),
    _binding(
        "connection.user_link_state",
        H,
        REGISTER_HEALTH,
        "facts",
        transform=RuntimeValueTransform.MODEM_HEALTH_CONNECTION,
    ),

    # Command/action readback.
    _binding(
        "command.status",
        CMD,
        REGISTER_OPERATIONAL_STATUS,
        "",
        transform=RuntimeValueTransform.LAST_REQUEST_TEXT,
    ),
    _binding(
        "command.action_capabilities",
        CMD,
        REGISTER_ACTION_AUXILIARY,
        "action_capabilities",
    ),
    _binding(
        "manual.internet_pause_timing",
        CMD,
        REGISTER_ACTION_AUXILIARY,
        "internet_pause",
        transform=RuntimeValueTransform.INTERNET_PAUSE_TEXT,
    ),
    _binding("command.manual_available", CMD, REGISTER_MUTATION, "action_allowed"),
    _binding("command.manual_reason", CMD, REGISTER_MUTATION, "reason"),
    _binding(
        "command.command_protection_active",
        CMD,
        REGISTER_MUTATION,
        "sources.command_lifecycle.command_protection_active",
    ),
    _binding(
        "command.command_protection_reason",
        CMD,
        REGISTER_MUTATION,
        "sources.command_lifecycle.command_protection_reason",
    ),
    _binding(
        "command.operator_band_cooldown_active",
        CMD,
        REGISTER_MUTATION,
        "sources.band_action_policy.operator_cooldown_active",
    ),
    _binding(
        "command.operator_band_cooldown_remaining_s",
        CMD,
        REGISTER_MUTATION,
        "sources.band_action_policy.operator_cooldown_remaining_s",
    ),
    _binding(
        "command.band_locked",
        CMD,
        REGISTER_ACTION_AUXILIARY,
        "band_locked",
        valid="band_locked_valid",
        reason="band_locked_reason",
    ),
    _binding(
        "command.cell_locked",
        CMD,
        REGISTER_ACTION_AUXILIARY,
        "cell_locked",
        valid="cell_locked_valid",
        reason="cell_locked_reason",
    ),
    _binding("command.manual_transaction", CMD, REGISTER_ACTION, "core_command_status"),
    _binding("command.manual_cell_lock_timer", CMD, REGISTER_ACTION_AUXILIARY, "manual_cell_lock_timer"),
    _binding("command.ping_status", CMD, REGISTER_DOWNSTREAM_HEALTH, "message"),
    _binding("command.ping_enabled", CMD, REGISTER_DOWNSTREAM_HEALTH, "enabled"),
    _binding("command.ping_host", CMD, REGISTER_DOWNSTREAM_HEALTH, "target"),
    _binding("command.ping_count", CMD, REGISTER_DOWNSTREAM_HEALTH, "ping_count"),
    _binding("command.ping_every_s", CMD, REGISTER_DOWNSTREAM_HEALTH, "interval_s"),
)

def _extension_hook_id(source_id: str) -> str:
    if source_id in FOUNDATION_NGUI_SOURCE_IDS:
        return FOUNDATION_NGUI_EXTENSION_HOOK_ID
    if source_id in INTELLIGENCE_NGUI_SOURCE_IDS:
        return INTELLIGENCE_NGUI_EXTENSION_HOOK_ID
    return ""


_BINDINGS = tuple(
    binding
    for binding in _ALL_REGISTER_BINDINGS
    if (
        not _extension_hook_id(binding.source_id)
        and binding.source_id not in MODEM_LIVE_NGUI_CELL_ADDRESSES
    )
)
_EXTENSION_BINDINGS = tuple(
    RuntimeExtensionBinding(
        source_id=source_id,
        owner=NGUI_INPUT_SOURCE_REGISTRY[source_id].owner,
        hook_id=hook_id,
    )
    for source_id, hook_id in sorted(
        {
            source.source_id: _extension_hook_id(source.source_id)
            for source in NGUI_INPUT_SOURCE_REGISTRY.values()
            if _extension_hook_id(source.source_id)
        }.items()
    )
)


RUNTIME_INPUT_BINDINGS: Mapping[str, RuntimeInputBinding] = MappingProxyType(
    {binding.source_id: binding for binding in _BINDINGS}
)
RUNTIME_EXTENSION_BINDINGS: Mapping[str, RuntimeExtensionBinding] = MappingProxyType(
    {binding.source_id: binding for binding in _EXTENSION_BINDINGS}
)
RUNTIME_INPUT_REGISTER_NAMES: tuple[str, ...] = tuple(
    sorted({binding.register for binding in _BINDINGS})
)
_REQUIRED_LIVE_BOOLEAN_SOURCES: frozenset[str] = frozenset(
    {
        "diagnostic.rtt_compute_probe_ok",
        "diagnostic.congestion_trusted_rtt",
    }
)


def _accepted_root_fact_state(disposition: str) -> CoreCurrentFieldState:
    return {
        "available": CoreCurrentFieldState.AVAILABLE,
        "missing": CoreCurrentFieldState.MISSING,
        "invalid": CoreCurrentFieldState.INVALID,
        "unsupported": CoreCurrentFieldState.UNSUPPORTED,
    }[str(disposition)]


def _accepted_root_nested_value(
    value: Any,
    path: tuple[str, ...],
) -> tuple[bool, Any]:
    current = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return False, None
        current = current[key]
    return True, current




def _complete_extension_batches(
    extension: NGUIExtensionFrame,
) -> tuple[OwnerInputBatch, ...]:
    expected = {
        source_id: binding
        for source_id, binding in RUNTIME_EXTENSION_BINDINGS.items()
        if binding.hook_id == extension.hook_id
    }
    values = extension.values.to_mapping()
    if set(values) != set(expected):
        raise ValueError("ngui extension source denominator mismatch")
    validity = extension.validity.to_mapping()
    freshness = extension.freshness.to_mapping()
    reasons = extension.reasons.to_mapping()
    sampled_at = extension.sampled_at.to_mapping()
    sample_sequences = extension.sample_sequences.to_mapping()
    grouped: dict[SourceOwner, dict[str, Any]] = {}
    for source_id, value in values.items():
        grouped.setdefault(expected[source_id].owner, {})[source_id] = value
    return tuple(
        adapt_owner_register(
            owner=owner,
            sequence=extension.sequence,
            published_at=extension.published_at,
            values=owner_values,
            validity={
                source_id: bool(validity[source_id])
                for source_id in owner_values
            },
            sampled_at={
                source_id: float(sampled_at[source_id])
                for source_id in owner_values
            },
            sample_sequences={
                source_id: int(sample_sequences[source_id])
                for source_id in owner_values
            },
            freshness={
                source_id: FreshnessState(str(freshness[source_id]))
                for source_id in owner_values
            },
            reasons={
                source_id: str(reasons[source_id])
                for source_id in owner_values
            },
            fact_states={
                source_id: (
                    CoreCurrentFieldState.AVAILABLE
                    if bool(validity[source_id])
                    else CoreCurrentFieldState.MISSING
                )
                for source_id in owner_values
            },
        )
        for owner, owner_values in sorted(
            grouped.items(),
            key=lambda row: row[0].value,
        )
    )


def _complete_register_owner_batch(
    snapshot: CoreRuntimeSnapshot,
    owner: SourceOwner,
    registers: Mapping[str, Any],
) -> OwnerInputBatch:
    bindings = tuple(
        binding for binding in _BINDINGS if binding.owner is owner
    )
    if not bindings:
        raise RuntimeError("ngui runtime owner has no bindings:" + owner.value)
    rows = tuple(registers[binding.register] for binding in bindings)
    register_sequence = max(
        int(row.get("sequence", 0) or 0) for row in rows
    )
    batch_sequence = max(1, register_sequence)
    measurement_owner = owner in {SourceOwner.LATENCY, SourceOwner.QUALITY, SourceOwner.CA}
    health = snapshot.current_health if measurement_owner else None
    if measurement_owner:
        # Delivery can change on health alone; physical sample IDs stay intact.
        batch_sequence += health.register_sequence
    published_at = max(
        (
            float(row.get("updated_at", 0.0) or 0.0)
            for row in rows
        ),
        default=0.0,
    )
    published_at = max(published_at, snapshot.published_at)
    values: dict[str, Any] = {}
    validity: dict[str, bool] = {}
    freshness: dict[str, FreshnessState] = {}
    reasons: dict[str, str] = {}
    fact_states: dict[str, CoreCurrentFieldState] = {}
    sampled_at: dict[str, float] = {}
    sample_sequences: dict[str, int] = {}
    for binding in bindings:
        row = registers[binding.register]
        row_sequence = int(row.get("sequence", 0) or 0)
        value_map = (
            row.get("value")
            if isinstance(row.get("value"), Mapping)
            else {}
        )
        found, value = _read_binding(binding, row, value_map)
        if not found:
            if binding.source_id == "command.status" and row_sequence == 0:
                values[binding.source_id] = "Idle"
                validity[binding.source_id] = True
                freshness[binding.source_id] = FreshnessState.FRESH
                reasons[binding.source_id] = ""
                fact_states[binding.source_id] = CoreCurrentFieldState.AVAILABLE
                sampled_at[binding.source_id] = 1.0
                sample_sequences[binding.source_id] = 1
                continue
            values[binding.source_id] = None
            validity[binding.source_id] = False
            freshness[binding.source_id] = FreshnessState.UNKNOWN
            reasons[binding.source_id] = (
                str(row.get("error") or "")
                or "core_register_binding_unavailable"
            )
            fact_states[binding.source_id] = CoreCurrentFieldState.MISSING
            sampled_at[binding.source_id] = float(
                row.get("updated_at", 0.0) or 0.0
            )
            sample_sequences[binding.source_id] = row_sequence
            continue
        values[binding.source_id] = value
        state = _binding_fact_state(binding, value_map, row)
        valid = bool(
            row.get("valid", False)
            and state is CoreCurrentFieldState.AVAILABLE
            and _binding_valid(binding, value_map)
        )
        physical_sampled_at = (
            _binding_sampled_at(binding, value_map, row)
            if binding.sampled_at_path
            else 0.0
        )
        physical_sequence = (
            _binding_sample_sequence(binding, value_map, row)
            if binding.sample_sequence_path
            else 0
        )
        if valid and (
            (binding.sampled_at_path and physical_sampled_at <= 0.0)
            or (binding.sample_sequence_path and physical_sequence <= 0)
        ):
            raise RuntimeError(
                "ngui valid register source evidence invalid:"
                + binding.source_id
            )
        validity[binding.source_id] = valid
        freshness[binding.source_id] = _binding_freshness(
            binding,
            value_map,
            row,
        )
        reasons[binding.source_id] = _binding_reason(
            binding,
            value_map,
            row,
        )
        fact_states[binding.source_id] = state
        sampled_at[binding.source_id] = (
            physical_sampled_at
            if physical_sampled_at > 0.0
            else float(row.get("updated_at", 0.0) or 0.0)
        )
        source_matrix_sequence = int(
            value_map.get("sample_sequence", 0) or 0
        )
        sample_sequences[binding.source_id] = (
            physical_sequence
            if physical_sequence > 0
            else source_matrix_sequence
            if source_matrix_sequence > 0
            else row_sequence
        )
    for binding in bindings if measurement_owner else ():
        if binding.source_id.startswith("tuning."):
            continue  # User settings are not modem measurements.
        acquired_at = (sampled_at[binding.source_id] if binding.sampled_at_path
                       else snapshot.accepted_source_sampled_at)
        if not health.allows_measurement(acquired_at):
            values[binding.source_id] = None
            validity[binding.source_id] = False
            freshness[binding.source_id] = FreshnessState.UNKNOWN
            reasons[binding.source_id] = "modem_measurement_interrupted"
            fact_states[binding.source_id] = CoreCurrentFieldState.MISSING
    expected_sources = {
        source_id
        for source_id, spec in NGUI_INPUT_SOURCE_REGISTRY.items()
        if spec.owner is owner
    }
    if set(values) != expected_sources:
        raise RuntimeError(
            "ngui runtime owner source denominator mismatch:"
            f"{owner.value}:"
            f"missing={sorted(expected_sources - set(values))}:"
            f"extra={sorted(set(values) - expected_sources)}"
        )
    return adapt_owner_register(
        owner=owner,
        sequence=batch_sequence,
        published_at=published_at,
        values=values,
        validity=validity,
        sampled_at=sampled_at,
        sample_sequences=sample_sequences,
        freshness=freshness,
        reasons=reasons,
        fact_states=fact_states,
    )


def build_runtime_owner_batches_from_snapshot(
    snapshot: CoreRuntimeSnapshot,
    extensions: tuple[NGUIExtensionFrame, ...],
) -> tuple[OwnerInputBatch, ...]:
    """Translate one complete Core and Foundation view into NGUI owner batches."""

    if not isinstance(snapshot, CoreRuntimeSnapshot):
        raise TypeError("ngui complete Core snapshot required")
    registers = snapshot.registers.to_mapping()
    if set(registers) != set(
        CORE_RUNTIME_SNAPSHOT_REGISTER_NAMES
    ):
        raise RuntimeError("ngui complete Core register denominator mismatch")
    if not isinstance(extensions, tuple) or any(
        not isinstance(item, NGUIExtensionFrame) for item in extensions
    ):
        raise TypeError("ngui typed extension tuple required")
    extension_by_hook = {item.hook_id: item for item in extensions}
    if len(extension_by_hook) != len(extensions):
        raise RuntimeError("ngui duplicate extension hook")
    allowed_hooks = {
        FOUNDATION_NGUI_EXTENSION_HOOK_ID,
        INTELLIGENCE_NGUI_EXTENSION_HOOK_ID,
    }
    unknown_hooks = set(extension_by_hook) - allowed_hooks
    if unknown_hooks:
        raise RuntimeError(
            "ngui unknown extension hook:" + ",".join(sorted(unknown_hooks))
        )
    if FOUNDATION_NGUI_EXTENSION_HOOK_ID not in extension_by_hook:
        raise RuntimeError("ngui Foundation extension required")
    canonical_extensions = tuple(
        extension_by_hook[hook_id]
        for hook_id in (
            FOUNDATION_NGUI_EXTENSION_HOOK_ID,
            INTELLIGENCE_NGUI_EXTENSION_HOOK_ID,
        )
        if hook_id in extension_by_hook
    )
    if extensions != canonical_extensions:
        raise RuntimeError("ngui extension order is not canonical")
    if any(
        extension.namespace != snapshot.matrix_identity.namespace
        for extension in canonical_extensions
    ):
        raise RuntimeError("ngui extension namespace mismatch")

    # Matrix inputs come from accepted facts already in this snapshot. Do not
    # create a second transport for accepted-root state.
    facts = {fact.path: fact for fact in snapshot.accepted_current_facts.facts}
    health = snapshot.current_health
    controlled_transition = any(
        facts.get(path) is not None
        and facts[path].value.get("present") is True
        and facts[path].value.get("value") is True
        for path in (
            "health.modem_live_io_quiet_active",
            "health.live_register_quiet_hold",
        )
    )
    matrix_values: list[InputValue] = []
    for source_id, address in sorted(MODEM_LIVE_NGUI_CELL_ADDRESSES.items()):
        if address[0] == "$matrix":
            matrix_values.append(
                InputValue(
                    source_id=source_id,
                    value=snapshot.matrix_identity.matrix_sequence,
                    valid=True,
                    sampled_at=snapshot.accepted_source_sampled_at,
                    sample_sequence=snapshot.matrix_identity.matrix_sequence,
                )
            )
            continue
        path = f"{address[0]}.{address[1]}"
        fact = facts.get(path)
        if fact is None:
            raise RuntimeError("ngui accepted current fact missing:" + path)
        present = fact.value.require("present") is True
        nested_present, value = _accepted_root_nested_value(
            fact.value.get("value"),
            tuple(address[2:]),
        )
        available = bool(present and nested_present)
        interrupted = (
            available
            and address[0] in {"rf", "radio", "traffic", "slow"}
            and address[:2] not in MODEM_LIVE_NON_MEASUREMENT_CELL_ADDRESSES
            and not health.allows_measurement(fact.acquired_at)
        )
        if interrupted:
            available = False
        transition = bool(
            available
            and controlled_transition
            and address[0] in {"radio", "rf", "traffic"}
        )
        matrix_values.append(
            InputValue(
                source_id=source_id,
                value=value if available else None,
                valid=available,
                sampled_at=(
                    fact.sampled_at
                    if fact.sampled_at > 0.0
                    else snapshot.accepted_source_sampled_at
                ),
                sample_sequence=max(
                    1,
                    fact.source_sequence,
                    snapshot.matrix_identity.matrix_sequence,
                ),
                freshness=(
                    FreshnessState.TRANSITION
                    if transition
                    else (
                        FreshnessState.FRESH
                        if available
                        else FreshnessState.UNKNOWN
                    )
                ),
                reason=(
                    "controlled_transition"
                    if transition
                    else ("modem_measurement_interrupted" if interrupted else "" if available else fact.reason)
                ),
                fact_state=(
                    CoreCurrentFieldState.AVAILABLE
                    if available
                    else CoreCurrentFieldState.MISSING if interrupted else _accepted_root_fact_state(fact.disposition)
                ),
            )
        )
    batches: list[OwnerInputBatch] = [
        OwnerInputBatch(
            owner=SourceOwner.MODEM_MATRIX,
            sequence=snapshot.matrix_identity.matrix_sequence + health.register_sequence,
            published_at=snapshot.published_at,
            values=tuple(matrix_values),
        )
    ]
    for owner in (
        SourceOwner.LATENCY,
        SourceOwner.QUALITY,
        SourceOwner.CA,
        SourceOwner.HEALTH,
        SourceOwner.RUNTIME_CLOCK,
        SourceOwner.CORE_REACHABILITY,
        SourceOwner.COMMAND,
    ):
        batches.append(
            _complete_register_owner_batch(
                snapshot,
                owner,
                registers,
            )
        )
    for extension in canonical_extensions:
        batches.extend(_complete_extension_batches(extension))
    result = tuple(batches)
    if tuple(batch.owner for batch in result) != runtime_owner_order_for_extensions(
        extensions
    ):
        raise RuntimeError("ngui complete runtime owner denominator mismatch")
    return result


class NGUICompleteFrameProjector:
    """Emit a frame when any owner generation changes."""

    def __init__(
        self,
        modem_live_ready: ModemLiveReadyReceipt,
    ) -> None:
        if not isinstance(modem_live_ready, ModemLiveReadyReceipt):
            raise TypeError("ngui complete frame Modem Live ready required")
        self._namespace = modem_live_ready.matrix_identity.namespace
        self._frame_sequence = 0
        self._last_vector: tuple[Any, ...] | None = None
        self._last_terminal_identity: tuple[Any, ...] | None = None
        self._pending_vectors: dict[int, tuple[Any, ...]] = {}
        self._last_owner_order: tuple[SourceOwner, ...] = ()
        self._last_owner_fingerprints: dict[SourceOwner, str] = {}
        self._pending_owner_states: dict[
            int,
            tuple[tuple[SourceOwner, ...], dict[SourceOwner, str]],
        ] = {}

    def project(
        self,
        snapshot: CoreRuntimeSnapshot,
        extensions: tuple[NGUIExtensionFrame, ...],
    ) -> RuntimeInputFrame | None:
        if snapshot.matrix_identity.namespace != self._namespace:
            raise RuntimeError("ngui complete frame foreign namespace")
        extension_vector = tuple(
            (item.provider_id, item.sequence, item.values.sha256)
            for item in extensions
        )
        vector = (
            snapshot.accepted_root_fingerprint,
            snapshot.registers.sha256,
            extension_vector,
        )
        if vector == self._last_vector:
            return None
        sequence = self._frame_sequence + 1
        complete_batches = build_runtime_owner_batches_from_snapshot(
            snapshot,
            extensions,
        )
        complete_owner_order = runtime_owner_order_for_extensions(extensions)
        complete_fingerprints = {
            batch.owner: owner_input_batch_fingerprint(batch)
            for batch in complete_batches
        }
        bootstrap = bool(
            not self._last_owner_fingerprints
            or complete_owner_order != self._last_owner_order
        )
        owner_batches = (
            complete_batches
            if bootstrap
            else tuple(
                batch
                for batch in complete_batches
                if self._last_owner_fingerprints.get(batch.owner)
                != complete_fingerprints[batch.owner]
            )
        )
        owner_fingerprints = tuple(
            complete_fingerprints[batch.owner]
            for batch in owner_batches
        )
        frame = RuntimeInputFrame(
            sequence=sequence,
            published_at=snapshot.published_at,
            matrix_identity=snapshot.matrix_identity,
            accepted_commit_id=snapshot.accepted_commit_id,
            accepted_root_fingerprint=snapshot.accepted_root_fingerprint,
            accepted_source_sampled_at=(
                snapshot.accepted_source_sampled_at
            ),
            snapshot_sequence=snapshot.sequence,
            command_context=snapshot.command_context,
            owner_batches=owner_batches,
            owner_order=tuple(batch.owner for batch in owner_batches),
            complete_owner_order=complete_owner_order,
            owner_fingerprints=owner_fingerprints,
            bootstrap=bootstrap,
            extension_vector=extension_vector,
        )
        self._pending_vectors[sequence] = vector
        self._pending_owner_states[sequence] = (
            complete_owner_order,
            complete_fingerprints,
        )
        return frame

    def commit(self, frame: RuntimeInputFrame) -> None:
        identity = runtime_frame_identity(frame)
        if (
            frame.sequence == self._frame_sequence
            and identity == self._last_terminal_identity
        ):
            return
        if frame.sequence != self._frame_sequence + 1:
            raise RuntimeError("ngui complete frame commit stale")
        vector = self._pending_vectors.pop(frame.sequence, None)
        if vector is None:
            raise RuntimeError("ngui complete frame candidate missing")
        owner_state = self._pending_owner_states.pop(frame.sequence, None)
        if owner_state is None:
            raise RuntimeError("ngui projected owner candidate missing")
        self._frame_sequence = frame.sequence
        self._last_vector = vector
        self._last_terminal_identity = identity
        self._last_owner_order = owner_state[0]
        self._last_owner_fingerprints = dict(owner_state[1])
        self._pending_vectors.clear()
        self._pending_owner_states.clear()

    def discard(self, frame: RuntimeInputFrame) -> None:
        """Quarantine a rejected candidate and advance frame ordering."""

        identity = runtime_frame_identity(frame)
        if (
            frame.sequence == self._frame_sequence
            and identity == self._last_terminal_identity
        ):
            return
        if frame.sequence != self._frame_sequence + 1:
            raise RuntimeError("ngui complete frame discard stale")
        vector = self._pending_vectors.pop(frame.sequence, None)
        if vector is None:
            raise RuntimeError("ngui complete frame discard candidate missing")
        if self._pending_owner_states.pop(frame.sequence, None) is None:
            raise RuntimeError("ngui projected owner discard candidate missing")
        self._frame_sequence = frame.sequence
        self._last_vector = vector
        self._last_terminal_identity = identity
        # The parent kept its previous complete state. The next candidate must
        # contain a full projection, not a delta from the rejected candidate.
        self._last_owner_order = ()
        self._last_owner_fingerprints = {}
        self._pending_vectors.clear()
        self._pending_owner_states.clear()




def _read_binding(
    binding: RuntimeInputBinding,
    row: Mapping[str, Any],
    values: Mapping[str, Any],
) -> tuple[bool, Any]:
    if binding.source_id == "command.status" and int(row.get("sequence", 0) or 0) == 0:
        # The canonical register has no receipt until its first publication.
        return False, None
    if binding.metadata_key:
        if binding.metadata_key not in row:
            return False, None
        return True, _apply_binding_transform(binding, row.get(binding.metadata_key))
    for path in (binding.path,):
        found, value = _read_path(values, path)
        if found:
            normalized = _normalize_runtime_value(
                binding.source_id,
                _apply_binding_transform(binding, value),
            )
            if (
                binding.source_id in _REQUIRED_LIVE_BOOLEAN_SOURCES
                and type(normalized) is not bool
            ):
                raise TypeError(
                    f"{binding.source_id}: strict boolean value required"
                )
            return True, normalized
    if (
        binding.source_id in _REQUIRED_LIVE_BOOLEAN_SOURCES
        and int(values.get("sample_sequence", 0) or 0) > 0
    ):
        raise RuntimeError(
            f"{binding.source_id}: required accepted-generation value is missing"
        )
    return False, None


def _apply_binding_transform(binding: RuntimeInputBinding, value: Any) -> Any:
    if binding.transform is RuntimeValueTransform.IDENTITY:
        return value
    if binding.transform is RuntimeValueTransform.LAST_REQUEST_TEXT:
        latest = normalize_operational_service_status(value)["latest"]
        stamp = datetime.fromtimestamp(latest["occurred_at"]).strftime("%H:%M:%S")
        return f"{stamp} — {latest['message']}"
    if binding.transform is RuntimeValueTransform.FRACTION_TO_PERCENT:
        if isinstance(value, bool):
            raise TypeError(f"{binding.source_id}: boolean is not a CQ fraction")
        try:
            return float(value) * 100.0
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"{binding.source_id}: expected numeric CQ fraction, got {value!r}"
            ) from exc
    if binding.transform is RuntimeValueTransform.INTERNET_PAUSE_TEXT:
        if not isinstance(value, Mapping):
            raise TypeError(
                f"{binding.source_id}: internet pause mapping required"
            )
        if (
            value.get("schema") != "CoreActionInternetPause.v1"
            or value.get("owner") != "54guc.core.action_internet_pause"
        ):
            raise ValueError(
                f"{binding.source_id}: internet pause owner/schema invalid"
            )
        state = value.get("state")
        if state == "idle":
            return "---"
        if state == "connecting":
            return "...connecting..."
        if state == "measured":
            return format_elapsed_milliseconds(
                value.get("value_ms"),
                missing_text="...connecting...",
            )
        raise ValueError(
            f"{binding.source_id}: internet pause state invalid"
        )
    if binding.transform is RuntimeValueTransform.REACHABILITY_DISPOSITION_TO_BOOL:
        disposition = str(value or "").strip().lower()
        if disposition == "reachable":
            return True
        if disposition == "outage":
            return False
        if disposition == "unavailable":
            return None
        raise ValueError(
            f"{binding.source_id}: reachability disposition invalid"
        )
    if binding.transform in {
        RuntimeValueTransform.MODEM_HEALTH_ALIVE,
        RuntimeValueTransform.MODEM_HEALTH_CONNECTION,
    }:
        if not isinstance(value, Mapping):
            raise TypeError(
                f"{binding.source_id}: modem health facts mapping required"
            )
        state = str(value.get("state") or "").strip().lower()
        if state not in {"ok", "reconnecting", "degraded"}:
            raise ValueError(
                f"{binding.source_id}: modem health state invalid"
            )
        visible = value.get("modem_read_missed_operator_visible")
        if type(visible) is not bool:
            raise TypeError(
                f"{binding.source_id}: modem read visibility bool required"
            )
        alive = bool(state == "ok" or not visible)
        if binding.transform is RuntimeValueTransform.MODEM_HEALTH_ALIVE:
            return alive
        if alive:
            return "Connected"
        return "Degraded" if state == "degraded" else "Reconnecting"
    raise RuntimeError(
        f"{binding.source_id}: unsupported runtime value transform {binding.transform!r}"
    )


def _normalize_runtime_value(source_id: str, value: Any) -> Any:
    if source_id == "smartness.selected_action" and str(value or "").strip() == "":
        return "none"
    return _json_safe_input_value(value)


def _json_safe_input_value(value: Any) -> Any:
    """Normalize runtime values into the NGUI and log-safe value model."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return _json_safe_input_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe_input_value(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe_input_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe_input_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_json_safe_input_value(item) for item in value]
        return sorted(normalized, key=repr)
    raise TypeError(
        f"NGUI runtime input value is not boundary-safe: {type(value).__name__}"
    )


def _binding_valid(binding: RuntimeInputBinding, values: Mapping[str, Any]) -> bool:
    if not binding.valid_path:
        return True
    found, value = _read_path(values, binding.valid_path)
    return bool(found and value is True)


def _binding_fact_state(
    binding: RuntimeInputBinding,
    values: Mapping[str, Any],
    row: Mapping[str, Any],
) -> CoreCurrentFieldState:
    if binding.state_path:
        found, value = _read_path(values, binding.state_path)
        if not found:
            raise RuntimeError(
                f"{binding.source_id}: required fact-state metadata is missing"
            )
        return core_current_field_state(value)
    return CoreCurrentFieldState.AVAILABLE


def _required_binding_metadata(
    binding: RuntimeInputBinding,
    values: Mapping[str, Any],
    path: tuple[str, ...],
    label: str,
) -> Any:
    found, value = _read_path(values, path)
    if not found:
        raise RuntimeError(f"{binding.source_id}: required {label} metadata is missing")
    return value


def _binding_sampled_at(
    binding: RuntimeInputBinding,
    values: Mapping[str, Any],
    row: Mapping[str, Any],
) -> float:
    if not binding.sampled_at_path:
        return float(row.get("updated_at", 0.0) or 0.0)
    return float(
        _required_binding_metadata(
            binding,
            values,
            binding.sampled_at_path,
            "sampled_at",
        )
        or 0.0
    )


def _binding_sample_sequence(
    binding: RuntimeInputBinding,
    values: Mapping[str, Any],
    row: Mapping[str, Any],
) -> int:
    if not binding.sample_sequence_path:
        return int(row.get("sequence", 0) or 0)
    return int(
        _required_binding_metadata(
            binding,
            values,
            binding.sample_sequence_path,
            "sample_sequence",
        )
        or 0
    )


def _binding_freshness(
    binding: RuntimeInputBinding,
    values: Mapping[str, Any],
    row: Mapping[str, Any],
) -> FreshnessState:
    if not binding.freshness_path:
        return _freshness(row)
    raw = str(
        _required_binding_metadata(
            binding,
            values,
            binding.freshness_path,
            "freshness",
        )
        or ""
    ).strip().lower()
    if raw == "unavailable":
        return FreshnessState.UNKNOWN
    try:
        return FreshnessState(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{binding.source_id}: invalid freshness metadata {raw!r}"
        ) from exc


def _binding_reason(
    binding: RuntimeInputBinding,
    values: Mapping[str, Any],
    row: Mapping[str, Any],
) -> str:
    if not binding.reason_path:
        return str(row.get("error") or "")
    return str(
        _required_binding_metadata(
            binding,
            values,
            binding.reason_path,
            "reason",
        )
        or ""
    )


def _read_path(values: Mapping[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    current: Any = values
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _freshness(row: Mapping[str, Any]) -> FreshnessState:
    if bool(row.get("transition", False)) or bool(row.get("dirty", False)):
        return FreshnessState.TRANSITION
    if bool(row.get("stale", False)):
        return FreshnessState.STALE
    return FreshnessState.FRESH
