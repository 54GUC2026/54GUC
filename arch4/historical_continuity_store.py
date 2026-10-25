"""Atomic, generation-scoped storage for approved historical continuity.

Background workers may write only private staging directories.  Runtime
consumers discover history through one commit receipt written after every
required artifact has been validated and the complete directory has been
renamed into the immutable generation store.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
from typing import Any, Mapping
from uuid import uuid4

from arch4.atomic_io import atomic_write_text
from arch4.external_memory_evidence_scope import (
    external_projection_scope,
    validated_external_projection_scope,
)


SCHEMA = "HistoricalContinuityCommit.v1"
OWNER = "54guc.intelligence.historical_evidence"
COMMIT_FILENAME = "historical_continuity_commit.json"
STAGING_DIRNAME = ".historical_continuity_staging"
GENERATIONS_DIRNAME = "historical_continuity_generations"

MANIFEST_FILENAME = "external_memory_auto_manifest.json"
PROMOTION_PLAN_FILENAME = "external_memory_auto_promotion_plan.json"
PRIOR_PACK_FILENAME = "external_memory_promoted_priors.json"
IMPORT_MARKER_FILENAME = "external_memory_import_marker.json"
AUTO_STATUS_FILENAME = "external_memory_auto_import_status.json"
CHART_HISTORY_FILENAME = "telemetry_production_continuity_import.jsonl.gz"
CATALOG_FILENAME = "historical_continuity_catalog.json"

REQUIRED_ARTIFACTS = (
    MANIFEST_FILENAME,
    PROMOTION_PLAN_FILENAME,
    PRIOR_PACK_FILENAME,
    IMPORT_MARKER_FILENAME,
    AUTO_STATUS_FILENAME,
    CHART_HISTORY_FILENAME,
    CATALOG_FILENAME,
)

_SAFE_COMPONENT = re.compile(r"[^a-zA-Z0-9_.-]+")


class HistoricalContinuityCommitError(RuntimeError):
    """The staged generation or its canonical commit receipt is invalid."""


def _safe_component(value: object) -> str:
    normalized = _SAFE_COMPONENT.sub("-", str(value or "").strip()).strip(".-")
    if not normalized:
        raise HistoricalContinuityCommitError("historical continuity id is empty")
    return normalized[:120]


def _json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HistoricalContinuityCommitError(
            f"historical continuity artifact is unreadable: {path.name}"
        ) from exc
    if not isinstance(payload, dict):
        raise HistoricalContinuityCommitError(
            f"historical continuity artifact is not an object: {path.name}"
        )
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def create_staging_generation(
    memory_dir: str | Path,
    *,
    transaction_id: str,
    attempt: int,
) -> Path:
    """Create one private generation that no runtime reader may discover."""

    root = Path(memory_dir).resolve() / STAGING_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    name = (
        f"{_safe_component(transaction_id)}-a{max(1, int(attempt))}-"
        f"{uuid4().hex}"
    )
    path = root / name
    path.mkdir(parents=False, exist_ok=False)
    return path


def discard_staging_generation(memory_dir: str | Path, path: str | Path) -> None:
    root = (Path(memory_dir).resolve() / STAGING_DIRNAME).resolve()
    candidate = Path(path).resolve()
    if candidate == root or not _within(candidate, root):
        raise HistoricalContinuityCommitError(
            "refusing to discard a non-staging history path"
        )
    shutil.rmtree(candidate, ignore_errors=True)


def cleanup_abandoned_staging(memory_dir: str | Path) -> int:
    """Remove crash leftovers before a new owner can start child work."""

    root = Path(memory_dir).resolve() / STAGING_DIRNAME
    if not root.is_dir():
        return 0
    removed = 0
    for child in tuple(root.iterdir()):
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
        elif child.is_file():
            child.unlink(missing_ok=True)
            removed += 1
    return removed


def cleanup_superseded_generations(memory_dir: str | Path) -> int:
    """Delete immutable generations not named by the active commit pointer."""

    memory = Path(memory_dir).resolve()
    generations = (memory / GENERATIONS_DIRNAME).resolve()
    if not generations.is_dir():
        return 0
    status = committed_generation_status(memory)
    active = (
        Path(str(status["generation_dir"])).resolve()
        if status.get("present")
        else None
    )
    removed = 0
    for child in tuple(generations.iterdir()):
        candidate = child.resolve()
        if candidate == active:
            continue
        if candidate.parent != generations:
            raise HistoricalContinuityCommitError(
                "historical continuity generation cleanup escaped its root"
            )
        if candidate.is_dir():
            shutil.rmtree(candidate, ignore_errors=False)
            removed += 1
        elif candidate.is_file():
            candidate.unlink()
            removed += 1
    return removed


def _validate_staged_artifacts(
    staging_dir: Path,
    *,
    transaction_id: str,
    prepared_artifacts: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    missing = [name for name in REQUIRED_ARTIFACTS if not (staging_dir / name).is_file()]
    if missing:
        raise HistoricalContinuityCommitError(
            "historical continuity generation incomplete: " + ", ".join(missing)
        )

    expected_schemas = {
        MANIFEST_FILENAME: "external_memory_library_v1",
        PROMOTION_PLAN_FILENAME: "external_memory_promotion_v1",
        PRIOR_PACK_FILENAME: "external_memory_prior_pack_v1",
        IMPORT_MARKER_FILENAME: "external_memory_prior_import_marker_v1",
        AUTO_STATUS_FILENAME: "external_memory_auto_import_status_v1",
        CATALOG_FILENAME: "HistoricalContinuityCatalog.v1",
    }
    payloads: dict[str, dict[str, Any]] = {}
    for name, schema in expected_schemas.items():
        payload = _json(staging_dir / name)
        if payload.get("schema") != schema:
            raise HistoricalContinuityCommitError(
                f"historical continuity schema mismatch: {name}"
            )
        payloads[name] = payload
    if payloads[AUTO_STATUS_FILENAME].get("status") != "imported":
        raise HistoricalContinuityCommitError(
            "historical continuity auto import was not approved"
        )
    if str(payloads[CATALOG_FILENAME].get("transaction_id") or "") != transaction_id:
        raise HistoricalContinuityCommitError(
            "historical continuity chart catalog transaction mismatch"
        )
    chart_path = staging_dir / CHART_HISTORY_FILENAME
    try:
        with gzip.open(chart_path, "rb") as handle:
            handle.read(1)
    except (OSError, EOFError) as exc:
        raise HistoricalContinuityCommitError(
            "historical continuity chart artifact is not valid gzip"
        ) from exc

    artifacts: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_ARTIFACTS:
        path = staging_dir / name
        prepared = (
            prepared_artifacts.get(name)
            if isinstance(prepared_artifacts, Mapping)
            else None
        )
        if isinstance(prepared, Mapping):
            size_bytes = int(prepared.get("size_bytes") or -1)
            sha256 = str(prepared.get("sha256") or "").strip().lower()
            if size_bytes != int(path.stat().st_size) or not re.fullmatch(
                r"[0-9a-f]{64}", sha256
            ):
                raise HistoricalContinuityCommitError(
                    f"historical continuity prepared receipt mismatch: {name}"
                )
            artifacts[name] = {
                "size_bytes": size_bytes,
                "sha256": sha256,
            }
        else:
            artifacts[name] = {
                "size_bytes": int(path.stat().st_size),
                "sha256": _sha256(path),
            }
    return artifacts


def prepare_staged_generation_receipt(
    staging_dir: str | Path,
    *,
    transaction_id: str,
) -> dict[str, dict[str, Any]]:
    """Validate and hash the private generation inside the bulk child."""

    return _validate_staged_artifacts(
        Path(staging_dir).resolve(),
        transaction_id=transaction_id,
    )


def commit_staged_generation(
    memory_dir: str | Path,
    staging_dir: str | Path,
    *,
    transaction_id: str,
    attempt: int,
    target_identity: Mapping[str, Any],
    prepared_artifacts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Seal a complete generation and atomically publish its sole pointer."""

    memory = Path(memory_dir).resolve()
    staging_root = (memory / STAGING_DIRNAME).resolve()
    staging = Path(staging_dir).resolve()
    if staging == staging_root or not _within(staging, staging_root):
        raise HistoricalContinuityCommitError(
            "historical continuity commit requires a private staging generation"
        )
    artifacts = _validate_staged_artifacts(
        staging,
        transaction_id=transaction_id,
        prepared_artifacts=prepared_artifacts,
    )
    pack = _json(staging / PRIOR_PACK_FILENAME)
    try:
        staged_scope = validated_external_projection_scope(
            pack.get("target_evidence_scope")
            if isinstance(pack.get("target_evidence_scope"), Mapping)
            else {}
        )
        requested_scope = external_projection_scope(target_identity, evidence_count=1)
    except (TypeError, ValueError) as exc:
        raise HistoricalContinuityCommitError(
            "historical continuity target evidence scope is invalid"
        ) from exc
    if not staged_scope.same_physical_namespace(requested_scope):
        raise HistoricalContinuityCommitError(
            "historical continuity staged generation target mismatch"
        )
    if (
        staged_scope.provider_plmn
        and requested_scope.provider_plmn
        and staged_scope.provider_plmn != requested_scope.provider_plmn
    ):
        raise HistoricalContinuityCommitError(
            "historical continuity staged generation provider mismatch"
        )
    generations = memory / GENERATIONS_DIRNAME
    generations.mkdir(parents=True, exist_ok=True)
    generation_name = (
        f"{_safe_component(transaction_id)}-a{max(1, int(attempt))}-"
        f"{time.time_ns()}"
    )
    generation = generations / generation_name
    staging.replace(generation)
    receipt = {
        "schema": SCHEMA,
        "owner": OWNER,
        "status": "committed",
        "all_history_available": True,
        "transaction_id": str(transaction_id),
        "attempt": max(1, int(attempt)),
        "committed_at_ts": time.time(),
        "generation": f"{GENERATIONS_DIRNAME}/{generation_name}",
        "target_identity": dict(target_identity),
        "artifacts": artifacts,
    }
    atomic_write_text(
        memory / COMMIT_FILENAME,
        json.dumps(receipt, indent=2, sort_keys=True),
    )
    return receipt


