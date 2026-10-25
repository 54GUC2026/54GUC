"""Pure accepted-generation matcher for declared cellular command targets.

Command confirmation and transition attribution share this boundary adapter.
It owns no lifecycle, polling, modem protocol, or selection state.
"""

from __future__ import annotations

from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.network_settings import validate_network_observation

from dataclasses import dataclass
import math
from typing import Any, Mapping

from arch4.core_contracts.accepted_current_facts import (
    CoreAcceptedCurrentFacts,
)
from arch4.core_contracts.command_targets import (
    CORE_COMMAND_TARGET_TYPES,
    CoreBandSetTarget,
    CoreMobileDataTarget,
    CoreCellLockTarget,
    CoreCommandTarget,
    CoreNrBandTarget,
    CorePBandTarget,
    CoreRestartTarget,
    CoreWifiRadioTarget,
    CoreNetworkSettingsTarget,
    CoreRadioModeTarget,
    validate_radio_mode_observation,
    core_command_target_from_payload,
    core_command_target_payload,
    core_lte_pci,
)
from arch4.interfaces import LeverType
from arch4.runtime_capability import capability_contract_action_meta
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)


@dataclass(frozen=True, slots=True)
class RadioTargetMatch:
    matched: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RadioTargetFacts:
    source_sequence: int
    connected: bool | None
    serving_band_available: bool
    serving_band_unavailable_reason: str
    primary_band: int
    selected_bands_available: bool
    selected_bands_unavailable_reason: str
    selected_bands: frozenset[int]
    cell_locked: bool | None
    pci: int | None
    earfcn: int
    nr_bands: frozenset[int]
    wifi_24ghz_enabled: bool | None = None
    wifi_5ghz_enabled: bool | None = None
    wifi_6ghz_enabled: bool | None = None
    mobile_data_enabled: bool | None = None
    mobile_data_acquired_at: float = 0.0
    radio_mode: CoreFrozenPayload | None = None
    radio_mode_acquired_at: float = 0.0
    network_settings: CoreFrozenPayload | None = None
    network_settings_acquired_at: float = 0.0
    serving_plmn: str = ""
    serving_plmn_acquired_at: float = 0.0
    restart_recovery_observed: bool = False
    restart_recovery_root_id: str = ""
    restart_recovery_observed_at: float = 0.0
    pband_best_effort: bool = False

    def __post_init__(self) -> None:
        if self.mobile_data_enabled is not None and type(self.mobile_data_enabled) is not bool:
            raise TypeError("mobile_data_observation_bool_required")
        if self.network_settings is not None:
            if not isinstance(self.network_settings, CoreFrozenPayload):
                raise TypeError("network_target_typed_observation_required")
            validate_network_observation(self.network_settings.to_mapping())
        if type(self.serving_plmn) is not str:
            raise TypeError("network_target_serving_plmn_invalid")
        if self.radio_mode is not None:
            if not isinstance(self.radio_mode, CoreFrozenPayload):
                raise TypeError("radio_mode_typed_readback_required")
            validate_radio_mode_observation(self.radio_mode.to_mapping())
        for value in (self.network_settings_acquired_at, self.serving_plmn_acquired_at, self.mobile_data_acquired_at, self.radio_mode_acquired_at):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("network_target_acquisition_time_invalid")
        if type(self.source_sequence) is not int or self.source_sequence <= 0:
            raise TypeError("radio_target_positive_source_sequence_required")
        if self.connected is not None and type(self.connected) is not bool:
            raise TypeError("radio_target_connected_bool_required")
        for field_name in (
            "serving_band_available",
            "selected_bands_available",
            "restart_recovery_observed",
            "pband_best_effort",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"radio_target_{field_name}_bool_required")
        if not isinstance(self.serving_band_unavailable_reason, str):
            raise TypeError("radio_target_unavailable_reason_string_required")
        if self.cell_locked is not None and type(self.cell_locked) is not bool:
            raise TypeError("radio_target_cell_locked_bool_required")
        if not isinstance(self.selected_bands_unavailable_reason, str):
            raise TypeError(
                "radio_target_selected_bands_unavailable_reason_string_required"
            )
        if self.serving_band_available:
            if self.serving_band_unavailable_reason:
                raise ValueError(
                    "radio_target_available_band_reason_forbidden"
                )
            if self.primary_band <= 0:
                raise TypeError("radio_target_serving_band_integer_required")
        else:
            if not self.serving_band_unavailable_reason:
                raise ValueError(
                    "radio_target_unavailable_band_reason_required"
                )
            if self.primary_band != 0:
                raise ValueError(
                    "radio_target_unavailable_band_value_forbidden"
                )
        if self.selected_bands_available:
            if self.selected_bands_unavailable_reason:
                raise ValueError(
                    "radio_target_selected_bands_available_reason_forbidden"
                )
        elif not self.selected_bands_unavailable_reason:
            raise ValueError(
                "radio_target_selected_bands_unavailable_reason_required"
            )
        if self.pci is not None:
            core_lte_pci(self.pci)
        for field_name in ("primary_band", "earfcn"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise TypeError(f"radio_target_{field_name}_integer_required")
        for field_name in ("selected_bands", "nr_bands"):
            value = getattr(self, field_name)
            if not isinstance(value, frozenset):
                raise TypeError(f"radio_target_{field_name}_frozenset_required")
        for field_name in (
            "wifi_24ghz_enabled",
            "wifi_5ghz_enabled",
            "wifi_6ghz_enabled",
        ):
            value = getattr(self, field_name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"radio_target_{field_name}_bool_required")
        if not isinstance(self.restart_recovery_root_id, str):
            raise TypeError("radio_target_restart_root_string_required")
        if (
            isinstance(self.restart_recovery_observed_at, bool)
            or not isinstance(self.restart_recovery_observed_at, (int, float))
            or not math.isfinite(float(self.restart_recovery_observed_at))
            or float(self.restart_recovery_observed_at) < 0.0
        ):
            raise TypeError("radio_target_restart_timestamp_invalid")

    def as_mapping(self) -> dict[str, Any]:
        return {
            "source_sequence": self.source_sequence,
            "connected": self.connected,
            "serving_band_available": self.serving_band_available,
            "serving_band_unavailable_reason": (
                self.serving_band_unavailable_reason
            ),
            "primary_band": self.primary_band,
            "pband_best_effort": self.pband_best_effort,
            "selected_bands_available": self.selected_bands_available,
            "selected_bands_unavailable_reason": (
                self.selected_bands_unavailable_reason
            ),
            "selected_bands": sorted(self.selected_bands),
            "cell_locked": self.cell_locked,
            "pci": self.pci,
            "earfcn": self.earfcn,
            "nr_bands": sorted(self.nr_bands),
            "wifi_24ghz_enabled": self.wifi_24ghz_enabled,
            "wifi_5ghz_enabled": self.wifi_5ghz_enabled,
            "wifi_6ghz_enabled": self.wifi_6ghz_enabled,
            "mobile_data_enabled": self.mobile_data_enabled,
            "mobile_data_acquired_at": self.mobile_data_acquired_at,
            "radio_mode": self.radio_mode.to_mapping() if self.radio_mode else None,
            "radio_mode_acquired_at": self.radio_mode_acquired_at,
            "network_settings": self.network_settings.to_mapping() if self.network_settings else None,
            "network_settings_acquired_at": self.network_settings_acquired_at,
            "serving_plmn": self.serving_plmn,
            "serving_plmn_acquired_at": self.serving_plmn_acquired_at,
            "restart_recovery_observed": self.restart_recovery_observed,
            "restart_recovery_root_id": self.restart_recovery_root_id,
            "restart_recovery_observed_at": float(
                self.restart_recovery_observed_at
            ),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RadioTargetFacts":
        if not isinstance(value, Mapping):
            raise TypeError("radio_target_facts_mapping_required")
        expected = {
            "source_sequence",
            "connected",
            "serving_band_available",
            "serving_band_unavailable_reason",
            "primary_band",
            "selected_bands_available",
            "selected_bands_unavailable_reason",
            "selected_bands",
            "cell_locked",
            "pci",
            "earfcn",
            "nr_bands",
            "wifi_24ghz_enabled",
            "wifi_5ghz_enabled",
            "wifi_6ghz_enabled",
            "mobile_data_enabled", "mobile_data_acquired_at",
            "network_settings", "network_settings_acquired_at",
            "serving_plmn", "serving_plmn_acquired_at",
            "restart_recovery_observed",
            "restart_recovery_root_id",
            "restart_recovery_observed_at",
        }
        # Older durable receipts predate the capability-qualified settlement.
        mode_fields = {"radio_mode", "radio_mode_acquired_at"}
        if set(value) not in (expected, expected | {"pband_best_effort"},
                              expected | mode_fields, expected | mode_fields | {"pband_best_effort"}):
            raise ValueError("radio_target_facts_fields_invalid")
        selected = value.get("selected_bands")
        nr_bands = value.get("nr_bands")
        if not isinstance(selected, list) or not isinstance(nr_bands, list):
            raise TypeError("radio_target_facts_band_lists_required")
        if any(type(item) is not int or item <= 0 for item in selected):
            raise TypeError("radio_target_selected_bands_invalid")
        if any(type(item) is not int or item <= 0 for item in nr_bands):
            raise TypeError("radio_target_nr_bands_invalid")
        if selected != sorted(set(selected)) or nr_bands != sorted(set(nr_bands)):
            raise ValueError("radio_target_facts_band_lists_not_canonical")
        return cls(
            source_sequence=value.get("source_sequence"),
            connected=value.get("connected"),
            serving_band_available=value.get("serving_band_available"),
            serving_band_unavailable_reason=value.get(
                "serving_band_unavailable_reason"
            ),
            primary_band=value.get("primary_band"),
            pband_best_effort=value.get("pband_best_effort", False),
            selected_bands_available=value.get(
                "selected_bands_available"
            ),
            selected_bands_unavailable_reason=value.get(
                "selected_bands_unavailable_reason"
            ),
            selected_bands=frozenset(selected),
            cell_locked=value.get("cell_locked"),
            pci=value.get("pci"),
            earfcn=value.get("earfcn"),
            nr_bands=frozenset(nr_bands),
            mobile_data_enabled=value["mobile_data_enabled"],
            mobile_data_acquired_at=value["mobile_data_acquired_at"],
            network_settings=CoreFrozenPayload.from_mapping(validate_network_observation(value["network_settings"])) if value["network_settings"] is not None else None,
            network_settings_acquired_at=value["network_settings_acquired_at"],
            radio_mode=(CoreFrozenPayload.from_mapping(value["radio_mode"]) if value.get("radio_mode") is not None else None),
            radio_mode_acquired_at=value.get("radio_mode_acquired_at", 0.0),
            serving_plmn=value["serving_plmn"],
            serving_plmn_acquired_at=value["serving_plmn_acquired_at"],
            wifi_24ghz_enabled=value.get("wifi_24ghz_enabled"),
            wifi_5ghz_enabled=value.get("wifi_5ghz_enabled"),
            wifi_6ghz_enabled=value.get("wifi_6ghz_enabled"),
            restart_recovery_observed=value.get(
                "restart_recovery_observed"
            ),
            restart_recovery_root_id=value.get(
                "restart_recovery_root_id"
            ),
            restart_recovery_observed_at=value.get(
                "restart_recovery_observed_at"
            ),
        )


@dataclass(frozen=True, slots=True)
class TargetConfirmationReceipt:
    """Exact committed-Matrix proof for one declared Core command target."""

    request_id: str
    transaction_root_id: str
    matrix_identity: MatrixGenerationIdentity
    target: CoreCommandTarget
    observed: RadioTargetFacts
    committed_at: float
    reason: str
    schema: str = "TargetConfirmationReceipt.v2"
    owner: str = "54guc.core.command_lifecycle_service"

    def __post_init__(self) -> None:
        for name in (
            "request_id",
            "transaction_root_id",
            "reason",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise TypeError(f"target_confirmation_{name}_required")
        if not isinstance(self.target, CORE_COMMAND_TARGET_TYPES):
            raise TypeError("target_confirmation_typed_target_required")
        if not isinstance(self.observed, RadioTargetFacts):
            raise TypeError("target_confirmation_typed_observed_facts_required")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("target_confirmation_matrix_identity_required")
        if (
            self.matrix_identity.matrix_sequence
            != self.observed.source_sequence
        ):
            raise TypeError("target_confirmation_generation_invalid")
        if (
            isinstance(self.committed_at, bool)
            or not isinstance(self.committed_at, (int, float))
            or not math.isfinite(float(self.committed_at))
            or float(self.committed_at) <= 0.0
        ):
            raise TypeError("target_confirmation_timestamp_invalid")
        if self.schema != "TargetConfirmationReceipt.v2":
            raise ValueError("target_confirmation_schema_invalid")
        if self.owner != "54guc.core.command_lifecycle_service":
            raise ValueError("target_confirmation_owner_invalid")

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "request_id": self.request_id,
            "transaction_root_id": self.transaction_root_id,
            "matrix_identity": self.matrix_identity.as_dict(),
            "target": core_command_target_payload(self.target),
            "observed": self.observed.as_mapping(),
            "committed_at": float(self.committed_at),
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "TargetConfirmationReceipt":
        if not isinstance(value, Mapping):
            raise TypeError("target_confirmation_mapping_required")
        if set(value) != {
            "schema",
            "owner",
            "request_id",
            "transaction_root_id",
            "matrix_identity",
            "target",
            "observed",
            "committed_at",
            "reason",
        }:
            raise ValueError("target_confirmation_fields_invalid")
        observed = value.get("observed")
        if not isinstance(observed, Mapping):
            raise TypeError("target_confirmation_observed_mapping_required")
        return cls(
            request_id=value.get("request_id"),
            transaction_root_id=value.get("transaction_root_id"),
            matrix_identity=MatrixGenerationIdentity.from_mapping(
                value.get("matrix_identity")
            ),
            target=core_command_target_from_payload(value.get("target")),
            observed=RadioTargetFacts.from_mapping(observed),
            committed_at=value.get("committed_at"),
            reason=value.get("reason"),
            schema=value.get("schema"),
            owner=value.get("owner"),
        )


def _optional_matrix_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise TypeError("radio_target_matrix_bool_required")
    return value


def bands_from_value(value: Any) -> frozenset[int]:
    del value
    raise RuntimeError(
        "post_matrix_band_text_normalization_forbidden:"
        "consume_canonical_accepted_current_facts"
    )


def radio_target_facts_from_generation(
    generation: Any,
) -> RadioTargetFacts:
    del generation
    raise RuntimeError(
        "controller_generation_target_projection_illegal:"
        "use_committed_core_observation"
    )


def accepted_serving_band_at_matrix_boundary(
    current_facts: CoreAcceptedCurrentFacts,
) -> tuple[bool, int, str]:
    """One serving-band interpretation for command and transition consumers."""
    availability = current_facts.exact("radio.serving_band_available")
    band = current_facts.exact("radio.serving_band")
    reason = current_facts.exact("radio.serving_band_unavailable_reason")
    available = availability.value.to_mapping()["value"] if availability.disposition == "available" else False
    if type(available) is not bool:
        raise TypeError("radio_target_serving_available_bool_required")
    band_value = band.value.to_mapping()["value"]
    reason_value = reason.value.to_mapping()["value"]
    if available:
        if type(band_value) is not int or band_value <= 0:
            raise TypeError("radio_target_serving_band_integer_required")
        if reason_value not in (None, ""):
            raise ValueError("radio_target_available_band_reason_forbidden")
        return True, band_value, ""
    if band_value is not None:
        raise ValueError("radio_target_unavailable_band_value_forbidden")
    if reason.disposition != "available":
        reason_value = availability.reason or reason.reason
    if type(reason_value) is not str or not reason_value or reason_value != reason_value.strip():
        raise TypeError("radio_target_unavailable_band_reason_required")
    return False, 0, reason_value


def build_radio_target_facts_at_accepted_matrix_boundary(
    current_facts: CoreAcceptedCurrentFacts,
) -> RadioTargetFacts:
    """Interpret the one complete accepted-current-facts projection."""

    if not isinstance(current_facts, CoreAcceptedCurrentFacts):
        raise TypeError("radio_target_requires_accepted_current_facts")

    def value(path: str) -> Any:
        fact = current_facts.exact(path)
        payload = fact.value.to_mapping()
        return payload["value"] if payload["present"] is True else None

    # Older typed durable projections predate this optional command. They may
    # still verify their original targets; they can never confirm a RAT change.
    mode_fact = (current_facts.exact("slow.radio_mode")
                 if "slow.radio_mode" in current_facts.declared_paths else None)
    mode_value = (mode_fact.value.get("value")
                  if mode_fact is not None and mode_fact.disposition == "available" and mode_fact.retention is None else None)

    connected_raw = value("health.radio_connected")
    disabled_raw = value("health.lte_interface_disabled")
    if (current_facts.exact("health.radio_connected").disposition == "available"
            and type(connected_raw) is not bool):
        raise TypeError("radio_target_connected_bool_required")
    if disabled_raw is not None and type(disabled_raw) is not bool:
        raise TypeError("radio_target_interface_disabled_bool_required")
    serving_available, serving_band, serving_reason = accepted_serving_band_at_matrix_boundary(current_facts)
    selected_fact = current_facts.exact("radio.selected_band_set")
    selected_raw = value("radio.selected_band_set")
    selected_available = selected_fact.disposition == "available"
    selected_reason = "" if selected_available else selected_fact.reason
    if selected_available:
        if type(selected_raw) is not list or any(
            type(item) is not int or item <= 0 for item in selected_raw
        ):
            raise TypeError("radio_target_selected_band_list_required")
        if selected_raw != sorted(set(selected_raw)):
            raise ValueError("radio_target_selected_band_list_not_canonical")
    else:
        selected_raw = []
    connectivity_receipt_raw = value("health.connectivity_receipt")
    connectivity_receipt = (
        connectivity_receipt_raw
        if isinstance(connectivity_receipt_raw, Mapping)
        else {}
    )
    connectivity_observation = (
        connectivity_receipt.get("observation")
        if isinstance(connectivity_receipt.get("observation"), Mapping)
        else {}
    )
    expected_interruption = (
        connectivity_observation.get("expected_interruption")
        if isinstance(
            connectivity_observation.get("expected_interruption"),
            Mapping,
        )
        else {}
    )
    observed_scope = connectivity_observation.get("session_scope_key")
    same_session = bool(
        type(observed_scope) is str
        and observed_scope
        == current_facts.matrix_identity.session_scope_key
    )
    restart_recovery_observed = bool(
        same_session
        and connectivity_receipt.get("completed_expected_interruption") is True
        and connectivity_receipt.get("expected_interruption_miss_seen") is True
        and connectivity_observation.get("outcome") == "success"
        and expected_interruption.get("active") is True
        and expected_interruption.get("lever") == LeverType.RESTART.name
        and type(expected_interruption.get("operation_id")) is str
        and expected_interruption["operation_id"]
    )
    pci = value("rf.pci")
    earfcn = value("rf.earfcn_dl")
    cell_locked = value("radio.cell_locked")
    if current_facts.exact("radio.cell_locked").disposition != "available":
        cell_locked = None
    nr_band = value("radio.nr_band")
    for name, item in (
        ("pci", pci),
        ("earfcn", earfcn),
    ):
        if item is not None and (type(item) is not int or item < 0):
            raise TypeError(f"radio_target_{name}_integer_required")
    if cell_locked is not None and type(cell_locked) is not bool:
        raise TypeError("radio_target_cell_locked_bool_required")
    if nr_band is None:
        nr_bands = frozenset()
    elif type(nr_band) is int and nr_band == 0:
        nr_bands = frozenset()
    elif type(nr_band) is int and nr_band > 0:
        nr_bands = frozenset((nr_band,))
    elif type(nr_band) is list and all(
        type(item) is int and item > 0 for item in nr_band
    ):
        if nr_band != sorted(set(nr_band)):
            raise ValueError("radio_target_nr_band_list_not_canonical")
        nr_bands = frozenset(nr_band)
    else:
        raise TypeError("radio_target_nr_band_contract_invalid")
    recovery_at = connectivity_observation.get("observed_ts")
    if restart_recovery_observed and (
        type(recovery_at) is not float
        or not math.isfinite(recovery_at)
        or recovery_at <= 0.0
    ):
        raise TypeError("radio_target_restart_timestamp_invalid")
    capabilities = (
        value("capability.runtime_capabilities")
        if "capability.runtime_capabilities" in current_facts.declared_paths
        and current_facts.exact("capability.runtime_capabilities").disposition == "available"
        else None
    )
    pband_capability = capability_contract_action_meta(capabilities, "pband") or {}
    return RadioTargetFacts(
        # PBAND requests a preference, not a cell lock. Writable support does
        # not guarantee the serving band after the selected pool is restored.
        pband_best_effort=(pband_capability.get("state") in {"estimated", "supported"}
                           and pband_capability.get("write_allowed") is True),
        mobile_data_enabled=value("slow.mobile_data_enabled"),
        mobile_data_acquired_at=current_facts.exact("slow.mobile_data_enabled").acquired_at,
        radio_mode=(CoreFrozenPayload.from_mapping(validate_radio_mode_observation(mode_value)) if mode_value is not None else None),
        radio_mode_acquired_at=mode_fact.acquired_at if mode_fact is not None else 0.0,
        network_settings=(CoreFrozenPayload.from_mapping(validate_network_observation(value("slow.network_settings"))) if value("slow.network_settings") is not None else None),
        network_settings_acquired_at=current_facts.exact("slow.network_settings").acquired_at,
        serving_plmn=str(value("identity.provider_plmn") or ""),
        serving_plmn_acquired_at=current_facts.exact("identity.provider_plmn").acquired_at,
        source_sequence=current_facts.matrix_identity.matrix_sequence,
        connected=connected_raw and disabled_raw is not True,
        serving_band_available=serving_available,
        serving_band_unavailable_reason=serving_reason,
        primary_band=serving_band,
        selected_bands_available=selected_available,
        selected_bands_unavailable_reason=selected_reason,
        selected_bands=frozenset(selected_raw),
        cell_locked=cell_locked,
        pci=pci,
        earfcn=0 if earfcn is None else earfcn,
        nr_bands=nr_bands,
        wifi_24ghz_enabled=_optional_matrix_bool(
            value("slow.wifi_24ghz_enabled")
        ),
        wifi_5ghz_enabled=_optional_matrix_bool(
            value("slow.wifi_5ghz_enabled")
        ),
        wifi_6ghz_enabled=_optional_matrix_bool(
            value("slow.wifi_6ghz_enabled")
        ),
        restart_recovery_observed=restart_recovery_observed,
        restart_recovery_root_id=(
            expected_interruption["operation_id"]
            if restart_recovery_observed
            else ""
        ),
        restart_recovery_observed_at=(
            recovery_at
            if restart_recovery_observed
            else 0.0
        ),
    )


def match_radio_target(
    *,
    target: CoreCommandTarget,
    facts: RadioTargetFacts | None,
    before_source_generation: int = 0,
    expected_root_id: str = "",
    required_after_ts: float = 0.0,
) -> RadioTargetMatch:
    """Match one declared target against one accepted upper-layer generation."""

    if facts is None:
        return RadioTargetMatch(False, "accepted_generation_missing")
    if not isinstance(facts, RadioTargetFacts):
        raise TypeError("radio_target_typed_facts_required")
    if not isinstance(
        target,
        (
            CoreBandSetTarget,
            CoreMobileDataTarget,
            CorePBandTarget,
            CoreCellLockTarget,
            CoreNrBandTarget,
            CoreRestartTarget,
            CoreWifiRadioTarget,
            CoreNetworkSettingsTarget,
            CoreRadioModeTarget,
        ),
    ):
        return RadioTargetMatch(
            False,
            f"target_confirmation_unsupported:{target.command_kind.lower()}",
        )
    lever_name = target.command_kind
    connected = facts.connected

    if isinstance(target, CoreRadioModeTarget):
        if facts.radio_mode is None or facts.radio_mode_acquired_at <= required_after_ts:
            return RadioTargetMatch(False, "radio_mode_fresh_readback_pending")
        expected = target.configuration()
        if facts.radio_mode.to_mapping() != expected:
            return RadioTargetMatch(False, "radio_mode_or_preserved_restrictions_not_observed")
        if not connected:
            return RadioTargetMatch(False, "radio_mode_reconnection_pending")
        return RadioTargetMatch(True, "radio_mode_and_preserved_restrictions_observed")

    if isinstance(target, CoreMobileDataTarget):
        if facts.mobile_data_acquired_at <= required_after_ts:
            return RadioTargetMatch(False, "mobile_data_fresh_readback_pending")
        if facts.mobile_data_enabled is not target.enabled:
            return RadioTargetMatch(False, "mobile_data_setting_not_observed")
        return RadioTargetMatch(True, "mobile_data_setting_observed")

    if isinstance(target, CoreNetworkSettingsTarget):
        if facts.network_settings is None or facts.network_settings_acquired_at <= required_after_ts:
            return RadioTargetMatch(False, "network_configuration_fresh_readback_pending")
        observed = facts.network_settings.to_mapping()
        if observed["subscription_key"] != target.subscription_key:
            return RadioTargetMatch(False, "network_subscription_changed")
        if target.roaming_enabled is not None and observed["roaming_enabled"] is not target.roaming_enabled:
            return RadioTargetMatch(False, "network_roaming_readback_pending")
        if target.selection != "keep" and observed["selection"] != target.selection:
            return RadioTargetMatch(False, "network_selection_readback_pending")
        if target.selection == "manual":
            if observed["target_plmn"] != target.target_plmn:
                return RadioTargetMatch(False, "network_operator_configuration_pending")
            if not connected or facts.serving_plmn_acquired_at <= required_after_ts or facts.serving_plmn != target.target_plmn:
                return RadioTargetMatch(False, "network_requested_operator_registration_pending")
        if target.apn:
            apn = observed["apn"]
            if not target.apn.matches_observation(apn):
                return RadioTargetMatch(False, "network_apn_readback_pending")
        return RadioTargetMatch(True, "network_configuration_observed_password_not_readback_verified")

    if isinstance(target, CoreWifiRadioTarget):
        observed = {
            "2.4ghz": facts.wifi_24ghz_enabled,
            "5ghz": facts.wifi_5ghz_enabled,
            "6ghz": facts.wifi_6ghz_enabled,
        }[target.band]
        if observed is None:
            return RadioTargetMatch(False, "wifi_matrix_target_unavailable")
        if observed is not target.enabled:
            return RadioTargetMatch(False, "wifi_matrix_target_not_observed")
        return RadioTargetMatch(True, "wifi_matrix_target_observed")

    if lever_name == LeverType.RESTART.name:
        root_id = str(expected_root_id or "").strip()
        if not root_id:
            return RadioTargetMatch(False, "restart_expected_root_missing")
        if not connected:
            return RadioTargetMatch(False, "restart_reconnect_pending")
        if not facts.restart_recovery_observed:
            return RadioTargetMatch(False, "restart_recovery_receipt_pending")
        if facts.restart_recovery_root_id != root_id:
            return RadioTargetMatch(False, "restart_recovery_root_mismatch")
        return RadioTargetMatch(True, "restart_recovery_observed")
    if not connected:
        return RadioTargetMatch(False, "radio_not_connected")
    if lever_name in {
        LeverType.BAND_SELECTION.name,
        LeverType.BAND_LOCK.name,
        LeverType.PBAND.name,
        LeverType.CELL_LOCK.name,
    } and not facts.serving_band_available:
        return RadioTargetMatch(
            False,
            facts.serving_band_unavailable_reason
            or "accepted_serving_band_not_observed",
        )

    if lever_name in {LeverType.BAND_SELECTION.name, LeverType.BAND_LOCK.name}:
        if not isinstance(target, CoreBandSetTarget):
            raise TypeError("radio_target_band_set_target_required")
        requested_bands = frozenset(target.bands)
        if not facts.selected_bands_available:
            return RadioTargetMatch(
                False,
                "accepted_selected_band_set_unavailable:"
                + facts.selected_bands_unavailable_reason,
            )
        current = facts.selected_bands
        return RadioTargetMatch(
            current == requested_bands,
            f"band_set:{sorted(current)}",
        )

    if lever_name == LeverType.PBAND.name:
        if not isinstance(target, CorePBandTarget):
            raise TypeError("radio_target_pband_target_required")
        target_pband = target.pband
        current_pband = facts.primary_band
        alternative = current_pband != target_pband
        if target_pband <= 0 or (alternative and not facts.pband_best_effort):
            return RadioTargetMatch(False, f"pband:{current_pband}")
        target_pool = set(target.ca_pool)
        if target_pool:
            target_pool.add(target_pband)
            if not facts.selected_bands_available:
                return RadioTargetMatch(
                    False,
                    "accepted_selected_band_set_unavailable:"
                    + facts.selected_bands_unavailable_reason,
                )
            current_pool = facts.selected_bands
            if current_pool != frozenset(target_pool):
                return RadioTargetMatch(False, f"pband_pool:{sorted(current_pool)}")
        if alternative:
            if not target_pool or current_pband not in target_pool:
                return RadioTargetMatch(False, f"pband:{current_pband}")
            return RadioTargetMatch(True, f"pband_best_effort:requested=B{target_pband}:actual=B{current_pband}")
        return RadioTargetMatch(True, f"pband:B{current_pband}")

    if lever_name == LeverType.CELL_LOCK.name:
        if not isinstance(target, CoreCellLockTarget):
            raise TypeError("radio_target_cell_lock_target_required")
        if target.unlock:
            if facts.cell_locked is None:
                return RadioTargetMatch(False, "cell_lock_readback_unavailable")
            return RadioTargetMatch(
                facts.cell_locked is False,
                "cell_unlock",
            )
        target_pci = target.pci
        target_earfcn = target.earfcn
        current_pci = facts.pci
        current_earfcn = facts.earfcn
        if current_pci is None:
            return RadioTargetMatch(False, "cell_lock_pci_readback_unavailable")
        return RadioTargetMatch(
            bool(
                target_earfcn > 0
                and current_pci == target_pci
                and current_earfcn == target_earfcn
            ),
            f"cell:{current_pci}:{current_earfcn}",
        )

    if lever_name == LeverType.NR_BAND_LOCK.name:
        if not isinstance(target, CoreNrBandTarget):
            raise TypeError("radio_target_nr_band_target_required")
        requested_bands = frozenset(target.nr_bands)
        current = facts.nr_bands
        if not requested_bands:
            return RadioTargetMatch(
                not current,
                f"nr_band:{sorted(current)}",
            )
        if not current:
            return RadioTargetMatch(False, "nr_band_truth_missing")
        if target.release_manual and len(requested_bands) > 1 and facts.radio_mode is not None:
            # NR AUTO restores an allow-list, not simultaneous attachment to
            # every allowed band. Use the existing native selected-mask fact;
            # a serving-band overlap alone cannot prove that mask was applied.
            if facts.radio_mode_acquired_at <= required_after_ts:
                return RadioTargetMatch(False, "nr_auto_fresh_selection_pending")
            configuration = facts.radio_mode.to_mapping()
            if (configuration["mode"] != "nsa_preferred"
                    or frozenset(configuration["nr_bands"]) != requested_bands):
                return RadioTargetMatch(False, "nr_auto_selected_mask_not_observed")
            return RadioTargetMatch(current.issubset(requested_bands), "nr_auto_selection_and_attachment")
        return RadioTargetMatch(
            current == requested_bands,
            f"nr_band:{sorted(current)}",
        )

    return RadioTargetMatch(
        False,
        f"target_confirmation_unsupported:{lever_name.lower()}",
    )


__all__ = [
    "RadioTargetMatch",
    "RadioTargetFacts",
    "TargetConfirmationReceipt",
    "bands_from_value",
    "build_radio_target_facts_at_accepted_matrix_boundary",
    "match_radio_target",
    "radio_target_facts_from_generation",
]
