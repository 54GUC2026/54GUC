"""
Resolve canonical runtime output/memory/config roots.

All runtime paths are relative to an "app root" which is:
- Frozen (freezer): directory containing the executable
- Development: repository root (parent of arch4/)

Live runs may override via environment variables:
- ARCH_APP_ROOT: override app root entirely
- ARCH_RUNTIME_OUTPUT_DIR: override output directory
- ARCH_RUNTIME_MEMORY_DIR: override memory directory
- ARCH_RUNTIME_OUTPUT_MODEL_DIR: override isolated model-port output
- ARCH_RUNTIME_MEMORY_MODEL_DIR: override isolated model-port memory

This allows the runtime to be fully portable (USB stick) or to split
storage across filesystems (e.g., fast local SSD for output, network
share for memory).

Historical/settings migration is intentionally not implemented here.  Runtime
path resolution cannot copy prior outputs, learned state, or operator settings
into a current owner.  Named Foundation/Intelligence boundary adapters own any
approved import after validation.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

from arch4.interfaces import (
    ARCH_RUNTIME_MEMORY_DIR,
    ARCH_RUNTIME_MEMORY_MODEL_DIR,
    ARCH_RUNTIME_OUTPUT_DIR,
    ARCH_RUNTIME_OUTPUT_MODEL_DIR,
)
from arch4.config_file_contract import CONFIG_FILENAME

log = logging.getLogger("arch4.runtime_paths")
CGF_STATE_DIRNAME = "cgf_state"

# ── App Root Resolution ──────────────────────────────────────────────────────

_app_root_cache: Optional[Path] = None


def resolve_app_root() -> Path:
    """
    Resolve the application root directory.
    
    Order of precedence:
    1. ARCH_APP_ROOT environment variable (if set)
    2. Frozen mode: directory containing sys.executable
    3. Development mode: repository root (parent of arch4/)
    
    The result is cached for performance.
    """
    global _app_root_cache
    if _app_root_cache is not None:
        return _app_root_cache
    
    # Check environment override first
    env_root = os.environ.get("ARCH_APP_ROOT", "").strip()
    if env_root:
        _app_root_cache = Path(env_root).expanduser().resolve()
        return _app_root_cache
    
    # Frozen (freezer) mode
    if getattr(sys, "frozen", False):
        _app_root_cache = Path(sys.executable).resolve().parent
        return _app_root_cache
    
    # Development mode: arch4/ is inside repo root
    _app_root_cache = Path(__file__).resolve().parents[1]
    return _app_root_cache


def clear_app_root_cache() -> None:
    """Clear the cached app root (for testing)."""
    global _app_root_cache
    _app_root_cache = None


# ── Runtime Directory Resolution ─────────────────────────────────────────────

def _resolve_path(env_name: str, default_name: str) -> Path:
    """
    Resolve a runtime path from environment variable or default.
    
    If env var is set, use it (supports ~ expansion).
    Otherwise, return default_name relative to app root.
    """
    raw = os.environ.get(env_name, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return resolve_app_root() / default_name


# ── Writable Root Fallback ───────────────────────────────────────────────────

_writable_root_cache: Optional[Path] = None
_writable_root_fallback_used: bool = False


def _probe_writable(path: Path) -> bool:
    """Test whether *path* can be created and written to.

    Returns True if a small file can be written and removed.
    Does NOT create persistent directories — only probes.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".guc_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def _fallback_candidates() -> list[Path]:
    """Return ordered fallback roots for writable storage.

    Order:
      1. %LOCALAPPDATA%/54GUC  (Windows user-local, survives USB removal)
      2. %APPDATA%/54GUC       (Windows roaming, less preferred)
      3. %TEMP%/54GUC          (last resort — data lost on reboot)
      4. ~/.54guc              (POSIX fallback)
    """
    candidates: list[Path] = []
    for env_name in ("LOCALAPPDATA", "APPDATA", "TEMP", "TMP"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            candidates.append(Path(raw) / "54GUC")
    # POSIX fallback
    home = Path.home()
    if home.is_dir():
        candidates.append(home / ".54guc")
    # Final POSIX-safe fallback when TEMP/TMP are unset and HOME may be
    # unavailable or non-writable in restricted test/runtime environments.
    try:
        temp_root = Path(tempfile.gettempdir())
        candidates.append(temp_root / "54GUC")
    except Exception:
        pass
    # Preserve order while removing duplicates.
    deduped: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    return deduped


def resolve_writable_root() -> Path:
    """Resolve a writable root directory with deterministic fallback.

    Tries app_root first (portable layout). If app_root is not writable
    (read-only USB, Program Files, locked drive), falls back through
    system-level candidates.

    The chosen root is cached for the process lifetime.

    Returns the resolved writable root. Logs a warning if a fallback
    was used. Raises RuntimeError only if ALL candidates fail.
    """
    global _writable_root_cache, _writable_root_fallback_used

    if _writable_root_cache is not None:
        return _writable_root_cache

    app_root = resolve_app_root()

    # Try app root first (preferred: portable layout).
    if _probe_writable(app_root):
        _writable_root_cache = app_root
        _writable_root_fallback_used = False
        return app_root

    # App root not writable — try fallbacks.
    log.warning(
        "App root not writable: %s — trying fallback locations", app_root
    )
    for candidate in _fallback_candidates():
        if _probe_writable(candidate):
            _writable_root_cache = candidate
            _writable_root_fallback_used = True
            log.warning(
                "Using fallback writable root: %s (app root %s not writable)",
                candidate, app_root,
            )
            return candidate

    raise RuntimeError(
        f"No writable root found. Tried app root ({app_root}) and "
        f"fallback locations. Check disk permissions."
    )


def is_fallback_root_active() -> bool:
    """Return True if the writable root is a fallback, not app_root."""
    return bool(_writable_root_fallback_used)


def clear_writable_root_cache() -> None:
    """Clear the writable root cache (for testing)."""
    global _writable_root_cache, _writable_root_fallback_used
    _writable_root_cache = None
    _writable_root_fallback_used = False


def resolve_runtime_output_dir() -> Path:
    """Resolve the runtime output directory (telemetry, logs, health files)."""
    return _resolve_path("ARCH_RUNTIME_OUTPUT_DIR", ARCH_RUNTIME_OUTPUT_DIR)


def resolve_runtime_memory_dir() -> Path:
    """Resolve the runtime memory directory (brain state, calibration)."""
    return _resolve_path("ARCH_RUNTIME_MEMORY_DIR", ARCH_RUNTIME_MEMORY_DIR)


def resolve_model_runtime_output_dir() -> Path:
    """Resolve isolated output for the hardware-incapable model test port."""
    raw = os.environ.get("ARCH_RUNTIME_OUTPUT_MODEL_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return _resolve_path("ARCH_RUNTIME_OUTPUT_DIR", ARCH_RUNTIME_OUTPUT_MODEL_DIR)


def resolve_model_runtime_memory_dir() -> Path:
    """Resolve isolated memory for the hardware-incapable model test port."""
    raw = os.environ.get("ARCH_RUNTIME_MEMORY_MODEL_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return _resolve_path("ARCH_RUNTIME_MEMORY_DIR", ARCH_RUNTIME_MEMORY_MODEL_DIR)


def resolve_cgf_product_storage(
    modem_backend: str,
    *,
    runtime_dir: str | Path | None = None,
) -> tuple[Path, Path]:
    """Select the sole CGF runtime/state roots for one launch.

    The hardware-incapable model port must never read or overwrite live CGF
    state.  Operational correctness state always lives in the dedicated
    ``cgf_state`` namespace below the selected memory root; diagnostic output
    is never a restore source.  An explicit caller-supplied runtime root is an
    isolated boundary whose state remains segregated in the same topology.
    """

    if runtime_dir is not None:
        explicit = Path(runtime_dir).expanduser().resolve()
        return explicit, explicit / CGF_STATE_DIRNAME
    if str(modem_backend or "").strip().lower() == "model":
        return (
            resolve_model_runtime_output_dir(),
            resolve_model_runtime_memory_dir() / CGF_STATE_DIRNAME,
        )
    return (
        resolve_runtime_output_dir(),
        resolve_runtime_memory_dir() / CGF_STATE_DIRNAME,
    )


def resolve_config_dir() -> Path:
    """Resolve the configuration directory (guc_config.xml location)."""
    raw = os.environ.get("ARCH_CONFIG_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return resolve_app_root()


def resolve_config_path() -> Path:
    """Resolve the external settings input path."""
    return resolve_config_dir() / CONFIG_FILENAME
