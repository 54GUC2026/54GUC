"""Define proposals for controlled Modem Live profile tuning.

Other parts may find evidence that a profile needs adjustment, but they cannot
change Modem Live timing directly. Only the Modem Live profile owner may apply
an accepted proposal.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from arch4.core_contracts.configuration_keys import (
    PROFILE_TUNING_PROPOSAL_KEYS,
)

PROFILE_TUNING_PROPOSAL_OWNER = "modem_live_access_profile"
PROFILE_TUNING_PROPOSAL_SCHEMA = "profile_tuning_proposal.v1"

_PROFILE_TUNING_SAFETY_FLOORS: dict[str, dict[str, float]] = {
    "live_maintenance_interval_s": {"min_s": 1.0, "max_s": 300.0},
    "modem_live_register_interval_s": {"min_s": 0.5, "max_s": 30.0},
    "modem_live_register_stale_s": {"min_s": 2.0, "max_s": 120.0},
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return float(value)
    except Exception:
        return default


def build_profile_tuning_proposal(
    *,
    key: str,
    proposed_value: Any,
    current_value: Any,
    active_metadata: Mapping[str, Any],
    evidence_owner: str,
    evidence_reason: str,
) -> dict[str, Any]:
    """Build a Modem Live access-profile tuning proposal.

    Other parts may create these rows as evidence. They must not change the
    effective Modem Live access timing directly.
    """

    parameter_id = _text(key)
    if parameter_id not in PROFILE_TUNING_PROPOSAL_KEYS:
        raise ValueError(f"profile_tuning_key_not_allowed:{parameter_id}")
    metadata = dict(active_metadata or {})
    proposed = _float(proposed_value)
    current = _float(current_value)
    safety_floor = dict(_PROFILE_TUNING_SAFETY_FLOORS[parameter_id])
    in_floor = safety_floor["min_s"] <= proposed <= safety_floor["max_s"]
    confidence = _float(metadata.get("candidate_primary_metric_confidence"), 0.75)
    confidence = max(0.0, min(1.0, confidence if confidence > 0.0 else 0.75))
    return {
        "schema": PROFILE_TUNING_PROPOSAL_SCHEMA,
        "owner": PROFILE_TUNING_PROPOSAL_OWNER,
        "parameter_group": "modem_live_access_timing",
        "parameter_id": parameter_id,
        "status": "proposed" if in_floor else "blocked",
        "reason": "" if in_floor else "outside_safety_floor",
        "current": current,
        "proposed": proposed,
        "delta": proposed - current,
        "evidence": {
            "owner": _text(evidence_owner),
            "reason": _text(evidence_reason),
            "active_profile_id": _text(metadata.get("active_profile_id")),
            "active_profile_version": int(metadata.get("active_profile_version", 0) or 0),
            "active_reference_marker": _text(metadata.get("active_reference_marker")),
        },
        "confidence": confidence,
        "safety_floor": safety_floor,
        "rollback_condition": "modem_live_access_profile_rejects_or_next_confirmed_profile",
        "scope": {
            "active_profile_id": _text(metadata.get("active_profile_id")),
            "active_profile_version": int(metadata.get("active_profile_version", 0) or 0),
            "active_reference_marker": _text(metadata.get("active_reference_marker")),
        },
        "expiry_s": 0,
        "ts": time.time(),
        "direct_modem_actuation": False,
    }