def committed_generation_status(memory_dir: str | Path) -> dict[str, Any]:
    memory = Path(memory_dir).resolve()
    commit_path = memory / COMMIT_FILENAME
    if not commit_path.is_file():
        return {
            "present": False,
            "reason": "historical_continuity_commit_missing",
            "commit_path": str(commit_path),
        }
    try:
        receipt = _json(commit_path)
        if (
            receipt.get("schema") != SCHEMA
            or receipt.get("owner") != OWNER
            or receipt.get("status") != "committed"
            or receipt.get("all_history_available") is not True
        ):
            raise HistoricalContinuityCommitError(
                "historical continuity commit receipt contract mismatch"
            )
        relative = Path(str(receipt.get("generation") or ""))
        if relative.is_absolute():
            raise HistoricalContinuityCommitError(
                "historical continuity generation path must be relative"
            )
        generation = (memory / relative).resolve()
        generations = (memory / GENERATIONS_DIRNAME).resolve()
        if not _within(generation, generations) or not generation.is_dir():
            raise HistoricalContinuityCommitError(
                "historical continuity committed generation is unavailable"
            )
        declared = receipt.get("artifacts")
        if not isinstance(declared, Mapping):
            raise HistoricalContinuityCommitError(
                "historical continuity artifact receipt is missing"
            )
        for name in REQUIRED_ARTIFACTS:
            row = declared.get(name)
            path = generation / name
            if not isinstance(row, Mapping) or not path.is_file():
                raise HistoricalContinuityCommitError(
                    f"historical continuity committed artifact missing: {name}"
                )
            if int(row.get("size_bytes") or -1) != int(path.stat().st_size):
                raise HistoricalContinuityCommitError(
                    f"historical continuity committed artifact size mismatch: {name}"
                )
    except HistoricalContinuityCommitError as exc:
        return {
            "present": False,
            "reason": str(exc),
            "commit_path": str(commit_path),
        }
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return {
            "present": False,
            "reason": f"historical continuity commit unreadable: {type(exc).__name__}",
            "commit_path": str(commit_path),
        }
    return {
        "present": True,
        "reason": "committed_generation_available",
        "commit_path": str(commit_path),
        "generation_dir": str(generation),
        "receipt": dict(receipt),
    }


