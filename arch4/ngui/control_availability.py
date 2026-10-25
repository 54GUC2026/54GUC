"""Show control availability from facts owned elsewhere."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from arch4.coerce import safe_float
from arch4.action_admission import runtime_action_unsupported_reason
from arch4.interfaces import LeverType


PHYSICAL_CONTROLS = (
    "band_selection",
    "pband",
    "nr_band_lock",
    "band_lock",
    "cell_lock",
    "restart",
    "wifi_control",
    "network_settings",
    "mobile_data",
)
BAND_CONTROLS = frozenset(
    {"band_selection", "pband", "nr_band_lock", "band_lock", "cell_lock"}
)
WORKFLOW_CONTROL = "runtime_workflow"


@dataclass(frozen=True, slots=True)
class ControlAvailability:
    enabled: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("ngui_control_availability_bool_required")
        if type(self.reason) is not str:
            raise TypeError("ngui_control_availability_reason_required")
        if self.enabled is bool(self.reason):
            raise ValueError("ngui_control_availability_reason_invalid")

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "reason": self.reason}


def _common_reason(evidence: Mapping[str, Any], *, license_required: bool) -> str:
    if evidence.get("connected") is not True:
        return "Connect to a modem first."
    if evidence.get("identity_confirmed") is not True:
        return "Wait for the modem serial number or IMEI."
    if evidence.get("runtime_running") is not True:
        return "Wait for the modem runtime to be ready."
    if evidence.get("communication_alive") is not True:
        return "Wait for fresh modem communication."
    if evidence.get("live_mode") is not True:
        return "This session is observe-only; controls are disabled."
    if license_required and evidence.get("licensed") is not True:
        return "A valid modem-bound license is required for modem actions."
    return ""


def build_control_availability(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Build a widget view without accepting any action."""

    if not isinstance(evidence, Mapping):
        raise TypeError("ngui_control_availability_evidence_mapping_required")
    capabilities = evidence.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, Mapping) else {}
    capability_reasons = evidence.get("capability_reasons")
    capability_reasons = (
        capability_reasons if isinstance(capability_reasons, Mapping) else {}
    )
    controls: dict[str, dict[str, Any]] = {}
    common = _common_reason(evidence, license_required=True)
    for control in PHYSICAL_CONTROLS:
        reason = common
        if not reason and control == "nr_band_lock" and runtime_action_unsupported_reason(
            LeverType.NR_BAND_LOCK, profile_id=str(evidence.get("profile_id") or "")
        ):
            reason = "5G NR bands can be monitored; changing them is not enabled in this release."
        if not reason and evidence.get("command_protection_active") is True:
            reason = str(
                evidence.get("command_protection_reason")
                or "Modem command protection is active."
            )
        supported = capabilities.get(control)
        if not reason and supported is not True:
            reason = str(capability_reasons.get(control) or "")
            if not reason:
                label = control.replace("_", " ").title()
                reason = (
                    f"{label} capability is not confirmed."
                    if supported is None
                    else f"{label} is not supported by this modem."
                )
        if not reason and control != "wifi_control":
            if evidence.get("manual_pending") is True:
                reason = "Wait for the current manual action to finish."
            elif control != "mobile_data" and evidence.get("manual_available") is not True:
                reason = str(
                    evidence.get("manual_reason")
                    or "Cellular manual actions are currently unavailable."
                )
        if (
            not reason
            and control in BAND_CONTROLS
            and evidence.get("operator_band_cooldown_active") is True
        ):
            remaining = max(
                1,
                int(
                    round(
                        max(
                            0.0,
                            safe_float(
                                evidence.get("operator_band_cooldown_remaining_s"),
                                0.0,
                            ),
                        )
                    )
                ),
            )
            reason = f"Band controls are stabilizing ({remaining} seconds remaining)."
        controls[control] = ControlAvailability(not bool(reason), reason).as_dict()
    workflow_reason = _common_reason(evidence, license_required=False)
    controls[WORKFLOW_CONTROL] = ControlAvailability(
        not bool(workflow_reason), workflow_reason
    ).as_dict()
    return controls


def control_availability_from_mapping(
    payload: Mapping[str, Any] | None,
    *controls: str,
) -> ControlAvailability:
    data = payload if isinstance(payload, Mapping) else {}
    if not controls:
        return ControlAvailability(False, "Control identity is required.")
    for control in controls:
        row = data.get(control)
        if not isinstance(row, Mapping):
            return ControlAvailability(False, "Control availability is not ready.")
        enabled = row.get("enabled")
        reason = row.get("reason")
        if (
            type(enabled) is not bool
            or type(reason) is not str
            or enabled is bool(reason)
        ):
            return ControlAvailability(False, "Control availability is not ready.")
        if not enabled:
            return ControlAvailability(False, reason)
    return ControlAvailability(True, "")


__all__ = (
    "BAND_CONTROLS",
    "ControlAvailability",
    "PHYSICAL_CONTROLS",
    "WORKFLOW_CONTROL",
    "build_control_availability",
    "control_availability_from_mapping",
)
