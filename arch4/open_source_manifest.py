"""Canonical validator/reader for the generated dependency manifest."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "OpenSourceDependencyManifest.v1"
OWNER = "tools.generate_open_source_manifest"
FILENAME = "open_source_manifest.json"


class OpenSourceManifestError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OpenSourceComponent:
    name: str
    license: str
    use: str
    category: str
    evidence: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "license": self.license,
            "use": self.use,
            "category": self.category,
            "evidence": self.evidence,
        }


def component_fingerprint(components: list[dict[str, str]]) -> str:
    identity = json.dumps(
        components,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def validate_open_source_manifest(
    payload: object,
) -> tuple[OpenSourceComponent, ...]:
    if not isinstance(payload, Mapping):
        raise OpenSourceManifestError("open_source_manifest_object_required")
    if payload.get("schema") != SCHEMA or payload.get("owner") != OWNER:
        raise OpenSourceManifestError("open_source_manifest_owner_mismatch")
    if set(payload) != {
        "schema",
        "owner",
        "catalog_sha256",
        "generated_from",
        "components",
    }:
        raise OpenSourceManifestError("open_source_manifest_fields_mismatch")
    generated_from = payload.get("generated_from")
    if not isinstance(generated_from, list) or not all(
        isinstance(item, str) and item.strip() for item in generated_from
    ):
        raise OpenSourceManifestError("open_source_manifest_inputs_required")
    rows = payload.get("components")
    if not isinstance(rows, list) or not rows:
        raise OpenSourceManifestError("open_source_manifest_components_required")
    components: list[OpenSourceComponent] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise OpenSourceManifestError(
                f"open_source_manifest_component_invalid:{index}"
            )
        required_fields = {"name", "license", "use", "category", "evidence"}
        if set(row) != required_fields:
            raise OpenSourceManifestError(
                f"open_source_manifest_component_fields_mismatch:{index}"
            )
        values = {
            key: str(row.get(key, "") or "").strip()
            for key in ("name", "license", "use", "category", "evidence")
        }
        if not all(values.values()):
            raise OpenSourceManifestError(
                f"open_source_manifest_component_incomplete:{index}"
            )
        components.append(OpenSourceComponent(**values))
    canonical_rows = [component.as_dict() for component in components]
    if str(payload.get("catalog_sha256") or "") != component_fingerprint(
        canonical_rows
    ):
        raise OpenSourceManifestError("open_source_manifest_fingerprint_mismatch")
    return tuple(components)


def load_open_source_manifest(app_root: Path) -> tuple[OpenSourceComponent, ...]:
    if not isinstance(app_root, Path) or not app_root.is_absolute():
        raise TypeError("open_source_inventory_absolute_app_root_required")
    path = app_root / FILENAME
    if not path.is_file():
        raise OpenSourceManifestError("open_source_manifest_missing")
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OpenSourceManifestError(
            f"open_source_manifest_invalid:{type(exc).__name__}:{exc}"
        ) from exc
    return validate_open_source_manifest(payload)


__all__ = [
    "FILENAME",
    "OWNER",
    "SCHEMA",
    "OpenSourceComponent",
    "OpenSourceManifestError",
    "component_fingerprint",
    "load_open_source_manifest",
    "validate_open_source_manifest",
]
