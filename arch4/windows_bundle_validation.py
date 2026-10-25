from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable
import zipfile

from arch4.open_source_manifest import (
    OpenSourceManifestError,
    validate_open_source_manifest,
)
from arch4.updater import validate_update_channel_payload


BETA_PACKAGE_DOCUMENTS = (
    "START_HERE.txt",
    "USER_GUIDE.txt",
    "SUPPORTED_HARDWARE_AND_LIMITATIONS.txt",
    "RECOVERY_AND_SUPPORT.txt",
    "BETA_RELEASE_NOTES.txt",
)


REQUIRED_STAGE_ENTRIES = (
    "54GUC.exe",
    "version_manifest.json",
    "open_source_manifest.json",
    "UPDATE_CHANNEL.json",
    "portable_runtime",
    "portable_runtime/guc_output",
    "portable_runtime/guc_memory",
    "support_bundles",
    "_internal",
    "_internal/PySide6/QtCore.pyd",
    "_internal/PySide6/QtGui.pyd",
    "_internal/PySide6/QtWidgets.pyd",
    *(f"BETA_PACKAGE/{name}" for name in BETA_PACKAGE_DOCUMENTS),
)

OPTIONAL_EMPTY_ZIP_DIRS = frozenset(
    {
        "portable_runtime",
        "portable_runtime/guc_output",
        "portable_runtime/guc_memory",
        "support_bundles",
        "_internal",
    }
)


REQUIRED_MANIFEST_KEYS = (
    "release_tag",
    "stamp",
    "profile",
    "bundle_type",
    "bundle_layout",
    "launcher",
    "version",
    "build_date",
)


def _norm_zip_name(name: str) -> str:
    return str(name or "").strip().replace("\\", "/").rstrip("/")


def _stage_entry_exists(root: Path, entry: str) -> bool:
    path = root / entry
    if path.exists():
        return True
    norm = _norm_zip_name(entry)
    if norm == "portable_runtime":
        return (root / "guc_output").exists() and (root / "guc_memory").exists()
    if norm == "portable_runtime/guc_output":
        return (root / "guc_output").exists()
    if norm == "portable_runtime/guc_memory":
        return (root / "guc_memory").exists()
    return False


def _zip_entry_exists(names: set[str], entry: str) -> bool:
    norm = _norm_zip_name(entry)
    if norm in names or any(name.startswith(f"{norm}/") for name in names):
        return True
    if norm == "portable_runtime":
        return _zip_entry_exists(names, "guc_output") and _zip_entry_exists(names, "guc_memory")
    if norm == "portable_runtime/guc_output":
        return _zip_entry_exists(names, "guc_output")
    if norm == "portable_runtime/guc_memory":
        return _zip_entry_exists(names, "guc_memory")
    return False


