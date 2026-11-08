"""Read-only network targets at the named redacted support-export boundary."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from arch4.core_contracts.command_targets import core_command_target_payload
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.network_settings import network_target_from_mapping
from arch4.new_horizon.contracts import RefereeTarget
from arch4.new_horizon.history import SUPPORT_HISTORY_EXPORT_OWNER, SUPPORT_HISTORY_EXPORT_SCHEMA
from arch4.privacy_scrub import SCRUB_MASK


SUPPORT_NETWORK_TARGET_SCHEMA = "NewHorizonSupportNetworkSettingsTarget.v1"


@dataclass(frozen=True, slots=True)
class SupportRefereeTarget(RefereeTarget):
    """Diagnostic evidence only; the Core command parser rejects this schema."""

    def __post_init__(self) -> None:
        if not isinstance(self.target, CoreFrozenPayload):
            raise TypeError("referee_target_payload_required")
        payload = self.target.to_mapping()
        if set(payload) != {"schema", "command_kind", "value"} or (
            payload["schema"] != SUPPORT_NETWORK_TARGET_SCHEMA
            or payload["command_kind"] != "NETWORK_SETTINGS"
        ):
            raise ValueError("support_network_target_schema_invalid")
        value = payload["value"]
        if not isinstance(value, Mapping) or value.get("subscription_key") != SCRUB_MASK:
            raise ValueError("support_network_target_subscription_mask_required")
        validation_value = dict(value)
        # Reuse the one public-value validator. These disposable placeholders
        # never escape validation, identify a SIM, decrypt credentials or form
        # the returned diagnostic target.
        validation_value["subscription_key"] = "0" * 64
        apn = value.get("apn")
        if apn is not None:
            if not isinstance(apn, Mapping) or apn.get("protected_credentials") != SCRUB_MASK:
                raise ValueError("support_network_target_credentials_mask_required")
            validation_value["apn"] = {**apn, "protected_credentials": "dpapi:v1:" + "A" * 40}
        validated = network_target_from_mapping(validation_value)
        RefereeTarget(
            action_family=self.action_family,
            target_signature=self.target_signature,
            target=CoreFrozenPayload.from_mapping(core_command_target_payload(validated)),
        )


def referee_target_from_history_record(row: Mapping[str, Any]) -> RefereeTarget:
    """Select an explicit support adapter, never retry failed live validation."""
    payload = row["payload"]
    target = dict(payload.get("target") or {})
    # Storage rows do not carry an export envelope. A malformed/unknown envelope
    # is an error, not permission to fall back to a live target.
    if any(key in row for key in ("schema", "owner", "record_type")):
        if (
            row.get("schema") != SUPPORT_HISTORY_EXPORT_SCHEMA
            or row.get("owner") != SUPPORT_HISTORY_EXPORT_OWNER
            or row.get("record_type") != "evidence"
        ):
            raise ValueError("support_referee_export_envelope_invalid")
        if target.get("command_kind") == "NETWORK_SETTINGS":
            if target.get("schema") != "CoreNetworkSettingsTarget.v1":
                raise ValueError("support_network_source_schema_invalid")
            return SupportRefereeTarget(
                action_family=str(payload.get("action_family") or ""),
                target_signature=str(payload.get("target_signature") or ""),
                target=CoreFrozenPayload.from_mapping({**target, "schema": SUPPORT_NETWORK_TARGET_SCHEMA}),
            )
    return RefereeTarget(
        action_family=str(payload.get("action_family") or ""),
        target_signature=str(payload.get("target_signature") or ""),
        target=CoreFrozenPayload.from_mapping(target),
    )
