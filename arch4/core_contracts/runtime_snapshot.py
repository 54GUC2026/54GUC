"""Immutable Core state snapshot and optional presentation-extension contracts."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Protocol

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from arch4.core_contracts.accepted_current_facts import CoreAcceptedCurrentFacts
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.current_health import CoreCurrentHealthView
from arch4.runtime_register_contract_names import (
    CORE_CUSTOMER_REGISTER_NAMES,
)


CORE_RUNTIME_SNAPSHOT_SCHEMA = "CoreRuntimeSnapshot.v3"
CORE_RUNTIME_SNAPSHOT_OWNER = "54guc.core.runtime_snapshot"
CORE_RUNTIME_SNAPSHOT_HOOK_ID = "runtime_snapshot"
CORE_RUNTIME_SNAPSHOT_REGISTER_NAMES = CORE_CUSTOMER_REGISTER_NAMES


class CoreRuntimeSnapshotPort(Protocol):
    """Read-only customer boundary for coherent Core snapshots."""

    def snapshot_for(
        self,
        client_id: str,
        *,
        accepted_root: Any | None = None,
        health: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        published_at: float | None = None,
    ) -> "CoreRuntimeSnapshot": ...

FOUNDATION_NGUI_EXTENSION_HOOK_ID = "foundation_ngui_extension"
FOUNDATION_NGUI_EXTENSION_SCHEMA = "FoundationNGUIExtension.v1"
NGUI_EXTENSION_FRAME_SCHEMA = "NGUIExtensionFrame.v2"

FOUNDATION_ANTENNA_GUIDE_SOURCE_IDS = frozenset(
    {
        "antenna_guide.action",
        "antenna_guide.actions_enabled",
        "antenna_guide.active_action",
        "antenna_guide.antenna_meter_segments",
        "antenna_guide.antenna_score_role",
        "antenna_guide.antenna_score_text",
        "antenna_guide.nr_score_text",
        "antenna_guide.nr_score_role",
        "antenna_guide.antenna_trend_role",
        "antenna_guide.check_progress",
        "antenna_guide.check_status",
        "antenna_guide.check_trust",
        "antenna_guide.confidence",
        "antenna_guide.contributors",
        "antenna_guide.current_check",
        "antenna_guide.guidance",
        "antenna_guide.hint",
        "antenna_guide.monitor_state",
        "antenna_guide.new_result",
        "antenna_guide.next_step",
        "antenna_guide.saved_setup",
        "antenna_guide.saved_setup_ready",
        "antenna_guide.rsrp_scale",
        "antenna_guide.rsrq_scale",
        "antenna_guide.rssi_scale",
        "antenna_guide.sinr_scale",
        "antenna_guide.status",
        "antenna_guide.summary",
        "antenna_guide.trend",
        "command.antenna_guide_status",
    }
)
FOUNDATION_DETACHED_ANTENNA_GUIDE_SOURCE_IDS = frozenset(
    {
        "antenna_guide.action",
        "antenna_guide.check_progress",
        "antenna_guide.check_status",
        "antenna_guide.check_trust",
        "antenna_guide.current_check",
        "antenna_guide.new_result",
        "antenna_guide.next_step",
        "antenna_guide.saved_setup",
        "antenna_guide.saved_setup_ready",
        "antenna_guide.summary",
    }
)
FOUNDATION_ACTIVE_ANTENNA_GUIDE_SOURCE_IDS = (
    FOUNDATION_ANTENNA_GUIDE_SOURCE_IDS
    - FOUNDATION_DETACHED_ANTENNA_GUIDE_SOURCE_IDS
)

FOUNDATION_DATA_BUDGET_NGUI_SOURCE_IDS = frozenset(
    {
        "traffic.budget_cycle_day",
        "traffic.budget_day_used_bytes",
        "traffic.budget_left_bytes",
        "traffic.budget_limit_bytes",
        "traffic.budget_month_used_bytes",
        "traffic.budget_total_used_bytes",
    }
)

FOUNDATION_NGUI_SOURCE_IDS = frozenset(
    {
        "command.band_schedule_status",
        "command.band_sequence_status",
        "command.sms_enabled",
        "command.sms_every_s",
        "command.sms_keyword",
        "command.sms_message",
        "event.connectivity_transition",
        "device.health_status",
        "device.persistence_status",
    }
) | FOUNDATION_ACTIVE_ANTENNA_GUIDE_SOURCE_IDS | FOUNDATION_DATA_BUDGET_NGUI_SOURCE_IDS

@dataclass(frozen=True, slots=True)
class CoreRuntimeSnapshot:
    matrix_identity: MatrixGenerationIdentity
    accepted_commit_id: str
    accepted_root_fingerprint: str
    accepted_source_sampled_at: float
    accepted_current_facts: CoreAcceptedCurrentFacts
    sequence: int
    published_at: float
    registers: CoreFrozenPayload
    command_context: CoreFrozenPayload
    diagnostics: CoreFrozenPayload
    schema: str = CORE_RUNTIME_SNAPSHOT_SCHEMA
    owner: str = CORE_RUNTIME_SNAPSHOT_OWNER

    @property
    def current_health(self) -> CoreCurrentHealthView:
        """Read the existing health owner, without storing a second state."""
        row = self.registers.require("health")
        if row["sequence"] == 0:
            return CoreCurrentHealthView.pending(namespace=self.matrix_identity.namespace)
        return CoreCurrentHealthView.from_register_value(
            row["value"], register_sequence=row["sequence"],
            register_updated_at=row["updated_at"],
            expected_namespace=self.matrix_identity.namespace,
        )

    def __post_init__(self) -> None:
        if self.schema != CORE_RUNTIME_SNAPSHOT_SCHEMA:
            raise ValueError("core_runtime_snapshot_schema_invalid")
        if self.owner != CORE_RUNTIME_SNAPSHOT_OWNER:
            raise ValueError("core_runtime_snapshot_owner_invalid")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("core_runtime_snapshot_matrix_identity_required")
        if (
            type(self.accepted_commit_id) is not str
            or not self.accepted_commit_id
            or self.accepted_commit_id != self.accepted_commit_id.strip()
        ):
            raise ValueError("core_runtime_snapshot_commit_id_invalid")
        if (
            type(self.accepted_root_fingerprint) is not str
            or len(self.accepted_root_fingerprint) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.accepted_root_fingerprint
            )
        ):
            raise ValueError(
                "core_runtime_snapshot_root_fingerprint_invalid"
            )
        if (
            type(self.accepted_source_sampled_at) is not float
            or not math.isfinite(self.accepted_source_sampled_at)
            or self.accepted_source_sampled_at <= 0.0
        ):
            raise ValueError(
                "core_runtime_snapshot_source_timestamp_invalid"
            )
        if not isinstance(self.accepted_current_facts, CoreAcceptedCurrentFacts):
            raise TypeError("core_runtime_snapshot_accepted_current_facts_required")
        if self.accepted_current_facts.matrix_identity != self.matrix_identity:
            raise ValueError(
                "core_runtime_snapshot_accepted_current_facts_identity_mismatch"
            )
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("core_runtime_snapshot_positive_sequence_required")
        if (
            type(self.published_at) is not float
            or not math.isfinite(self.published_at)
            or self.published_at <= 0.0
        ):
            raise ValueError("core_runtime_snapshot_positive_timestamp_required")
        for field_name in ("registers", "command_context", "diagnostics"):
            if not isinstance(getattr(self, field_name), CoreFrozenPayload):
                raise TypeError(f"core_runtime_snapshot_{field_name}_payload_required")
        register_names = tuple(sorted(self.registers.to_mapping()))
        if register_names != tuple(sorted(CORE_RUNTIME_SNAPSHOT_REGISTER_NAMES)):
            raise ValueError("core_runtime_snapshot_register_catalog_mismatch")
        diagnostics = self.diagnostics.to_mapping()
        if set(diagnostics) != {
            "schema",
            "owner",
            "decision_inert",
            "health",
            "metadata",
        }:
            raise ValueError(
                "core_runtime_snapshot_diagnostics_contract_invalid"
            )
        if (
            diagnostics["schema"] != "CoreRuntimeSnapshotDiagnostics.v1"
            or diagnostics["owner"] != CORE_RUNTIME_SNAPSHOT_OWNER
            or diagnostics["decision_inert"] is not True
            or not isinstance(diagnostics["health"], Mapping)
            or not isinstance(diagnostics["metadata"], Mapping)
        ):
            raise ValueError(
                "core_runtime_snapshot_diagnostics_contract_invalid"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "matrix_identity": self.matrix_identity.as_dict(),
            "accepted_commit_id": self.accepted_commit_id,
            "accepted_root_fingerprint": self.accepted_root_fingerprint,
            "accepted_source_sampled_at": self.accepted_source_sampled_at,
            "accepted_current_facts": self.accepted_current_facts.to_payload(),
            "sequence": self.sequence,
            "published_at": self.published_at,
            "registers": self.registers.to_payload(),
            "command_context": self.command_context.to_payload(),
            "diagnostics": self.diagnostics.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class NGUIExtensionFrame:
    namespace: RuntimeGenerationNamespace
    hook_id: str
    payload_schema: str
    provider_id: str
    sequence: int
    published_at: float
    values: CoreFrozenPayload
    validity: CoreFrozenPayload
    freshness: CoreFrozenPayload
    reasons: CoreFrozenPayload
    sampled_at: CoreFrozenPayload
    sample_sequences: CoreFrozenPayload
    schema: str = NGUI_EXTENSION_FRAME_SCHEMA
    owner: str = "54guc.core.presentation_extension_boundary"

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError("ngui_extension_runtime_namespace_required")
        if self.schema != NGUI_EXTENSION_FRAME_SCHEMA:
            raise ValueError("ngui_extension_frame_schema_invalid")
        if self.owner != "54guc.core.presentation_extension_boundary":
            raise ValueError("ngui_extension_frame_owner_invalid")
        hook_id = str(self.hook_id or "").strip()
        if not hook_id:
            raise ValueError("ngui_extension_hook_required")
        payload_schema = str(self.payload_schema or "").strip()
        if not payload_schema:
            raise ValueError("ngui_extension_payload_schema_required")
        provider_id = str(self.provider_id or "").strip()
        if not provider_id:
            raise ValueError("ngui_extension_provider_required")
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("ngui_extension_positive_sequence_timestamp_required")
        if type(self.published_at) not in {int, float}:
            raise TypeError("ngui_extension_published_at_number_required")
        published_at = float(self.published_at)
        if not math.isfinite(published_at) or published_at <= 0.0:
            raise ValueError("ngui_extension_positive_sequence_timestamp_required")
        object.__setattr__(self, "hook_id", hook_id)
        object.__setattr__(self, "payload_schema", payload_schema)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "published_at", published_at)
        for field_name in (
            "values",
            "validity",
            "freshness",
            "reasons",
            "sampled_at",
            "sample_sequences",
        ):
            if not isinstance(getattr(self, field_name), CoreFrozenPayload):
                raise TypeError(f"ngui_extension_{field_name}_payload_required")
        value_keys = set(self.values.to_mapping())
        if not value_keys:
            raise ValueError("ngui_extension_values_required")
        for field_name in (
            "validity",
            "freshness",
            "reasons",
            "sampled_at",
            "sample_sequences",
        ):
            metadata_keys = set(getattr(self, field_name).to_mapping())
            extra = metadata_keys - value_keys
            if extra:
                raise ValueError(f"ngui_extension_{field_name}_unknown_source")
            missing = value_keys - metadata_keys
            if missing:
                raise ValueError(f"ngui_extension_{field_name}_source_missing")
        validity = self.validity.to_mapping()
        freshness = self.freshness.to_mapping()
        reasons = self.reasons.to_mapping()
        sample_sequences = self.sample_sequences.to_mapping()
        for source_id in value_keys:
            if type(validity[source_id]) is not bool:
                raise TypeError(
                    "ngui_extension_validity_bool_required:" + source_id
                )
            if freshness[source_id] not in {"fresh", "stale", "unknown"}:
                raise ValueError(
                    "ngui_extension_freshness_invalid:" + source_id
                )
            if type(reasons[source_id]) is not str:
                raise TypeError(
                    "ngui_extension_reason_string_required:" + source_id
                )
            if validity[source_id] and reasons[source_id]:
                raise ValueError(
                    "ngui_extension_valid_source_reason_invalid:" + source_id
                )
            if not validity[source_id] and not reasons[source_id]:
                raise ValueError(
                    "ngui_extension_unavailable_reason_required:" + source_id
                )
        for sample_source, sampled_at in self.sampled_at.to_mapping().items():
            if type(sampled_at) not in {int, float}:
                raise TypeError("ngui_extension_sampled_at_number_required")
            normalized_sampled_at = float(sampled_at)
            source_valid = validity[sample_source]
            if not math.isfinite(normalized_sampled_at):
                raise ValueError(
                    "ngui_extension_sampled_at_invalid:" + str(sample_source)
                )
            if source_valid and (
                normalized_sampled_at <= 0.0
                or normalized_sampled_at > self.published_at
            ):
                raise ValueError(
                    "ngui_extension_sampled_at_invalid:" + str(sample_source)
                )
            if not source_valid and normalized_sampled_at != 0.0:
                raise ValueError(
                    "ngui_extension_unavailable_sampled_at_must_be_zero:"
                    + str(sample_source)
                )
        for sample_source, sample_sequence in sample_sequences.items():
            if type(sample_sequence) is not int:
                raise TypeError("ngui_extension_sample_sequence_int_required")
            normalized_sequence = sample_sequence
            if validity[sample_source] and normalized_sequence <= 0:
                raise ValueError("ngui_extension_sample_sequence_invalid")
            if not validity[sample_source] and normalized_sequence != 0:
                raise ValueError(
                    "ngui_extension_unavailable_sample_sequence_must_be_zero:"
                    + str(sample_source)
                )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            **self.namespace.as_dict(),
            "hook_id": self.hook_id,
            "payload_schema": self.payload_schema,
            "provider_id": self.provider_id,
            "sequence": self.sequence,
            "published_at": self.published_at,
            "values": self.values.to_payload(),
            "validity": self.validity.to_payload(),
            "freshness": self.freshness.to_payload(),
            "reasons": self.reasons.to_payload(),
            "sampled_at": self.sampled_at.to_payload(),
            "sample_sequences": self.sample_sequences.to_payload(),
        }


__all__ = (
    "CORE_RUNTIME_SNAPSHOT_HOOK_ID",
    "CORE_RUNTIME_SNAPSHOT_OWNER",
    "CORE_RUNTIME_SNAPSHOT_REGISTER_NAMES",
    "CORE_RUNTIME_SNAPSHOT_SCHEMA",
    "FOUNDATION_NGUI_EXTENSION_HOOK_ID",
    "FOUNDATION_NGUI_EXTENSION_SCHEMA",
    "FOUNDATION_ACTIVE_ANTENNA_GUIDE_SOURCE_IDS",
    "FOUNDATION_ANTENNA_GUIDE_SOURCE_IDS",
    "FOUNDATION_DATA_BUDGET_NGUI_SOURCE_IDS",
    "FOUNDATION_DETACHED_ANTENNA_GUIDE_SOURCE_IDS",
    "FOUNDATION_NGUI_SOURCE_IDS",
    "NGUI_EXTENSION_FRAME_SCHEMA",
    "CoreRuntimeSnapshot",
    "CoreRuntimeSnapshotPort",
    "NGUIExtensionFrame",
)