def require_committed_generation(memory_dir: str | Path) -> Path:
    status = committed_generation_status(memory_dir)
    if not status.get("present"):
        raise HistoricalContinuityCommitError(str(status.get("reason") or "commit missing"))
    return Path(str(status["generation_dir"])).resolve()


def committed_artifact_path(
    memory_dir: str | Path,
    filename: str,
    *,
    verify_sha256: bool = False,
) -> Path | None:
    if filename not in REQUIRED_ARTIFACTS:
        raise HistoricalContinuityCommitError(
            f"unknown historical continuity artifact: {filename}"
        )
    status = committed_generation_status(memory_dir)
    if not status.get("present"):
        return None
    path = Path(str(status["generation_dir"])).resolve() / filename
    if verify_sha256:
        receipt = status.get("receipt") if isinstance(status.get("receipt"), Mapping) else {}
        artifacts = receipt.get("artifacts") if isinstance(receipt, Mapping) else {}
        row = artifacts.get(filename) if isinstance(artifacts, Mapping) else {}
        expected = str(row.get("sha256") or "") if isinstance(row, Mapping) else ""
        if not expected or _sha256(path) != expected:
            raise HistoricalContinuityCommitError(
                f"historical continuity committed artifact checksum mismatch: {filename}"
            )
    return path


__all__ = [
    "AUTO_STATUS_FILENAME",
    "CATALOG_FILENAME",
    "CHART_HISTORY_FILENAME",
    "COMMIT_FILENAME",
    "GENERATIONS_DIRNAME",
    "HistoricalContinuityCommitError",
    "IMPORT_MARKER_FILENAME",
    "MANIFEST_FILENAME",
    "OWNER",
    "PRIOR_PACK_FILENAME",
    "PROMOTION_PLAN_FILENAME",
    "REQUIRED_ARTIFACTS",
    "SCHEMA",
    "STAGING_DIRNAME",
    "cleanup_abandoned_staging",
    "cleanup_superseded_generations",
    "commit_staged_generation",
    "committed_artifact_path",
    "committed_generation_status",
    "create_staging_generation",
    "discard_staging_generation",
    "prepare_staged_generation_receipt",
    "require_committed_generation",
]
