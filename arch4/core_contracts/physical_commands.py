"""Transport-neutral physical command dispatch evidence contract."""

from __future__ import annotations
from arch4.core_contracts.network_settings import network_stage_kinds

import math
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping

from arch4.core_contracts.command_targets import (
    CORE_COMMAND_TARGET_TYPES,
    CoreBandSetTarget,
    CoreBandAutoTarget,
    CoreNrBandTarget,
    CoreRadioModeTarget,
    CoreCommandTarget,
    CorePBandTarget,
    CoreUeCapabilityTarget,
    CoreWifiRadioTarget,
    CoreNetworkSettingsTarget,
    core_command_target_from_payload,
    core_command_target_payload,
)


PHYSICAL_COORDINATION_OPERATIONS = frozenset(
    {
        "device_local_recovery_pause",
        "device_local_recovery_resume",
        "wifi_commit",
        "wifi_reload",
    }
)


@dataclass(frozen=True, slots=True)
class PhysicalCommandCoordinationTarget:
    """A narrow non-user write required around an exact requested target."""

    SCHEMA_VERSION: ClassVar[str] = "PhysicalCommandCoordinationTarget.v1"

    operation: str

    def __post_init__(self) -> None:
        operation = _exact_string(
            self.operation,
            "physical_coordination_operation_required",
        )
        if operation not in PHYSICAL_COORDINATION_OPERATIONS:
            raise ValueError("physical_coordination_operation_invalid")
        object.__setattr__(self, "operation", operation)

    def as_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "operation": self.operation,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "PhysicalCommandCoordinationTarget":
        if not isinstance(value, Mapping):
            raise TypeError("physical_coordination_mapping_required")
        if set(value) != {"schema_version", "operation"}:
            raise ValueError("physical_coordination_fields_invalid")
        if value.get("schema_version") != cls.SCHEMA_VERSION:
            raise ValueError("physical_coordination_schema_invalid")
        return cls(operation=value.get("operation"))


@dataclass(frozen=True, slots=True)
class PhysicalCommandFailureDisposition:
    """Typed non-transport reason why a physical transaction could not finish."""

    SCHEMA_VERSION: ClassVar[str] = "PhysicalCommandFailureDisposition.v1"

    phase: str
    reason: str
    blocked_stage_kind: str
    blocked_ordinal: int
    after_stage_ordinal: int

    def __post_init__(self) -> None:
        phase = _exact_string(
            self.phase,
            "physical_failure_phase_required",
        )
        if phase not in {"interstage_confirmation", "pre_dispatch"}:
            raise ValueError("physical_failure_phase_invalid")
        reason = _exact_string(
            self.reason,
            "physical_failure_reason_required",
        )
        blocked_stage_kind = _exact_string(
            self.blocked_stage_kind,
            "physical_failure_blocked_stage_required",
        )
        blocked_ordinal = _exact_positive_int(
            self.blocked_ordinal,
            "physical_failure_blocked_ordinal_invalid",
        )
        after_stage_ordinal = _exact_nonnegative_int(
            self.after_stage_ordinal,
            "physical_failure_after_ordinal_invalid",
        )
        if blocked_ordinal != after_stage_ordinal + 1:
            raise ValueError("physical_failure_ordinal_relation_invalid")
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "blocked_stage_kind", blocked_stage_kind)
        object.__setattr__(self, "blocked_ordinal", blocked_ordinal)
        object.__setattr__(self, "after_stage_ordinal", after_stage_ordinal)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "phase": self.phase,
            "reason": self.reason,
            "blocked_stage_kind": self.blocked_stage_kind,
            "blocked_ordinal": self.blocked_ordinal,
            "after_stage_ordinal": self.after_stage_ordinal,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "PhysicalCommandFailureDisposition":
        if not isinstance(value, Mapping):
            raise TypeError("physical_failure_mapping_required")
        expected = {
            "schema_version",
            "phase",
            "reason",
            "blocked_stage_kind",
            "blocked_ordinal",
            "after_stage_ordinal",
        }
        if set(value) != expected:
            raise ValueError("physical_failure_fields_invalid")
        if value.get("schema_version") != cls.SCHEMA_VERSION:
            raise ValueError("physical_failure_schema_invalid")
        return cls(
            phase=value.get("phase"),
            reason=value.get("reason"),
            blocked_stage_kind=value.get("blocked_stage_kind"),
            blocked_ordinal=value.get("blocked_ordinal"),
            after_stage_ordinal=value.get("after_stage_ordinal"),
        )


