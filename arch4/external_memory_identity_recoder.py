"""Offline adapter for recoding old history into identity-code context.

This is an import-boundary adapter only. Runtime SmartLoop does not read old
backend-family context; old history must be recoded before it can seed priors.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from arch4.modem.identity_code import (
    EXACT_IDENTITY_STATUS,
    PROVISIONAL_IDENTITY_STATUS,
    UNKNOWN_IDENTITY_CODE,
    UNKNOWN_IDENTITY_STATUS,
)
from arch4.modem_live.compatibility_identity import DecodeCatalogReplica


SCHEMA = "external_memory_identity_recode.v2"
OWNER = "arch4.external_memory_identity_recoder"

_IDENTITY_CONTAINER_KEYS = frozenset(
    {
        "identity",
        "scope",
        "target_identity",
        "runtime_identity",
        "resolved_identity_card",
        "modem_identity",
    }
)


def recode_external_memory_payload(
    payload: Mapping[str, Any] | None,
    *,
    decode_catalog_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copy of a history/import payload with identity-code context."""

    replica = _catalog_replica(decode_catalog_snapshot)
    data = _recode_value(dict(payload or {}), replica=replica)
    if isinstance(data, dict):
        current = replica.current if replica is not None else None
        data.setdefault(
            "identity_recode",
            {
                "schema": SCHEMA,
                "owner": OWNER,
                "catalog_version": current.catalog_version if current is not None else 0,
                "catalog_checksum": current.catalog_checksum if current is not None else "",
            },
        )
    return data if isinstance(data, dict) else {}


def recode_identity_mapping(
    identity: Mapping[str, Any] | None,
    *,
    decode_catalog_snapshot: Mapping[str, Any] | None = None,
    replica: DecodeCatalogReplica | None = None,
) -> dict[str, Any]:
    """Normalize imported identity without minting a Modem Live identity code."""

    data = dict(identity or {})
    if not data:
        return {}
    catalog_replica = replica or _catalog_replica(decode_catalog_snapshot)
    profile_id = _first_text(
        data.get("profile_id"),
        data.get("resolved_profile_id"),
        data.get("field_trust_profile_id"),
    )
    model = _first_text(data.get("model"), data.get("router_model"), data.get("device_model"))
    firmware = _first_text(
        data.get("firmware"),
        data.get("software_version"),
        data.get("software"),
        data.get("os_version"),
    )
    code = _first_text(data.get("identity_code"))
    legacy_code = code if _valid_legacy_identity_code(code) else ""
    if legacy_code and catalog_replica is not None:
        migration = catalog_replica.legacy_migration(
            legacy_code,
            evidence=_migration_evidence(data),
        )
        if migration is not None:
            code = str(migration["target_identity_code"])
            data["legacy_identity_migration"] = {
                "legacy_schema": str(migration.get("legacy_schema") or ""),
                "legacy_code": legacy_code,
                "target_identity_code": code,
                "evidence_guard": str(migration.get("evidence_guard") or ""),
                "review_reference": str(migration.get("review_reference") or ""),
            }
    catalog_verified = _valid_identity_code(code) and _catalog_identity_matches(
        catalog_replica,
        code,
        data,
    )
    if not catalog_verified:
        code = UNKNOWN_IDENTITY_CODE
    status = _first_text(data.get("identity_status"))
    if code == UNKNOWN_IDENTITY_CODE:
        status = PROVISIONAL_IDENTITY_STATUS if any((profile_id, model, firmware)) else UNKNOWN_IDENTITY_STATUS
        data["identity_quarantine_reason"] = (
            "legacy_identity_evidence_or_catalog_mismatch"
            if legacy_code
            else "modem_live_catalog_validation_required"
        )
    else:
        status = EXACT_IDENTITY_STATUS
    data["identity_code"] = code
    data["identity_status"] = status
    current_catalog = catalog_replica.current if catalog_replica is not None else None
    if code != UNKNOWN_IDENTITY_CODE and current_catalog is not None:
        data["identity_schema"] = "ModemIdentityKey.v1"
        data["catalog_version"] = current_catalog.catalog_version
        data["catalog_checksum"] = current_catalog.catalog_checksum
        data.pop("identity_quarantine_reason", None)
    if profile_id:
        data["profile_id"] = profile_id
    if model:
        data["model"] = model
    if firmware:
        data["firmware"] = firmware
    return data


