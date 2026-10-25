"""Intelligence-owned import of its external preference slice."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arch4.atomic_io import atomic_write_json
from arch4.external_settings_decoder import (
    ExternalSettingsImportError,
    decode_external_settings_source,
)
from arch4.intelligence.preferences import IntelligencePreferencesStore


SCHEMA = "IntelligenceExternalSettingsImportReceipt.v1"
OWNER = "54guc.intelligence.external_settings_import"
RECEIPT_FILENAME = "intelligence_external_settings_import_receipt.json"


def _load_receipt(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ExternalSettingsImportError(
            f"Intelligence external settings receipt cannot be read: {exc}"
        ) from exc
    expected = {
        "schema",
        "owner",
        "source_name",
        "source_sha256",
        "owner_store_action",
        "ignored_input_fields",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ExternalSettingsImportError(
            "Intelligence external settings receipt fields mismatch"
        )
    if payload["schema"] != SCHEMA or payload["owner"] != OWNER:
        raise ExternalSettingsImportError(
            "Intelligence external settings receipt owner mismatch"
        )
    return payload


def import_intelligence_external_settings_once(
    storage_root: str | Path,
) -> dict[str, Any]:
    """Seed only Intelligence preferences; never mutate another owner."""

    root = Path(storage_root).resolve()
    receipt_path = root / RECEIPT_FILENAME
    receipt = _load_receipt(receipt_path)
    decoded = decode_external_settings_source(root)
    if decoded is None:
        if receipt is not None:
            return dict(receipt)
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            "source_name": "",
            "source_sha256": "",
            "owner_store_action": "not_applicable",
            "ignored_input_fields": [],
        }
    if receipt is not None:
        if (
            receipt["source_name"] != decoded.source_name
            or receipt["source_sha256"] != decoded.source_sha256
        ):
            raise ExternalSettingsImportError(
                "external settings changed after Intelligence owner import; "
                "edit through the owning service"
            )
        return dict(receipt)

    store = IntelligencePreferencesStore(root)
    if store.exists:
        action = "preserved_existing_owner_store"
    else:
        store.save(decoded.intelligence)
        action = "seeded_from_external_input"
    payload = {
        "schema": SCHEMA,
        "owner": OWNER,
        "source_name": decoded.source_name,
        "source_sha256": decoded.source_sha256,
        "owner_store_action": action,
        "ignored_input_fields": list(decoded.ignored_input_fields),
    }
    atomic_write_json(receipt_path, payload, sort_keys=True, trailing_newline=True)
    return payload


__all__ = (
    "ExternalSettingsImportError",
    "import_intelligence_external_settings_once",
)
