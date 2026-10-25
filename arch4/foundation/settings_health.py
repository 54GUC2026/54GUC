"""Read-only health projection for Foundation-composition settings stores."""

from __future__ import annotations

import os
from pathlib import Path

from arch4.core.runtime_defaults import FILENAME as CORE_SETTINGS_FILENAME
from arch4.foundation.operator_settings import FILENAME as FOUNDATION_SETTINGS_FILENAME
from arch4.license_credential_store import STATE_FILENAME as LICENSE_SETTINGS_FILENAME
from arch4.ngui.settings_store import FILENAME as NGUI_SETTINGS_FILENAME


FOUNDATION_SETTING_FILENAMES = (
    CORE_SETTINGS_FILENAME,
    NGUI_SETTINGS_FILENAME,
    FOUNDATION_SETTINGS_FILENAME,
    LICENSE_SETTINGS_FILENAME,
)


def foundation_settings_persistence_status(
    storage_root: str | Path,
) -> dict[str, object]:
    """Report light-product storage health without querying Intelligence."""

    root = Path(storage_root).resolve()
    root_exists = root.is_dir()
    root_writable = bool(root_exists and os.access(root, os.W_OK))
    files = {
        name: {
            "exists": (root / name).is_file(),
            "writable": (
                bool(os.access(root / name, os.W_OK))
                if (root / name).is_file()
                else root_writable
            ),
        }
        for name in FOUNDATION_SETTING_FILENAMES
    }
    unwritable = [
        name
        for name, status in files.items()
        if status["exists"] and not status["writable"]
    ]
    degraded = not root_exists or not root_writable or bool(unwritable)
    if not root_exists:
        reason = f"Settings directory does not exist: {root}"
    elif not root_writable:
        reason = f"Settings directory not writable: {root}"
    elif unwritable:
        reason = "Owner settings files not writable: " + ",".join(unwritable)
    else:
        reason = ""
    return {
        "settings_dir": str(root),
        "settings_files": files,
        "settings_writable": root_writable,
        "degraded": degraded,
        "reason": reason,
    }


__all__ = (
    "FOUNDATION_SETTING_FILENAMES",
    "foundation_settings_persistence_status",
)