PhysicalCommandStageTarget = (
    CoreCommandTarget | PhysicalCommandCoordinationTarget
)


def physical_stage_target_payload(
    target: PhysicalCommandStageTarget,
) -> dict[str, Any]:
    if isinstance(target, CORE_COMMAND_TARGET_TYPES):
        return core_command_target_payload(target)
    if isinstance(target, PhysicalCommandCoordinationTarget):
        return target.as_dict()
    raise TypeError("physical_stage_typed_target_required")


def physical_stage_target_from_payload(
    value: Mapping[str, Any],
) -> PhysicalCommandStageTarget:
    if not isinstance(value, Mapping):
        raise TypeError("physical_stage_target_payload_required")
    if value.get("schema_version") == (
        PhysicalCommandCoordinationTarget.SCHEMA_VERSION
    ):
        return PhysicalCommandCoordinationTarget.from_mapping(value)
    return core_command_target_from_payload(value)


def _exact_string(value: Any, reason: str) -> str:
    if not isinstance(value, str):
        raise TypeError(reason)
    text = value.strip()
    if not text:
        raise ValueError(reason)
    return text


def _exact_positive_int(value: Any, reason: str) -> int:
    if type(value) is not int or value <= 0:
        raise TypeError(reason)
    return value


def _exact_nonnegative_int(value: Any, reason: str) -> int:
    if type(value) is not int or value < 0:
        raise TypeError(reason)
    return value


