"""
arch4/app_info.py
═══════════════════════════════════════════════════════════════
Build and system information for the INFO dialog.

Gathers version, commit hash, interpreter and GUI toolkit versions, OS details,
and runtime paths.  Pure data — no GUI imports.
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import datetime
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ARCH_SW_LABEL = "54GUC"


@dataclass(frozen=True)
class AppInfo:
    luc_version: str
    commit_hash: str
    python_version: str
    pyside_version: str
    os_platform: str
    os_version: str
    architecture: str
    modem_backend: str
    runtime_dir: str
    memory_dir: str
    frozen: bool


def _cheap_absolute_path(pathish: str | os.PathLike[str]) -> Path:
    """Return an absolute-looking path without filesystem canonicalization.

    Runtime sidecars call app/version metadata from hot health paths.  On HP
    source runs, Windows Python sees the app root as a WSL UNC path; resolving
    that path can block the GUI/runner threads.  Metadata discovery only needs
    candidate paths, not symlink/network canonicalization.
    """

    path = Path(pathish).expanduser()
    if path.is_absolute():
        return path
    try:
        return Path.cwd() / path
    except Exception:
        return path


def _candidate_dir_path(pathish: object) -> Path | None:
    if not pathish:
        return None
    try:
        path = _cheap_absolute_path(str(pathish))
    except Exception:
        return None
    if path.suffix:
        return path.parent
    return path


def _version_search_roots(
    *,
    module_file: str | os.PathLike[str],
    executable: str | os.PathLike[str] | None = None,
    argv0: str | os.PathLike[str] | None = None,
    env_root: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> list[Path]:
    """Return candidate roots that may contain VERSION / BUILD_DATE.

    Kept outside ``interfaces.py`` so the heavily imported data-contract module
    does not own bundle filesystem discovery.
    """

    roots: list[Path] = []
    try:
        module_path = _cheap_absolute_path(str(module_file))
    except Exception:
        module_path = _cheap_absolute_path(__file__)

    def _add(pathish: object) -> None:
        path = _candidate_dir_path(pathish)
        if path is None:
            return
        if path not in roots:
            roots.append(path)

    exe = executable if executable is not None else sys.executable
    exe_path = _cheap_absolute_path(str(exe))
    _add(exe_path.parent)
    _add(exe_path.parent.parent)
    _add(argv0 if argv0 is not None else (getattr(sys, "argv", [""])[0] if getattr(sys, "argv", None) else ""))
    _add(env_root if env_root is not None else os.environ.get("ARCH_APP_ROOT", ""))
    _add(cwd if cwd is not None else Path.cwd())
    if len(module_path.parents) >= 3:
        _add(module_path.parents[2])
    _add(module_path.parent.parent)
    return roots


def _read_manifest_value_from_roots(roots: list[Path], *keys: str) -> str:
    wanted = [str(k or "").strip() for k in keys if str(k or "").strip()]
    if not wanted:
        return ""
    for base in roots:
        mf = base / "version_manifest.json"
        if not mf.is_file():
            continue
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for key in wanted:
            value = str(data.get(key, "") or "").strip()
            if value:
                return value
    return ""


def read_runtime_version(
    *,
    module_file: str | os.PathLike[str],
    executable: str | os.PathLike[str] | None = None,
    argv0: str | os.PathLike[str] | None = None,
    env_root: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> str:
    roots = _version_search_roots(
        module_file=module_file,
        executable=executable,
        argv0=argv0,
        env_root=env_root,
        cwd=cwd,
    )
    for base in roots:
        vf = base / "VERSION"
        if vf.is_file():
            try:
                return vf.read_text(encoding="utf-8").strip()
            except Exception:
                pass
    manifest_version = _read_manifest_value_from_roots(roots, "version")
    if manifest_version:
        return manifest_version
    return "v0.0"


def read_runtime_build_date(
    *,
    module_file: str | os.PathLike[str],
    executable: str | os.PathLike[str] | None = None,
    argv0: str | os.PathLike[str] | None = None,
    env_root: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> str:
    roots = _version_search_roots(
        module_file=module_file,
        executable=executable,
        argv0=argv0,
        env_root=env_root,
        cwd=cwd,
    )
    for base in roots:
        bf = base / "BUILD_DATE"
        if bf.is_file():
            try:
                return bf.read_text(encoding="utf-8").strip()
            except Exception:
                pass
    manifest_build_date = _read_manifest_value_from_roots(roots, "build_date")
    if manifest_build_date:
        return manifest_build_date
    for base in roots:
        vf = base / "VERSION"
        if vf.is_file():
            try:
                return datetime.fromtimestamp(vf.stat().st_mtime).strftime("%Y-%m-%d")
            except Exception:
                pass
    return ""


def read_runtime_release_channel(
    *,
    module_file: str | os.PathLike[str],
    executable: str | os.PathLike[str] | None = None,
    argv0: str | os.PathLike[str] | None = None,
    env_root: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> str:
    roots = _version_search_roots(
        module_file=module_file,
        executable=executable,
        argv0=argv0,
        env_root=env_root,
        cwd=cwd,
    )
    for base in roots:
        cf = base / "RELEASE_CHANNEL"
        if cf.is_file():
            try:
                value = str(cf.read_text(encoding="utf-8").strip() or "").strip().lower()
            except Exception:
                value = ""
            if value:
                return value
    manifest_channel = _read_manifest_value_from_roots(roots, "release_channel")
    if manifest_channel:
        return str(manifest_channel).strip().lower()
    return "mainline"


def _normalize_git_path(path: Path, *, repo_root: Path) -> Path:
    """Translate WSL-style gitdir pointers when running under Windows Python."""
    try:
        text = str(path).strip()
        norm = text.replace("\\", "/")
        if os.name == "nt" and norm.startswith("/mnt/") and len(norm) > 6:
            drive = norm[5:6]
            rest = norm[6:]
            rest = rest.replace("/", "\\")
            return Path(f"{drive.upper()}:\\{rest.lstrip('\\\\')}")
    except Exception:
        pass
    return path if path.is_absolute() else (repo_root / path)


def _resolve_git_dir(repo_root: Path) -> Path | None:
    git_path = repo_root / ".git"
    if git_path.is_dir():
        return git_path
    if not git_path.is_file():
        return None
    try:
        line = git_path.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not line.lower().startswith("gitdir:"):
        return None
    target = line.split(":", 1)[1].strip()
    if not target:
        return None
    return _normalize_git_path(Path(target), repo_root=repo_root)


def _read_packed_ref(git_dir: Path, ref_name: str) -> str:
    packed_refs = git_dir / "packed-refs"
    if not packed_refs.is_file():
        return ""
    try:
        for raw_line in packed_refs.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("^"):
                continue
            try:
                commit, name = line.split(" ", 1)
            except ValueError:
                continue
            if name.strip() == ref_name:
                return commit.strip()
    except Exception:
        return ""
    return ""


def _resolve_common_git_dir(git_dir: Path) -> Path:
    commondir = git_dir / "commondir"
    if not commondir.is_file():
        return git_dir
    try:
        target = commondir.read_text(encoding="utf-8").strip()
    except Exception:
        return git_dir
    if not target:
        return git_dir
    return _normalize_git_path(Path(target), repo_root=git_dir)


def _read_commit_hash_from_git_files(repo_root: Path) -> str:
    git_dir = _resolve_git_dir(repo_root)
    if git_dir is None:
        return ""
    common_git_dir = _resolve_common_git_dir(git_dir)
    head_path = git_dir / "HEAD"
    if not head_path.is_file():
        return ""
    try:
        head = head_path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    if not head:
        return ""
    if not head.startswith("ref:"):
        return head[:12]
    ref_name = head.split(":", 1)[1].strip()
    if not ref_name:
        return ""
    for ref_path in (git_dir / ref_name, common_git_dir / ref_name):
        try:
            if ref_path.is_file():
                return ref_path.read_text(encoding="utf-8").strip()[:12]
        except Exception:
            pass
    for packed_dir in (git_dir, common_git_dir):
        commit = _read_packed_ref(packed_dir, ref_name)[:12]
        if commit:
            return commit
    return ""


@lru_cache(maxsize=1)
def _get_commit_hash() -> str:
    """Try to read the commit hash from version_manifest.json first,
    then fall back to running git."""
    # 0) Source-run mirrors may intentionally keep their own .git directory
    # while the active code is refreshed from the canonical committed tree.
    # In that case the launcher can provide the canonical source commit.
    try:
        env_commit = str(os.environ.get("ARCH_SOURCE_COMMIT", "") or "").strip()
        if env_commit:
            return env_commit[:12]
    except Exception:
        pass

    # 1) Check embedded manifest (frozen builds)
    try:
        if getattr(sys, "frozen", False):
            manifest_path = _cheap_absolute_path(str(sys.executable)).parent / "version_manifest.json"
        else:
            manifest_path = _cheap_absolute_path(__file__).parents[1] / "version_manifest.json"
        if manifest_path.is_file():
            import json
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            commit = str(data.get("commit", "") or data.get("source_commit", "") or "").strip()
            if commit:
                return commit[:12]
    except Exception:
        pass

    # 2) Try git directly (development mode)
    try:
        repo_root = _cheap_absolute_path(__file__).parents[1]
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    # 3) Read .git metadata directly (explicit source-tree metadata path; also works when
    # git.exe is unavailable but the checkout is present on disk).
    try:
        repo_root = _cheap_absolute_path(__file__).parents[1]
        commit = _read_commit_hash_from_git_files(repo_root)
        if commit:
            return commit
    except Exception:
        pass

    return "unknown"


@lru_cache(maxsize=1)
def _resolve_app_root() -> Path:
    try:
        if getattr(sys, "frozen", False):
            return _cheap_absolute_path(str(sys.executable)).parent
    except Exception:
        pass
    return _cheap_absolute_path(__file__).parents[1]


@lru_cache(maxsize=1)
def _static_runtime_provenance() -> tuple[tuple[str, object], ...]:
    app_root = _resolve_app_root()
    try:
        cwd = str(Path.cwd())
    except Exception:
        cwd = ""
    try:
        python_executable = str(_cheap_absolute_path(str(sys.executable)))
    except Exception:
        python_executable = str(sys.executable)
    payload = {
        "sw_label": ARCH_SW_LABEL,
        "version": read_runtime_version(module_file=__file__),
        "build_date": read_runtime_build_date(module_file=__file__),
        "release_channel": read_runtime_release_channel(module_file=__file__),
        "commit": _get_commit_hash(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "app_root": str(app_root),
        "cwd": cwd,
        "python_executable": python_executable,
    }
    return tuple(payload.items())


def build_runtime_provenance(
    *,
    runtime_dir: str = "",
    memory_dir: str = "",
    sidecar_role: str = "",
    extra: Optional[dict] = None,
) -> dict:
    payload = dict(_static_runtime_provenance())
    if runtime_dir:
        payload["runtime_dir"] = str(Path(runtime_dir).expanduser())
    if memory_dir:
        payload["memory_dir"] = str(Path(memory_dir).expanduser())
    if sidecar_role:
        payload["sidecar_role"] = str(sidecar_role)
    if isinstance(extra, dict):
        for key, value in extra.items():
            if value in (None, ""):
                continue
            payload[str(key)] = value
    return payload


def _get_pyside_version() -> str:
    try:
        import PySide6
        return PySide6.__version__
    except Exception:
        return "unknown"


def gather_app_info(
    *,
    modem_backend: str = "",
    runtime_dir: str = "",
    memory_dir: str = "",
) -> AppInfo:
    return AppInfo(
        luc_version=read_runtime_version(module_file=__file__),
        commit_hash=_get_commit_hash(),
        python_version=platform.python_version(),
        pyside_version=_get_pyside_version(),
        os_platform=platform.system(),
        os_version=platform.version(),
        architecture=platform.machine(),
        modem_backend=modem_backend or os.environ.get("ARCH_WINDOWS_ALLOWED_BACKENDS", ""),
        runtime_dir=runtime_dir or os.environ.get("ARCH_RUNTIME_OUTPUT_DIR", ""),
        memory_dir=memory_dir or os.environ.get("ARCH_RUNTIME_MEMORY_DIR", ""),
        frozen=bool(getattr(sys, "frozen", False)),
    )
