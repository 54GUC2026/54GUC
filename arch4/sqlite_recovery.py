"""Narrow filesystem boundary for established SQLite corruption only."""
from pathlib import Path
import sqlite3
from uuid import uuid4

from arch4.atomic_io import fsync_parent_dir


def quarantine_corrupt_sqlite(path: Path, error: sqlite3.DatabaseError) -> tuple[Path, ...]:
    """Preserve the database/WAL/SHM or abort; access errors are not corruption.

    Native SQLite result codes are the authority. Unknown errors fail closed;
    messages, exception inheritance and a failed open do not authorize reset.
    Move the database last and restore completed moves if preservation fails.
    """
    code = getattr(error, 'sqlite_errorcode', None)
    if type(code) is not int or code & 0xff not in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        raise error
    token = uuid4().hex
    moved: list[tuple[Path, Path]] = []
    try:
        for suffix in ('-wal', '-shm', ''):
            source = Path(str(path) + suffix)
            if not source.exists():
                continue
            target = source.with_name(source.name + '.corrupt-' + token)
            source.rename(target)
            moved.append((source, target))
        if moved:
            fsync_parent_dir(path)
    except OSError:
        for source, target in reversed(moved):
            target.rename(source)
        raise
    return tuple(target for _, target in moved)
