from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import time
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arch4.atomic_io import atomic_write_text
from arch4.evidence_scope import validated_evidence_scope
from arch4.historical_artifact_reader import (
    FAMILY_COMPARE,
    FAMILY_PRODUCTION,
    HistoricalArtifactReader,
)
from arch4.protected_operator_context import PROTECTED_OPERATOR_CONTEXT_ARTIFACT
from arch4.customer_contracts.handoff_identity import (
    confirmed_handoff_identity_matches,
    handoff_identity_token,
)
from arch4.versioning import parse_version_key


_STAGE_PREFIX = "54GUC_Windows_portable_"
_RUNTIME_DIR = "portable_runtime"
_MEMORY_DIR = "guc_memory"
_OUTPUT_DIR = "guc_output"
_CONFIG_NAME = "guc_config.xml"
_RELEASE_CHANNEL_NAME = "RELEASE_CHANNEL"
_ROLLUP_NAME = "rollup_hourly.jsonl"
_EXE_NAME = "54GUC.exe"
_MANIFEST_NAME = "version_manifest.json"
_IMPORT_MARKER_NAME = ".portable_state_imported.json"
_IMPORT_DECISION_IMPORTED = "imported"
_IMPORT_DECISION_SKIPPED = "skipped_explicit"
_PROFILE_RESOLUTION_GATE_SCHEMA = "profile_resolution_gate_v1"
_PROFILE_RESOLUTION_OWNER = "54guc.modem_live.scout"
_PROFILE_AUTHORITY_OWNER = "arch4.modem.catalog"
_PORTABLE_STATE_LIBRARY_DIR = "_portable_state_library"
_PORTABLE_STATE_LIBRARY_LATEST = "latest"
_NEXT_START_HANDOFF_DIR = "_portable_next_start_handoff"
_NEXT_START_EVIDENCE_HANDOFF_NAME = "next_start_evidence_handoff.zip"
_LEGACY_COMBINED_HANDOFF_NAME = "next_start_handoff.zip"
_NEXT_START_EVIDENCE_HANDOFF_SCHEMA = "portable_next_start_evidence_handoff_v1"
_LEGACY_COMBINED_HANDOFF_SCHEMA = "portable_next_start_handoff_v1"
CONFIRMED_HANDOFF_CONTINUITY_RECEIPT_SCHEMA = (
    "PortableConfirmedHandoffContinuityReceipt.v1"
)
CONFIRMED_HANDOFF_CONTINUITY_RECEIPT_OWNER = (
    "arch4.portable_state_import.evidence_boundary"
)
_PREDICTIVE_HISTORY_DAYS = 14
_BETA_SOAK_MATURE_BUILD_DAYS = 7
_NON_CANONICAL_MEMORY_ARTIFACTS = frozenset(
    {
        "crystal_ball.json",
        "intelligence_checkpoint_manifest.json",
        "intelligence_checkpoint_manifest.previous.json",
        "engine_checkpoint_manifest.json",
        "engine_checkpoint_manifest.previous.json",
        "historical_continuity_commit.json",
        "historical_evidence_status.json",
        # Retired S5b service-state path is rejected, never imported.
        "historical_continuity_import_status.json",
        "memory_compaction_transaction.json",
    }
)
_SUPPORT_BUNDLE_GLOBS = (
    "*.zip",
    "support_bundles/*.zip",
    "portable_runtime/support_bundles/*.zip",
)
_IMPORT_ROOTS_ENV = "ARCH_PORTABLE_STATE_IMPORT_ROOTS"
_NEXT_START_EVIDENCE_HANDOFF_ENV = "ARCH_PORTABLE_NEXT_START_EVIDENCE_HANDOFF"
_LEGACY_COMBINED_HANDOFF_ENV = "ARCH_PORTABLE_NEXT_START_HANDOFF"


class PortableStateConfirmedIdentityRequired(RuntimeError):
    """Raised when retired pre-identity state-copy APIs are invoked."""


@dataclass(frozen=True)
class PortableStateImportCandidate:
    stage_dir: Path
    runtime_root: Path
    memory_dir: Path
    output_dir: Path
    config_path: Path
    memory_file_count: int
    history_file_count: int
    has_rollup: bool
    has_config: bool
    last_write_time: float
    version_text: str
    version_key: tuple[int, ...]
    source_priority: int = 0


@dataclass(frozen=True)
class PortableStateImportResult:
    target_stage: Path
    source_stage: Path
    memory_files: int
    history_files: int
    rollup_copied: bool
    config_copied: bool
    config_backup_created: bool
    protected_context_deferred: bool = False


@dataclass(frozen=True)
class PortableOperatorConfigImportResult:
    target_stage: Path
    source_paths: tuple[Path, ...]
    merged_items: int
    config_updated: bool
    backend: str
    host: str


@dataclass(frozen=True)
class PortableStatePublishResult:
    source_stage: Path
    library_stage: Path
    memory_files: int
    history_files: int
    rollup_copied: bool
    config_copied: bool
    protected_context_published: bool = False


@dataclass(frozen=True)
class PortableEvidenceHandoffPublishResult:
    source_stage: Path
    handoff_path: Path
    memory_files: int
    history_files: int
    rollup_copied: bool


def _resolve_stage_dir(stage_dir: str | Path | None = None) -> Path:
    if stage_dir is not None:
        resolved = Path(stage_dir).resolve()
        if resolved.parent.name == _RUNTIME_DIR and (
            resolved.name == _OUTPUT_DIR or resolved.name.startswith(f"{_OUTPUT_DIR}_")
        ):
            return resolved.parent.parent
        if resolved.name == _RUNTIME_DIR:
            return resolved.parent
        return resolved
    env_root = str(os.environ.get("ARCH_APP_ROOT", "") or "").strip()
    if env_root:
        return Path(env_root).resolve()
    return Path.cwd().resolve()


def _library_tier_label(tier_path: str) -> str:
    text = str(tier_path or "").strip().lower()
    if not text or text == "latest":
        return "latest generic pack"
    if text.startswith("by_provider_cell/"):
        return "provider + cell"
    if text.startswith("by_provider_site/"):
        return "provider + site"
    if text.startswith("by_provider_band/"):
        return "provider + band"
    if text.startswith("by_provider_model/"):
        return "provider + model"
    if text.startswith("by_provider_plmn/"):
        return "provider"
    if text.startswith("by_model_firmware/"):
        return "model + firmware"
    if text.startswith("by_model/"):
        return "model"
    if text.startswith("by_backend_family/"):
        return "backend family"
    return text.replace("_", " ")


def _merge_rollup_jsonl_union(target_path: Path, source_path: Path) -> bool:
    """Merge the non-historical hourly rollup compatibility projection."""

    if not source_path.is_file():
        return False
    def _lines(path: Path) -> list[str]:
        try:
            return path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return []
    source_lines = _lines(source_path)
    if not target_path.exists():
        _write_text_atomic(
            target_path,
            "".join(f"{line}\n" for line in source_lines),
        )
        return True
    try:
        existing_lines = _lines(target_path)
    except Exception:
        existing_lines = []
    seen = set(existing_lines)
    try:
        source_lines = _lines(source_path)
    except Exception:
        source_lines = []
    appended = False
    merged = list(existing_lines)
    for line in source_lines:
        if line in seen:
            continue
        seen.add(line)
        merged.append(line)
        appended = True
    if appended:
        text = "".join(f"{line}\n" for line in merged)
        _write_text_atomic(target_path, text)
    return True


def _portable_state_library_root(target_stage_dir: str | Path) -> Path:
    stage = Path(target_stage_dir).resolve()
    for parent in stage.parents:
        candidate = parent / _PORTABLE_STATE_LIBRARY_DIR
        if candidate.is_dir():
            return candidate
    if (
        stage.name.lower().endswith("_source_run")
        and stage.parent.name.lower() == "54guc"
    ):
        return stage.parent.parent / _PORTABLE_STATE_LIBRARY_DIR
    return stage.parent / _PORTABLE_STATE_LIBRARY_DIR