def _exact_positive_number(value: Any, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(reason)
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise ValueError(reason)
    return parsed


def _physical_stage_kind_for_target(target: CoreCommandTarget) -> str:
    if isinstance(target, CorePBandTarget):
        return "pband_anchor"
    return target.command_kind.lower()


def _validate_stage_target(
    *,
    stage_kind: str,
    target: PhysicalCommandStageTarget,
) -> None:
    if isinstance(target, PhysicalCommandCoordinationTarget):
        if stage_kind != target.operation:
            raise ValueError("physical_coordination_stage_target_mismatch")
        return
    if stage_kind == "pband_anchor":
        if not isinstance(target, CorePBandTarget) or target.ca_pool:
            raise ValueError("physical_pband_anchor_target_invalid")
        return
    if isinstance(target, CoreRadioModeTarget) and stage_kind in {"radio_disconnect", "radio_reconnect"}:
        return
    if stage_kind == "ca_pool":
        if (
            not isinstance(target, CoreBandSetTarget)
            or target.command_kind != "BAND_SELECTION"
        ):
            raise ValueError("physical_ca_pool_target_invalid")
        return
    if isinstance(target, CoreNetworkSettingsTarget):
        if stage_kind not in target.stage_kinds + network_stage_kinds(target, coordination_profile="zte_zwrt") + network_stage_kinds(target, coordination_profile="tplink_dev2"):
            raise ValueError("network_settings_stage_target_mismatch")
        return
    if stage_kind == "wifi_config":
        if not isinstance(target, CoreWifiRadioTarget):
            raise ValueError("physical_wifi_config_target_invalid")
        return
    if stage_kind in {
        "ue_dl_256qam",
        "ue_ul_64qam",
        "ue_restart",
    }:
        if not isinstance(target, CoreUeCapabilityTarget):
            raise ValueError("physical_ue_stage_target_invalid")
        field = {
            "ue_dl_256qam": target.dl_256qam,
            "ue_ul_64qam": target.ul_64qam,
            "ue_restart": target.restart,
        }[stage_kind]
        if field is None:
            raise ValueError("physical_ue_stage_value_missing")
        return
    if stage_kind != _physical_stage_kind_for_target(target):
        raise ValueError("physical_dispatch_stage_target_mismatch")


@dataclass(frozen=True, slots=True)
class PhysicalCommandDispatchEvidence:
    """One exact modem-write dispatch translated into Core vocabulary."""

    SCHEMA_VERSION: ClassVar[str] = "PhysicalCommandDispatchEvidence.v2"
    OWNER: ClassVar[str] = "modem_live"

    stage_kind: str
    ordinal: int
    request_dispatched_at: float
    transport_returned_at: float
    outcome: str
    reason: str
    detail: Mapping[str, Any]
    target: PhysicalCommandStageTarget

    def __post_init__(self) -> None:
        stage_kind = _exact_string(
            self.stage_kind,
            "physical_dispatch_stage_kind_required",
        )
        outcome = _exact_string(
            self.outcome,
            "physical_dispatch_outcome_required",
        )
        _exact_string(
            self.reason,
            "physical_dispatch_reason_required",
        )
        ordinal = _exact_positive_int(
            self.ordinal,
            "physical_dispatch_ordinal_invalid",
        )
        dispatched_at = _exact_positive_number(
            self.request_dispatched_at,
            "physical_dispatch_timestamp_invalid",
        )
        returned_at = _exact_positive_number(
            self.transport_returned_at,
            "physical_transport_return_timestamp_invalid",
        )
        if returned_at < dispatched_at:
            raise ValueError("physical_transport_return_timestamp_invalid")
        if outcome not in {
            "accepted",
            "response_unavailable",
            "rejected",
        }:
            raise ValueError("physical_dispatch_outcome_invalid")
        if not isinstance(self.detail, Mapping):
            raise TypeError("physical_dispatch_detail_mapping_required")
        if not isinstance(
            self.target,
            (*CORE_COMMAND_TARGET_TYPES, PhysicalCommandCoordinationTarget),
        ):
            raise TypeError("physical_dispatch_typed_target_required")
        _validate_stage_target(stage_kind=stage_kind, target=self.target)
        if "native_auto_readback" in self.detail:
            proof = self.detail["native_auto_readback"]
            required = {"schema", "owner", "domain", "started_at", "finished_at",
                        "source", "native_auto", "acquisition_attempt_id"}
            if (not isinstance(self.target, CoreBandAutoTarget)
                    or not isinstance(proof, Mapping) or set(proof) != required
                    or proof["schema"] != "NativeAutoReadback.v1"
                    or proof["owner"] != self.OWNER
                    or proof["domain"] != self.target.domain
                    or proof["native_auto"] is not True):
                raise ValueError("physical_native_auto_readback_invalid")
            start = _exact_positive_number(proof["started_at"], "physical_native_auto_readback_time_invalid")
            finish = _exact_positive_number(proof["finished_at"], "physical_native_auto_readback_time_invalid")
            if start < returned_at or finish < start:
                raise ValueError("physical_native_auto_readback_stale")
            _exact_string(proof["source"], "physical_native_auto_readback_source_required")
            _exact_string(proof["acquisition_attempt_id"], "physical_native_auto_readback_attempt_required")
        if "native_data_session_readback" in self.detail:
            proof = self.detail["native_data_session_readback"]
            required = {"schema", "owner", "started_at", "finished_at", "source",
                        "acquisition_attempt_id", "profile_id", "configuration",
                        "data_session_connected"}
            if (not isinstance(self.target, CoreRadioModeTarget)
                    or stage_kind != "radio_mode" or ordinal != 2 or outcome != "accepted"
                    or not isinstance(proof, Mapping) or set(proof) != required
                    or proof["schema"] != "NativeDataSessionReadback.v1"
                    or proof["owner"] != self.OWNER
                    or proof["configuration"] != self.target.configuration()
                    or proof["data_session_connected"] is not True):
                raise ValueError("physical_native_data_session_readback_invalid")
            start = _exact_positive_number(proof["started_at"], "physical_native_data_session_readback_time_invalid")
            finish = _exact_positive_number(proof["finished_at"], "physical_native_data_session_readback_time_invalid")
            if start < returned_at or finish < start:
                raise ValueError("physical_native_data_session_readback_stale")
            for field in ("source", "acquisition_attempt_id", "profile_id"):
                _exact_string(proof[field], "physical_native_data_session_readback_" + field + "_required")

    @property
    def permits_target_verification(self) -> bool:
        """Whether exact Matrix readback may decide the requested target."""

        return self.outcome in {"accepted", "response_unavailable"}

    @property
    def confirmed_by_transport(self) -> bool:
        return self.outcome == "accepted"

    @property
    def native_auto_release_confirmed(self) -> bool:
        if "native_auto_readback" in self.detail:
            # Frozen dataclasses do not deep-freeze a caller's Mapping.
            # Revalidate at consumption; mutated proof cannot open stage two.
            self.__post_init__()
        return isinstance(self.target, CoreBandAutoTarget) and (
            self.confirmed_by_transport or (self.outcome == "response_unavailable"
                                           and "native_auto_readback" in self.detail))

    @property
    def native_data_session_confirmed(self) -> bool:
        if "native_data_session_readback" not in self.detail:
            return False
        self.__post_init__()  # Revalidate nested mappings at consumption.
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "owner": self.OWNER,
            "stage_kind": self.stage_kind,
            "ordinal": int(self.ordinal),
            "request_dispatched_at": float(self.request_dispatched_at),
            "transport_returned_at": float(self.transport_returned_at),
            "outcome": self.outcome,
            "reason": self.reason,
            "detail": dict(self.detail),
            "target": physical_stage_target_payload(self.target),
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "PhysicalCommandDispatchEvidence":
        if not isinstance(value, Mapping):
            raise TypeError("physical_dispatch_evidence_mapping_required")
        expected = {
            "schema_version",
            "owner",
            "stage_kind",
            "ordinal",
            "request_dispatched_at",
            "transport_returned_at",
            "outcome",
            "reason",
            "detail",
            "target",
        }
        if set(value) != expected:
            raise ValueError("physical_dispatch_evidence_fields_invalid")
        if value.get("schema_version") != cls.SCHEMA_VERSION:
            raise ValueError("physical_dispatch_evidence_schema_invalid")
        if value.get("owner") != cls.OWNER:
            raise ValueError("physical_dispatch_evidence_owner_invalid")
        return cls(
            stage_kind=value.get("stage_kind"),
            ordinal=value.get("ordinal"),
            request_dispatched_at=value.get("request_dispatched_at"),
            transport_returned_at=value.get("transport_returned_at"),
            outcome=value.get("outcome"),
            reason=value.get("reason"),
            detail=value.get("detail"),
            target=physical_stage_target_from_payload(value.get("target")),
        )


@dataclass(frozen=True, slots=True)
class PhysicalCommandReceipt:
    """Exact Modem Live executor result for one admitted Core command."""

    SCHEMA_VERSION: ClassVar[str] = "PhysicalCommandReceipt.v3"
    OWNER: ClassVar[str] = "modem_live"

    request_id: str
    transaction_root_id: str
    target: CoreCommandTarget
    execution_started_at: float
    transaction_completed_at: float
    accepted: bool
    reason: str
    stages: tuple[PhysicalCommandDispatchEvidence, ...]
    failure: PhysicalCommandFailureDisposition | None = None

    def __post_init__(self) -> None:
        _exact_string(self.request_id, "physical_receipt_request_id_required")
        _exact_string(
            self.transaction_root_id,
            "physical_receipt_transaction_root_required",
        )
        reason = _exact_string(self.reason, "physical_receipt_reason_required")
        started_at = _exact_positive_number(
            self.execution_started_at,
            "physical_receipt_started_at_invalid",
        )
        completed_at = _exact_positive_number(
            self.transaction_completed_at,
            "physical_receipt_completed_at_invalid",
        )
        if not isinstance(self.target, CORE_COMMAND_TARGET_TYPES):
            raise TypeError("physical_receipt_typed_target_required")
        if completed_at < started_at:
            raise ValueError("physical_receipt_completed_at_invalid")
        if type(self.accepted) is not bool:
            raise TypeError("physical_receipt_accepted_bool_required")
        if self.accepted and self.failure is not None:
            raise ValueError("physical_receipt_accepted_failure_invalid")
        if not isinstance(self.stages, tuple) or any(
            not isinstance(stage, PhysicalCommandDispatchEvidence)
            for stage in self.stages
        ):
            raise TypeError("physical_receipt_typed_stages_required")
        ordinals = tuple(stage.ordinal for stage in self.stages)
        if self.failure is None:
            if ordinals != tuple(range(1, len(self.stages) + 1)):
                raise ValueError("physical_receipt_stage_order_invalid")
        else:
            if not isinstance(
                self.failure,
                PhysicalCommandFailureDisposition,
            ):
                raise TypeError("physical_receipt_failure_type_invalid")
            before = tuple(
                ordinal
                for ordinal in ordinals
                if ordinal < self.failure.blocked_ordinal
            )
            if before != tuple(
                range(1, self.failure.blocked_ordinal)
            ):
                raise ValueError("physical_receipt_failure_prefix_invalid")
            after = tuple(
                ordinal
                for ordinal in ordinals
                if ordinal > self.failure.blocked_ordinal
            )
            if len(after) > 1:
                raise ValueError("physical_receipt_failure_suffix_invalid")
            if self.failure.blocked_ordinal in ordinals:
                raise ValueError("physical_receipt_blocked_stage_dispatched")
        previous_return = started_at
        for stage in self.stages:
            if stage.request_dispatched_at < previous_return:
                raise ValueError("physical_receipt_stage_time_order_invalid")
            if stage.transport_returned_at > completed_at:
                raise ValueError("physical_receipt_stage_after_completion")
            previous_return = stage.transport_returned_at
            for proof_key in ("native_auto_readback", "native_data_session_readback"):
                if proof_key in stage.detail:
                    stage.__post_init__()
                    previous_return = max(previous_return, stage.detail[proof_key]["finished_at"])
                    if previous_return > completed_at:
                        raise ValueError("physical_receipt_readback_after_completion")
        if self.accepted and not self.stages and reason != (
            "physical_command_not_required_current_target"
        ):
            raise ValueError("physical_receipt_accepted_stage_required")
        if self.accepted and any(
            not stage.permits_target_verification for stage in self.stages
        ):
            raise ValueError("physical_receipt_accepted_stage_outcome_invalid")
        rejected_stage = any(
            stage.outcome == "rejected" for stage in self.stages
        )
        if not self.accepted:
            if (
                rejected_stage
                and self.failure is not None
                and self.failure.phase != "pre_dispatch"
            ):
                raise ValueError("physical_receipt_duplicate_failure_invalid")
            if self.stages and not rejected_stage and self.failure is None:
                raise ValueError(
                    "physical_receipt_rejected_stage_outcome_invalid"
                )
        if self.failure is not None:
            self._validate_failure_disposition()
        self._validate_stage_sequence()

    def _validate_failure_disposition(self) -> None:
        failure = self.failure
        if failure is None:
            return
        if failure.phase == "pre_dispatch":
            suffix = tuple(
                stage
                for stage in self.stages
                if stage.ordinal > failure.blocked_ordinal
            )
            if suffix and (
                len(suffix) != 1
                or suffix[0].stage_kind != "device_local_recovery_resume"
                or not suffix[0].permits_target_verification
            ):
                raise ValueError("physical_receipt_pre_dispatch_suffix_invalid")
            if (
                failure.blocked_stage_kind
                != "device_local_recovery_resume"
                and any(stage.outcome == "rejected" for stage in self.stages)
            ):
                raise ValueError("physical_receipt_pre_dispatch_prefix_invalid")
            return
        if (
            not isinstance(self.target, CorePBandTarget)
            or not self.target.ca_pool
        ):
            raise ValueError("physical_receipt_interstage_target_invalid")
        if (
            failure.phase != "interstage_confirmation"
            or failure.blocked_stage_kind != "ca_pool"
        ):
            raise ValueError("physical_receipt_interstage_failure_invalid")
        if not self.stages:
            raise ValueError("physical_receipt_interstage_prefix_required")
        preceding = tuple(
            stage
            for stage in self.stages
            if stage.ordinal <= failure.after_stage_ordinal
        )
        if not preceding or preceding[-1].stage_kind != "pband_anchor":
            raise ValueError("physical_receipt_interstage_anchor_required")
        if any(
            not stage.permits_target_verification
            for stage in self.stages
        ):
            raise ValueError("physical_receipt_interstage_outcome_invalid")
        suffix = tuple(
            stage
            for stage in self.stages
            if stage.ordinal > failure.blocked_ordinal
        )
        if suffix and (
            len(suffix) != 1
            or suffix[0].stage_kind != "device_local_recovery_resume"
        ):
            raise ValueError("physical_receipt_interstage_suffix_invalid")

    def _validate_stage_sequence(self) -> None:
        if not self.stages:
            return
        stage_kinds = tuple(stage.stage_kind for stage in self.stages)
        recovery_coordination = (
            "device_local_recovery_pause" in stage_kinds
            or "device_local_recovery_resume" in stage_kinds
        )
        if recovery_coordination:
            if stage_kinds.count("device_local_recovery_pause") != 1:
                raise ValueError("physical_receipt_recovery_pause_count_invalid")
            if stage_kinds.count("device_local_recovery_resume") > 1:
                raise ValueError("physical_receipt_recovery_resume_count_invalid")
            if stage_kinds[0] != "device_local_recovery_pause":
                raise ValueError("physical_receipt_recovery_pause_order_invalid")
            if "device_local_recovery_resume" in stage_kinds:
                if stage_kinds[-1] != "device_local_recovery_resume":
                    raise ValueError(
                        "physical_receipt_recovery_resume_order_invalid"
                    )
            elif self.accepted:
                raise ValueError("physical_receipt_recovery_resume_required")
            if self.stages[0].outcome == "rejected" and len(self.stages) != 1:
                raise ValueError(
                    "physical_receipt_write_after_failed_recovery_pause"
                )
            for stage in self.stages:
                if isinstance(
                    stage.target,
                    PhysicalCommandCoordinationTarget,
                ) and stage.stage_kind not in {
                    "device_local_recovery_pause",
                    "device_local_recovery_resume",
                }:
                    raise ValueError(
                        "physical_receipt_recovery_coordination_invalid"
                    )
        rejected_indexes = tuple(
            index
            for index, stage in enumerate(self.stages)
            if stage.outcome == "rejected"
        )
        if rejected_indexes:
            first_rejected = rejected_indexes[0]
            suffix = self.stages[first_rejected + 1 :]
            if suffix and not (
                recovery_coordination
                and len(suffix) == 1
                and suffix[0].stage_kind == "device_local_recovery_resume"
            ):
                raise ValueError("physical_receipt_write_after_rejected_stage")
        requested_stages = tuple(
            stage
            for stage in self.stages
            if not isinstance(
                stage.target,
                PhysicalCommandCoordinationTarget,
            )
        )
        coordination_stages = tuple(
            stage
            for stage in self.stages
            if isinstance(
                stage.target,
                PhysicalCommandCoordinationTarget,
            )
        )
        if (isinstance(self.target, CoreNrBandTarget) and requested_stages
                and isinstance(requested_stages[0].target, CoreRadioModeTarget)):
            first = requested_stages[0]
            if (coordination_stages or self.target.release_manual or not self.target.nr_bands
                    or first.stage_kind != "radio_mode" or first.target.mode != "nsa_preferred"
                    or first.target.expected_mode != "lte_only" or len(requested_stages) > 2
                    or (self.accepted and len(requested_stages) != 2)):
                raise ValueError("physical_receipt_nr_mode_prefix_invalid")
            if len(requested_stages) == 2:
                last = requested_stages[1]
                if (not first.confirmed_by_transport or last.stage_kind != "nr_band_lock"
                        or last.target != self.target):
                    raise ValueError("physical_receipt_nr_mode_band_stage_invalid")
            return
        if isinstance(self.target, (CoreBandSetTarget, CoreNrBandTarget)) and self.target.release_manual:
            expected = ('band_auto', self.target.command_kind.lower())
            actual = tuple(stage.stage_kind for stage in requested_stages)
            if actual != expected[:len(actual)] or (self.accepted and actual != expected):
                raise ValueError('physical_receipt_auto_stage_order_invalid')
            domain = 'nr_approved_auto' if isinstance(self.target, CoreNrBandTarget) else 'lte_allocated_auto'
            if requested_stages and requested_stages[0].target != CoreBandAutoTarget(domain):
                raise ValueError('physical_receipt_auto_release_target_invalid')
            if len(requested_stages) == 2:
                if not requested_stages[0].native_auto_release_confirmed or requested_stages[1].target != self.target:
                    raise ValueError('physical_receipt_auto_pool_target_invalid')
            return
        if isinstance(self.target, CoreRadioModeTarget) and stage_kinds and stage_kinds[0] == "radio_disconnect":
            expected = ("radio_disconnect", "radio_mode", "radio_reconnect")
            native_connected = (stage_kinds == expected[:2]
                and self.stages[-1].native_data_session_confirmed)
            if native_connected:
                binding = self.stages[0].detail.get("radio_mode_transaction")
                if (not isinstance(binding, Mapping)
                        or set(binding) != {"request_id", "transaction_root_id", "write_token_hash"}
                        or binding["request_id"] != self.request_id
                        or binding["transaction_root_id"] != self.transaction_root_id
                        or type(binding["write_token_hash"]) is not str
                        or len(binding["write_token_hash"]) != 64
                        or any(c not in "0123456789abcdef" for c in binding["write_token_hash"])
                        or self.stages[1].detail.get("radio_mode_transaction") != binding):
                    raise ValueError("physical_receipt_radio_mode_transaction_invalid")
            if (stage_kinds != expected[:len(stage_kinds)]
                    or (self.accepted and stage_kinds != expected and not native_connected)
                    or any(stage.target != self.target for stage in self.stages)
                    or any(not stage.confirmed_by_transport for stage in self.stages[:-1])
                    or (stage_kinds == expected and any(stage.native_data_session_confirmed for stage in self.stages))):
                raise ValueError("physical_receipt_radio_mode_stage_order_invalid")
            return
        if isinstance(self.target, CoreNetworkSettingsTarget):
            expected = network_stage_kinds(self.target, coordination_profile=(
                "tplink_dev2" if self.target.apn and self.target.apn.creates_profile
                else "zte_zwrt" if stage_kinds and stage_kinds[0] == "network_data_hold" else "direct"))
            if stage_kinds != expected[:len(stage_kinds)]:
                raise ValueError("physical_receipt_network_stage_order_invalid")
            if self.accepted and stage_kinds != expected:
                raise ValueError("physical_receipt_network_stage_count_invalid")
            if any(stage.target != self.target for stage in self.stages):
                raise ValueError("physical_receipt_network_target_mismatch")
            return
        if isinstance(self.target, CoreWifiRadioTarget):
            # Direct WebUI/AT transports perform one real radio write.  Only
            # OpenWRT/ZWRT transports have separate config/commit/reload
            # operations.  This receipt records transport work; resulting
            # Wi-Fi state is confirmed exclusively from Modem Live's Matrix.
            if stage_kinds == ("wifi_radio",):
                if len(requested_stages) != 1:
                    raise ValueError("physical_receipt_wifi_target_invalid")
                if requested_stages[0].target != self.target:
                    raise ValueError("physical_receipt_wifi_target_invalid")
                return
            expected = ("wifi_config", "wifi_commit", "wifi_reload")
            if stage_kinds != expected[: len(stage_kinds)]:
                raise ValueError("physical_receipt_wifi_stage_order_invalid")
            if self.accepted and stage_kinds != expected:
                raise ValueError("physical_receipt_wifi_stage_count_invalid")
            if not requested_stages or requested_stages[0].target != self.target:
                raise ValueError("physical_receipt_wifi_target_invalid")
            return
        if coordination_stages and not recovery_coordination:
            raise ValueError("physical_receipt_coordination_not_allowed")
        if recovery_coordination and not requested_stages:
            return
        if isinstance(self.target, CorePBandTarget):
            if not requested_stages:
                return
            first = requested_stages[0]
            expected_anchor = CorePBandTarget(pband=self.target.pband)
            if (
                first.stage_kind != "pband_anchor"
                or first.target != expected_anchor
            ):
                raise ValueError("physical_receipt_pband_anchor_stage_invalid")
            expected_count = (
                2
                if self.target.has_ca_companions
                else 1
            )
            if self.accepted and len(requested_stages) != expected_count:
                raise ValueError("physical_receipt_pband_stage_count_invalid")
            if len(requested_stages) > expected_count:
                raise ValueError("physical_receipt_pband_stage_count_invalid")
            if (
                self.target.has_ca_companions
                and len(requested_stages) == 2
            ):
                second = requested_stages[1]
                expected_pool = CoreBandSetTarget(
                    bands=self.target.ca_pool,
                    command_kind="BAND_SELECTION",
                )
                if (
                    second.stage_kind != "ca_pool"
                    or second.target != expected_pool
                ):
                    raise ValueError("physical_receipt_ca_pool_stage_invalid")
            return
        if isinstance(self.target, CoreUeCapabilityTarget):
            expected = tuple(
                kind
                for kind, value in (
                    ("ue_dl_256qam", self.target.dl_256qam),
                    ("ue_ul_64qam", self.target.ul_64qam),
                    ("ue_restart", self.target.restart),
                )
                if value is not None
            )
            actual = tuple(stage.stage_kind for stage in requested_stages)
            if actual != expected[: len(actual)]:
                raise ValueError("physical_receipt_ue_stage_order_invalid")
            if self.accepted and actual != expected:
                raise ValueError("physical_receipt_ue_stage_count_invalid")
            if any(stage.target != self.target for stage in requested_stages):
                raise ValueError("physical_receipt_ue_target_invalid")
            return
        if len(requested_stages) != 1:
            raise ValueError("physical_receipt_single_stage_required")
        stage = requested_stages[0]
        if (
            stage.stage_kind != _physical_stage_kind_for_target(self.target)
            or stage.target != self.target
        ):
            raise ValueError("physical_receipt_stage_target_mismatch")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "owner": self.OWNER,
            "request_id": self.request_id,
            "transaction_root_id": self.transaction_root_id,
            "target": core_command_target_payload(self.target),
            "execution_started_at": self.execution_started_at,
            "transaction_completed_at": self.transaction_completed_at,
            "accepted": self.accepted,
            "reason": self.reason,
            "stages": [stage.as_dict() for stage in self.stages],
            "failure": (
                self.failure.as_dict()
                if self.failure is not None
                else None
            ),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PhysicalCommandReceipt":
        if not isinstance(value, Mapping):
            raise TypeError("physical_receipt_mapping_required")
        expected = {
            "schema_version",
            "owner",
            "request_id",
            "transaction_root_id",
            "target",
            "execution_started_at",
            "transaction_completed_at",
            "accepted",
            "reason",
            "stages",
            "failure",
        }
        if set(value) != expected:
            raise ValueError("physical_receipt_fields_invalid")
        if value.get("schema_version") != cls.SCHEMA_VERSION:
            raise ValueError("physical_receipt_schema_invalid")
        if value.get("owner") != cls.OWNER:
            raise ValueError("physical_receipt_owner_invalid")
        stages = value.get("stages")
        if not isinstance(stages, (list, tuple)):
            raise TypeError("physical_receipt_stages_sequence_required")
        failure = value.get("failure")
        if failure is not None and not isinstance(failure, Mapping):
            raise TypeError("physical_receipt_failure_mapping_required")
        return cls(
            request_id=value.get("request_id"),
            transaction_root_id=value.get("transaction_root_id"),
            target=core_command_target_from_payload(value.get("target")),
            execution_started_at=value.get("execution_started_at"),
            transaction_completed_at=value.get("transaction_completed_at"),
            accepted=value.get("accepted"),
            reason=value.get("reason"),
            stages=tuple(
                PhysicalCommandDispatchEvidence.from_mapping(stage)
                for stage in stages
            ),
            failure=(
                PhysicalCommandFailureDisposition.from_mapping(failure)
                if failure is not None
                else None
            ),
        )


__all__ = (
    "PHYSICAL_COORDINATION_OPERATIONS",
    "PhysicalCommandCoordinationTarget",
    "PhysicalCommandDispatchEvidence",
    "PhysicalCommandFailureDisposition",
    "PhysicalCommandReceipt",
    "PhysicalCommandStageTarget",
    "physical_stage_target_from_payload",
    "physical_stage_target_payload",
)
