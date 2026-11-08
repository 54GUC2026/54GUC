"""Canonical protected operator-context handoff contract.

The portable handoff needs to retain operator-owned setup facts for the same
physical modem, but it must never carry a copy of ``guc_config.xml``.  Raw
configuration also contains connection hints and can grow new secret fields
without the recovery path noticing.  This module is therefore the only owner
of the explicit, versioned allowlist used by next-start handoff publication.

The payload is sensitive local state.  It is not a support-bundle artifact and
must only be applied after the source and live modem identities match.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from arch4.customer_contracts.settings import (
    FoundationOperatorSettings,
    NGUIConnectionHint,
)
from arch4.foundation.operator_settings import FoundationOperatorSettingsStore
from arch4.customer_contracts.apn_profiles import MAX_APN_PROFILES, SavedApnProfile
from arch4.license_credential_store import load_license_credentials
from arch4.ngui.settings_store import NGUISettingsStore


PROTECTED_OPERATOR_CONTEXT_SCHEMA = "ProtectedOperatorContext.v3"
PREVIOUS_PROTECTED_OPERATOR_CONTEXT_SCHEMA = "ProtectedOperatorContext.v2"
PROTECTED_OPERATOR_CONTEXT_OWNER = "arch4.protected_operator_context"
PROTECTED_OPERATOR_CONTEXT_ARTIFACT = "protected_operator_context.json"

# Operator/profile facts intentionally retained for an identity-confirmed
# continuation. Plaintext passwords and unknown settings are absent by construction.
PROTECTED_TEXT_FIELDS: tuple[str, ...] = (
    "license_key",
    "license_nonce",
    "license_email",
    "license_tier",
    "operator_name",
    "customer_name",
    "user_name",
    "site_name",
    "site_address",
    "install_address",
    "address",
    "address_line1",
    "address_line2",
    "city",
    "contact_name",
    "contact_phone",
    "contact_email",
    "site_notes",
)

PROTECTED_TRAFFIC_PLAN_FIELDS: tuple[str, ...] = (
    "subscription_key",
    "scope_id",
    "modem_backend",
    "provider_name",
    "provider_plmn",
    "profile_id",
    "monthly_allowance_gb",
    "cycle_start_day",
    "override_enabled",
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema",
        "owner",
        "fields",
        "traffic_plan_scopes",
        "apn_profiles",
        "ngui_connection_hint",
        "raw_config_included",
        "connection_credentials_included",
        "content_sha256",
    }
)
_MAX_VALUE_CHARS = 4096
_MAX_TRAFFIC_SCOPES = 256


class ProtectedOperatorContextError(ValueError):
    """Raised when a protected context is corrupt or outside its allowlist."""


def _canonical_content(payload: Mapping[str, Any]) -> dict[str, Any]:
    schema = payload.get("schema", PROTECTED_OPERATOR_CONTEXT_SCHEMA)
    content = {
        "schema": schema,
        "owner": PROTECTED_OPERATOR_CONTEXT_OWNER,
        "fields": dict(payload.get("fields") or {}),
        "traffic_plan_scopes": list(payload.get("traffic_plan_scopes") or []),
        "ngui_connection_hint": dict(payload.get("ngui_connection_hint") or {}),
        "raw_config_included": False,
        "connection_credentials_included": False,
    }
    if schema == PROTECTED_OPERATOR_CONTEXT_SCHEMA:
        content["apn_profiles"] = list(payload.get("apn_profiles") or [])
    return content


def _content_sha256(content: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(content),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_protected_operator_context(
    storage_root: str | Path,
    *,
    profile_id: str = "",
) -> dict[str, Any] | None:
    """Project canonical owner stores into the protected handoff schema."""

    root = Path(storage_root).resolve()
    operator = FoundationOperatorSettingsStore(root).load()
    credential = load_license_credentials(root) or {}
    ngui = NGUISettingsStore(root).load()
    exact_profile_id = str(profile_id or "").strip()
    hint = (
        ngui.connection_hints.get(exact_profile_id)
        if exact_profile_id
        else None
    )
    license_fields = {
        target_name: str(credential.get(source_name) or "").strip()
        for source_name, target_name in (
            ("key", "license_key"),
            ("nonce", "license_nonce"),
            ("email", "license_email"),
            ("tier", "license_tier"),
        )
        if str(credential.get(source_name) or "").strip()
    }
    return build_protected_operator_context_from_slices(
        operator_settings=operator,
        connection_hint=hint,
        license_fields=license_fields,
    )


def build_protected_operator_context_from_slices(
    *,
    operator_settings: FoundationOperatorSettings,
    connection_hint: NGUIConnectionHint | None = None,
    license_fields: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Build the canonical context from already typed owner slices."""

    if type(operator_settings) is not FoundationOperatorSettings:
        raise TypeError(
            "protected context slices require FoundationOperatorSettings"
        )
    if (
        connection_hint is not None
        and type(connection_hint) is not NGUIConnectionHint
    ):
        raise TypeError("protected context slices require NGUIConnectionHint")
    source_license = dict(license_fields or {})
    allowed_license = {
        "license_key",
        "license_nonce",
        "license_email",
        "license_tier",
    }
    if set(source_license) - allowed_license:
        raise ProtectedOperatorContextError(
            "protected context slices have unknown license fields"
        )

    fields = {
        name: str(value or "").strip()[:_MAX_VALUE_CHARS]
        for name, value in operator_settings.operator_fields.items()
        if name in PROTECTED_TEXT_FIELDS and str(value or "").strip()
    }
    for name, raw_value in source_license.items():
        value = str(raw_value or "").strip()
        if value:
            fields[str(name)] = value[:_MAX_VALUE_CHARS]

    scopes: list[dict[str, str]] = []
    for plan in list(operator_settings.traffic_plans.values())[:_MAX_TRAFFIC_SCOPES]:
        row = {
            "subscription_key": plan.subscription_key,
            "scope_id": plan.scope_id,
            "modem_backend": plan.modem_backend,
            "provider_name": plan.provider_name,
            "provider_plmn": plan.provider_plmn,
            "profile_id": plan.profile_id,
            "monthly_allowance_gb": f"{plan.monthly_allowance_gb:g}",
            "cycle_start_day": str(plan.cycle_start_day),
            "override_enabled": "true" if plan.override_enabled else "false",
        }
        scopes.append({name: value for name, value in row.items() if value})

    for raw in operator_settings.unassigned_traffic_plans.values():
        if len(scopes) >= _MAX_TRAFFIC_SCOPES:
            break
        scopes.append({name: str(value).lower() if isinstance(value, bool) else str(value)
                       for name, value in raw.items() if name in PROTECTED_TRAFFIC_PLAN_FIELDS and value is not None})

    hint: dict[str, str] = {}
    if connection_hint is not None:
        hint = {
            "profile_id": connection_hint.profile_id,
            "address": connection_hint.address,
            "username": connection_hint.username,
        }

    profiles = [profile.to_mapping() for _, profile in sorted(operator_settings.apn_profiles.items())]
    if not fields and not scopes and not hint and not profiles:
        return None
    content = _canonical_content(
        {
            "fields": fields,
            "traffic_plan_scopes": scopes,
            "apn_profiles": profiles,
            "ngui_connection_hint": hint,
        }
    )
    return {**content, "content_sha256": _content_sha256(content)}


