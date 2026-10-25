from __future__ import annotations

from typing import Any, Mapping

from arch4.coerce import safe_bool
from arch4.modem_live.profile_catalog import (
    SUPPORT_LEVEL_ADVANCED_CONTROL,
    SUPPORT_LEVEL_FULL_EXPERT,
    SUPPORT_LEVEL_MONITOR,
    SUPPORT_LEVEL_SAFE_CONTROL,
)

TIER_UNSUPPORTED = 0
TIER_OBSERVED = 1
TIER_READ_ONLY = 2
TIER_MONITOR_SAFE = 3
TIER_EXPERIMENTAL = 4
TIER_MANUAL_ACTION_SAFE = 5
TIER_AUTOMATION_ELIGIBLE = 6
TIER_UNKNOWN = TIER_UNSUPPORTED

SUPPORT_STATE_FULL = "full"
SUPPORT_STATE_PARTIAL = "partial"
SUPPORT_STATE_UNKNOWN = "unknown"

SUPPORT_AUTHORITY_FIELDS = (
    "support_state",
    "support_tier",
    "support_tier_label",
    "write_authority",
    "write_block_reason",
)

_TIER_TOKENS = {
    TIER_UNSUPPORTED: "unsupported",
    TIER_OBSERVED: "observed",
    TIER_READ_ONLY: "read-only",
    TIER_MONITOR_SAFE: "monitor-safe",
    TIER_EXPERIMENTAL: "experimental",
    TIER_MANUAL_ACTION_SAFE: "manual-action-safe",
    TIER_AUTOMATION_ELIGIBLE: "automation-eligible",
}
_TIER_WRITE_ALLOWED = {TIER_MANUAL_ACTION_SAFE, TIER_AUTOMATION_ELIGIBLE}
_ESTIMATED_PROVENANCE = {"community", "inferred"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _control_surface_gap(name: Any) -> bool:
    """Return True when a canonical gap affects control/runtime authority."""
    norm = _lower(name)
    if not norm:
        return False
    return not (norm.startswith("gui_only") or norm.startswith("display_subset"))


def support_tier_label(tier: int) -> str:
    token = _TIER_TOKENS.get(int(tier), "unsupported")
    return f"T{int(tier)} {token.replace('-', ' ').title()}"


def support_tier_write_allowed(tier: int) -> bool:
    return int(tier or 0) in _TIER_WRITE_ALLOWED


def support_state_from_tier(tier: int) -> str:
    if int(tier or 0) <= 0:
        return SUPPORT_STATE_UNKNOWN
    # Support tier is diagnostic provenance. It is not a product permission
    # gate. Per-action write permission is owned by the capability contract and
    # product permission is owned by the license gate.
    return SUPPORT_STATE_FULL


def support_state_block_reason(state: str) -> str:
    norm = _lower(state)
    if norm == SUPPORT_STATE_UNKNOWN:
        return (
            "This device has not been identified. "
            "54GUC is in observe-only mode until the device identity and "
            "support level are confirmed."
        )
    if norm == SUPPORT_STATE_PARTIAL:
        return ""
    return ""


def has_any_support_authority_field(payload: Mapping[str, Any] | None) -> bool:
    data = payload if isinstance(payload, Mapping) else {}
    return any(field in data for field in SUPPORT_AUTHORITY_FIELDS)


def has_complete_support_authority(payload: Mapping[str, Any] | None) -> bool:
    data = payload if isinstance(payload, Mapping) else {}
    return (
        data.get("support_state") not in (None, "")
        and data.get("support_tier") not in (None, "")
        and data.get("support_tier_label") not in (None, "")
        and data.get("write_authority") not in (None, "")
        and "write_block_reason" in data
        and data.get("write_block_reason") is not None
    )


def publish_support_authority_fields(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    tier = support_tier_from_payload(payload)
    state = support_state_from_tier(tier)
    tier_payload = payload.get("support_tier") if isinstance(payload.get("support_tier"), Mapping) else {}
    write_allowed = safe_bool(tier_payload.get("write_allowed", True), True)
    label = _text(tier_payload.get("label") or support_tier_label(tier))

    payload["support_state"] = state
    payload["support_tier_label"] = label
    payload["write_authority"] = "full" if write_allowed and state == SUPPORT_STATE_FULL else ""
    payload["write_block_reason"] = "" if payload["write_authority"] == "full" else support_state_block_reason(state)

    support_section = payload.get("support")
    if isinstance(support_section, dict):
        support_section["support_tier"] = dict(tier_payload)
        support_section["support_state"] = payload["support_state"]
        support_section["support_tier_label"] = payload["support_tier_label"]
        support_section["write_authority"] = payload["write_authority"]
        support_section["write_block_reason"] = payload["write_block_reason"]
    return payload


def _max_tier_from_support_level(level: str) -> int:
    norm = _lower(level)
    if norm == SUPPORT_LEVEL_MONITOR:
        return TIER_MONITOR_SAFE
    if norm == SUPPORT_LEVEL_SAFE_CONTROL:
        return TIER_MANUAL_ACTION_SAFE
    if norm in {SUPPORT_LEVEL_ADVANCED_CONTROL, SUPPORT_LEVEL_FULL_EXPERT}:
        return TIER_AUTOMATION_ELIGIBLE
    return TIER_OBSERVED


def build_support_tier(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    data = payload if isinstance(payload, Mapping) else {}
    identity = data.get("runtime_identity") if isinstance(data.get("runtime_identity"), Mapping) else {}
    resolved_card = (
        data.get("resolved_identity_card")
        if isinstance(data.get("resolved_identity_card"), Mapping)
        else {}
    )
    contract = data.get("capability_contract") if isinstance(data.get("capability_contract"), Mapping) else {}
    support = data.get("support") if isinstance(data.get("support"), Mapping) else {}
    onboarding = data.get("onboarding_status") if isinstance(data.get("onboarding_status"), Mapping) else {}
    identity_state = _lower(identity.get("identity_state"))
    support_level = _lower(support.get("support_level"))
    profile_provenance = _lower(contract.get("profile_provenance"))
    resolved_exact_identity = safe_bool(resolved_card.get("exact_identity", False), False)
    if resolved_exact_identity and (_text(resolved_card.get("profile_id")) or _text(resolved_card.get("model"))):
        identity_state = "identified"
        profile_provenance = _lower(resolved_card.get("profile_provenance")) or profile_provenance
        support_level = _lower(resolved_card.get("support_level")) or support_level
    write_allowed_actions = contract.get("write_allowed_actions")
    if not isinstance(write_allowed_actions, (list, tuple, set, frozenset)):
        write_allowed_actions = []
    allowed_count = len([name for name in write_allowed_actions if _text(name)])
    actions = contract.get("actions") if isinstance(contract.get("actions"), Mapping) else {}
    best_effort_rf_actions = sorted(
        str(name)
        for name, meta in actions.items()
        if isinstance(meta, Mapping)
        and safe_bool(meta.get("write_allowed"), False)
        and _lower(meta.get("action_family")) == "rf_control"
        and _lower(meta.get("state")) != "supported"
    )
    canonical_surface = contract.get("canonical_surface") if isinstance(contract.get("canonical_surface"), Mapping) else {}
    canonical_complete = safe_bool(canonical_surface.get("complete"), True) if canonical_surface else True
    canonical_conflicts = canonical_surface.get("field_conflicts") if isinstance(canonical_surface, Mapping) else []
    if not isinstance(canonical_conflicts, (list, tuple, set, frozenset)):
        canonical_conflicts = []
    canonical_missing = canonical_surface.get("missing_groups") if isinstance(canonical_surface, Mapping) else []
    if not isinstance(canonical_missing, (list, tuple, set, frozenset)):
        canonical_missing = []
    canonical_degraded = canonical_surface.get("degraded_groups") if isinstance(canonical_surface, Mapping) else []
    if not isinstance(canonical_degraded, (list, tuple, set, frozenset)):
        canonical_degraded = []
    canonical_control_incomplete = bool(
        not canonical_complete
        and (
            any(_control_surface_gap(name) for name in canonical_missing)
            or any(_control_surface_gap(name) for name in canonical_degraded)
        )
    )
    reads = contract.get("reads") if isinstance(contract.get("reads"), Mapping) else {}
    supported_reads = 0
    for meta in reads.values():
        if isinstance(meta, Mapping) and _lower(meta.get("state")) == "supported":
            supported_reads += 1

    tier = TIER_UNSUPPORTED
    reason = "identity_unknown"
    if identity_state == "identified":
        tier = TIER_READ_ONLY
        reason = "identified_read_only"
        if supported_reads > 0:
            tier = TIER_MONITOR_SAFE
            reason = "monitor_safe_reads_confirmed"
        if resolved_exact_identity and allowed_count > 0:
            if profile_provenance in _ESTIMATED_PROVENANCE:
                tier = TIER_EXPERIMENTAL
                reason = "experimental_profile_requires_manual_promotion"
            else:
                tier = TIER_MANUAL_ACTION_SAFE
                reason = "manual_actions_confirmed"
                max_tier = _max_tier_from_support_level(support_level)
                if (
                    max_tier >= TIER_AUTOMATION_ELIGIBLE
                    and profile_provenance in {"observed", "documented"}
                    and allowed_count >= 2
                    and not canonical_control_incomplete
                    and not best_effort_rf_actions
                ):
                    tier = TIER_AUTOMATION_ELIGIBLE
                    reason = "automation_eligible_profile"
                elif max_tier >= TIER_AUTOMATION_ELIGIBLE and canonical_control_incomplete:
                    reason = "canonical_surface_incomplete"
                elif max_tier >= TIER_AUTOMATION_ELIGIBLE and best_effort_rf_actions:
                    reason = "best_effort_rf_actions"
    elif identity_state == "family_known_model_unknown":
        tier = TIER_OBSERVED
        reason = "family_known_model_unknown"
    elif identity_state == "vendor_known_model_unknown":
        tier = TIER_UNKNOWN
        reason = "vendor_known_model_unknown"
    elif _text(identity.get("backend_family")):
        tier = TIER_OBSERVED
        reason = "family_known"

    max_tier = _max_tier_from_support_level(support_level)
    if safe_bool(onboarding.get("write_downgraded"), False) and not resolved_exact_identity:
        max_tier = min(max_tier, TIER_OBSERVED)
        if _text(onboarding.get("state")):
            reason = _text(onboarding.get("state"))
    if canonical_conflicts:
        max_tier = min(max_tier, TIER_UNSUPPORTED)
        reason = "canonical_surface_conflict"
    tier = min(tier, max_tier)

    return {
        "tier": int(tier),
        "token": _TIER_TOKENS.get(int(tier), "unknown"),
        "label": support_tier_label(int(tier)),
        "write_allowed": support_tier_write_allowed(int(tier)),
        "reason": reason,
        "backend_support_level": support_level,
        "max_tier": int(max_tier),
        "canonical_surface_complete": bool(canonical_complete),
        "canonical_surface_missing_groups": sorted(str(name) for name in canonical_missing if _text(name)),
        "canonical_surface_degraded_groups": sorted(str(name) for name in canonical_degraded if _text(name)),
        "canonical_surface_field_conflicts": sorted(str(name) for name in canonical_conflicts if _text(name)),
        "best_effort_rf_actions": best_effort_rf_actions,
    }


def support_tier_from_payload(payload: Mapping[str, Any] | None) -> int:
    data = payload if isinstance(payload, Mapping) else {}
    tier = data.get("support_tier")
    if isinstance(tier, Mapping):
        try:
            return int(tier.get("tier", 0) or 0)
        except Exception:
            token = _lower(tier.get("token") or tier.get("label"))
            return _tier_from_token(token)
    try:
        return int(tier or 0)
    except Exception:
        return _tier_from_token(_lower(tier))


def _tier_from_token(token: str) -> int:
    normalized = _lower(token).replace("_", "-")
    if not normalized:
        return TIER_UNSUPPORTED
    for tier, value in _TIER_TOKENS.items():
        if normalized == value or normalized.endswith(value):
            return int(tier)
    return TIER_UNSUPPORTED


def support_tier_allows_writes(payload: Mapping[str, Any] | None) -> bool | None:
    data = payload if isinstance(payload, Mapping) else {}
    if "support_tier" not in data:
        return None
    return support_tier_write_allowed(support_tier_from_payload(data))
