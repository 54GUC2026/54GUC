"""Atomic checkpoint generations owned exclusively by Intelligence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping
import uuid

from arch4.atomic_io import atomic_write_json, atomic_write_text, fsync_parent_dir


CHECKPOINT_SCHEMA = "IntelligenceCheckpointSet.v1"
CHECKPOINT_OWNER = "54guc.intelligence.checkpoint_store"
CHECKPOINT_MANIFEST = "intelligence_checkpoint_manifest.json"
PREVIOUS_CHECKPOINT_MANIFEST = "intelligence_checkpoint_manifest.previous.json"
CHECKPOINT_GENERATION_DIR = "intelligence_checkpoint_generations"
CHECKPOINT_QUARANTINE_DIR = "intelligence_checkpoint_quarantine"
RETIRED_ENGINE_MANIFESTS = (
    "engine_checkpoint_manifest.json",
    "engine_checkpoint_manifest.previous.json",
)
MEMORY_ARTIFACTS = frozenset(
    {"memory.json", "stm_snapshot.json", "ltm_snapshot.json", "memory_meta.json"}
)
INTELLIGENCE_ARTIFACTS = frozenset(
    {
        "historical.json",
        "crystal_ball.json",
        "calibration_authority.json",
        "intelligence_runtime.json",
    }
)
REQUIRED_ARTIFACTS = MEMORY_ARTIFACTS | INTELLIGENCE_ARTIFACTS


class IntelligenceCheckpointCorruption(RuntimeError):
    """No manifest-selected Intelligence checkpoint can be validated."""


class RetiredGlobalCheckpointDetected(RuntimeError):
    """A global Engine checkpoint was found after its reader was deleted."""


@dataclass(frozen=True, slots=True)
class IntelligenceCheckpointSelection:
    generation: str
    manifest_name: str
    recovered_previous: bool
    payloads: Mapping[str, Any]
    receipt: Mapping[str, Any]

    def artifact(self, logical_name: str) -> Any:
        if logical_name not in self.payloads:
            raise KeyError(f"intelligence_checkpoint_artifact_not_selected:{logical_name}")
        return self.payloads[logical_name]


def _snapshot_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _snapshot_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_snapshot_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(
        "intelligence_checkpoint_value_not_json:" + type(value).__name__
    )


def _encoded(payload: Any) -> bytes:
    return json.dumps(
        _snapshot_json_value(payload),
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_path(root: Path, *, previous: bool = False) -> Path:
    return root / (
        PREVIOUS_CHECKPOINT_MANIFEST if previous else CHECKPOINT_MANIFEST
    )


def checkpoint_manifest_exists(root_dir: Path | str) -> bool:
    root = Path(root_dir)
    return _manifest_path(root).exists() or _manifest_path(
        root,
        previous=True,
    ).exists()


def _read_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dict(value) if isinstance(value, dict) else None


def _validate_manifest(
    root: Path,
    manifest: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    if not isinstance(manifest, Mapping):
        return False, "manifest_missing_or_invalid"
    if manifest.get("schema") != CHECKPOINT_SCHEMA:
        return False, "manifest_schema_invalid"
    if manifest.get("owner") != CHECKPOINT_OWNER:
        return False, "manifest_owner_invalid"
    generation = str(manifest.get("generation") or "")
    if not generation or Path(generation).name != generation:
        return False, "manifest_generation_invalid"
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return False, "manifest_artifacts_invalid"
    declared = {str(name) for name in artifacts}
    if declared != REQUIRED_ARTIFACTS:
        return False, "manifest_artifact_set_invalid"
    expected_parent = Path(CHECKPOINT_GENERATION_DIR) / generation
    for logical_name, descriptor in artifacts.items():
        if not isinstance(descriptor, Mapping):
            return False, f"artifact_descriptor_invalid:{logical_name}"
        relative = Path(str(descriptor.get("path") or ""))
        if relative.is_absolute() or relative.parent != expected_parent:
            return False, f"artifact_path_invalid:{logical_name}"
        if relative.name != str(logical_name):
            return False, f"artifact_name_invalid:{logical_name}"
        try:
            data = (root / relative).read_bytes()
        except OSError as exc:
            return False, f"artifact_unreadable:{logical_name}:{type(exc).__name__}"
        try:
            expected_bytes = int(descriptor.get("bytes", -1))
        except (TypeError, ValueError):
            return False, f"artifact_size_invalid:{logical_name}"
        if len(data) != expected_bytes:
            return False, f"artifact_size_mismatch:{logical_name}"
        if _sha256(data) != str(descriptor.get("sha256") or ""):
            return False, f"artifact_checksum_mismatch:{logical_name}"
        try:
            json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            return False, f"artifact_json_invalid:{logical_name}:{type(exc).__name__}"
    return True, ""


def _quarantine(
    root: Path,
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any] | None,
    reason: str,
    include_manifest: bool = True,
) -> None:
    target = root / CHECKPOINT_QUARANTINE_DIR / (
        f"{time.time_ns()}-{uuid.uuid4().hex}"
    )
    target.mkdir(parents=True, exist_ok=False)
    generation = str((manifest or {}).get("generation") or "")
    if generation and Path(generation).name == generation:
        generation_dir = root / CHECKPOINT_GENERATION_DIR / generation
        if generation_dir.is_dir():
            os.replace(generation_dir, target / generation)
            fsync_parent_dir(generation_dir, logger=None)
    if include_manifest and manifest_path.exists():
        os.replace(manifest_path, target / manifest_path.name)
        fsync_parent_dir(manifest_path, logger=None)
    atomic_write_json(
        target / "quarantine_receipt.json",
        {
            "schema": "IntelligenceCheckpointQuarantine.v1",
            "owner": CHECKPOINT_OWNER,
            "reason": str(reason),
            "generation": generation,
            "quarantined_at": float(time.time()),
        },
        indent=2,
        sort_keys=True,
    )


def _decode_selection(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    manifest_name: str,
    recovered_previous: bool,
) -> IntelligenceCheckpointSelection:
    payloads = {
        str(name): json.loads(
            (root / str(descriptor.get("path") or "")).read_text(
                encoding="utf-8"
            )
        )
        for name, descriptor in dict(manifest.get("artifacts") or {}).items()
    }
    return IntelligenceCheckpointSelection(
        generation=str(manifest.get("generation") or ""),
        manifest_name=manifest_name,
        recovered_previous=bool(recovered_previous),
        payloads=payloads,
        receipt={
            "schema": CHECKPOINT_SCHEMA,
            "owner": CHECKPOINT_OWNER,
            "generation": str(manifest.get("generation") or ""),
            "manifest": manifest_name,
            "recovered_previous": bool(recovered_previous),
            "artifact_count": len(payloads),
            "reason": str(manifest.get("reason") or ""),
            "committed_at": float(manifest.get("committed_at", 0.0) or 0.0),
        },
    )


def select_checkpoint(
    root_dir: Path | str,
    *,
    quarantine_invalid: bool = True,
) -> IntelligenceCheckpointSelection | None:
    root = Path(root_dir)
    if not checkpoint_manifest_exists(root):
        return None
    failures: list[str] = []
    for previous in (False, True):
        path = _manifest_path(root, previous=previous)
        if not path.exists():
            continue
        manifest = _read_manifest(path)
        valid, reason = _validate_manifest(root, manifest)
        if valid and manifest is not None:
            try:
                return _decode_selection(
                    root,
                    manifest,
                    manifest_name=path.name,
                    recovered_previous=previous,
                )
            except (OSError, ValueError, TypeError) as exc:
                reason = f"artifact_decode_failed:{type(exc).__name__}"
        label = "previous" if previous else "current"
        failures.append(f"{label}:{reason}")
        if quarantine_invalid:
            _quarantine(
                root,
                manifest_path=path,
                manifest=manifest,
                reason=f"{label}:{reason}",
            )
    raise IntelligenceCheckpointCorruption(
        ";".join(failures) or "intelligence_checkpoint_manifest_unreadable"
    )


def select_startup_checkpoint(
    root_dir: Path | str,
) -> IntelligenceCheckpointSelection | None:
    root = Path(root_dir)
    selected = select_checkpoint(root)
    if selected is not None:
        return selected
    retired = [name for name in RETIRED_ENGINE_MANIFESTS if (root / name).exists()]
    if retired:
        raise RetiredGlobalCheckpointDetected(
            "retired_global_engine_checkpoint_rejected:" + ",".join(retired)
        )
    return None


def commit_checkpoint(
    root_dir: Path | str,
    payloads: Mapping[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    names = {str(name) for name in payloads}
    if names != REQUIRED_ARTIFACTS:
        missing = sorted(REQUIRED_ARTIFACTS - names)
        extra = sorted(names - REQUIRED_ARTIFACTS)
        raise ValueError(
            "intelligence_checkpoint_artifact_set_invalid:"
            f"missing={','.join(missing)}:extra={','.join(extra)}"
        )
    prepared = {
        logical_name: _encoded(payloads[logical_name])
        for logical_name in sorted(names)
    }
    root = Path(root_dir)
    root.mkdir(parents=True, exist_ok=True)
    generation = f"{time.time_ns()}-{uuid.uuid4().hex}"
    generation_dir = root / CHECKPOINT_GENERATION_DIR / generation
    generation_dir.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "schema": CHECKPOINT_SCHEMA,
        "owner": CHECKPOINT_OWNER,
        "generation": generation,
        "reason": str(reason or "manual"),
        "committed_at": float(time.time()),
        "artifacts": {},
    }
    try:
        for logical_name, encoded in prepared.items():
            target = generation_dir / logical_name
            atomic_write_text(target, encoded.decode("utf-8"))
            manifest["artifacts"][logical_name] = {
                "path": str(target.relative_to(root)),
                "bytes": len(encoded),
                "sha256": _sha256(encoded),
            }
        fsync_parent_dir(generation_dir, logger=None)
        valid, error = _validate_manifest(root, manifest)
        if not valid:
            raise IntelligenceCheckpointCorruption(
                f"prepared_intelligence_checkpoint_invalid:{error}"
            )
    except BaseException as exc:
        _quarantine(
            root,
            manifest_path=_manifest_path(root),
            manifest=manifest,
            reason=f"prepared:{type(exc).__name__}:{exc}",
            include_manifest=False,
        )
        raise

    current_path = _manifest_path(root)
    published = False
    try:
        current = _read_manifest(current_path)
        current_valid, current_error = _validate_manifest(root, current)
        if current_valid and current is not None:
            atomic_write_json(
                _manifest_path(root, previous=True),
                current,
                indent=2,
                sort_keys=True,
            )
        elif current_path.exists():
            _quarantine(
                root,
                manifest_path=current_path,
                manifest=current,
                reason=f"superseded_invalid_current:{current_error}",
            )
        atomic_write_json(current_path, manifest, indent=2, sort_keys=True)
        published = True
    except BaseException as exc:
        _quarantine(
            root,
            manifest_path=current_path,
            manifest=manifest,
            reason=f"selection:{type(exc).__name__}:{exc}",
            include_manifest=published,
        )
        raise
    return dict(manifest)


__all__ = (
    "CHECKPOINT_GENERATION_DIR",
    "CHECKPOINT_MANIFEST",
    "CHECKPOINT_OWNER",
    "CHECKPOINT_QUARANTINE_DIR",
    "CHECKPOINT_SCHEMA",
    "INTELLIGENCE_ARTIFACTS",
    "MEMORY_ARTIFACTS",
    "PREVIOUS_CHECKPOINT_MANIFEST",
    "REQUIRED_ARTIFACTS",
    "IntelligenceCheckpointCorruption",
    "IntelligenceCheckpointSelection",
    "RetiredGlobalCheckpointDetected",
    "checkpoint_manifest_exists",
    "commit_checkpoint",
    "select_checkpoint",
    "select_startup_checkpoint",
)
