"""Low-level runtime sidecar health counters.

This module is intentionally below runtime_sidecar and engine_runtime_status in
the import graph. Status builders can read sidecar write health without
importing the sidecar writer/export module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import time

LAST_JSON_SIDECAR_WRITE_FAILURES: dict[str, dict[str, Any]] = {}
JSON_SIDECAR_WRITE_FAILURE_COUNT = 0
JSON_SIDECAR_WRITE_ATTEMPT_COUNT = 0
JSON_SIDECAR_PRIMARY_SUCCESS_COUNT = 0
JSON_SIDECAR_UNCHANGED_SKIP_COUNT = 0
JSON_SIDECAR_LAST_PRIMARY_SUCCESS: dict[str, Any] = {}
JSON_SIDECAR_LAST_TEXT_BY_PATH: dict[str, str] = {}


def format_runtime_iso_utc(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_sidecar_failure_key(path: Path) -> str:
    try:
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        return str(target)
    except Exception:
        return str(path)


def record_json_sidecar_write_attempt(count: int = 1) -> None:
    global JSON_SIDECAR_WRITE_ATTEMPT_COUNT
    JSON_SIDECAR_WRITE_ATTEMPT_COUNT += max(0, int(count or 0))


def record_json_sidecar_unchanged_skip(count: int = 1) -> None:
    global JSON_SIDECAR_UNCHANGED_SKIP_COUNT
    JSON_SIDECAR_UNCHANGED_SKIP_COUNT += max(0, int(count or 0))


def record_json_sidecar_write_failure(
    path: Path,
    exc: Exception,
    *,
    attempts: int,
    phase: str,
) -> None:
    global JSON_SIDECAR_WRITE_FAILURE_COUNT
    now = time.time()
    target = Path(path)
    JSON_SIDECAR_WRITE_FAILURE_COUNT += 1
    LAST_JSON_SIDECAR_WRITE_FAILURES[json_sidecar_failure_key(target)] = {
        "path": str(target),
        "filename": target.name,
        "phase": str(phase or "primary"),
        "attempts": int(max(0, attempts)),
        "error_type": type(exc).__name__,
        "error": str(exc),
        "ts": now,
        "iso": format_runtime_iso_utc(now),
    }


def clear_json_sidecar_write_failure(path: Path) -> None:
    LAST_JSON_SIDECAR_WRITE_FAILURES.pop(json_sidecar_failure_key(Path(path)), None)


def last_json_sidecar_write_failure(path: Path) -> dict[str, Any]:
    detail = LAST_JSON_SIDECAR_WRITE_FAILURES.get(json_sidecar_failure_key(Path(path)))
    return dict(detail) if isinstance(detail, dict) else {}


def json_sidecar_write_health() -> dict[str, Any]:
    failures = list(LAST_JSON_SIDECAR_WRITE_FAILURES.values())
    last = max(
        (dict(item) for item in failures if isinstance(item, dict)),
        key=lambda item: float(item.get("ts", 0.0) or 0.0),
        default={},
    )
    return {
        "write_failure_count": int(JSON_SIDECAR_WRITE_FAILURE_COUNT),
        "write_attempt_count": int(JSON_SIDECAR_WRITE_ATTEMPT_COUNT),
        "primary_success_count": int(JSON_SIDECAR_PRIMARY_SUCCESS_COUNT),
        "unchanged_skip_count": int(JSON_SIDECAR_UNCHANGED_SKIP_COUNT),
        "active_failure_count": len(failures),
        "degraded": bool(failures),
        "last_failure": last,
        "last_primary_success": dict(JSON_SIDECAR_LAST_PRIMARY_SUCCESS),
    }


def record_json_sidecar_primary_success(path: Path) -> None:
    global JSON_SIDECAR_PRIMARY_SUCCESS_COUNT
    now = time.time()
    target = Path(path)
    JSON_SIDECAR_PRIMARY_SUCCESS_COUNT += 1
    JSON_SIDECAR_LAST_PRIMARY_SUCCESS.clear()
    JSON_SIDECAR_LAST_PRIMARY_SUCCESS.update(
        {
            "path": str(target),
            "filename": target.name,
            "ts": now,
            "iso": format_runtime_iso_utc(now),
        }
    )
