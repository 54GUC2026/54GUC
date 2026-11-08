"""Build deterministic Modem Live runtime identity values."""

from __future__ import annotations

import time
from typing import Any, Mapping

from arch4.modem.catalog import (
    describe_backend_support,
    describe_runtime_profile,
)
from arch4.modem.identity import normalize_modem_identity_fields
from arch4.runtime_identity import (
    build_runtime_session_scope,
    coerce_provider_plmn_text,
    merge_runtime_session_scope,
    runtime_device_namespace_key,
    runtime_session_scope_key,
)
from arch4.runtime_identity_card import coerce_bool


def _best_status_text(base_status: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        text = str(base_status.get(key) or "").strip()
        if text:
            return text
    return ""


def build_verified_modem_identity_section(
    *,
    runtime_identity: Mapping[str, Any] | None = None,
    resolved_identity_card: Mapping[str, Any] | None = None,
    previous_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build identity from the verified current read without profile inference."""

    del resolved_identity_card, previous_scope
    identity = runtime_identity if isinstance(runtime_identity, Mapping) else {}
    scope = build_runtime_session_scope(identity)
    backend = str(scope.get("backend") or identity.get("backend") or "").strip()
    model = str(scope.get("model") or identity.get("model") or "").strip()
    firmware = str(scope.get("firmware") or identity.get("firmware") or "").strip()
    serial = str(scope.get("serial") or identity.get("serial") or "").strip()
    imei = str(scope.get("imei") or identity.get("imei") or "").strip()
    imsi = str(scope.get("imsi") or identity.get("imsi") or "").strip()
    sim_id = str(scope.get("sim_id") or identity.get("sim_id") or "").strip()
    # Login requires a permanent hardware identifier. IMSI and SIM identify the
    # subscription, while model and firmware identify a product family. None of
    # them may complete login. Monitoring continues while identity is pending.
    if not backend or not (serial or imei):
        return {}
    backend_family = str(
        scope.get("backend_family") or identity.get("backend_family") or ""
    ).strip()
    provider_name = str(
        scope.get("provider_name") or identity.get("provider_name") or ""
    ).strip()
    provider_plmn = str(
        scope.get("provider_plmn") or identity.get("provider_plmn") or ""
    ).strip()
    identity_key = str(runtime_device_namespace_key(scope) or "").strip()
    return {
        "schema": "verified_modem_identity.v1",
        "manufacturer": str(identity.get("vendor") or "").strip(),
        "backend": backend,
        "backend_family": backend_family,
        "model": model,
        "serial": serial,
        "imei": imei,
        "imsi_or_sim_id": imsi or sim_id,
        "firmware": firmware,
        "software": str(identity.get("software_version") or "").strip(),
        "webui": str(identity.get("webui") or "").strip(),
        "hardware_version": str(identity.get("hardware_version") or "").strip(),
        "provider_name": provider_name,
        "provider_plmn": provider_plmn,
        "identity_key": identity_key,
        "verified_at_ts": time.time(),
        "source": "live_modem_readback",
    }


def runtime_identity_scope_lock_status(
    *,
    runtime_identity: Mapping[str, Any] | None = None,
    resolved_identity_card: Mapping[str, Any] | None = None,
    previous_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    identity = runtime_identity if isinstance(runtime_identity, Mapping) else {}
    card = resolved_identity_card if isinstance(resolved_identity_card, Mapping) else {}
    scope = build_runtime_session_scope(identity)
    if previous_scope:
        scope = merge_runtime_session_scope(previous_scope, scope)
    exact = coerce_bool(card.get("exact_identity")) or coerce_bool(
        identity.get("resolved_identity_exact")
    )
    provisional = coerce_bool(card.get("provisional")) or coerce_bool(
        identity.get("resolved_identity_provisional")
    )
    profile_id = str(
        card.get("profile_id") or identity.get("resolved_profile_id") or ""
    ).strip()
    reason = str(
        card.get("provisional_reason")
        or identity.get("resolved_identity_provisional_reason")
        or ""
    ).strip()
    strong_ids = any(
        str(scope.get(key) or "").strip()
        for key in ("serial", "imei")
    )
    model = str(scope.get("model") or "").strip()
    firmware = str(scope.get("firmware") or "").strip()
    provider = str(
        scope.get("provider_plmn") or scope.get("provider_name") or ""
    ).strip()
    verified_identity = build_verified_modem_identity_section(
        runtime_identity=identity,
        resolved_identity_card=card,
        previous_scope=scope,
    )
    locked = bool(verified_identity)
    if not locked and not reason:
        if not (model or firmware or strong_ids):
            reason = "identity_readback_unavailable"
        elif provisional:
            reason = "resolved_identity_provisional"
        elif not exact:
            reason = "resolved_identity_not_exact"
        else:
            reason = "identity_scope_pending"
    return {
        "schema": "runtime_identity_scope_lock.v1",
        "locked": locked,
        "exact_identity": exact,
        "provisional": provisional,
        "reason": "" if locked else reason,
        "profile_id": profile_id,
        "has_strong_id": bool(strong_ids),
        "scope": dict(scope),
        "scope_key": runtime_device_namespace_key(scope),
        "model": model,
        "firmware": firmware,
        "provider": provider,
        "verified_modem_identity": dict(verified_identity),
    }


def build_resolved_identity_card_section(
    base_status: Mapping[str, Any],
) -> dict[str, Any]:
    base = normalize_modem_identity_fields(dict(base_status or {}))
    backend = str(base.get("backend") or "").strip().lower()
    vendor = str(base.get("vendor") or "").strip()
    model = _best_status_text(base, "model", "router_model")
    firmware = _best_status_text(base, "firmware", "software_version", "software")
    hardware_version = str(base.get("hardware_version") or "").strip()
    software_version = str(base.get("software_version") or "").strip()
    webui = str(base.get("webui") or "").strip()
    config_version = str(base.get("config_version") or "").strip()
    provider_name = str(
        base.get("provider_name") or base.get("provider") or ""
    ).strip()
    provider_plmn = coerce_provider_plmn_text(base.get("provider_plmn"))
    if not provider_plmn:
        provider_plmn = coerce_provider_plmn_text(base.get("plmn"))
    os_name = str(base.get("os_name") or "").strip()
    os_version = str(base.get("os_version") or software_version or "").strip()
    if not any(
        (
            backend,
            vendor,
            model,
            firmware,
            hardware_version,
            software_version,
            webui,
            config_version,
            provider_name,
            provider_plmn,
            os_name,
            os_version,
        )
    ):
        return {}
    backend_support = describe_backend_support(backend) if backend else {}
    runtime_profile = describe_runtime_profile(
        backend=backend,
        vendor=vendor,
        model=model,
        firmware=firmware,
        provider_name=provider_name,
        provider_plmn=provider_plmn,
        os_name=os_name,
        os_version=os_version,
    )
    capability_profile = {
        "profile_id": str(runtime_profile.get("profile_id") or ""),
        "provenance": str(runtime_profile.get("provenance") or ""),
        "field_trust": dict(runtime_profile.get("field_trust") or {}),
    }
    profile_id = str(runtime_profile.get("profile_id") or "")
    match_quality = str(runtime_profile.get("match_quality") or "").strip().lower()
    firmware_revalidation_required = coerce_bool(
        runtime_profile.get("firmware_revalidation_required", False)
    )
    exact_identity = bool(
        profile_id
        and match_quality == "exact_firmware"
        and not firmware_revalidation_required
    )
    if not profile_id:
        provisional_reason = "no_profile_match"
    elif firmware_revalidation_required:
        provisional_reason = "firmware_revalidation_required"
    elif match_quality == "family_baseline":
        provisional_reason = "family_baseline"
    elif match_quality == "model_specific":
        provisional_reason = "model_specific_needs_exact_firmware"
    else:
        provisional_reason = ""
    identity_parts = (
        ("backend", backend),
        ("model", model),
        ("hardware", hardware_version),
        ("firmware", firmware),
        ("software", software_version),
        ("webui", webui),
        ("config", config_version),
        ("provider_plmn", provider_plmn),
        ("os_name", os_name),
        ("os_version", os_version),
    )
    return {
        "version": "v1",
        "identity_key": "|".join(
            f"{key}={value}" for key, value in identity_parts if value
        ),
        "backend": backend,
        "vendor": vendor
        or str(runtime_profile.get("vendor") or backend_support.get("vendor") or ""),
        "family": str(
            runtime_profile.get("family") or backend_support.get("family") or ""
        ),
        "support_level": str(backend_support.get("support_level") or ""),
        "implemented": coerce_bool(backend_support.get("implemented", False)),
        "model": model,
        "hardware_version": hardware_version,
        "firmware": firmware,
        "software_version": software_version,
        "webui": webui,
        "config_version": config_version,
        "os_name": os_name,
        "os_version": os_version,
        "provider_name": provider_name,
        "provider_plmn": provider_plmn,
        "profile_id": profile_id,
        "profile_display_name": str(runtime_profile.get("display_name") or ""),
        "profile_provenance": str(runtime_profile.get("provenance") or ""),
        "scheduler_signal_mode": str(
            runtime_profile.get("scheduler_signal_mode") or ""
        ),
        "match_quality": match_quality,
        "coverage_level": str(runtime_profile.get("coverage_level") or ""),
        "firmware_revalidation_required": firmware_revalidation_required,
        "known_specific_model_variants": coerce_bool(
            runtime_profile.get("known_specific_model_variants", False)
        ),
        "backend_model_revalidation_required": coerce_bool(
            runtime_profile.get("backend_model_revalidation_required", False)
        ),
        "exact_identity": exact_identity,
        "provisional": not exact_identity,
        "provisional_reason": provisional_reason,
        "field_trust_profile_id": str(capability_profile.get("profile_id") or ""),
        "field_trust_provenance": str(capability_profile.get("provenance") or ""),
    }


__all__ = (
    "build_resolved_identity_card_section",
    "build_verified_modem_identity_section",
    "runtime_identity_scope_lock_status",
)