def _portable_state_library_latest_stage(target_stage_dir: str | Path) -> Path:
    return (
        _portable_state_library_root(target_stage_dir) / _PORTABLE_STATE_LIBRARY_LATEST
    )


def _next_start_handoff_root(stage_dir: str | Path) -> Path:
    return Path(stage_dir).resolve().parent / _NEXT_START_HANDOFF_DIR


def _next_start_evidence_handoff_path(stage_dir: str | Path) -> Path:
    return _next_start_handoff_root(stage_dir) / _NEXT_START_EVIDENCE_HANDOFF_NAME


def _legacy_combined_handoff_path(stage_dir: str | Path) -> Path:
    return _next_start_handoff_root(stage_dir) / _LEGACY_COMBINED_HANDOFF_NAME


def _resolve_next_start_boundary_handoff_path(
    *,
    boundary_name: str,
    boundary_env: str,
    stage_dir: str | Path | None = None,
    runtime_memory_dir: str | Path | None = None,
    explicit_path: str | Path | None = None,
) -> Path | None:
    """Resolve one owner archive plus the named previous-version adapter."""

    candidates: list[Path] = []

    def add(raw: str | Path | None, *, archive_name: str) -> None:
        if raw in (None, ""):
            return
        path = _portable_marker_path(str(raw))
        if path.is_dir() or path.suffix.lower() != ".zip":
            path = path / archive_name
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved not in candidates:
            candidates.append(resolved)

    add(explicit_path, archive_name=boundary_name)
    add(os.environ.get(boundary_env, ""), archive_name=boundary_name)
    if runtime_memory_dir not in (None, ""):
        memory = _portable_marker_path(str(runtime_memory_dir))
        for parent in (memory, *memory.parents):
            if parent.name.lower() == "54b":
                add(
                    parent / _NEXT_START_HANDOFF_DIR,
                    archive_name=boundary_name,
                )
                break
    if stage_dir not in (None, ""):
        add(
            _next_start_handoff_root(Path(stage_dir)),
            archive_name=boundary_name,
        )
    # Permanent previous-version input adapter. New writers never create the
    # combined archive; each owner reads only its admitted slice from it.
    add(
        os.environ.get(_LEGACY_COMBINED_HANDOFF_ENV, ""),
        archive_name=_LEGACY_COMBINED_HANDOFF_NAME,
    )
    if stage_dir not in (None, ""):
        add(
            _next_start_handoff_root(Path(stage_dir)),
            archive_name=_LEGACY_COMBINED_HANDOFF_NAME,
        )
    return next((path for path in candidates if path.is_file()), None)


def resolve_next_start_evidence_handoff_path(
    *,
    stage_dir: str | Path | None = None,
    runtime_memory_dir: str | Path | None = None,
    explicit_path: str | Path | None = None,
) -> Path | None:
    return _resolve_next_start_boundary_handoff_path(
        boundary_name=_NEXT_START_EVIDENCE_HANDOFF_NAME,
        boundary_env=_NEXT_START_EVIDENCE_HANDOFF_ENV,
        stage_dir=stage_dir,
        runtime_memory_dir=runtime_memory_dir,
        explicit_path=explicit_path,
    )


def _library_slug(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:96]


def _portable_state_library_identity_stages(
    target_stage_dir: str | Path, identity: dict | None
) -> list[Path]:
    root = _portable_state_library_root(target_stage_dir)
    ident = identity if isinstance(identity, dict) else {}
    backend_family = _library_slug(str(ident.get("backend_family") or ""))
    model = _library_slug(str(ident.get("model") or ""))
    firmware = _library_slug(str(ident.get("firmware") or ""))
    provider_plmn = _library_slug(str(ident.get("provider_plmn") or ""))
    try:
        serving_band = int(ident.get("serving_band") or 0)
    except Exception:
        serving_band = 0
    try:
        pci = int(ident.get("pci") or 0)
    except Exception:
        pci = 0
    try:
        earfcn = int(ident.get("earfcn") or 0)
    except Exception:
        earfcn = 0

    stages: list[Path] = [root / _PORTABLE_STATE_LIBRARY_LATEST]
    if backend_family:
        stages.append(
            root / "by_backend_family" / backend_family / _PORTABLE_STATE_LIBRARY_LATEST
        )
    if model:
        stages.append(root / "by_model" / model / _PORTABLE_STATE_LIBRARY_LATEST)
    if model and firmware:
        stages.append(
            root
            / "by_model_firmware"
            / model
            / firmware
            / _PORTABLE_STATE_LIBRARY_LATEST
        )
    if provider_plmn:
        stages.append(
            root / "by_provider_plmn" / provider_plmn / _PORTABLE_STATE_LIBRARY_LATEST
        )
    if provider_plmn and model:
        stages.append(
            root
            / "by_provider_model"
            / provider_plmn
            / model
            / _PORTABLE_STATE_LIBRARY_LATEST
        )
    if provider_plmn and serving_band > 0:
        band_slug = f"b{serving_band}"
        stages.append(
            root
            / "by_provider_band"
            / provider_plmn
            / band_slug
            / _PORTABLE_STATE_LIBRARY_LATEST
        )
        if earfcn > 0:
            stages.append(
                root
                / "by_provider_site"
                / provider_plmn
                / band_slug
                / f"earfcn_{earfcn}"
                / _PORTABLE_STATE_LIBRARY_LATEST
            )
        if pci > 0 and earfcn > 0:
            stages.append(
                root
                / "by_provider_cell"
                / provider_plmn
                / band_slug
                / f"earfcn_{earfcn}"
                / f"pci_{pci}"
                / _PORTABLE_STATE_LIBRARY_LATEST
            )
        elif pci > 0:
            stages.append(
                root
                / "by_provider_cell"
                / provider_plmn
                / band_slug
                / f"pci_{pci}"
                / _PORTABLE_STATE_LIBRARY_LATEST
            )

    unique: list[Path] = []
    seen: set[Path] = set()
    for stage in stages:
        resolved = stage.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(stage)
    return unique


def _write_text_atomic(path: Path, text: str) -> None:
    atomic_write_text(path, text)


def _portable_marker_path(text: str) -> Path:
    raw = str(text or "").strip()
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
    if match and os.name != "nt":
        drive = match.group(1).lower()
        rest = str(match.group(2) or "").replace("\\", "/")
        return Path("/mnt") / drive / rest
    return Path(raw)


def _current_state_payloads_from_support_bundle(bundle_path: Path) -> list[dict]:
    if not bundle_path.is_file():
        return []
    payloads: list[dict] = []
    try:
        with zipfile.ZipFile(bundle_path, "r") as bundle:
            for name in bundle.namelist():
                if Path(name).name != "current_state.json":
                    continue
                try:
                    data = json.loads(
                        bundle.read(name).decode("utf-8", errors="ignore")
                    )
                except Exception:
                    continue
                if isinstance(data, dict):
                    payloads.append(data)
    except zipfile.BadZipFile:
        return []
    return payloads


def _support_bundle_current_state_payloads(stage_dir: Path) -> list[dict]:
    payloads: list[dict] = []
    for pattern in _SUPPORT_BUNDLE_GLOBS:
        for bundle_path in sorted(stage_dir.glob(pattern)):
            payloads.extend(_current_state_payloads_from_support_bundle(bundle_path))
    return payloads


def portable_state_import_contract_errors(stage_dir: str | Path) -> list[str]:
    raise RuntimeError(
        "portable_state_import_contract_errors is illegal; use "
        "arch4.foundation.settings_handoff."
        "foundation_settings_handoff_contract_errors"
    )


def _parse_version_key(text: str) -> tuple[int, ...]:
    return parse_version_key(text)


