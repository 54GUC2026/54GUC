"""Read-only helpers for current-run JSON artifacts. Operator diagnostics and guard scripts
can inspect Foundation output without importing Intelligence, history, or old sidecar
code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


_NO_DEFAULT = object()


def read_json_artifact(
    path: str | Path,
    *,
    default_value: Any = _NO_DEFAULT,
) -> tuple[Any | None, str, Path | None]:
    """Read one JSON artifact and return ``(payload, source, resolved_path)``.

    ``source`` is explicit evidence, not truth promotion:

    - ``primary`` means the requested file was decoded;
    - ``missing`` means no file exists and no default value was requested;
    - ``invalid_json`` means the file exists but could not be decoded;
    - ``default_value`` means the caller requested a display/report default.
    """

    del path, default_value
    raise RuntimeError(
        "foundation_weaker_artifact_reader_deleted_use_evidence_snapshot"
    )


__all__ = ("read_json_artifact",)
