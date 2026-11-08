"""Import the Foundation part of external settings. This boundary may seed Core defaults,
NGUI hints, and operator settings. It cannot create or save Intelligence state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arch4.atomic_io import atomic_write_json
from arch4.core.runtime_defaults import CoreRuntimeDefaultsStore
from arch4.external_settings_decoder import (
    ExternalSettingsImportError,
    decode_external_settings_source,
)
from arch4.foundation.operator_settings import FoundationOperatorSettingsStore
from arch4.ngui.settings_store import NGUISettingsStore


SCHEMA = "FoundationExternalSettingsImportReceipt.v1"
OWNER = "54guc.foundation.external_settings_import"
RECEIPT_FILENAME = "foundation_external_settings_import_receipt.json"


def _load_receipt(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ExternalSettingsImportError(
            f"Foundation external settings receipt cannot be read: {exc}"
        ) from exc
    expected = {
        "schema",
        "owner",
        "source_name",
        "source_sha256",
        "owner_store_actions",
        "ignored_input_fields",
        "deferred_license_candidate",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ExternalSettingsImportError(
            "Foundation external settings receipt fields mismatch"
        )
    if payload["schema"] != SCHEMA or payload["owner"] != OWNER:
        raise ExternalSettingsImportError(
            "Foundation external settings receipt owner mismatch"
        )
    actions = payload.get("owner_store_actions")
    if not isinstance(actions, dict) or set(actions) != {
        "core",
        "ngui",
        "foundation",
    }:
        raise ExternalSettingsImportError(
            "Foundation external settings owner actions mismatch"
        )
    return payload


def import_foundation_external_settings_once(
    *,
    source_root: str | Path,
    storage_root: str | Path,
) -> dict[str, Any]:
    """Seed only the three Foundation-composition owner stores once."""

    source = Path(source_root).resolve()
    storage = Path(storage_root).resolve()
    receipt_path = storage / RECEIPT_FILENAME
    receipt = _load_receipt(receipt_path)
    decoded = decode_external_settings_source(source)
    if decoded is None:
        if receipt is not None:
            return dict(receipt)
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            "source_name": "",
            "source_sha256": "",
            "owner_store_actions": {},
            "ignored_input_fields": [],
            "deferred_license_candidate": False,
        }
    if receipt is not None:
        if (
            receipt["source_name"] != decoded.source_name
            or receipt["source_sha256"] != decoded.source_sha256
        ):
            raise ExternalSettingsImportError(
                "external settings changed after Foundation owner import; "
                "edit through the owning service"
            )
        return dict(receipt)

    stores = (
        ("core", CoreRuntimeDefaultsStore(storage), decoded.core),
        ("ngui", NGUISettingsStore(storage), decoded.ngui),
        (
            "foundation",
            FoundationOperatorSettingsStore(storage),
            decoded.foundation,
        ),
    )
    actions: dict[str, str] = {}
    for name, store, value in stores:
        if store.exists:
            actions[name] = "preserved_existing_owner_store"
        else:
            store.save(value)
            actions[name] = "seeded_from_external_input"

    payload = {
        "schema": SCHEMA,
        "owner": OWNER,
        "source_name": decoded.source_name,
        "source_sha256": decoded.source_sha256,
        "owner_store_actions": actions,
        "ignored_input_fields": list(decoded.ignored_input_fields),
        "deferred_license_candidate": decoded.deferred_license_candidate,
    }
    atomic_write_json(receipt_path, payload, sort_keys=True, trailing_newline=True)
    return payload


__all__ = (
    "ExternalSettingsImportError",
    "import_foundation_external_settings_once",
)