def validate_protected_operator_context(payload: object) -> dict[str, Any]:
    """Validate and normalize a protected context without accepting extras."""

    if not isinstance(payload, Mapping):
        raise ProtectedOperatorContextError("protected context must be an object")
    unknown_top = set(payload) - _TOP_LEVEL_FIELDS
    if unknown_top:
        raise ProtectedOperatorContextError(
            "protected context has unknown top-level fields"
        )
    schema = payload.get("schema")
    if schema not in {PROTECTED_OPERATOR_CONTEXT_SCHEMA, PREVIOUS_PROTECTED_OPERATOR_CONTEXT_SCHEMA}:
        raise ProtectedOperatorContextError("protected context schema mismatch")
    if schema == PREVIOUS_PROTECTED_OPERATOR_CONTEXT_SCHEMA and "apn_profiles" in payload:
        raise ProtectedOperatorContextError("previous protected context cannot contain APN profiles")
    if str(payload.get("owner") or "") != PROTECTED_OPERATOR_CONTEXT_OWNER:
        raise ProtectedOperatorContextError("protected context owner mismatch")
    if payload.get("raw_config_included") is not False:
        raise ProtectedOperatorContextError(
            "protected context cannot include raw config"
        )
    if payload.get("connection_credentials_included") is not False:
        raise ProtectedOperatorContextError(
            "protected context cannot include connection credentials"
        )

    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, Mapping):
        raise ProtectedOperatorContextError(
            "protected context fields must be an object"
        )
    unknown_fields = set(raw_fields) - set(PROTECTED_TEXT_FIELDS)
    if unknown_fields:
        raise ProtectedOperatorContextError(
            "protected context has non-allowlisted fields"
        )
    fields: dict[str, str] = {}
    for name, raw_value in raw_fields.items():
        if not isinstance(raw_value, str):
            raise ProtectedOperatorContextError("protected context field must be text")
        value = raw_value.strip()
        if len(value) > _MAX_VALUE_CHARS:
            raise ProtectedOperatorContextError("protected context field is oversized")
        if value:
            fields[str(name)] = value

    raw_scopes = payload.get("traffic_plan_scopes")
    if not isinstance(raw_scopes, list) or len(raw_scopes) > _MAX_TRAFFIC_SCOPES:
        raise ProtectedOperatorContextError("protected traffic scopes are invalid")
    scopes: list[dict[str, str]] = []
    for raw_scope in raw_scopes:
        if not isinstance(raw_scope, Mapping):
            raise ProtectedOperatorContextError(
                "protected traffic scope must be an object"
            )
        if set(raw_scope) - set(PROTECTED_TRAFFIC_PLAN_FIELDS):
            raise ProtectedOperatorContextError(
                "protected traffic scope has unknown fields"
            )
        scope: dict[str, str] = {}
        for name, raw_value in raw_scope.items():
            if not isinstance(raw_value, str):
                raise ProtectedOperatorContextError(
                    "protected traffic value must be text"
                )
            value = raw_value.strip()
            if len(value) > _MAX_VALUE_CHARS:
                raise ProtectedOperatorContextError(
                    "protected traffic value is oversized"
                )
            if value:
                scope[str(name)] = value
        if not scope.get("scope_id"):
            raise ProtectedOperatorContextError("protected traffic scope has no scope_id")
        scopes.append(scope)

    raw_hint = payload.get("ngui_connection_hint")
    if not isinstance(raw_hint, Mapping):
        raise ProtectedOperatorContextError(
            "protected NGUI connection hint must be an object"
        )
    if set(raw_hint) - {"profile_id", "address", "username"}:
        raise ProtectedOperatorContextError(
            "protected NGUI connection hint has unknown fields"
        )
    hint: dict[str, str] = {}
    for name, raw_value in raw_hint.items():
        if not isinstance(raw_value, str):
            raise ProtectedOperatorContextError(
                "protected NGUI connection hint value must be text"
            )
        value = raw_value.strip()
        if len(value) > _MAX_VALUE_CHARS:
            raise ProtectedOperatorContextError(
                "protected NGUI connection hint value is oversized"
            )
        if value:
            hint[str(name)] = value
    if hint and not hint.get("profile_id"):
        raise ProtectedOperatorContextError(
            "protected NGUI connection hint requires exact profile_id"
        )
    if hint and not hint.get("address"):
        raise ProtectedOperatorContextError(
            "protected NGUI connection hint requires address"
        )

    profiles = payload.get("apn_profiles", [])
    if not isinstance(profiles, list) or len(profiles) > MAX_APN_PROFILES:
        raise ProtectedOperatorContextError("protected APN profiles are invalid")
    try:
        profiles = [SavedApnProfile.from_mapping(row).to_mapping() for row in profiles]
    except (ValueError, TypeError) as exc:
        raise ProtectedOperatorContextError("protected APN profile is invalid") from exc
    if len({row["profile_id"] for row in profiles}) != len(profiles):
        raise ProtectedOperatorContextError("duplicate protected APN profile")
    content = _canonical_content(
        {
            "schema": schema,
            "fields": fields,
            "traffic_plan_scopes": scopes,
            "apn_profiles": profiles,
            "ngui_connection_hint": hint,
        }
    )
    expected = _content_sha256(content)
    if str(payload.get("content_sha256") or "") != expected:
        raise ProtectedOperatorContextError("protected context checksum mismatch")
    return {**content, "content_sha256": expected}


def encode_protected_operator_context(payload: object) -> bytes:
    clean = validate_protected_operator_context(payload)
    return (
        json.dumps(clean, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def decode_protected_operator_context(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ProtectedOperatorContextError(
            "protected context is not valid UTF-8 JSON"
        ) from exc
    return validate_protected_operator_context(payload)