def _recode_value(
    value: Any,
    *,
    key_hint: str = "",
    replica: DecodeCatalogReplica | None = None,
) -> Any:
    if key_hint in {
        "decode_catalog_snapshot",
        "decoded_identity_catalogs",
        "evidence_scope",
        "evidence_scopes",
        "evidence_scope_spans",
        "source_evidence_scope",
        "source_evidence_scopes",
        "source_evidence_scope_spans",
        "target_evidence_scope",
        "confirmed_evidence_scope",
        "formula_vector",
    }:
        # Canonical evidence and decoded-catalog rows are immutable read-only
        # evidence.  Recoding may add an adjacent interpreted identity, but it
        # must never rewrite checksummed source facts in place.
        return copy.deepcopy(value)
    if isinstance(value, list):
        return [_recode_value(item, replica=replica) for item in value]
    if not isinstance(value, Mapping):
        return value
    data = {
        str(key): _recode_value(item, key_hint=str(key), replica=replica)
        for key, item in value.items()
    }
    if key_hint in _IDENTITY_CONTAINER_KEYS or _looks_like_identity(data):
        data = recode_identity_mapping(data, replica=replica)
    if isinstance(data.get("identity"), Mapping) and isinstance(data.get("scope"), Mapping):
        identity = recode_identity_mapping(data.get("identity"), replica=replica)
        scope = recode_identity_mapping(data.get("scope"), replica=replica)
        identity_code = str(identity.get("identity_code") or "")
        scope_code = str(scope.get("identity_code") or "")
        if _valid_identity_code(identity_code) and (
            not _valid_identity_code(scope_code)
            or (
                str(identity.get("identity_status") or "") == EXACT_IDENTITY_STATUS
                and str(scope.get("identity_status") or "") != EXACT_IDENTITY_STATUS
            )
        ):
            scope["identity_code"] = identity.get("identity_code")
            scope["identity_status"] = identity.get("identity_status", "")
            if identity.get("model") and not scope.get("model"):
                scope["model"] = identity.get("model")
            if identity.get("profile_id") and not scope.get("profile_id"):
                scope["profile_id"] = identity.get("profile_id")
            if identity.get("firmware") and not scope.get("firmware"):
                scope["firmware"] = identity.get("firmware")
            for key in ("identity_schema", "catalog_version", "catalog_checksum"):
                if identity.get(key) not in (None, ""):
                    scope[key] = identity.get(key)
            scope.pop("identity_quarantine_reason", None)
        data["identity"] = identity
        data["scope"] = scope
        if data.get("scope_tier"):
            data["scope_tier"] = _identity_scope_tier(data["scope"], str(data.get("scope_tier") or ""))
    action_params = data.get("action_params")
    if isinstance(action_params, Mapping):
        data["action_params"] = _recode_action_params(action_params, data, replica=replica)
    return data


def _recode_action_params(
    params: Mapping[str, Any],
    parent: Mapping[str, Any],
    *,
    replica: DecodeCatalogReplica | None,
) -> dict[str, Any]:
    out = dict(params)
    if _valid_identity_code(out.get("_tsar_context_identity_code")):
        return out
    sibling_identity = parent.get("identity") if isinstance(parent.get("identity"), Mapping) else {}
    identity = recode_identity_mapping(
        {
            "identity_code": _first_text(parent.get("identity_code"), sibling_identity.get("identity_code")),
            "identity_status": _first_text(parent.get("identity_status"), sibling_identity.get("identity_status")),
            "model": _first_text(parent.get("model"), sibling_identity.get("model"), out.get("_tsar_context_model")),
            "profile_id": _first_text(
                parent.get("profile_id"),
                sibling_identity.get("profile_id"),
                out.get("_tsar_context_profile_id"),
            ),
            "firmware": _first_text(
                parent.get("firmware"),
                sibling_identity.get("firmware"),
                out.get("_tsar_context_firmware"),
            ),
            "backend": _first_text(parent.get("backend"), sibling_identity.get("backend"), parent.get("modem_backend")),
        },
        replica=replica,
    )
    code = str(identity.get("identity_code") or "")
    if _valid_identity_code(code):
        out["_tsar_context_identity_code"] = code
        out["_tsar_context_identity_status"] = str(identity.get("identity_status") or "")
        out["_tsar_context_model"] = str(identity.get("model") or "")
    out.pop("_tsar_context_backend_family", None)
    return out


