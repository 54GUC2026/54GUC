from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_ATOMIC_TEMP_ID_RE = re.compile(r"[0-9a-f]{32}")


def _tmp_path(path: Path) -> Path:
    # One UUID is already a collision-resistant transaction identity. Keeping
    # the target name is useful for crash inspection, while removing redundant
    # PID/thread/nanosecond components keeps real Windows staging paths below
    # the Windows MAX_PATH budget without weakening atomic replace semantics.
    return path.with_name(f".{path.name}.tmp.{uuid.uuid4().hex}")


def cleanup_abandoned_atomic_temps(path: str | Path) -> int:
    """Remove only this target owner's recognized abandoned UUID temp files.

    Callers invoke this once before starting their sole writer. Files with a
    different target prefix, an old/unknown suffix, or a non-regular file type
    are not touched.
    """

    target = Path(path)
    parent = target.parent
    if not parent.is_dir():
        return 0
    prefix = f".{target.name}.tmp."
    removed = 0
    for candidate in parent.iterdir():
        name = candidate.name
        if not name.startswith(prefix):
            continue
        temp_id = name[len(prefix):]
        if _ATOMIC_TEMP_ID_RE.fullmatch(temp_id) is None:
            continue
        mode = candidate.lstat().st_mode
        if not stat.S_ISREG(mode):
            continue
        candidate.unlink()
        removed += 1
    return removed


def fsync_parent_dir(path: str | Path, *, logger: Any = None) -> None:
    path = Path(path)
    if os.name == "nt":
        return
    fd: int | None = None
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        os.fsync(fd)
    except Exception as exc:
        if logger is not None:
            logger.debug("fsync_parent_dir: suppressed %s", exc)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception as exc:
                if logger is not None:
                    logger.debug("fsync_parent_dir: suppressed %s", exc)


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    replace_attempts: int = 8,
    retry_base_s: float = 0.03,
    sync_parent: bool = True,
    mode: int | None = None,
    logger: Any = None,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(target)
    try:
        # Atomic JSON/checkpoint descriptors are byte contracts.  Disable the
        # Windows text layer's LF -> CRLF translation so the bytes hashed by a
        # caller are the bytes durably published on every platform.
        with open(tmp, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            tmp.chmod(int(mode))

        attempts = max(1, int(replace_attempts or 1))
        replace_error: Exception | None = None
        for attempt in range(attempts):
            try:
                os.replace(tmp, target)
                replace_error = None
                break
            except OSError as exc:
                replace_error = exc
                if attempt >= attempts - 1:
                    raise
                time.sleep(float(retry_base_s) * float(2 ** attempt))
        if replace_error is not None:
            raise replace_error
        if sync_parent:
            fsync_parent_dir(target, logger=logger)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception as exc:
            if logger is not None:
                logger.debug("atomic_write_text cleanup suppressed %s", exc)


@contextmanager
def atomic_output_path(
    path: str | Path,
    *,
    mode: int | None = None,
    sync_parent: bool = True,
    logger: Any = None,
) -> Iterator[Path]:
    """Yield a sibling path and publish it only after its writer closes.

    Binary/container writers such as gzip and ZipFile cannot use
    :func:`atomic_write_text`.  This boundary gives them the same durable
    temp-write -> close -> replace lifecycle and never exposes a partial final
    artifact to readers.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(target)
    try:
        yield tmp
        if not tmp.is_file():
            raise FileNotFoundError(f"atomic output writer did not create {tmp}")
        # Windows' CRT rejects fsync on a descriptor opened read-only.  Reopen
        # the already completed container without truncation but with write
        # capability, sync its bytes, then apply any final restrictive mode.
        with open(tmp, "r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            tmp.chmod(int(mode))
        os.replace(tmp, target)
        if sync_parent:
            fsync_parent_dir(target, logger=logger)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception as exc:
            if logger is not None:
                logger.debug("atomic_output_path cleanup suppressed %s", exc)


def atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
    ensure_ascii: bool = True,
    trailing_newline: bool = False,
    **kwargs: Any,
) -> None:
    text = json.dumps(payload, indent=indent, sort_keys=sort_keys, ensure_ascii=ensure_ascii)
    if trailing_newline:
        text += "\n"
    atomic_write_text(path, text, **kwargs)


def append_text_durable(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    sync_parent: bool = True,
    logger: Any = None,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding=encoding) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    if sync_parent:
        fsync_parent_dir(target, logger=logger)
