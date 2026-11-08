"""Bounded reader for live JSON diagnostics published by atomic replacement."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any


class LiveArtifactReadError(RuntimeError):
    """A live diagnostic snapshot could not be read within its boundary budget."""


def read_live_bytes(
    path: str | Path,
    *,
    timeout_s: float = 1.0,
    retry_interval_s: float = 0.025,
) -> bytes:
    """Read one complete live artifact through a bounded sharing-window adapter."""

    source = Path(path)
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    interval = max(0.0, float(retry_interval_s))
    attempts = 0
    last_error: Exception | None = None
    while True:
        attempts += 1
        try:
            return source.read_bytes()
        except (FileNotFoundError, PermissionError) as exc:
            last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            time.sleep(min(interval, remaining))

    assert last_error is not None
    raise LiveArtifactReadError(
        "live diagnostic byte read failed "
        f"after {attempts} attempts: {source}: "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


def read_live_json_object(
    path: str | Path,
    *,
    timeout_s: float = 1.0,
    retry_interval_s: float = 0.025,
) -> dict[str, Any]:
    """Read one complete JSON object while another process may replace its file. Windows
    can briefly block the open during replacement, so retry only for that short window.
    Never use an older file or hide a lasting failure.
    """

    source = Path(path)
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    interval = max(0.0, float(retry_interval_s))
    attempts = 0
    last_error: Exception | None = None
    while True:
        attempts += 1
        try:
            payload = json.loads(source.read_bytes().decode("utf-8"))
        except (
            FileNotFoundError,
            PermissionError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            time.sleep(min(interval, remaining))
            continue
        if not isinstance(payload, dict):
            raise LiveArtifactReadError(
                f"live diagnostic JSON is not an object: {source}"
            )
        return payload

    assert last_error is not None
    raise LiveArtifactReadError(
        "live diagnostic JSON read failed "
        f"after {attempts} attempts: {source}: "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


__all__ = ("LiveArtifactReadError", "read_live_bytes", "read_live_json_object")