def _extract_stage_version_text(stage_name: str) -> str:
    name = str(stage_name or "").strip()
    if not name:
        return ""

    prefix_match = re.search(
        rf"{re.escape(_STAGE_PREFIX)}([^_]+)", name, flags=re.IGNORECASE
    )
    if prefix_match:
        return str(prefix_match.group(1) or "").strip()

    dotted_match = re.search(r"(?i)V\d+\.\d+", name)
    if dotted_match:
        return str(dotted_match.group(0) or "").strip()

    compact_match = re.search(r"(?i)V\d{3,}", name)
    if compact_match:
        return str(compact_match.group(0) or "").strip()

    return ""


def _read_stage_version(stage_dir: Path) -> tuple[str, tuple[int, ...]]:
    manifest_path = stage_dir / _MANIFEST_NAME
    if manifest_path.is_file():
        try:
            raw = manifest_path.read_text(encoding="utf-8", errors="ignore")
            match = re.search(r'"version"\s*:\s*"([^"]+)"', raw)
            if match:
                version_text = str(match.group(1) or "").strip()
                return version_text, _parse_version_key(version_text)
        except Exception:
            pass
    version_file = stage_dir / "VERSION"
    if version_file.is_file():
        try:
            version_text = str(
                version_file.read_text(encoding="utf-8", errors="ignore") or ""
            ).strip()
            if version_text:
                return version_text, _parse_version_key(version_text)
        except Exception:
            pass
    version_text = _extract_stage_version_text(stage_dir.name)
    return version_text, _parse_version_key(version_text)


def _read_stage_manifest(stage_dir: Path) -> dict:
    manifest_path = stage_dir / _MANIFEST_NAME
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _is_known_version_key(version_key: tuple[int, ...]) -> bool:
    if not version_key:
        return False
    return any(int(part or 0) > 0 for part in version_key)


def _is_stable_v_line_version(text: str) -> bool:
    return bool(re.fullmatch(r"(?i)V\d+(?:\.\d+){1,2}", str(text or "").strip()))


def _candidate_version_allowed_for_import(
    *,
    target_stage: Path,
    target_version_key: tuple[int, ...],
    source_version_text: str,
    source_version_key: tuple[int, ...],
) -> bool:
    target_version_known = _is_known_version_key(target_version_key)
    source_version_known = _is_known_version_key(source_version_key)
    if not (target_version_known and source_version_known):
        return True
    if source_version_key <= target_version_key:
        return True
    target_channel = _stage_release_channel(target_stage)
    if target_channel == "beta" and _is_stable_v_line_version(source_version_text):
        return True
    return False


def _looks_like_portable_stage(stage_dir: Path) -> bool:
    if not stage_dir.is_dir():
        return False
    runtime_root = stage_dir / _RUNTIME_DIR
    if not runtime_root.is_dir():
        return False
    if (stage_dir / _EXE_NAME).is_file():
        return True
    if (stage_dir / _MANIFEST_NAME).is_file():
        return True
    return (
        (runtime_root / _OUTPUT_DIR).is_dir()
        or (runtime_root / _MEMORY_DIR).is_dir()
        or any(path.is_dir() for path in runtime_root.glob(f"{_OUTPUT_DIR}_*"))
    )


def _split_import_roots_env(raw: str) -> list[Path]:
    text = str(raw or "").strip()
    if not text:
        return []
    separator = ";" if ";" in text else "|"
    return [Path(part.strip()) for part in text.split(separator) if part.strip()]


def _default_windows_profile_roots(target_stage: Path) -> list[Path]:
    roots: list[Path] = []
    target_parts = {part.lower() for part in target_stage.parts}
    if "54b" not in target_parts and "00_mikrotik" not in target_parts:
        return roots

    for parent in (target_stage, *target_stage.parents):
        if parent.name.lower() == "54b":
            roots.append(parent)
            break
    for parent in (target_stage, *target_stage.parents):
        if parent.name.upper() == "00_MIKROTIK":
            roots.append(parent)
            break
    return roots


def _portable_state_import_search_roots(target_stage: Path) -> list[Path]:
    roots: list[Path] = [target_stage.parent]
    roots.extend(_split_import_roots_env(os.environ.get(_IMPORT_ROOTS_ENV, "")))
    roots.extend(_default_windows_profile_roots(target_stage))

    unique: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except Exception:
            resolved = root
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _bounded_import_search_dirs(root: Path) -> list[Path]:
    dirs: list[Path] = [root]
    try:
        children = [
            child
            for child in sorted(root.iterdir(), key=lambda item: item.name.lower())
            if child.is_dir()
        ]
    except Exception:
        children = []
    dirs.extend(children)
    if root.name.upper() == "00_MIKROTIK":
        for child in children:
            try:
                dirs.extend(
                    grandchild
                    for grandchild in sorted(
                        child.iterdir(), key=lambda item: item.name.lower()
                    )
                    if grandchild.is_dir()
                )
            except Exception:
                continue
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in dirs:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _iter_import_stage_dirs(root: Path, target_stage: Path) -> list[Path]:
    stages: list[Path] = []
    seen: set[Path] = set()

    def maybe_add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved in seen or resolved == target_stage:
            return
        if _looks_like_portable_stage(resolved):
            seen.add(resolved)
            stages.append(resolved)

    for path in _bounded_import_search_dirs(root):
        if path.name == _PORTABLE_STATE_LIBRARY_DIR:
            continue
        maybe_add(path)
    return stages


def _runtime_paths(stage_dir: Path) -> tuple[Path, Path, Path]:
    runtime_root = stage_dir / _RUNTIME_DIR
    return runtime_root, runtime_root / _MEMORY_DIR, runtime_root / _OUTPUT_DIR