def validate_manifest_data(data: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["version_manifest.json is not a JSON object"]
    for key in REQUIRED_MANIFEST_KEYS:
        if not str(data.get(key, "") or "").strip():
            errors.append(f"version_manifest.json missing '{key}'")
    if data.get("bundle_type") != "windows_native":
        errors.append("version_manifest.json bundle_type must be 'windows_native'")
    if data.get("bundle_layout") != "54GUC_portable_v1":
        errors.append("version_manifest.json bundle_layout must be '54GUC_portable_v1'")
    if data.get("launcher") != "54GUC.exe":
        errors.append("version_manifest.json launcher must be '54GUC.exe'")
    return errors


def validate_open_source_manifest_data(data: object) -> list[str]:
    try:
        validate_open_source_manifest(data)
    except OpenSourceManifestError as exc:
        return [f"open_source_manifest.json invalid: {exc}"]
    return []


def validate_update_channel_data(data: object) -> list[str]:
    try:
        validate_update_channel_payload(data)
    except (RuntimeError, ValueError) as exc:
        return [f"UPDATE_CHANNEL.json invalid: {exc}"]
    return []


def validate_stage_dir(stage_dir: Path) -> list[str]:
    errors: list[str] = []
    root = Path(stage_dir)
    if not root.is_dir():
        return [f"stage directory not found: {root}"]

    for entry in REQUIRED_STAGE_ENTRIES:
        if not _stage_entry_exists(root, entry):
            errors.append(f"missing stage entry: {entry}")

    beta_dir = root / "BETA_PACKAGE"
    if beta_dir.is_dir():
        actual_beta_docs = {path.name for path in beta_dir.iterdir() if path.is_file()}
        expected_beta_docs = set(BETA_PACKAGE_DOCUMENTS)
        for name in sorted(actual_beta_docs - expected_beta_docs):
            errors.append(f"unexpected beta package document: BETA_PACKAGE/{name}")
    for path in root.iterdir():
        if path.is_file() and path.suffix.lower() in {".md", ".txt", ".html"}:
            errors.append(f"customer document outside BETA_PACKAGE: {path.name}")
    if (root / "docs").exists():
        errors.append("internal docs tree must not be shipped")

    manifest_path = root / "version_manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"invalid version_manifest.json: {exc}")
        else:
            errors.extend(validate_manifest_data(manifest))
    oss_path = root / "open_source_manifest.json"
    if oss_path.is_file():
        try:
            oss_manifest = json.loads(oss_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"invalid open_source_manifest.json: {exc}")
        else:
            errors.extend(validate_open_source_manifest_data(oss_manifest))
    update_path = root / "UPDATE_CHANNEL.json"
    if update_path.is_file():
        try:
            update_channel = json.loads(update_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"invalid UPDATE_CHANNEL.json: {exc}")
        else:
            errors.extend(validate_update_channel_data(update_channel))

    return errors


def validate_bundle_zip(bundle_path: Path) -> list[str]:
    errors: list[str] = []
    path = Path(bundle_path)
    if not path.is_file():
        return [f"bundle zip not found: {path}"]

    try:
        with zipfile.ZipFile(path) as bundle:
            names = {_norm_zip_name(name) for name in bundle.namelist()}
            for entry in REQUIRED_STAGE_ENTRIES:
                norm = _norm_zip_name(entry)
                if norm in OPTIONAL_EMPTY_ZIP_DIRS:
                    continue
                if not _zip_entry_exists(names, entry):
                    errors.append(f"missing zip entry: {entry}")

            expected_beta_docs = {
                f"BETA_PACKAGE/{name}" for name in BETA_PACKAGE_DOCUMENTS
            }
            actual_beta_docs = {
                name
                for name in names
                if name.startswith("BETA_PACKAGE/") and "/" not in name[len("BETA_PACKAGE/") :]
            }
            for name in sorted(actual_beta_docs - expected_beta_docs):
                errors.append(f"unexpected beta package document: {name}")
            for name in names:
                if "/" not in name and Path(name).suffix.lower() in {".md", ".txt", ".html"}:
                    errors.append(f"customer document outside BETA_PACKAGE: {name}")
            if any(name == "docs" or name.startswith("docs/") for name in names):
                errors.append("internal docs tree must not be shipped")

            if "version_manifest.json" in names:
                try:
                    manifest = json.loads(bundle.read("version_manifest.json").decode("utf-8"))
                except Exception as exc:
                    errors.append(f"invalid zipped version_manifest.json: {exc}")
                else:
                    errors.extend(validate_manifest_data(manifest))
            if "open_source_manifest.json" in names:
                try:
                    oss_manifest = json.loads(
                        bundle.read("open_source_manifest.json").decode("utf-8")
                    )
                except Exception as exc:
                    errors.append(f"invalid zipped open_source_manifest.json: {exc}")
                else:
                    errors.extend(validate_open_source_manifest_data(oss_manifest))
            if "UPDATE_CHANNEL.json" in names:
                try:
                    update_channel = json.loads(
                        bundle.read("UPDATE_CHANNEL.json").decode("utf-8")
                    )
                except Exception as exc:
                    errors.append(f"invalid zipped UPDATE_CHANNEL.json: {exc}")
                else:
                    errors.extend(validate_update_channel_data(update_channel))
    except Exception as exc:
        errors.append(f"invalid bundle zip: {exc}")

    return errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate 54GUC Windows portable bundle outputs")
    parser.add_argument("--stage-dir", default="", help="Stage directory to validate")
    parser.add_argument("--bundle-zip", default="", help="Bundle zip to validate")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    checks_run = 0
    errors: list[str] = []

    if str(args.stage_dir or "").strip():
        checks_run += 1
        errors.extend(validate_stage_dir(Path(str(args.stage_dir))))
    if str(args.bundle_zip or "").strip():
        checks_run += 1
        errors.extend(validate_bundle_zip(Path(str(args.bundle_zip))))

    if checks_run <= 0:
        parser.error("at least one of --stage-dir or --bundle-zip is required")

    if errors:
        for item in errors:
            print(item)
        return 1

    print("Windows portable bundle validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
