"""
Calibration profile runtime application policy owner.

The engine owns config mutation. This module owns the policy decision for which
calibration profile values may become runtime config overrides.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from arch4.core_contracts.configuration_keys import (
    CALIBRATION_PROFILE_BLOCKED_KEYS,
    CALIBRATION_PROFILE_KEYS,
    PROFILE_TUNING_PROPOSAL_KEYS,
)
from arch4.coerce import safe_bool
from arch4.modem_live.profile_tuning_contract import (
    PROFILE_TUNING_PROPOSAL_OWNER,
    build_profile_tuning_proposal,
)


CALIBRATION_PROFILE_APPLICATION_OWNER = "calibration_profile_application"


def calibration_profile_application_input_keys() -> tuple[str, ...]:
    """Return config keys the engine must sample before requesting a plan."""

    return tuple(
        sorted(
            set(CALIBRATION_PROFILE_KEYS)
            | set(CALIBRATION_PROFILE_BLOCKED_KEYS)
            | set(PROFILE_TUNING_PROPOSAL_KEYS)
        )
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def build_calibration_profile_application_plan(
    *,
    active_metadata: Mapping[str, Any],
    active_profile: Mapping[str, Any],
    cfg_values: Mapping[str, Any],
    active_config_overrides: Mapping[str, Any],
    last_applied_version: int,
    force: bool = False,
) -> dict[str, Any]:
    """Build the only legal runtime config application plan for a profile."""

    metadata = _mapping(active_metadata)
    profile = _mapping(active_profile)
    current_values = _mapping(cfg_values)
    overrides = copy.deepcopy(_mapping(active_config_overrides))
    profile_values = _mapping(profile.get("values"))

    profile_version = int(metadata.get("active_profile_version", 0) or 0)
    profile_id = str(metadata.get("active_profile_id", "") or "")
    forced = safe_bool(force, False)
    if not forced and profile_version <= int(last_applied_version or 0):
        return {
            "owner": CALIBRATION_PROFILE_APPLICATION_OWNER,
            "apply": False,
            "reason": "profile_version_not_new",
            "active_profile_id": profile_id,
            "active_profile_version": profile_version,
            "profile_tuning_proposal_owner": PROFILE_TUNING_PROPOSAL_OWNER,
        }

    restore_config_values: dict[str, Any] = {}
    for key in CALIBRATION_PROFILE_BLOCKED_KEYS:
        prior = overrides.pop(key, None)
        if isinstance(prior, Mapping) and key in current_values and "startup" in prior:
            restore_config_values[key] = copy.deepcopy(prior["startup"])

    changed: list[str] = []
    ignored_profile_owner_proposals: list[str] = []
    ignored_unknown_keys: list[str] = []
    profile_tuning_proposals: list[dict[str, Any]] = []
    set_config_values: dict[str, Any] = {}
    allowed_apply_keys = set(CALIBRATION_PROFILE_KEYS)
    proposal_only_keys = set(PROFILE_TUNING_PROPOSAL_KEYS)

    for key, val in profile_values.items():
        if key in CALIBRATION_PROFILE_BLOCKED_KEYS:
            continue
        if key in proposal_only_keys:
            ignored_profile_owner_proposals.append(str(key))
            profile_tuning_proposals.append(
                build_profile_tuning_proposal(
                    key=str(key),
                    proposed_value=val,
                    current_value=current_values.get(key, 0.0),
                    active_metadata=metadata,
                    evidence_owner=CALIBRATION_PROFILE_APPLICATION_OWNER,
                    evidence_reason="active_calibration_profile_value",
                )
            )
            continue
        if key not in allowed_apply_keys:
            ignored_unknown_keys.append(str(key))
            continue
        if key not in current_values:
            continue
        prev = current_values[key]
        if prev != val:
            startup_val = _mapping(overrides.get(key)).get("startup", prev)
            overrides[key] = {
                "startup": copy.deepcopy(startup_val),
                "current": copy.deepcopy(val),
            }
            set_config_values[str(key)] = copy.deepcopy(val)
            changed.append(str(key))

    for key in list(overrides.keys()):
        if key in profile_values:
            continue
        prior = overrides.pop(key, None)
        if (
            isinstance(prior, Mapping)
            and key in current_values
            and "startup" in prior
        ):
            restore_config_values[key] = copy.deepcopy(prior["startup"])
            changed.append(str(key))

    return {
        "owner": CALIBRATION_PROFILE_APPLICATION_OWNER,
        "apply": True,
        "reason": "force" if forced else "profile_version_new",
        "active_profile_id": profile_id,
        "active_profile_version": profile_version,
        "restore_config_values": restore_config_values,
        "set_config_values": set_config_values,
        "active_config_overrides": overrides,
        "changed_keys": changed,
        "ignored_profile_owner_proposals": sorted(ignored_profile_owner_proposals),
        "profile_tuning_proposals": sorted(
            profile_tuning_proposals,
            key=lambda row: str(row.get("parameter_id", "")),
        ),
        "ignored_unknown_keys": sorted(ignored_unknown_keys),
        "profile_tuning_proposal_owner": PROFILE_TUNING_PROPOSAL_OWNER,
    }


__all__ = [
    "CALIBRATION_PROFILE_APPLICATION_OWNER",
    "build_calibration_profile_application_plan",
    "calibration_profile_application_input_keys",
]