def _looks_like_identity(data: Mapping[str, Any]) -> bool:
    return any(
        key in data
        for key in (
            "identity_code",
            "profile_id",
            "resolved_profile_id",
            "model",
            "firmware",
            "software_version",
            "webui",
            "hardware_version",
        )
    )


def _identity_scope_tier(scope: Mapping[str, Any], current: str) -> str:
    if not _valid_identity_code(scope.get("identity_code")):
        return current
    if scope.get("provider_plmn") and scope.get("serving_band") and scope.get("earfcn") and scope.get("pci"):
        return "identity_provider_cell"
    if scope.get("provider_plmn") and scope.get("serving_band"):
        return "identity_provider_band"
    return "identity"


def _valid_identity_code(value: Any) -> bool:
    text = str(value or "").strip()
    if not text.startswith("0x") or len(text) != 18:
        return False
    try:
        int(text[2:], 16)
    except ValueError:
        return False
    return text != UNKNOWN_IDENTITY_CODE


def _valid_legacy_identity_code(value: Any) -> bool:
    text = str(value or "").strip()
    if not text.startswith("0x") or len(text) != 10:
        return False
    try:
        return int(text[2:], 16) != 0
    except ValueError:
        return False


def _catalog_replica(
    decode_catalog_snapshot: Mapping[str, Any] | None,
) -> DecodeCatalogReplica | None:
    if not isinstance(decode_catalog_snapshot, Mapping):
        return None
    replica = DecodeCatalogReplica()
    replica.install(decode_catalog_snapshot)
    return replica


def _migration_evidence(data: Mapping[str, Any]) -> dict[str, str]:
    return {
        "manufacturer": _first_text(data.get("manufacturer"), data.get("vendor")),
        "model": _first_text(data.get("model"), data.get("router_model"), data.get("device_model")),
        "hardware_sku": _first_text(data.get("hardware_sku"), data.get("hardware_version")),
        "software_version": _first_text(data.get("software_version"), data.get("software")),
        "firmware_version": _first_text(data.get("firmware_version"), data.get("firmware")),
        "profile_id": _first_text(data.get("profile_id"), data.get("resolved_profile_id")),
        "backend": _first_text(data.get("backend"), data.get("modem_backend")),
        "webui": _first_text(data.get("webui")),
    }


def _catalog_identity_matches(
    replica: DecodeCatalogReplica | None,
    identity_code: str,
    evidence: Mapping[str, Any],
) -> bool:
    if replica is None or not _valid_identity_code(identity_code):
        return False
    row = replica.identity_entry(identity_code)
    if row is None:
        return False
    expected = row.get("normalized_tuple") if isinstance(row.get("normalized_tuple"), Mapping) else {}
    observed = _migration_evidence(evidence)
    required_observed = {
        "manufacturer": observed["manufacturer"],
        "model": observed["model"],
        "hardware_sku": observed["hardware_sku"],
        "software_version": observed["software_version"],
        "firmware_version": observed["firmware_version"],
    }
    if not all(required_observed.values()):
        return False
    return all(
        str(required_observed[key]).strip().casefold()
        == str(expected.get(key) or "").strip().casefold()
        for key in required_observed
    )


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "OWNER",
    "SCHEMA",
    "recode_external_memory_payload",
    "recode_identity_mapping",
]