def _output_history_dirs(stage_dir: Path) -> list[Path]:
    runtime_root, _memory_dir, output_dir = _runtime_paths(stage_dir)
    dirs: list[Path] = []
    if output_dir.is_dir():
        dirs.append(output_dir)
    if runtime_root.is_dir():
        for path in sorted(runtime_root.glob(f"{_OUTPUT_DIR}_*")):
            if path.is_dir():
                dirs.append(path)
    if HistoricalArtifactReader(stage_dir).discover(
        families=(FAMILY_PRODUCTION,),
    ):
        dirs.append(stage_dir)

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in dirs:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _history_file_day(path: Path) -> datetime | None:
    match = re.search(
        r"telemetry_(?:history|production)_(\d{8})\.jsonl(?:\.gz)?$",
        path.name,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return datetime.strptime(str(match.group(1)), "%Y%m%d").replace(
            tzinfo=timezone.utc
        )
    except Exception:
        return None


def _history_files_for_stage(stage_dir: Path, *, days: int | None = None) -> list[Path]:
    runtime_root, _memory_dir, output_dir = _runtime_paths(stage_dir)
    extra_roots: list[Path] = [stage_dir]
    if runtime_root.is_dir():
        extra_roots.extend(
            path
            for path in runtime_root.iterdir()
            if path.is_dir() and path.name.startswith(f"{_OUTPUT_DIR}_")
        )
    reader_root = output_dir if output_dir.is_dir() else stage_dir
    artifacts = HistoricalArtifactReader(
        reader_root,
        extra_roots=extra_roots,
    ).discover(families=(FAMILY_PRODUCTION,))
    files = [artifact.path for artifact in artifacts]
    if days is not None:
        now_day = datetime.now(timezone.utc).date()
        cutoff_day = now_day - timedelta(days=max(0, int(days) - 1))
        files = [
            path
            for path in files
            if (day := _history_file_day(path)) is None or day.date() >= cutoff_day
        ]
    return sorted(set(files), key=lambda path: (path.name, str(path.parent)))


def _validated_history_payloads_for_stage(
    stage_dir: Path,
    *,
    days: int = _PREDICTIVE_HISTORY_DAYS,
) -> dict[str, list[dict[str, object]]]:
    """Project bounded history only after canonical decode/schema/dedupe gates."""

    runtime_root, _memory_dir, output_dir = _runtime_paths(stage_dir)
    extra_roots: list[Path] = [stage_dir]
    if runtime_root.is_dir():
        extra_roots.extend(
            path
            for path in runtime_root.iterdir()
            if path.is_dir() and path.name.startswith(f"{_OUTPUT_DIR}_")
        )
    reader_root = output_dir if output_dir.is_dir() else stage_dir
    reader = HistoricalArtifactReader(reader_root, extra_roots=extra_roots)
    cutoff = time.time() - float(max(1, min(_PREDICTIVE_HISTORY_DAYS, int(days))) * 86400)
    grouped: dict[str, list[dict[str, object]]] = {}
    for record in reader.iter_records(
        families=(FAMILY_PRODUCTION,),
        since_ts=cutoff,
    ):
        logical_name = record.artifact.path.name
        if logical_name.lower().endswith(".gz"):
            logical_name = logical_name[:-3]
        grouped.setdefault(logical_name, []).append(dict(record.payload))
    return grouped


def _jsonl_payload_bytes(rows: list[Mapping[str, object]]) -> bytes:
    return "".join(
        json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n"
        for row in rows
    ).encode("utf-8")


def _rollup_files_for_stage(stage_dir: Path) -> list[Path]:
    files: list[Path] = []
    for output_dir in _output_history_dirs(stage_dir):
        path = output_dir / _ROLLUP_NAME
        if path.is_file():
            files.append(path)
    return files


def _history_window_days(output_dir: Path) -> int:
    days: list[datetime] = []
    for artifact in HistoricalArtifactReader(output_dir).discover(
        families=(FAMILY_PRODUCTION,),
    ):
        path = artifact.path
        day = _history_file_day(path)
        if day is not None:
            days.append(day)
    if not days:
        return 0
    ordered = sorted(days)
    return max(1, int((ordered[-1].date() - ordered[0].date()).days) + 1)


def _history_window_days_for_stage(stage_dir: Path) -> int:
    days: list[datetime] = []
    for path in _history_files_for_stage(stage_dir):
        day = _history_file_day(path)
        if day is not None:
            days.append(day)
    if not days:
        return 0
    ordered = sorted(days)
    return max(1, int((ordered[-1].date() - ordered[0].date()).days) + 1)


def _parse_build_date(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _stage_build_date(stage_dir: Path) -> str:
    manifest = _read_stage_manifest(stage_dir)
    text = str(manifest.get("build_date") or "").strip()
    if text:
        return text
    build_date_file = stage_dir / "BUILD_DATE"
    if build_date_file.is_file():
        try:
            return str(
                build_date_file.read_text(encoding="utf-8", errors="ignore") or ""
            ).strip()
        except Exception:
            return ""
    return ""


def _stage_release_channel(stage_dir: Path) -> str:
    manifest = _read_stage_manifest(stage_dir)
    text = str(manifest.get("release_channel") or "").strip().lower()
    if text:
        return text
    channel_file = stage_dir / _RELEASE_CHANNEL_NAME
    if channel_file.is_file():
        try:
            return (
                str(channel_file.read_text(encoding="utf-8", errors="ignore") or "")
                .strip()
                .lower()
                or "mainline"
            )
        except Exception:
            return "mainline"
    return "mainline"


def _should_copy_portable_state_file(path: Path) -> bool:
    name = str(path.name or "")
    if not path.is_file():
        return False
    if name.startswith(".") and ".tmp." in name:
        return False
    if name.endswith(".lock"):
        return False
    if name in _NON_CANONICAL_MEMORY_ARTIFACTS:
        return False
    return True


def _identity_metadata_from_current_state(data: dict) -> dict:
    identity = (
        data.get("runtime_identity")
        if isinstance(data.get("runtime_identity"), dict)
        else {}
    )
    session_scope = (
        data.get("runtime_session_scope")
        if isinstance(data.get("runtime_session_scope"), dict)
        else {}
    )
    try:
        serving_band = int(data.get("serving_band") or 0)
    except Exception:
        serving_band = 0
    try:
        pci = int(data.get("pci") or 0)
    except Exception:
        pci = 0
    try:
        earfcn = int(data.get("earfcn") or 0)
    except Exception:
        earfcn = 0
    return {
        "profile_id": str(
            identity.get("profile_id")
            or identity.get("resolved_profile_id")
            or data.get("profile_id")
            or data.get("resolved_profile_id")
            or ""
        ),
        "backend": str(identity.get("backend") or data.get("backend") or ""),
        "backend_family": str(
            identity.get("backend_family") or data.get("backend_family") or ""
        ),
        "model": str(identity.get("model") or data.get("model") or ""),
        "firmware": str(identity.get("firmware") or data.get("firmware") or ""),
        "provider_name": str(
            identity.get("provider_name") or data.get("provider_name") or ""
        ),
        "provider_plmn": str(
            identity.get("provider_plmn") or data.get("provider_plmn") or ""
        ),
        "serial": str(identity.get("serial") or data.get("serial") or ""),
        "imei": str(identity.get("imei") or data.get("imei") or ""),
        "imsi": str(identity.get("imsi") or data.get("imsi") or ""),
        "serving_band": serving_band,
        "pci": pci,
        "earfcn": earfcn,
        "session_scope_key": str(
            session_scope.get("session_scope_key")
            or data.get("session_scope_key")
            or ""
        ),
    }


def _stage_identity_metadata(stage_dir: Path) -> dict:
    for output_dir in _output_history_dirs(stage_dir):
        for name in ("current_state.json",):
            path = output_dir / name
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            identity = _identity_metadata_from_current_state(data)
            if any(
                str(identity.get(key) or "").strip()
                for key in ("backend", "backend_family", "model", "provider_plmn")
            ):
                return identity
    for data in _support_bundle_current_state_payloads(stage_dir):
        identity = _identity_metadata_from_current_state(data)
        if any(
            str(identity.get(key) or "").strip()
            for key in ("backend", "backend_family", "model", "provider_plmn")
        ):
            return identity
    return {}


def _import_marker_path(stage_dir: Path) -> Path:
    return stage_dir / _IMPORT_MARKER_NAME


def _read_import_marker(stage_dir: Path) -> dict:
    marker = _import_marker_path(stage_dir)
    if not marker.is_file():
        return {}
    try:
        data = json.loads(marker.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _profile_resolution_gate_payload(
    *, decision: str, reason: str = ""
) -> dict[str, object]:
    decision_text = str(decision or "").strip() or "profile_neutral_runtime_owner"
    return {
        "schema": _PROFILE_RESOLUTION_GATE_SCHEMA,
        "decision": decision_text,
        "ready": True,
        "owner": _PROFILE_RESOLUTION_OWNER,
        "profile_authority": _PROFILE_AUTHORITY_OWNER,
        "runtime_resolution": "resolved_by_modem_live_after_identity",
        "reason": str(reason or ""),
    }


def _profile_resolution_gate_ready(marker: Mapping[str, object]) -> tuple[bool, str]:
    gate = (
        marker.get("profile_resolution_gate") if isinstance(marker, Mapping) else None
    )
    if not isinstance(gate, Mapping):
        return False, "beta_profile_resolution_gate_missing"
    if str(gate.get("schema") or "") != _PROFILE_RESOLUTION_GATE_SCHEMA:
        return False, "beta_profile_resolution_gate_schema_invalid"
    if str(gate.get("owner") or "") != _PROFILE_RESOLUTION_OWNER:
        return False, "beta_profile_resolution_gate_owner_invalid"
    if str(gate.get("profile_authority") or "") != _PROFILE_AUTHORITY_OWNER:
        return False, "beta_profile_resolution_authority_invalid"
    if not bool(gate.get("ready", False)):
        return False, "beta_profile_resolution_gate_not_ready"
    decision = str(gate.get("decision") or "").strip()
    if decision not in {"profile_neutral_runtime_owner", _IMPORT_DECISION_SKIPPED}:
        return False, "beta_profile_resolution_gate_decision_invalid"
    return True, ""


def _import_marker_valid(stage_dir: Path) -> bool:
    data = _read_import_marker(stage_dir)
    source_stage = str(data.get("source_stage") or "").strip()
    return bool(source_stage)


def mark_portable_state_import_skipped(
    stage_dir: str | Path,
    *,
    reason: str = "explicit_operator_skip",
) -> dict[str, object]:
    stage = _resolve_stage_dir(stage_dir)
    payload: dict[str, object] = {
        "decision": _IMPORT_DECISION_SKIPPED,
        "reason": str(reason or "explicit_operator_skip"),
        "decided_at_ts": float(time.time()),
        "target_stage": str(stage),
        "release_channel": _stage_release_channel(stage),
        "profile_neutral_import_contract": "profile_neutral_import_v1",
        "profile_resolution_gate": _profile_resolution_gate_payload(
            decision=_IMPORT_DECISION_SKIPPED,
            reason=str(reason or "explicit_operator_skip"),
        ),
    }
    _write_text_atomic(
        _import_marker_path(stage), json.dumps(payload, indent=2, sort_keys=True)
    )
    return payload


def beta_state_import_decision(stage_dir: str | Path) -> dict[str, object]:
    stage = _resolve_stage_dir(stage_dir)
    release_channel = _stage_release_channel(stage)
    marker = _read_import_marker(stage)
    decision = str(marker.get("decision") or "").strip().lower()
    imported = decision == _IMPORT_DECISION_IMPORTED and bool(
        str(marker.get("source_stage") or "").strip()
    )
    skipped = decision == _IMPORT_DECISION_SKIPPED
    required = release_channel == "beta"
    gate_ready, gate_blocker = _profile_resolution_gate_ready(marker)
    ready = bool((not required) or ((imported or skipped) and gate_ready))
    blocker = ""
    if required and not ready:
        blocker = (
            gate_blocker
            if (imported or skipped) and gate_blocker
            else "beta_state_import_decision_missing"
        )
    return {
        "required": required,
        "ready": ready,
        "blocker": blocker,
        "release_channel": release_channel,
        "decision": decision,
        "marker_path": str(_import_marker_path(stage)),
        "source_stage": str(marker.get("source_stage") or ""),
        "profile_resolution_gate_ready": bool(gate_ready),
    }


def assert_beta_state_import_ready(stage_dir: str | Path) -> dict[str, object]:
    decision = beta_state_import_decision(stage_dir)
    if decision["ready"]:
        return decision
    raise RuntimeError(
        "Beta launch blocked: state import decision is missing. "
        "Run import-state first, or explicitly skip state import."
    )


def _target_is_build_seeded(stage_dir: Path) -> bool:
    manifest = _read_stage_manifest(stage_dir)
    seed = manifest.get("portable_runtime_seed")
    if not isinstance(seed, dict):
        return False
    if str(seed.get("source") or "").strip().lower() != "repo_portable_runtime":
        return False
    return bool(seed.get("memory_seeded")) or bool(seed.get("config_seeded"))


def continuity_status(stage_dir: str | Path | None = None) -> dict[str, object]:
    stage = _resolve_stage_dir(stage_dir)
    out: dict[str, object] = {
        "imported": False,
        "app_root": str(stage),
        "source_stage": "",
        "source_stage_name": "",
        "source_version": "",
        "source_stage_count": 0,
        "memory_files": 0,
        "history_files": 0,
        "rollup_copied": False,
        "config_copied": False,
        "predictive_history_days": 0,
        "tier_path": "",
        "tier_label": "",
        "identity": {},
    }
    marker = _import_marker_path(stage)
    if not marker.is_file():
        return out
    try:
        data = json.loads(marker.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return out
    if not isinstance(data, dict):
        return out
    source_stage_text = str(data.get("source_stage") or "").strip()
    if not source_stage_text:
        return out
    out["imported"] = True
    out["source_stage"] = source_stage_text
    out["source_stage_name"] = str(Path(source_stage_text).name or source_stage_text)
    merged = (
        data.get("source_stages_merged")
        if isinstance(data.get("source_stages_merged"), list)
        else []
    )
    out["source_stage_count"] = max(1, len([x for x in merged if str(x or "").strip()]))
    out["memory_files"] = int(data.get("memory_files") or 0)
    out["history_files"] = int(data.get("history_files") or 0)
    out["rollup_copied"] = bool(data.get("rollup_copied", False))
    out["config_copied"] = bool(data.get("config_copied", False))

    source_stage = Path(source_stage_text)
    manifest = _read_stage_manifest(source_stage)
    out["source_version"] = str(
        manifest.get("version") or _read_stage_version(source_stage)[0] or ""
    )
    library = (
        manifest.get("portable_state_library")
        if isinstance(manifest.get("portable_state_library"), dict)
        else {}
    )
    if library:
        out["predictive_history_days"] = int(
            library.get("predictive_history_days") or 0
        )
        out["tier_path"] = str(library.get("tier_path") or "")
        out["tier_label"] = _library_tier_label(str(library.get("tier_path") or ""))
        out["identity"] = (
            dict(library.get("identity") or {})
            if isinstance(library.get("identity"), dict)
            else {}
        )
    return out


def beta_soak_status(stage_dir: str | Path | None = None) -> dict[str, object]:
    stage = _resolve_stage_dir(stage_dir)
    continuity = continuity_status(stage)
    version_text, _version_key = _read_stage_version(stage)
    build_date_text = _stage_build_date(stage)
    build_dt = _parse_build_date(build_date_text)
    now = datetime.now(timezone.utc)
    build_age_days = 0
    if build_dt is not None:
        build_age_days = max(0, int((now - build_dt).total_seconds() // 86400))
    history_days = _history_window_days_for_stage(stage)
    history_target_days = int(_PREDICTIVE_HISTORY_DAYS)
    imported = bool(continuity.get("imported", False))
    if (
        history_days >= history_target_days
        and build_age_days >= _BETA_SOAK_MATURE_BUILD_DAYS
    ):
        status = "mature"
        status_label = "Mature"
    elif history_days > 0 or build_age_days > 0 or imported:
        status = "soaking"
        status_label = "Soaking"
    else:
        status = "young"
        status_label = "Young"
    release_channel = _stage_release_channel(stage)
    gate_blockers: list[str] = []
    if release_channel != "beta":
        gate_blockers.append("release_channel_not_beta")
    if build_age_days < _BETA_SOAK_MATURE_BUILD_DAYS:
        gate_blockers.append("build_age_below_mature_target")
    if history_days < history_target_days:
        gate_blockers.append("history_window_below_predictive_target")
    if not imported:
        gate_blockers.append("continuity_memory_not_imported")
    return {
        "status": status,
        "status_label": status_label,
        "build_version": str(version_text or ""),
        "build_date": build_date_text,
        "release_channel": release_channel,
        "build_age_days": build_age_days,
        "history_window_days": history_days,
        "history_target_days": history_target_days,
        "history_ready": bool(history_days >= history_target_days),
        "continuity_imported": imported,
        "continuity_source_version": str(continuity.get("source_version") or ""),
        "continuity_tier_label": str(continuity.get("tier_label") or ""),
        "continuity_source_stage_count": int(continuity.get("source_stage_count") or 0),
        "release_gate": {
            "name": "beta_soak_maturity",
            "eligible": bool(not gate_blockers),
            "blockers": list(gate_blockers),
            "required_channel": "beta",
            "required_build_age_days": int(_BETA_SOAK_MATURE_BUILD_DAYS),
            "required_history_days": int(history_target_days),
            "requires_continuity_import": True,
        },
    }


def supports_portable_state_import(stage_dir: str | Path) -> bool:
    return _looks_like_portable_stage(Path(stage_dir).resolve())


def target_has_portable_state(stage_dir: str | Path) -> bool:
    stage = Path(stage_dir).resolve()
    if _import_marker_valid(stage):
        return True
    _runtime_root, memory_dir, _output_dir = _runtime_paths(stage)
    build_seeded = _target_is_build_seeded(stage)
    if build_seeded:
        return False
    has_history = bool(_history_files_for_stage(stage))
    has_rollup = bool(_rollup_files_for_stage(stage))
    if has_history or has_rollup:
        return True
    if any(
        _should_copy_portable_state_file(path)
        for path in memory_dir.glob("*")
    ):
        return True
    # Operator config is not portable learned state and cannot suppress an
    # identity-scoped history/memory continuation.
    return False


def publish_portable_state_library(
    source_stage_dir: str | Path,
) -> PortableStatePublishResult | None:
    source_stage = Path(source_stage_dir).resolve()
    if not supports_portable_state_import(source_stage):
        return None
    if not target_has_portable_state(source_stage):
        return None

    _runtime_root, memory_dir, _output_dir = _runtime_paths(source_stage)
    identity = _stage_identity_metadata(source_stage)
    library_stages = _portable_state_library_identity_stages(source_stage, identity)
    library_stage = library_stages[0]
    version_text, _version_key = _read_stage_version(source_stage)
    manifest = {
        "version": version_text,
        "portable_state_library": {
            "source_stage": str(source_stage),
            "source_stage_name": str(source_stage.name or ""),
            "predictive_history_days": int(_PREDICTIVE_HISTORY_DAYS),
            "exported_at_ts": float(time.time()),
            "identity": identity,
        },
    }
    copied_memory = 0
    copied_history = 0
    rollup_copied = False
    history_payloads = _validated_history_payloads_for_stage(
        source_stage,
        days=_PREDICTIVE_HISTORY_DAYS,
    )
    for idx, stage in enumerate(library_stages):
        library_runtime, library_memory, library_output = _runtime_paths(stage)
        library_memory.mkdir(parents=True, exist_ok=True)
        library_output.mkdir(parents=True, exist_ok=True)

        for path in sorted(library_memory.glob("*")):
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        for artifact in HistoricalArtifactReader(library_output).discover(
            families=(FAMILY_PRODUCTION, FAMILY_COMPARE),
        ):
            artifact.path.unlink()
        stale = library_output / _ROLLUP_NAME
        if stale.exists():
            stale.unlink()
        # Recovery libraries carry learned state, never a raw operator config
        # or protected credentials.  Protected context has a separate exact-
        # physical-modem handoff owner below.
        for forbidden in (
            stage / _CONFIG_NAME,
            stage / PROTECTED_OPERATOR_CONTEXT_ARTIFACT,
        ):
            if forbidden.exists():
                forbidden.unlink()

        local_memory = 0
        for path in sorted(memory_dir.glob("*")):
            if not _should_copy_portable_state_file(path):
                continue
            shutil.copy2(path, library_memory / path.name)
            local_memory += 1

        local_history = 0
        for logical_name, rows in sorted(history_payloads.items()):
            if not rows:
                continue
            _write_text_atomic(
                library_output / logical_name,
                _jsonl_payload_bytes(rows).decode("utf-8"),
            )
            local_history += 1

        local_rollup = False
        for source_rollup in _rollup_files_for_stage(source_stage):
            if _merge_rollup_jsonl_union(
                library_output / source_rollup.name,
                source_rollup,
            ):
                local_rollup = True

        tier_manifest = dict(manifest)
        tier_manifest["portable_state_library"] = {
            **manifest["portable_state_library"],
            "tier_stage": str(stage),
            "tier_stage_name": str(stage.name or ""),
            "tier_path": str(
                stage.relative_to(_portable_state_library_root(source_stage))
            ),
            "raw_config_included": False,
            "protected_operator_context_included": False,
        }
        _write_text_atomic(stage / _MANIFEST_NAME, json.dumps(tier_manifest, indent=2))
        if idx == 0:
            copied_memory = local_memory
            copied_history = local_history
            rollup_copied = local_rollup

    return PortableStatePublishResult(
        source_stage=source_stage,
        library_stage=library_stage,
        memory_files=copied_memory,
        history_files=copied_history,
        rollup_copied=rollup_copied,
        config_copied=False,
        protected_context_published=False,
    )


def publish_next_start_settings_handoff(*args, **kwargs):
    raise RuntimeError(
        "portable_state_import settings writer is illegal; use "
        "arch4.foundation.settings_handoff.publish_next_start_settings_handoff"
    )


def publish_next_start_evidence_handoff(
    source_stage_dir: str | Path,
) -> PortableEvidenceHandoffPublishResult | None:
    """Publish only Intelligence evidence; never read or include settings."""

    source_stage = Path(source_stage_dir).resolve()
    if not supports_portable_state_import(source_stage):
        return None
    if not target_has_portable_state(source_stage):
        return None

    _runtime_root, memory_dir, _output_dir = _runtime_paths(source_stage)
    handoff_root = _next_start_handoff_root(source_stage)
    handoff_root.mkdir(parents=True, exist_ok=True)
    handoff_path = _next_start_evidence_handoff_path(source_stage)
    tmp_path = (
        handoff_root
        / f".{handoff_path.name}.tmp.{os.getpid()}.{time.time_ns()}.{uuid.uuid4().hex}"
    )

    version_text, version_key = _read_stage_version(source_stage)
    identity = _stage_identity_metadata(source_stage)
    memory_files = [
        path
        for path in sorted(memory_dir.glob("*"))
        if _should_copy_portable_state_file(path)
    ]
    history_payloads = _validated_history_payloads_for_stage(
        source_stage,
        days=_PREDICTIVE_HISTORY_DAYS,
    )
    rollup_sources = _rollup_files_for_stage(source_stage)
    context_files: dict[str, Path] = {}
    for output_dir in _output_history_dirs(source_stage):
        for name in ("current_state.json", "capabilities.json"):
            candidate = output_dir / name
            if not candidate.is_file():
                continue
            previous = context_files.get(name)
            try:
                newer = (
                    previous is None
                    or candidate.stat().st_mtime > previous.stat().st_mtime
                )
            except OSError:
                newer = previous is None
            if newer:
                context_files[name] = candidate
    manifest = {
        "schema": _NEXT_START_EVIDENCE_HANDOFF_SCHEMA,
        "owner": "54guc.intelligence.historical_evidence",
        "source_stage": str(source_stage),
        "source_stage_name": str(source_stage.name or ""),
        "version": version_text,
        "version_key": list(version_key),
        "predictive_history_days": int(_PREDICTIVE_HISTORY_DAYS),
        "created_at_ts": float(time.time()),
        "identity": identity,
        "memory_files": [path.name for path in memory_files],
        "history_files": sorted(history_payloads),
        "rollup_files": [_ROLLUP_NAME] if rollup_sources else [],
        "context_files": sorted(context_files),
        "config_copied": False,
        "raw_config_included": False,
        "protected_operator_context_included": False,
    }

    def merged_rollup_jsonl(paths: list[Path]) -> bytes:
        lines: list[str] = []
        seen: set[str] = set()
        for path in paths:
            try:
                source_lines = path.read_text(
                    encoding="utf-8",
                    errors="replace",
                ).splitlines()
            except OSError:
                continue
            for line in source_lines:
                if line in seen:
                    continue
                seen.add(line)
                lines.append(line)
        return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")

    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(
                "handoff_manifest.json", json.dumps(manifest, indent=2, sort_keys=True)
            )
            for path in memory_files:
                bundle.write(path, f"{_RUNTIME_DIR}/{_MEMORY_DIR}/{path.name}")
            for name, rows in sorted(history_payloads.items()):
                bundle.writestr(
                    f"{_RUNTIME_DIR}/{_OUTPUT_DIR}/{name}",
                    _jsonl_payload_bytes(rows),
                )
            if rollup_sources:
                bundle.writestr(
                    f"{_RUNTIME_DIR}/{_OUTPUT_DIR}/{_ROLLUP_NAME}",
                    merged_rollup_jsonl(rollup_sources),
                )
            for name, path in sorted(context_files.items()):
                bundle.write(path, f"{_RUNTIME_DIR}/{_OUTPUT_DIR}/{name}")
            version_manifest = source_stage / _MANIFEST_NAME
            if version_manifest.is_file():
                bundle.write(version_manifest, _MANIFEST_NAME)
        tmp_path.replace(handoff_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    return PortableEvidenceHandoffPublishResult(
        source_stage=source_stage,
        handoff_path=handoff_path,
        memory_files=len(memory_files),
        history_files=len(history_payloads),
        rollup_copied=bool(rollup_sources),
    )


def list_portable_state_import_candidates(
    target_stage_dir: str | Path,
) -> list[PortableStateImportCandidate]:
    target_stage = Path(target_stage_dir).resolve()
    if not supports_portable_state_import(target_stage):
        return []

    target_version_text, target_version_key = _read_stage_version(target_stage)
    candidates: list[PortableStateImportCandidate] = []
    seen: set[Path] = set()
    for root_priority, search_root in enumerate(
        _portable_state_import_search_roots(target_stage)
    ):
        for resolved_stage in _iter_import_stage_dirs(search_root, target_stage):
            if resolved_stage in seen:
                continue
            seen.add(resolved_stage)
            if resolved_stage == target_stage or not _looks_like_portable_stage(
                resolved_stage
            ):
                continue
            runtime_root, memory_dir, output_dir = _runtime_paths(resolved_stage)
            config_path = resolved_stage / _CONFIG_NAME
            memory_files = [
                path
                for path in memory_dir.glob("*")
                if _should_copy_portable_state_file(path)
            ]
            has_config = config_path.is_file()
            if not memory_files:
                continue
            version_text, version_key = _read_stage_version(resolved_stage)
            if not _candidate_version_allowed_for_import(
                target_stage=target_stage,
                target_version_key=target_version_key,
                source_version_text=version_text,
                source_version_key=version_key,
            ):
                continue
            candidates.append(
                PortableStateImportCandidate(
                    stage_dir=resolved_stage,
                    runtime_root=runtime_root,
                    memory_dir=memory_dir,
                    output_dir=output_dir,
                    config_path=config_path,
                    memory_file_count=len(memory_files),
                    history_file_count=0,
                    has_rollup=False,
                    has_config=has_config,
                    last_write_time=resolved_stage.stat().st_mtime,
                    version_text=version_text or target_version_text,
                    version_key=version_key,
                    source_priority=root_priority,
                )
            )
    candidates.sort(
        key=lambda item: (
            item.source_priority,
            not bool(item.version_key),
            tuple(-part for part in item.version_key),
            -item.last_write_time,
        ),
    )
    return candidates


def _read_boundary_handoff_manifest(
    handoff_path: Path,
    *,
    allowed_schemas: frozenset[str],
) -> dict[str, object]:
    if not handoff_path.is_file():
        return {}
    try:
        with zipfile.ZipFile(handoff_path, "r") as bundle:
            data = json.loads(bundle.read("handoff_manifest.json").decode("utf-8"))
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return {}
    if not isinstance(data, dict) or data.get("schema") not in allowed_schemas:
        return {}
    return data




def _read_evidence_handoff_manifest(handoff_path: Path) -> dict[str, object]:
    return _read_boundary_handoff_manifest(
        handoff_path,
        allowed_schemas=frozenset(
            {
                _NEXT_START_EVIDENCE_HANDOFF_SCHEMA,
                _LEGACY_COMBINED_HANDOFF_SCHEMA,
            }
        ),
    )


def _identity_value_digest(key: str, value: object) -> str:
    token = handoff_identity_token(value)
    if not token:
        return ""
    return hashlib.sha256(f"{str(key).strip().lower()}:{token}".encode("utf-8")).hexdigest()


def _handoff_source_identity_fingerprint(identity: Mapping[str, object]) -> str:
    content = {
        "backend": handoff_identity_token(identity.get("backend")),
        "model": handoff_identity_token(identity.get("model")),
        "serial_sha256": _identity_value_digest("serial", identity.get("serial")),
        "imei_sha256": _identity_value_digest("imei", identity.get("imei")),
    }
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _handoff_archive_index_sha256(
    bundle: zipfile.ZipFile,
    manifest_bytes: bytes,
) -> str:
    rows = [
        {
            "name": info.filename.replace("\\", "/"),
            "crc": int(info.CRC),
            "file_size": int(info.file_size),
            "compress_size": int(info.compress_size),
        }
        for info in sorted(bundle.infolist(), key=lambda item: item.filename)
        if not info.is_dir()
    ]
    content = {
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "members": rows,
    }
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _confirmed_handoff_continuity_receipt(
    handoff_path: Path,
    *,
    source_identity: Mapping[str, object],
    confirmed_identity: Mapping[str, object],
) -> dict[str, object]:
    raw_scope = confirmed_identity.get("evidence_scope")
    try:
        confirmed_scope = validated_evidence_scope(
            raw_scope if isinstance(raw_scope, Mapping) else {}
        )
    except (TypeError, ValueError):
        return {}
    immutable_match_hashes = {
        key: _identity_value_digest(key, source_identity.get(key))
        for key in ("serial", "imei")
        if handoff_identity_token(source_identity.get(key))
        and handoff_identity_token(source_identity.get(key))
        == handoff_identity_token(confirmed_identity.get(key))
    }
    if not immutable_match_hashes:
        return {}
    try:
        with zipfile.ZipFile(handoff_path, "r") as bundle:
            manifest_bytes = bundle.read("handoff_manifest.json")
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            archive_index_sha256 = _handoff_archive_index_sha256(
                bundle,
                manifest_bytes,
            )
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return {}
    manifest_schema = str(manifest.get("schema") or "")
    if manifest_schema not in {
        _NEXT_START_EVIDENCE_HANDOFF_SCHEMA,
        _LEGACY_COMBINED_HANDOFF_SCHEMA,
    }:
        return {}
    content: dict[str, object] = {
        "schema": CONFIRMED_HANDOFF_CONTINUITY_RECEIPT_SCHEMA,
        "owner": CONFIRMED_HANDOFF_CONTINUITY_RECEIPT_OWNER,
        "binding": "confirmed_login_exact_modem_handoff",
        "handoff_path": str(handoff_path.resolve()),
        "handoff_manifest_schema": manifest_schema,
        "handoff_created_at_ts": float(manifest.get("created_at_ts") or 0.0),
        "handoff_archive_index_sha256": archive_index_sha256,
        "source_identity_fingerprint": _handoff_source_identity_fingerprint(
            source_identity
        ),
        "immutable_match_hashes": immutable_match_hashes,
        "confirmed_evidence_scope": confirmed_scope.as_dict(),
        "admitted_at_ts": float(time.time()),
        "legacy_owner_files_restored": False,
    }
    content["receipt_id"] = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return content


def admit_confirmed_next_start_handoff_evidence(
    target_stage_dir: str | Path,
    *,
    confirmed_identity: Mapping[str, object],
    runtime_memory_dir: str | Path | None = None,
    handoff_path: str | Path | None = None,
) -> dict[str, object] | None:
    """Decode only an exact-modem evidence admission for Intelligence.

    This permanent archive boundary does not read or write Foundation/NGUI
    settings and does not restore any learned-owner file into active storage.
    """

    target_stage = Path(target_stage_dir).resolve()
    resolved_handoff = resolve_next_start_evidence_handoff_path(
        stage_dir=target_stage,
        runtime_memory_dir=runtime_memory_dir,
        explicit_path=handoff_path,
    )
    if resolved_handoff is None:
        return None
    manifest = _read_evidence_handoff_manifest(resolved_handoff)
    source_identity = manifest.get("identity")
    if not isinstance(source_identity, Mapping):
        return None
    if not confirmed_handoff_identity_matches(source_identity, confirmed_identity):
        return None
    receipt = _confirmed_handoff_continuity_receipt(
        resolved_handoff,
        source_identity=source_identity,
        confirmed_identity=confirmed_identity,
    )
    return receipt or None


def validate_confirmed_handoff_continuity_receipt(
    receipt: Mapping[str, object] | None,
    *,
    target_identity: Mapping[str, object],
    now_ts: float | None = None,
    max_age_days: int = _PREDICTIVE_HISTORY_DAYS,
) -> dict[str, object]:
    """Revalidate the urgent admission on the sole background import lane."""

    row = dict(receipt or {})
    reasons: list[str] = []
    if (
        row.get("schema") != CONFIRMED_HANDOFF_CONTINUITY_RECEIPT_SCHEMA
        or row.get("owner") != CONFIRMED_HANDOFF_CONTINUITY_RECEIPT_OWNER
        or row.get("binding") != "confirmed_login_exact_modem_handoff"
    ):
        reasons.append("confirmed handoff receipt owner/schema invalid")
    receipt_id = str(row.pop("receipt_id", "") or "")
    expected_receipt_id = hashlib.sha256(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    row["receipt_id"] = receipt_id
    if not receipt_id or receipt_id != expected_receipt_id:
        reasons.append("confirmed handoff receipt checksum mismatch")
    raw_confirmed_scope = row.get("confirmed_evidence_scope")
    raw_target_scope = target_identity.get("evidence_scope")
    try:
        confirmed_scope = validated_evidence_scope(
            raw_confirmed_scope if isinstance(raw_confirmed_scope, Mapping) else {}
        )
        target_scope = validated_evidence_scope(
            raw_target_scope if isinstance(raw_target_scope, Mapping) else {}
        )
        if not confirmed_scope.same_physical_namespace(target_scope):
            reasons.append("confirmed login generation no longer matches target")
    except (TypeError, ValueError):
        confirmed_scope = None
        reasons.append("confirmed or target exact EvidenceScope invalid")
    path_text = str(row.get("handoff_path") or "").strip()
    path = Path(path_text).expanduser().resolve() if path_text else Path()
    manifest: dict[str, object] = {}
    source_identity: Mapping[str, object] = {}
    archive_index_sha256 = ""
    try:
        with zipfile.ZipFile(path, "r") as bundle:
            manifest_bytes = bundle.read("handoff_manifest.json")
            loaded = json.loads(manifest_bytes.decode("utf-8"))
            manifest = loaded if isinstance(loaded, dict) else {}
            archive_index_sha256 = _handoff_archive_index_sha256(
                bundle,
                manifest_bytes,
            )
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        reasons.append("confirmed handoff archive unavailable or invalid")
    manifest_schema = str(manifest.get("schema") or "")
    if manifest_schema not in {
        _NEXT_START_EVIDENCE_HANDOFF_SCHEMA,
        _LEGACY_COMBINED_HANDOFF_SCHEMA,
    } or manifest_schema != str(row.get("handoff_manifest_schema") or ""):
        reasons.append("confirmed handoff manifest schema invalid")
    source_identity = (
        manifest.get("identity")
        if isinstance(manifest.get("identity"), Mapping)
        else {}
    )
    if (
        archive_index_sha256
        != str(row.get("handoff_archive_index_sha256") or "")
    ):
        reasons.append("confirmed handoff archive index changed after admission")
    if _handoff_source_identity_fingerprint(source_identity) != str(
        row.get("source_identity_fingerprint") or ""
    ):
        reasons.append("confirmed handoff source identity changed after admission")
    immutable_hashes = (
        row.get("immutable_match_hashes")
        if isinstance(row.get("immutable_match_hashes"), Mapping)
        else {}
    )
    source_hashes = {
        key: _identity_value_digest(key, source_identity.get(key))
        for key in ("serial", "imei")
    }
    if not any(
        digest and digest == str(immutable_hashes.get(key) or "")
        for key, digest in source_hashes.items()
    ):
        reasons.append("confirmed handoff immutable modem proof changed")
    target_hashes = {
        key: _identity_value_digest(key, target_identity.get(key))
        for key in ("serial", "imei")
        if handoff_identity_token(target_identity.get(key))
    }
    if target_hashes and not any(
        digest == str(immutable_hashes.get(key) or "")
        for key, digest in target_hashes.items()
    ):
        reasons.append("current modem immutable identity conflicts with handoff")
    created_at = float(manifest.get("created_at_ts") or 0.0)
    receipt_created_at = float(row.get("handoff_created_at_ts") or 0.0)
    now = float(time.time() if now_ts is None else now_ts)
    age_days = (now - created_at) / 86400.0 if created_at > 0.0 else -1.0
    if created_at <= 0.0 or created_at != receipt_created_at:
        reasons.append("confirmed handoff creation timestamp invalid")
    elif age_days < -0.01:
        reasons.append("confirmed handoff creation timestamp is in the future")
    elif age_days > float(max(1, min(_PREDICTIVE_HISTORY_DAYS, max_age_days))):
        reasons.append("confirmed handoff is older than continuity window")
    return {
        "schema": "ConfirmedHandoffContinuityAdmission.v1",
        "owner": CONFIRMED_HANDOFF_CONTINUITY_RECEIPT_OWNER,
        "admitted": not reasons,
        "rejection_reasons": reasons,
        "receipt_id": receipt_id,
        "handoff_path": str(path) if path_text else "",
        "handoff_created_at_ts": created_at,
        "age_days": max(0.0, age_days) if age_days >= 0.0 else None,
        "source_identity_fingerprint": str(
            row.get("source_identity_fingerprint") or ""
        ),
        "confirmed_evidence_scope": (
            confirmed_scope.as_dict() if confirmed_scope is not None else {}
        ),
        "legacy_owner_files_restored": False,
    }


def import_confirmed_next_start_handoff_settings(*args, **kwargs):
    raise RuntimeError(
        "portable_state_import settings reader is illegal; use "
        "arch4.foundation.settings_handoff."
        "import_confirmed_next_start_handoff_settings"
    )


def import_operator_config_for_confirmed_connection(
    target_stage_dir: str | Path,
    *,
    backend: str,
    host: str,
    username: str = "",
) -> PortableOperatorConfigImportResult | None:
    """Retired fail-closed adapter retained for API-level diagnosis only.

    Backend/address equality does not prove the same physical modem and cannot
    authorize license, user, site, or plan import.  The sole production route
    is ``import_confirmed_next_start_handoff_settings`` after immutable
    modem identity confirmation.
    """

    del target_stage_dir, backend, host, username
    raise PortableStateConfirmedIdentityRequired(
        "backend/host operator import is illegal; use the immutable-identity "
        "confirmed handoff boundary with exact profile_id"
    )


def import_portable_state(
    target_stage_dir: str | Path,
    source_stage_dir: str | Path | None = None,
) -> PortableStateImportResult:
    del target_stage_dir, source_stage_dir
    raise PortableStateConfirmedIdentityRequired(
        "generic portable-state copy is retired; complete exact modem identity "
        "confirmation, then let Intelligence admit historical evidence"
    )


def import_next_start_handoff(
    target_stage_dir: str | Path,
) -> PortableStateImportResult | None:
    del target_stage_dir
    raise PortableStateConfirmedIdentityRequired(
        "pre-identity next-start handoff copy is retired; protected operator "
        "context and bulk historical continuity have separate exact-identity owners"
    )


def auto_import_previous_portable_state(
    target_stage_dir: str | Path,
) -> PortableStateImportResult | None:
    del target_stage_dir
    raise PortableStateConfirmedIdentityRequired(
        "automatic pre-identity portable-state copy is forbidden"
    )
