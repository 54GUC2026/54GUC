"""Export reviewed committed source only; private privacy rules stay external.

This private release boundary does not participate in application builds.
Neither a clean checkout nor this scanner replaces review of a public manifest.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
import zlib


_MANIFEST_SCHEMA = "54GUC.PublicSourceExportManifest.v1"
_RULE_SCHEMA = "54GUC.PrivateSourcePrivacyRules.v1"
_EMBEDDED_MANIFEST = "PUBLIC_SOURCE_MANIFEST.json"
_MAX_FILE = 32 * 1024 * 1024
_MAX_TOTAL = 128 * 1024 * 1024
_MAX_METADATA = 16 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_ROLES = {"runtime_source", "resource", "build_source", "test_source", "test_fixture", "public_document", "legal_notice"}
_GUIDES = tuple("docs/CGF_MUST_READ/BETA_PACKAGE/" + name for name in (
    "START_HERE.txt", "USER_GUIDE.txt", "SUPPORTED_HARDWARE_AND_LIMITATIONS.txt",
    "RECOVERY_AND_SUPPORT.txt", "BETA_RELEASE_NOTES.txt",
))
_CODECS = tuple("arch4/modem_live/profile_definition/codecs/" + name for name in (
    "huawei-hilink.v1.json", "routeros.v1.json", "tplink-dev2.v1.json",
    "xiaomi-miwifi.v1.json", "zte-goform.v1.json", "zte-zwrt.v1.json",
))
_EXACT_REQUIRED = {
    "LICENSE", "LICENSING.md", "README.md", "CONTRIBUTING.md", "SECURITY.md",
    "docs/BUILDING.md", "docs/TESTING.md", "docs/PROTOCOL_REFERENCES.md",
    "VERSION", "RELEASE_CHANNEL", "UPDATE_CHANNEL.json", "open_source_manifest.json",
    "requirements.txt", "requirements-runtime-pins.txt", "requirements-dev.txt",
    "requirements-ngui-visual.txt", "pytest.ini", "LUC-Windows-Native.spec",
    "arch4/windows_native_launcher.py",
    "arch4/modem_live/profile_definition/field-definitions.v2.json",
    "arch4/modem_live/profile_definition/metadata-schema.json",
    "arch4/ngui/fonts/LICENSE-SIL-OFL.txt", "third_party/NOTICE.txt",
    "arch4/ngui/fonts/LiberationMono-Regular.ttf", "arch4/ngui/fonts/LiberationMono-Bold.ttf",
    "arch4/ngui/fonts/LiberationMono-Italic.ttf", "arch4/ngui/fonts/LiberationMono-BoldItalic.ttf",
    "scripts/windows_portable_control.ps1", "scripts/windows_native_bootstrap.ps1",
    "scripts/build_windows_portable.ps1", "scripts/export_windows_build.ps1",
    "scripts/build_windows_native_bundle.ps1", "scripts/test_windows_portable.ps1",
    "scripts/cgf_windows_path_registry.ps1", "tools/generate_open_source_manifest.py",
    "tools/prepare_public_source.py", "scripts/package_active_source.sh",
    *_GUIDES, *_CODECS,
}
_PRIVATE_TOP = {".git", ".github", ".venv", "venv", "archive", "GPT", "ops", "artifacts", "portable_runtime", "support_bundles", "logs"}
_PUBLIC_ISSUE_FORMS = {
    ".github/ISSUE_TEMPLATE/router-test.yml",
    ".github/ISSUE_TEMPLATE/profile-proposal.yml",
    ".github/ISSUE_TEMPLATE/bug-report.yml",
}
_PRIVATE_ROOT_FILES = {
    "AGENTS.md", "AGENT_BOOTSTRAP.md", "HANDOFF_54GUC_CURRENT.md",
    "MUST_READ_TX1_CODEX_HISTORY_RESTORE.md", "NEXT_STEP_COMMAND.txt", "TX1_LIVE_PROCESS.md",
}
_STATE_PARTS = {"__pycache__", ".git", "portable_runtime", "modems.local", "logs", "support_bundles", "guc_output", "guc_memory", ".replay_retention_state"}
_PRIVATE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".wal", ".shm", ".bak", ".pfx", ".p12", ".pem", ".key", ".patch", ".diff", ".pyc", ".pyd", ".exe", ".dll", ".7z", ".rar", ".xz", ".bz2", ".zst", ".tar"}
# Known unsupported containers are refused, not decoded by another parser.
_UNSUPPORTED_CONTAINER_MAGIC = (
    (0, b"BZh"), (0, b"\xfd7zXZ\x00"), (0, b"\x28\xb5\x2f\xfd"),
    (0, b"7z\xbc\xaf\x27\x1c"), (0, b"Rar!\x1a\x07"),
    (257, b"ustar\x00"), (257, b"ustar "),
)
_PEM_KEY = re.compile(rb"-----BEGIN (?:ENCRYPTED |RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\r\n]{32,}-----END (?:ENCRYPTED |RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")


class ExportBlocked(RuntimeError):
    pass


def _fail(code: str):
    # Codes are fixed literals from this module; never include a private rule,
    # source snippet, local path, native stderr or user configuration value.
    raise ExportBlocked("Source export blocked (" + code + "). No archive was published.")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("DUPLICATE_METADATA_FIELD")
        result[key] = value
    return result


def _json_bytes(path: Path, *, maximum: int) -> tuple[dict, bytes]:
    try:
        if not path.is_file() or path.is_symlink() or path.is_junction() or path.stat().st_size > maximum:
            _fail("METADATA_FILE_INVALID")
        raw = path.read_bytes()
        value = json.loads(raw, object_pairs_hook=_unique)
    except (OSError, ValueError, UnicodeError):
        _fail("METADATA_FILE_INVALID")
    if not isinstance(value, dict):
        _fail("METADATA_SHAPE_INVALID")
    return value, raw


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value or ":" in value or any(ord(c) < 32 for c in value):
        _fail("UNSAFE_RELATIVE_PATH")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or path.as_posix() != value or any(p in {"", ".", ".."} or p.endswith((".", " ")) for p in path.parts):
        _fail("UNSAFE_RELATIVE_PATH")
    for part in path.parts:
        if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part):
            _fail("UNSAFE_RELATIVE_PATH")
    return value


def _forbidden(relative: str, *, nested_fixture: bool = False):
    path = PurePosixPath(relative)
    lowered = {p.casefold() for p in path.parts}
    folded_name = path.name.casefold()
    folded_relative = relative.casefold()
    if (path.parts[0].casefold() in {p.casefold() for p in _PRIVATE_TOP} and relative not in _PUBLIC_ISSUE_FORMS) or folded_name in {name.casefold() for name in _PRIVATE_ROOT_FILES}:
        _fail("PRIVATE_OPERATIONAL_PATH")
    if path.suffix.casefold() in _PRIVATE_SUFFIXES or re.search(r"(?i)\.(?:sqlite3?|db)-(?:wal|shm)$", path.name) or path.name.casefold() in {"license.dat", "token.txt", ".env"} or folded_name.startswith(".env."):
        _fail("PRIVATE_KEY_STATE_OR_BINARY_PATH")
    if ".git" in lowered or (not nested_fixture and (lowered & _STATE_PARTS or any(p.casefold().endswith(("_output", "_memory")) for p in path.parts))):
        _fail("PRIVATE_RUNTIME_OR_HISTORY_PATH")
    if folded_relative.startswith("docs/must_read/") or (folded_relative.startswith("docs/cgf_must_read/") and relative not in _GUIDES):
        _fail("PRIVATE_OPERATIONAL_DOCUMENT")


def _git(source: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(["git", "-C", str(source), *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError:
        _fail("SOURCE_GIT_STATE_UNAVAILABLE")
    if result.returncode:
        _fail("SOURCE_GIT_STATE_UNAVAILABLE")
    return result.stdout


def _clean_commit(source: Path, commit: str):
    if _git(source, "rev-parse", "HEAD").decode("ascii", "strict").strip() != commit:
        _fail("SOURCE_COMMIT_CHANGED")
    if _git(source, "status", "--porcelain=v1", "--untracked-files=all").strip():
        _fail("SOURCE_NOT_CLEAN")


def _tracked_tree(source: Path, commit: str) -> dict[str, str]:
    result = {}
    try:
        for row in _git(source, "ls-tree", "-r", "-z", commit).split(b"\0"):
            if not row:
                continue
            header, name = row.split(b"\t", 1)
            mode, kind, object_id = header.decode("ascii").split(" ")
            relative = name.decode("utf-8", "strict")
            result[relative] = mode
    except (ValueError, UnicodeError):
        _fail("SOURCE_TREE_INVALID")
    return result


def _mandatory_paths(tracked: dict[str, str]) -> set[str]:
    profiles = {p for p in tracked if p.startswith("modem_profiles/") and PurePosixPath(p).suffix == ".json" and len(PurePosixPath(p).parts) == 2}
    if len(profiles) != 48:
        _fail("PROFILE_INVENTORY_REQUIRES_REVIEW")
    codecs = {p for p in tracked if p.startswith("arch4/modem_live/profile_definition/codecs/") and p.endswith(".json")}
    if codecs != set(_CODECS):
        _fail("CODEC_INVENTORY_REQUIRES_REVIEW")
    required = set(_EXACT_REQUIRED) | profiles
    required |= {p for p in tracked if (
        p.startswith("arch4/") and p.endswith(".py") and not p.startswith("arch4/tests/")
        or p.startswith(("hooks/", "third_party/", "arch4/ngui/fonts/", "config/"))
        or p.startswith("tools/modem_profile_exchange/") and "/tests/" not in p and not PurePosixPath(p).name.startswith("test_")
    )}
    if not required <= set(tracked):
        _fail("MANDATORY_SOURCE_INPUT_MISSING")
    return required


def _scan_bytes(data: bytes, private_literals: tuple[str, ...], *, depth: int = 0, fixture: bool = False, name_only: bool = False):
    if len(data) > _MAX_FILE:
        _fail("INPUT_SIZE_REQUIRES_REVIEW")
    if not name_only and any(data[offset:offset + len(magic)] == magic for offset, magic in _UNSUPPORTED_CONTAINER_MAGIC):
        _fail("UNSUPPORTED_COMPRESSED_CONTAINER")
    if _PEM_KEY.search(data):
        _fail("PRIVATE_KEY_MATERIAL_DETECTED")
    variants = []
    for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
        text = data.decode(encoding, errors="ignore")
        # Decode ordinary source/JSON escape spellings without executing code.
        text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), text)
        text = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m[1], 16)), text)
        text = text.replace("\\n", "\n").replace("\\r", "\r")
        if _PEM_KEY.search(text.encode("utf-8", errors="ignore")):
            _fail("PRIVATE_KEY_MATERIAL_DETECTED")
        variants.append(unicodedata.normalize("NFKC", text).casefold())
    if any(needle in text for needle in private_literals for text in variants):
        _fail("PRIVATE_RULE_HIT")
    if name_only:
        return
    if not fixture:
        if data.startswith(b"\x1f\x8b") or zipfile.is_zipfile(io.BytesIO(data)):
            _fail("COMPRESSED_DATA_NEEDS_FIXTURE_REVIEW")
        return
    if depth >= 2 and (data.startswith(b"\x1f\x8b") or zipfile.is_zipfile(io.BytesIO(data))):
        _fail("NESTED_FIXTURE_REQUIRES_REVIEW")
    try:
        if data.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                expanded = stream.read(_MAX_FILE + 1)
            _scan_bytes(expanded, private_literals, depth=depth + 1, fixture=True)
        elif zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as bundle:
                members = bundle.infolist()
                if len(members) > 2048 or sum(item.file_size for item in members) > _MAX_FILE:
                    _fail("FIXTURE_SIZE_REQUIRES_REVIEW")
                member_names = set()
                for item in members:
                    name = _relative(item.filename.rstrip("/"))
                    folded_name = name.casefold()
                    if folded_name in member_names:
                        _fail("DUPLICATE_FIXTURE_PATH")
                    member_names.add(folded_name)
                    _forbidden(name, nested_fixture=True)
                    _scan_bytes(name.encode("utf-8"), private_literals, name_only=True)
                    if stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1:
                        _fail("UNSAFE_OR_ENCRYPTED_FIXTURE_MEMBER")
                    if not item.is_dir():
                        with bundle.open(item) as member:
                            expanded = member.read(_MAX_FILE + 1)
                        _scan_bytes(expanded, private_literals, depth=depth + 1, fixture=True)
    except (OSError, ValueError, EOFError, zipfile.BadZipFile, zlib.error):
        _fail("FIXTURE_CANNOT_BE_INSPECTED")


def _file_path(source: Path, relative: str):
    current = source
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            _fail("SOURCE_LINK_REFUSED")
    if not current.is_file() or not _inside(current.resolve(), source):
        _fail("SOURCE_FILE_MISSING")


def _no_links(path: Path):
    for current in (path.absolute(), *path.absolute().parents):
        if current.is_symlink() or current.is_junction():
            _fail("INPUT_OR_OUTPUT_LINK_REFUSED")


def export_public_source(*, source: Path, manifest_file: Path, private_rules_file: Path, output: Path, expected_commit: str) -> dict:
    if os.name != "nt":
        _fail("NATIVE_WINDOWS_REQUIRED")
    # Publication and hold checks happen before output/temp-directory creation.
    manifest, manifest_raw = _json_bytes(manifest_file, maximum=_MAX_METADATA)
    if manifest.get("publication_ready") is not True or manifest.get("holds") != []:
        _fail("PUBLICATION_NOT_READY_OR_HELD")
    if set(manifest) != {"schema", "publication_ready", "holds", "source_commit", "entries"}:
        _fail("MANIFEST_SHAPE_REQUIRES_REVIEW")
    if manifest.get("schema") != _MANIFEST_SCHEMA or manifest.get("source_commit") != expected_commit or _COMMIT.fullmatch(expected_commit) is None:
        _fail("MANIFEST_COMMIT_INVALID")
    for path in (source, manifest_file, private_rules_file, output):
        _no_links(path)
    source = source.resolve()
    output = output.resolve()
    private_rules_file = private_rules_file.resolve()
    if _inside(private_rules_file, source) or private_rules_file == manifest_file.resolve():
        _fail("PRIVATE_RULES_MUST_BE_EXTERNAL")
    rules, rules_raw = _json_bytes(private_rules_file, maximum=1024 * 1024)
    raw_literals = rules.get("literal_denylist")
    if rules.get("schema") != _RULE_SCHEMA or rules.get("approved") is not True or rules.get("reviewed_manifest_sha256") != _sha(manifest_raw) or not isinstance(raw_literals, list) or not 1 <= len(raw_literals) <= 2048:
        _fail("PRIVATE_RULES_MISSING_OR_UNREVIEWED")
    if any(not isinstance(value, str) or not 2 <= len(value) <= 1024 for value in raw_literals):
        _fail("PRIVATE_RULES_INVALID")
    literals = tuple(unicodedata.normalize("NFKC", value).casefold() for value in raw_literals)
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        _fail("ALLOWLIST_EMPTY")
    selected = {}
    folded = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("review_status") != "approved" or entry.get("role") not in _ROLES:
            _fail("ENTRY_NOT_REVIEWED")
        if set(entry) != {"path", "sha256", "bytes", "review_status", "role"}:
            _fail("ENTRY_SHAPE_OR_HOLD_REQUIRES_REVIEW")
        relative = _relative(entry.get("path"))
        _forbidden(relative)
        if relative.casefold() in folded or relative.casefold() == _EMBEDDED_MANIFEST.casefold():
            _fail("DUPLICATE_OR_RESERVED_PATH")
        folded.add(relative.casefold())
        if not isinstance(entry.get("sha256"), str) or _SHA.fullmatch(entry["sha256"]) is None or type(entry.get("bytes")) is not int or entry["bytes"] < 0:
            _fail("ENTRY_HASH_INVALID")
        if PurePosixPath(relative).suffix.casefold() in {".gz", ".zip"} and entry["role"] != "test_fixture":
            _fail("DATA_FIXTURE_REVIEW_REQUIRED")
        if "/fixtures/" in relative.casefold() and entry["role"] != "test_fixture":
            _fail("DATA_FIXTURE_REVIEW_REQUIRED")
        _scan_bytes(relative.encode("utf-8"), literals, name_only=True)
        selected[relative] = entry
    if output.suffix.casefold() != ".zip" or not output.parent.is_dir() or output.exists() or _inside(output, source):
        _fail("OUTPUT_MUST_BE_NEW_EXTERNAL_ZIP")
    _clean_commit(source, expected_commit)
    tracked = _tracked_tree(source, expected_commit)
    if not _mandatory_paths(tracked) <= set(selected):
        _fail("MANDATORY_ALLOWLIST_INPUT_MISSING")
    payloads = {}
    total = 0
    for relative, entry in sorted(selected.items()):
        if tracked.get(relative) not in {"100644", "100755"}:
            _fail("UNTRACKED_OR_NONREGULAR_INPUT")
        _file_path(source, relative)
        # Read exact committed bytes, not ignored state or newline-filtered
        # working files. A final allowlist must hash these same Git blobs.
        data = _git(source, "cat-file", "blob", expected_commit + ":" + relative)
        if len(data) != entry["bytes"] or _sha(data) != entry["sha256"]:
            _fail("SOURCE_HASH_CHANGED")
        total += len(data)
        if total > _MAX_TOTAL:
            _fail("SOURCE_SIZE_REQUIRES_REVIEW")
        _scan_bytes(data, literals, fixture=entry["role"] == "test_fixture")
        payloads[relative] = data
    public_record = {"schema": "54GUC.PublicSourceArchive.v1", "source_commit": expected_commit, "reviewed_manifest_sha256": _sha(manifest_raw), "entries": [{key: entry[key] for key in ("path", "sha256", "bytes", "role")} for _, entry in sorted(selected.items())]}
    record = json.dumps(public_record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    _scan_bytes(record, literals)
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".54guc-public-source-", suffix=".tmp", dir=output.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w+b") as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for relative, data in [*payloads.items(), (_EMBEDDED_MANIFEST, record)]:
                    info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, data)
            stream.flush()
            os.fsync(stream.fileno())
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None or set(archive.namelist()) != set(payloads) | {_EMBEDDED_MANIFEST}:
                _fail("ARCHIVE_VERIFICATION_FAILED")
            for relative, data in payloads.items():
                if archive.read(relative) != data:
                    _fail("ARCHIVE_VERIFICATION_FAILED")
        archive_data = temporary.read_bytes()
        archive_sha256, archive_size = _sha(archive_data), len(archive_data)
        _clean_commit(source, expected_commit)
        # Windows rename refuses a destination that appeared during validation.
        os.rename(temporary, output)
        temporary = None
    except OSError:
        _fail("OUTPUT_NOT_PUBLISHED")
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return {"status": "SOURCE_ARCHIVE_CREATED", "source_commit": expected_commit, "files": len(payloads), "bytes": archive_size, "sha256": archive_sha256, "private_rules_sha256": _sha(rules_raw)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Export an explicitly reviewed clean public source snapshot")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--private-rules", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-commit", required=True)
    arguments = parser.parse_args()
    try:
        result = export_public_source(source=arguments.source, manifest_file=arguments.manifest, private_rules_file=arguments.private_rules, output=arguments.output, expected_commit=arguments.expected_commit)
    except ExportBlocked as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        print("Source export blocked (UNEXPECTED_INPUT_OR_IO). No archive was published.", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
