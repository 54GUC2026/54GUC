"""Small, non-authoritative filesystem helpers for the Windows launcher."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Tuple


log = logging.getLogger("arch4.portable_launcher")


def ensure_portable_dirs(
    output_dir: Path,
    memory_dir: Path,
    *,
    verify_write: bool = True,
) -> Tuple[bool, str]:
    """Create the declared roots and optionally verify output writability."""

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        memory_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, f"Cannot create directories: {exc}"
    if verify_write:
        probe = output_dir / ".write_test"
        try:
            probe.write_text("test", encoding="utf-8")
            probe.unlink()
        except Exception as exc:
            return False, f"Cannot write to output directory: {exc}"
    return True, ""


def validate_config_file(config_path: Path) -> Tuple[bool, str]:
    """Validate external settings through the sole decoder."""

    if not config_path.exists():
        return True, ""
    try:
        from arch4.external_settings_decoder import (
            EXTERNAL_FILENAMES,
            validate_external_settings_source,
        )

        if config_path.name not in EXTERNAL_FILENAMES:
            return False, "unsupported external settings filename"
        status = validate_external_settings_source(config_path.parent)
        if not status["present"] or status["source_name"] != config_path.name:
            return False, "external settings source resolution mismatch"
        return True, ""
    except Exception as exc:
        log.error("External settings validation failed: %s", exc)
        return False, str(exc)


def get_lock_file_pid(lock_path: Path) -> Optional[int]:
    """Read the PID from the single-instance lock file."""

    try:
        if not lock_path.exists():
            return None
        return int(lock_path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def is_process_running(pid: int) -> bool:
    """Return whether the declared lock owner is still running."""

    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information,
                False,
                pid,
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False
    except Exception:
        return False


def cleanup_stale_lock(lock_path: Path) -> bool:
    """Remove a lock only after proving its owning process is absent."""

    pid = get_lock_file_pid(lock_path)
    if pid is None or is_process_running(pid):
        return False
    try:
        lock_path.unlink()
        log.info("Removed stale lock file (PID %d not running)", pid)
        return True
    except Exception as exc:
        log.warning("Failed to remove stale lock: %s", exc)
        return False


__all__ = (
    "cleanup_stale_lock",
    "ensure_portable_dirs",
    "get_lock_file_pid",
    "is_process_running",
    "validate_config_file",
)
