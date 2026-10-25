"""Define typed Modem Live command plans and transport results.

Core owns the target, and Command Authority owns its lifecycle. Modem Live
builds the physical plan for the backend. The backend may run only a plan stage
and must return evidence from the real transport call. It must not reinterpret
the original ``LeverAction`` or invent timing later.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import hashlib
from typing import Any, Mapping
from arch4.core_contracts.network_settings import network_stage_kinds

from arch4.core_contracts.command_targets import (
    CORE_COMMAND_TARGET_TYPES,
    CoreBandSetTarget,
    CoreBandAutoTarget,
    CoreNrBandTarget,
    CoreRadioModeTarget,
    CoreCellLockTarget,
    CoreCommandTarget,
    CorePBandTarget,
    CoreRestartTarget,
    CoreUeCapabilityTarget,
    CoreWifiRadioTarget,
    CoreNetworkSettingsTarget,
)
from arch4.core_contracts.physical_commands import (
    PhysicalCommandCoordinationTarget,
    PhysicalCommandDispatchEvidence,
    PhysicalCommandStageTarget,
)


def _required_text(value: Any, reason: str) -> str:
    if not isinstance(value, str):
        raise TypeError(reason)
    text = value.strip()
    if not text:
        raise ValueError(reason)
    return text


def _positive_time(value: Any, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(reason)
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise ValueError(reason)
    return parsed


def _mode_transaction(plan):
    return {"request_id": plan.request_id, "transaction_root_id": plan.transaction_root_id,
            "write_token_hash": hashlib.sha256(plan.event_write_token.encode()).hexdigest()}


def _validate_stage_target(
    stage_kind: str,
    target: PhysicalCommandStageTarget,
) -> None:
    if isinstance(target, PhysicalCommandCoordinationTarget):
        if stage_kind != target.operation:
            raise ValueError("modem_live_coordination_stage_target_mismatch")
        return
    if stage_kind == "pband_anchor":
        if not isinstance(target, CorePBandTarget) or target.ca_pool:
            raise ValueError("modem_live_pband_anchor_target_invalid")
        return
    if isinstance(target, CoreRadioModeTarget) and stage_kind in {"radio_disconnect", "radio_reconnect"}:
        return
    if stage_kind == "ca_pool":
        if (
            not isinstance(target, CoreBandSetTarget)
            or target.command_kind != "BAND_SELECTION"
        ):
            raise ValueError("modem_live_ca_pool_target_invalid")
        return
    if isinstance(target, CoreNetworkSettingsTarget):
        if stage_kind not in target.stage_kinds + network_stage_kinds(target, coordination_profile="zte_zwrt") + network_stage_kinds(target, coordination_profile="tplink_dev2"):
            raise ValueError("network_settings_stage_target_mismatch")
        return
    if stage_kind == "wifi_config":
        if not isinstance(target, CoreWifiRadioTarget):
            raise ValueError("modem_live_wifi_config_target_invalid")
        return
    if stage_kind in {
        "ue_dl_256qam",
        "ue_ul_64qam",
        "ue_restart",
    }:
        if not isinstance(target, CoreUeCapabilityTarget):
            raise ValueError("modem_live_ue_stage_target_invalid")
        value = {
            "ue_dl_256qam": target.dl_256qam,
            "ue_ul_64qam": target.ul_64qam,
            "ue_restart": target.restart,
        }[stage_kind]
        if value is None:
            raise ValueError("modem_live_ue_stage_value_missing")
        return
    if stage_kind != target.command_kind.lower():
        raise ValueError("modem_live_stage_target_mismatch")


@dataclass(frozen=True, slots=True)
class ModemLivePhysicalStagePlan:
    """One typed modem write authorized by the parent Core command."""

    request_id: str
    transaction_root_id: str
    event_write_token: str
    stage_kind: str
    ordinal: int
    target: PhysicalCommandStageTarget
    preceding_auto: PhysicalCommandDispatchEvidence | None = None
    preceding_mode: PhysicalCommandDispatchEvidence | None = None
    preceding_disconnect: PhysicalCommandDispatchEvidence | None = None
    preceding_radio_mode: PhysicalCommandDispatchEvidence | None = None

    def __post_init__(self) -> None:
        _required_text(self.request_id, "modem_live_plan_request_id_required")
        _required_text(
            self.transaction_root_id,
            "modem_live_plan_transaction_root_required",
        )
        _required_text(
            self.event_write_token,
            "modem_live_plan_event_write_token_required",
        )
        kind = _required_text(
            self.stage_kind,
            "modem_live_plan_stage_kind_required",
        )
        if type(self.ordinal) is not int or self.ordinal <= 0:
            raise TypeError("modem_live_plan_stage_ordinal_invalid")
        if not isinstance(
            self.target,
            (*CORE_COMMAND_TARGET_TYPES, PhysicalCommandCoordinationTarget),
        ):
            raise TypeError("modem_live_plan_typed_target_required")
        _validate_stage_target(kind, self.target)
        if self.preceding_disconnect is not None:
            previous = self.preceding_disconnect
            if (not isinstance(self.target, CoreRadioModeTarget)
                    or kind not in {"radio_mode", "radio_reconnect"}
                    or not isinstance(previous, PhysicalCommandDispatchEvidence)
                    or previous.target != self.target or previous.stage_kind != "radio_disconnect"
                    or previous.ordinal != 1 or self.ordinal != (2 if kind == "radio_mode" else 3)
                    or not previous.confirmed_by_transport
                    or previous.detail.get("radio_mode_transaction") != _mode_transaction(self)):
                raise ValueError("radio_mode_disconnect_proof_invalid")
        if self.preceding_radio_mode is not None:
            previous = self.preceding_radio_mode
            if (not isinstance(self.target, CoreRadioModeTarget) or kind != "radio_reconnect"
                    or self.preceding_disconnect is None
                    or not isinstance(previous, PhysicalCommandDispatchEvidence)
                    or previous.target != self.target or previous.stage_kind != "radio_mode"
                    or previous.ordinal != self.ordinal - 1 or not previous.confirmed_by_transport
                    or previous.detail.get("radio_mode_transaction") != _mode_transaction(self)):
                raise ValueError("radio_mode_reconnect_proof_invalid")
        if self.preceding_mode is not None:
            previous = self.preceding_mode
            if (not isinstance(self.target, CoreNrBandTarget) or self.target.release_manual
                    or self.preceding_auto is not None
                    or not isinstance(previous, PhysicalCommandDispatchEvidence)
                    or not isinstance(previous.target, CoreRadioModeTarget)
                    or previous.stage_kind != "radio_mode" or previous.ordinal != self.ordinal - 1
                    or not previous.confirmed_by_transport
                    or previous.target.mode != "nsa_preferred" or previous.target.expected_mode != "lte_only"):
                raise ValueError("modem_live_nr_preceding_mode_invalid")
        if self.preceding_auto is not None:
            if (not isinstance(self.target, (CoreBandSetTarget, CoreNrBandTarget))
                    or not self.target.release_manual
                    or not isinstance(self.preceding_auto, PhysicalCommandDispatchEvidence)
                    or not isinstance(self.preceding_auto.target, CoreBandAutoTarget)
                    or not self.preceding_auto.native_auto_release_confirmed
                    or self.preceding_auto.ordinal != self.ordinal - 1):
                raise ValueError('modem_live_auto_preceding_stage_invalid')
            domain = 'nr_approved_auto' if isinstance(self.target, CoreNrBandTarget) else 'lte_allocated_auto'
            if self.preceding_auto.target.domain != domain:
                raise ValueError('modem_live_auto_preceding_domain_invalid')


@dataclass(frozen=True, slots=True)
class ModemLivePhysicalStageResult:
    """Immutable evidence returned by the backend transport writer."""

    plan: ModemLivePhysicalStagePlan
    request_dispatched_at: float
    transport_returned_at: float
    outcome: str
    reason: str
    radio_drop_detected: bool = False
    recovery_time_s: float = 0.0
    detail: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.plan, ModemLivePhysicalStagePlan):
            raise TypeError("modem_live_stage_result_plan_required")
        dispatched = _positive_time(
            self.request_dispatched_at,
            "modem_live_stage_dispatch_time_invalid",
        )
        returned = _positive_time(
            self.transport_returned_at,
            "modem_live_stage_return_time_invalid",
        )
        if returned < dispatched:
            raise ValueError("modem_live_stage_return_time_invalid")
        outcome = _required_text(
            self.outcome,
            "modem_live_stage_outcome_required",
        )
        if outcome not in {
            "accepted",
            "response_unavailable",
            "rejected",
        }:
            raise ValueError("modem_live_stage_outcome_invalid")
        _required_text(self.reason, "modem_live_stage_reason_required")
        if type(self.radio_drop_detected) is not bool:
            raise TypeError("modem_live_stage_radio_drop_bool_required")
        if (
            isinstance(self.recovery_time_s, bool)
            or not isinstance(self.recovery_time_s, (int, float))
            or not math.isfinite(float(self.recovery_time_s))
            or float(self.recovery_time_s) < 0.0
        ):
            raise ValueError("modem_live_stage_recovery_time_invalid")
        if self.detail is not None and not isinstance(self.detail, Mapping):
            raise TypeError("modem_live_stage_detail_mapping_required")

    @property
    def permits_target_verification(self) -> bool:
        return self.outcome in {"accepted", "response_unavailable"}

    @property
    def confirmed_by_transport(self) -> bool:
        return self.outcome == "accepted"

    def dispatch_evidence(self) -> PhysicalCommandDispatchEvidence:
        detail = dict(self.detail or {})
        if isinstance(self.plan.target, CoreRadioModeTarget) and (
                self.plan.stage_kind in {"radio_disconnect", "radio_reconnect"}
                or self.plan.stage_kind == "radio_mode" and self.plan.ordinal == 2):
            detail["radio_mode_transaction"] = _mode_transaction(self.plan)
        return PhysicalCommandDispatchEvidence(
            stage_kind=self.plan.stage_kind,
            ordinal=self.plan.ordinal,
            request_dispatched_at=self.request_dispatched_at,
            transport_returned_at=self.transport_returned_at,
            outcome=self.outcome,
            reason=self.reason,
            detail=detail,
            target=self.plan.target,
        )


def physical_stage_plans(
    *,
    request_id: str,
    transaction_root_id: str,
    event_write_token: str,
    target: CoreCommandTarget,
    coordination_profile: str,
    nr_enable_mode: CoreRadioModeTarget | None = None,
    radio_mode_disconnect: bool = False,
) -> tuple[ModemLivePhysicalStagePlan, ...]:
    """Build the physical plan from the immutable Core target."""

    if not isinstance(target, CORE_COMMAND_TARGET_TYPES):
        raise TypeError("modem_live_physical_plan_typed_target_required")
    if coordination_profile not in {
        "direct",
        "openwrt",
        "routeros",
        "zte_zwrt",
        "tplink_dev2",
        "zte_goform",
    }:
        raise ValueError("modem_live_coordination_profile_invalid")
    if type(radio_mode_disconnect) is not bool or (radio_mode_disconnect and
            (coordination_profile != "zte_goform" or not isinstance(target, CoreRadioModeTarget))):
        raise ValueError("radio_mode_disconnect_plan_invalid")
    requested: list[tuple[str, PhysicalCommandStageTarget]] = []
    if radio_mode_disconnect:
        requested.extend((kind, target) for kind in ("radio_disconnect", "radio_mode", "radio_reconnect"))
    elif isinstance(target, (CoreBandSetTarget, CoreNrBandTarget)) and target.release_manual:
        domain = 'nr_approved_auto' if isinstance(target, CoreNrBandTarget) else 'lte_allocated_auto'
        requested.extend((('band_auto', CoreBandAutoTarget(domain)),
                          (target.command_kind.lower(), target)))
    elif isinstance(target, CoreNetworkSettingsTarget):
        requested.extend((kind, target) for kind in network_stage_kinds(target, coordination_profile=coordination_profile))
    elif isinstance(target, CorePBandTarget):
        requested.append(
            ("pband_anchor", CorePBandTarget(pband=target.pband))
        )
        if target.has_ca_companions:
            requested.append(
                (
                    "ca_pool",
                    CoreBandSetTarget(
                        bands=target.ca_pool,
                        command_kind="BAND_SELECTION",
                    ),
                )
            )
    elif isinstance(target, CoreUeCapabilityTarget):
        requested.extend(
            (kind, target)
            for kind, value in (
                ("ue_dl_256qam", target.dl_256qam),
                ("ue_ul_64qam", target.ul_64qam),
                ("ue_restart", target.restart),
            )
            if value is not None
        )
    elif (
        isinstance(target, CoreWifiRadioTarget)
        and coordination_profile in {"openwrt", "zte_zwrt"}
    ):
        requested.extend(
            (
                ("wifi_config", target),
                (
                    "wifi_commit",
                    PhysicalCommandCoordinationTarget("wifi_commit"),
                ),
                (
                    "wifi_reload",
                    PhysicalCommandCoordinationTarget("wifi_reload"),
                ),
            )
        )
    else:
        requested.append((target.command_kind.lower(), target))

    if nr_enable_mode is not None:
        if (not isinstance(target, CoreNrBandTarget) or target.release_manual or not target.nr_bands
                or not isinstance(nr_enable_mode, CoreRadioModeTarget)
                or nr_enable_mode.mode != "nsa_preferred" or nr_enable_mode.expected_mode != "lte_only"):
            raise ValueError("modem_live_nr_enable_mode_target_invalid")
        requested.insert(0, ("radio_mode", nr_enable_mode))

    recovery_coordinated = (
        coordination_profile == "routeros"
        and isinstance(
            target,
            (
                CoreBandSetTarget,
                CoreCellLockTarget,
                CorePBandTarget,
                CoreRestartTarget,
                CoreUeCapabilityTarget,
            ),
        )
    )
    if recovery_coordinated:
        requested.insert(
            0,
            (
                "device_local_recovery_pause",
                PhysicalCommandCoordinationTarget(
                    "device_local_recovery_pause"
                ),
            ),
        )
        requested.append(
            (
                "device_local_recovery_resume",
                PhysicalCommandCoordinationTarget(
                    "device_local_recovery_resume"
                ),
            )
        )
    return tuple(
        ModemLivePhysicalStagePlan(
            request_id=request_id,
            transaction_root_id=transaction_root_id,
            event_write_token=event_write_token,
            stage_kind=stage_kind,
            ordinal=ordinal,
            target=stage_target,
        )
        for ordinal, (stage_kind, stage_target) in enumerate(requested, start=1)
    )


__all__ = (
    "ModemLivePhysicalStagePlan",
    "ModemLivePhysicalStageResult",
    "physical_stage_plans",
)
