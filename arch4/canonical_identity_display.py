"""Canonical writer-side identity display surface."""

from __future__ import annotations

from typing import Any, Mapping

from arch4.coerce import safe_bool
from arch4.runtime_support_tier import support_tier_from_payload, support_tier_label


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    return str(value or "").strip()


def _coalesce(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text and text.lower() not in {
            "-",
            "--",
            "---",
            "n/a",
            "na",
            "none",
            "null",
            "unknown",
            "miss",
            "true",
            "false",
        }:
            return text
    return ""


def _provider_identity(*sources: Mapping[str, Any] | None) -> tuple[str, str]:
    provider_name = ""
    provider_plmn = ""
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        if not provider_name:
            provider_name = _coalesce(source.get("provider_name"))
        if not provider_plmn:
            provider_plmn = _coalesce(source.get("provider_plmn"))
        if provider_name and provider_plmn:
            break
    return provider_name, provider_plmn


def build_canonical_identity_display(
    *,
    base_status: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    runtime_health: Mapping[str, Any] | None = None,
    capabilities: Mapping[str, Any] | None = None,
    backend_hint: str = "",
    backend_family_hint: str = "",
    runtime_dir: str = "",
    memory_dir: str = "",
    instance_id: str = "",
    profile_name: str = "",
) -> dict[str, Any]:
    base = _mapping(base_status)
    extra_map = _mapping(extra)
    caps = _mapping(capabilities)
    identity = dict(
        extra_map.get("runtime_identity_display")
        if isinstance(extra_map.get("runtime_identity_display"), Mapping)
        else caps.get("runtime_identity_display")
        if isinstance(caps.get("runtime_identity_display"), Mapping)
        else base.get("runtime_identity_display")
        if isinstance(base.get("runtime_identity_display"), Mapping)
        else extra_map.get("runtime_identity")
        if isinstance(extra_map.get("runtime_identity"), Mapping)
        else caps.get("runtime_identity")
        if isinstance(caps.get("runtime_identity"), Mapping)
        else caps.get("identity")
        if isinstance(caps.get("identity"), Mapping)
        else {}
    )

    resolved_card = _mapping(caps.get("resolved_identity_card"))
    if not resolved_card:
        resolved_card = _mapping(extra_map.get("resolved_identity_card"))
    if not resolved_card:
        resolved_card = _mapping(base.get("resolved_identity_card"))

    identity["provider_name"] = _text(identity.get("provider_name"))
    identity["provider_plmn"] = _text(identity.get("provider_plmn"))
    provider_name, provider_plmn = _provider_identity(
        identity,
        base,
        extra_map.get("_canonical_status") if isinstance(extra_map.get("_canonical_status"), Mapping) else {},
        runtime_health,
        caps,
    )
    identity["provider_name"] = provider_name
    identity["provider_plmn"] = provider_plmn
    identity["router_model"] = _coalesce(identity.get("router_model"), identity.get("model"), base.get("router_model"), base.get("model"))
    identity["serial"] = _coalesce(identity.get("serial"), base.get("serial"))
    identity["imei"] = _text(identity.get("imei"))
    identity["imsi"] = _text(identity.get("imsi"))
    identity["hardware_version"] = _text(identity.get("hardware_version"))
    identity["software"] = _text(identity.get("software") or identity.get("software_version"))
    identity["software_version"] = _text(identity.get("software_version"))
    identity["webui"] = _coalesce(identity.get("webui"), identity.get("webui_version"), base.get("webui"), base.get("webui_version"))
    access_contract = _mapping(identity.get("access_type_contract"))
    if not access_contract:
        access_contract = _mapping(caps.get("access_type_contract"))
    if not access_contract:
        access_contract = _mapping(base.get("access_type_contract"))
    access_type = _coalesce(
        access_contract.get("display_name"),
        identity.get("access_type"),
        base.get("access_type"),
    )
    identity.pop("access", None)
    identity["access_type"] = access_type
    identity["access_type_id"] = _coalesce(
        access_contract.get("access_type_id"),
        identity.get("access_type_id"),
        base.get("access_type_id"),
    )
    for key in (
        "identity_code",
        "identity_status",
    ):
        identity[key] = _coalesce(identity.get(key), base.get(key))
    antenna_mode = _coalesce(
        identity.get("antenna_mode"),
        identity.get("antenna"),
    )
    identity["antenna_mode"] = antenna_mode
    identity["antenna"] = antenna_mode
    identity["antenna_mode_source"] = _text(identity.get("antenna_mode_source"))
    identity["config_version"] = _text(identity.get("config_version"))
    identity["lan_mac"] = _coalesce(identity.get("lan_mac"), base.get("lan_mac"))
    identity["firmware"] = _text(identity.get("firmware"))
    identity["wan_ip"] = _coalesce(identity.get("wan_ip"), base.get("wan_ip"))

    explicit_support_tier = caps.get("support_tier")
    if explicit_support_tier not in (None, ""):
        try:
            support_tier = int(explicit_support_tier)
        except Exception:
            support_tier = support_tier_from_payload(caps)
    else:
        support_tier = support_tier_from_payload(caps)
    identity["support_tier"] = support_tier
    identity["support_tier_label"] = _coalesce(
        caps.get("support_tier_label"),
        identity.get("support_tier_label"),
        support_tier_label(support_tier),
    )

    if resolved_card:
        identity["profile_id"] = _text(resolved_card.get("profile_id") or identity.get("profile_id"))
        identity["profile_provenance"] = _text(
            resolved_card.get("profile_provenance") or identity.get("profile_provenance")
        )
        identity["match_quality"] = _text(
            resolved_card.get("match_quality") or identity.get("match_quality")
        )
        identity["resolved_identity_exact"] = safe_bool(
            resolved_card.get("exact_identity", identity.get("resolved_identity_exact", False)),
            False,
        )
        identity["resolved_identity_provisional"] = safe_bool(
            resolved_card.get("provisional", identity.get("resolved_identity_provisional", False)),
            False,
        )
        identity["resolved_identity_provisional_reason"] = _text(
            resolved_card.get("provisional_reason")
            or identity.get("resolved_identity_provisional_reason")
        )
        identity["firmware_revalidation_required"] = safe_bool(
            resolved_card.get(
                "firmware_revalidation_required",
                identity.get("firmware_revalidation_required", False),
            ),
            False,
        )

    onboarding = _mapping(caps.get("onboarding_status"))
    if onboarding:
        identity["identity_state"] = _text(identity.get("identity_state") or onboarding.get("state"))
        identity["onboarding_state"] = _text(onboarding.get("state"))
        identity["onboarding_next_step"] = _text(onboarding.get("next_step"))
        identity["collect_evidence"] = safe_bool(onboarding.get("collect_evidence"), False)
        identity["calibration_refresh_required"] = safe_bool(
            onboarding.get("calibration_refresh_required"),
            False,
        )
        identity["match_quality"] = _text(identity.get("match_quality") or onboarding.get("match_quality"))
        identity["evidence_status"] = _text(onboarding.get("evidence_status"))
        identity["evidence_sufficient"] = safe_bool(onboarding.get("evidence_sufficient"), False)
        identity["action_evidence_status"] = _text(onboarding.get("action_evidence_status"))
        identity["action_evidence_summary"] = _text(onboarding.get("action_evidence_summary"))
        identity["action_evidence_unlock_level"] = _text(onboarding.get("action_evidence_unlock_level"))
        identity["action_evidence_focus"] = _text(onboarding.get("action_evidence_focus"))
        identity["action_evidence_next_step"] = _text(onboarding.get("action_evidence_next_step"))

    return identity
