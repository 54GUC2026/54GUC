"""
arch4/brain/memory_calibration_store.py
───────────────────────────────────────────────────────────────────────────────
SQLite calibration DB helpers, extracted from memory.py.

These were inline methods on Memory. They are now standalone functions that
accept the db_path, lock, and config they need as explicit arguments, making
them independently testable and keeping memory.py smaller.

Memory wraps each with a thin one-liner that passes self's attributes.
───────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Iterator, List, Optional

from arch4.brain.calibration_contract import (
    calibration_formula_vector,
    calibration_observation_error,
)
from arch4.evidence_scope import validated_evidence_scope

log = logging.getLogger("arch4.brain.memory")

_SQLITE_BUSY_TIMEOUT_S = 2.0
_SQLITE_BUSY_TIMEOUT_MS = int(_SQLITE_BUSY_TIMEOUT_S * 1000)
_SQLITE_WRITE_RETRY_DELAYS_S = (0.05, 0.15, 0.30)
_OWNER_LOCK_TIMEOUT_S = 10.0
_OWNER_LOCK_POLL_S = 0.05
_WRITE_FAILURE_LOG_THROTTLE_S = 60.0

_CALIBRATION_DB_WRITE_HEALTH_LOCK = threading.RLock()
_CALIBRATION_DB_WRITE_HEALTH: Dict[str, Any] = {
    "owner": "arch4.brain.memory_calibration_store",
    "status": "healthy",
    "degraded": False,
    "attempt_count": 0,
    "success_count": 0,
    "failure_count": 0,
    "consecutive_failure_count": 0,
    "lock_timeout_count": 0,
    "sqlite_locked_count": 0,
    "last_attempt": {},
    "last_success": {},
    "last_failure": {},
}
_CALIBRATION_DB_WRITE_LOG_STATE: Dict[str, Dict[str, Any]] = {}

# ─── Schema ─────────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS calibration_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider_plmn TEXT,
    marker TEXT NOT NULL,
    marker_raw TEXT NOT NULL,
    marker_signature TEXT,
    needs_calibration INTEGER,
    baseline_ready INTEGER,
    sample_count INTEGER,
    expected_dl_mbps REAL,
    expected_rtt_ms REAL,
    expected_sinr_db REAL,
    gap_score REAL,
    throughput_dl_mbps REAL,
    throughput_ul_mbps REAL,
    rtt_ms REAL,
    rtt_sliding_avg REAL,
    sinr_db REAL,
    rsrp_dbm REAL,
    rsrq_db REAL,
    mcs_dl REAL,
    mcs_ul REAL,
    cqi REAL,
    ri REAL,
    ca_active INTEGER,
    ca_signature TEXT,
    carrier_count INTEGER,
    primary_band INTEGER,
    pci INTEGER,
    earfcn INTEGER,
    enb_id INTEGER,
    state TEXT,
    observation_id TEXT,
    identity_key TEXT,
    session_scope_key TEXT,
    formula_scope_key TEXT,
    identity_code TEXT,
    identity_status TEXT,
    identity_catalog_version TEXT,
    identity_catalog_checksum TEXT,
    source_epoch TEXT,
    source_sequence INTEGER,
    formula_vector_id TEXT,
    evidence_scope_id TEXT,
    evidence_scope_json TEXT,
    admission_state TEXT
)
"""

_CREATE_QUARANTINE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS calibration_evidence_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    calibration_sample_id INTEGER,
    observation_id TEXT,
    quarantined_at REAL NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
"""

_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_calib_marker_ts ON calibration_samples(marker, ts)",
    "CREATE INDEX IF NOT EXISTS idx_calib_raw_marker_ts ON calibration_samples(marker_raw, ts)",
    "CREATE INDEX IF NOT EXISTS idx_calib_provider_ts ON calibration_samples(provider_plmn, ts)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_calib_observation_id ON calibration_samples(observation_id) WHERE observation_id IS NOT NULL AND observation_id != ''",
]

_SCOPED_OBSERVATION_COLUMNS = {
    "observation_id": "TEXT",
    "identity_key": "TEXT",
    "session_scope_key": "TEXT",
    "formula_scope_key": "TEXT",
    "identity_code": "TEXT",
    "identity_status": "TEXT",
    "identity_catalog_version": "TEXT",
    "identity_catalog_checksum": "TEXT",
    "source_epoch": "TEXT",
    "source_sequence": "INTEGER",
    "formula_vector_id": "TEXT",
    "evidence_scope_id": "TEXT",
    "evidence_scope_json": "TEXT",
    "admission_state": "TEXT",
}

_INSERT_SQL = """
INSERT INTO calibration_samples (
    ts, provider_plmn, marker, marker_raw, marker_signature,
    needs_calibration, baseline_ready, sample_count,
    expected_dl_mbps, expected_rtt_ms, expected_sinr_db, gap_score,
    throughput_dl_mbps, throughput_ul_mbps, rtt_ms, rtt_sliding_avg,
    sinr_db, rsrp_dbm, rsrq_db, mcs_dl, mcs_ul, cqi, ri,
    ca_active, ca_signature, carrier_count, primary_band, pci, earfcn, enb_id, state
) VALUES (
    ?, ?, ?, ?, ?,
    ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?
)
"""

_HOURLY_QUERY_SQL = """
SELECT
    CAST(ts / 3600 AS INTEGER) * 3600 AS bucket_ts,
    COUNT(*) AS samples,
    MIN(sinr_db) AS sinr_min, MAX(sinr_db) AS sinr_max,
    MIN(rsrp_dbm) AS rsrp_min, MAX(rsrp_dbm) AS rsrp_max,
    MIN(rsrq_db) AS rsrq_min, MAX(rsrq_db) AS rsrq_max,
    MIN(rtt_ms) AS rtt_min, MAX(rtt_ms) AS rtt_max,
    MIN(throughput_dl_mbps) AS dl_min, MAX(throughput_dl_mbps) AS dl_max,
    MIN(gap_score) AS gap_min, MAX(gap_score) AS gap_max,
    MIN(expected_dl_mbps) AS exp_dl_min, MAX(expected_dl_mbps) AS exp_dl_max,
    MIN(expected_rtt_ms) AS exp_rtt_min, MAX(expected_rtt_ms) AS exp_rtt_max,
    MIN(expected_sinr_db) AS exp_sinr_min, MAX(expected_sinr_db) AS exp_sinr_max
FROM calibration_samples
WHERE marker = ? AND ts >= ? AND admission_state = 'admitted'
GROUP BY bucket_ts
ORDER BY bucket_ts ASC
"""

# ─── Functions ───────────────────────────────────────────────────────────────


def sqlite_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=_SQLITE_BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    except Exception:
        pass
    return conn


def _owner_lock_path(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.name}.owner.lock")


def _try_lock_file(handle: BinaryIO) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def calibration_db_owner_lock(
    db_path: Path,
    thread_lock: threading.Lock,
    *,
    timeout_s: float = _OWNER_LOCK_TIMEOUT_S,
) -> Iterator[None]:
    """Serialize calibration DB writes across threads and 54GUC processes."""

    lock_path = _owner_lock_path(db_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with thread_lock:
        with lock_path.open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + max(0.1, float(timeout_s))
            locked = False
            while time.monotonic() <= deadline:
                if _try_lock_file(handle):
                    locked = True
                    break
                time.sleep(_OWNER_LOCK_POLL_S)
            if not locked:
                raise sqlite3.OperationalError(
                    f"calibration db owner lock timeout: {lock_path}"
                )
            try:
                yield
            finally:
                _unlock_file(handle)


def _enable_wal(con: sqlite3.Connection) -> None:
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.DatabaseError as exc:
        log.debug("Calibration DB WAL setup skipped: %s", exc)


def _is_sqlite_locked(exc: Exception) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc or "").lower()
    return "database is locked" in text or "database table is locked" in text


def _is_owner_lock_timeout(exc: Exception) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    return "calibration db owner lock timeout" in str(exc or "").lower()


def _calibration_db_failure_kind(exc: Exception) -> str:
    if _is_owner_lock_timeout(exc):
        return "owner_lock_timeout"
    if _is_sqlite_locked(exc):
        return "sqlite_locked"
    return type(exc).__name__ or "error"


def _health_event(operation: str, db_path: Path) -> dict[str, Any]:
    now = time.time()
    return {
        "operation": str(operation or ""),
        "db_path": str(Path(db_path)),
        "ts": float(now),
    }


def _record_calibration_db_write_attempt(operation: str, db_path: Path) -> None:
    with _CALIBRATION_DB_WRITE_HEALTH_LOCK:
        _CALIBRATION_DB_WRITE_HEALTH["attempt_count"] = int(
            _CALIBRATION_DB_WRITE_HEALTH.get("attempt_count", 0) or 0
        ) + 1
        _CALIBRATION_DB_WRITE_HEALTH["last_attempt"] = _health_event(operation, db_path)


def _record_calibration_db_write_success(operation: str, db_path: Path) -> None:
    with _CALIBRATION_DB_WRITE_HEALTH_LOCK:
        _CALIBRATION_DB_WRITE_HEALTH["success_count"] = int(
            _CALIBRATION_DB_WRITE_HEALTH.get("success_count", 0) or 0
        ) + 1
        _CALIBRATION_DB_WRITE_HEALTH["consecutive_failure_count"] = 0
        _CALIBRATION_DB_WRITE_HEALTH["status"] = "healthy"
        _CALIBRATION_DB_WRITE_HEALTH["degraded"] = False
        _CALIBRATION_DB_WRITE_HEALTH["last_success"] = _health_event(operation, db_path)


def _record_calibration_db_write_failure(
    operation: str,
    db_path: Path,
    exc: Exception,
) -> None:
    kind = _calibration_db_failure_kind(exc)
    with _CALIBRATION_DB_WRITE_HEALTH_LOCK:
        _CALIBRATION_DB_WRITE_HEALTH["failure_count"] = int(
            _CALIBRATION_DB_WRITE_HEALTH.get("failure_count", 0) or 0
        ) + 1
        _CALIBRATION_DB_WRITE_HEALTH["consecutive_failure_count"] = int(
            _CALIBRATION_DB_WRITE_HEALTH.get("consecutive_failure_count", 0) or 0
        ) + 1
        if kind == "owner_lock_timeout":
            _CALIBRATION_DB_WRITE_HEALTH["lock_timeout_count"] = int(
                _CALIBRATION_DB_WRITE_HEALTH.get("lock_timeout_count", 0) or 0
            ) + 1
        if kind == "sqlite_locked":
            _CALIBRATION_DB_WRITE_HEALTH["sqlite_locked_count"] = int(
                _CALIBRATION_DB_WRITE_HEALTH.get("sqlite_locked_count", 0) or 0
            ) + 1
        _CALIBRATION_DB_WRITE_HEALTH["status"] = "degraded"
        _CALIBRATION_DB_WRITE_HEALTH["degraded"] = True
        failure = _health_event(operation, db_path)
        failure.update(
            {
                "kind": kind,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        _CALIBRATION_DB_WRITE_HEALTH["last_failure"] = failure


def _log_calibration_db_write_failure(
    message: str,
    *args: Any,
    operation: str,
    exc: Exception,
) -> None:
    now = time.time()
    key = f"{operation}:{_calibration_db_failure_kind(exc)}"
    with _CALIBRATION_DB_WRITE_HEALTH_LOCK:
        state = _CALIBRATION_DB_WRITE_LOG_STATE.setdefault(
            key,
            {"last_log_ts": 0.0, "suppressed": 0},
        )
        last_log_ts = float(state.get("last_log_ts", 0.0) or 0.0)
        if now - last_log_ts < _WRITE_FAILURE_LOG_THROTTLE_S:
            state["suppressed"] = int(state.get("suppressed", 0) or 0) + 1
            return
        suppressed = int(state.get("suppressed", 0) or 0)
        state["last_log_ts"] = now
        state["suppressed"] = 0
    if suppressed:
        log.warning(message + " (suppressed=%d)", *args, suppressed)
    else:
        log.warning(message, *args)


def calibration_db_write_health() -> Dict[str, Any]:
    with _CALIBRATION_DB_WRITE_HEALTH_LOCK:
        return {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in _CALIBRATION_DB_WRITE_HEALTH.items()
        }


def _run_sqlite_write_with_retry(operation: Callable[[], None]) -> None:
    last_exc: Exception | None = None
    for attempt, delay_s in enumerate((0.0, *_SQLITE_WRITE_RETRY_DELAYS_S), start=1):
        if delay_s > 0.0:
            time.sleep(delay_s)
        try:
            operation()
            return
        except Exception as exc:
            last_exc = exc
            if not _is_sqlite_locked(exc) or attempt > len(_SQLITE_WRITE_RETRY_DELAYS_S):
                raise
    if last_exc is not None:
        raise last_exc


def init_calibration_db(
    db_path: Path,
    lock: threading.Lock,
    *,
    enabled: bool,
) -> None:
    if not enabled:
        return
    operation = "init"
    _record_calibration_db_write_attempt(operation, db_path)
    try:
        with calibration_db_owner_lock(db_path, lock), sqlite_conn(db_path) as con:
            _enable_wal(con)
            con.execute(_CREATE_TABLE_SQL)
            con.execute(_CREATE_QUARANTINE_TABLE_SQL)
            existing_columns = {
                str(row[1])
                for row in con.execute("PRAGMA table_info(calibration_samples)").fetchall()
            }
            for column, column_type in _SCOPED_OBSERVATION_COLUMNS.items():
                if column not in existing_columns:
                    con.execute(
                        f"ALTER TABLE calibration_samples ADD COLUMN {column} {column_type}"
                    )
            for idx_sql in _CREATE_INDEXES_SQL:
                con.execute(idx_sql)
            # Rows written before EvidenceScope.v1 are retained as forensic
            # evidence, but are never admitted into calibration aggregates.
            con.execute(
                """
                INSERT INTO calibration_evidence_quarantine (
                    calibration_sample_id, observation_id, quarantined_at,
                    reason, payload_json
                )
                SELECT id, COALESCE(observation_id, ''), ?,
                       'legacy_unscoped_calibration_row',
                       json_object('id', id, 'ts', ts, 'marker', marker)
                FROM calibration_samples
                WHERE COALESCE(admission_state, '') = ''
                  AND NOT EXISTS (
                      SELECT 1 FROM calibration_evidence_quarantine q
                      WHERE q.calibration_sample_id = calibration_samples.id
                  )
                """,
                (float(time.time()),),
            )
            con.execute(
                """
                UPDATE calibration_samples
                SET admission_state = 'quarantined_legacy_unscoped'
                WHERE COALESCE(admission_state, '') = ''
                """
            )
            con.commit()
        _record_calibration_db_write_success(operation, db_path)
    except Exception as e:
        _record_calibration_db_write_failure(operation, db_path, e)
        _log_calibration_db_write_failure(
            "Calibration DB init failed: %s",
            e,
            operation=operation,
            exc=e,
        )


def prune_calibration_db(
    db_path: Path,
    lock: threading.Lock,
    *,
    enabled: bool,
    retention_days: int,
    now: Optional[float] = None,
) -> None:
    if not enabled or retention_days <= 0:
        return
    operation = "prune"
    _record_calibration_db_write_attempt(operation, db_path)
    ts = time.time() if now is None else float(now)
    cutoff = ts - float(retention_days) * 86400.0
    try:
        def _write() -> None:
            with calibration_db_owner_lock(db_path, lock), sqlite_conn(db_path) as con:
                con.execute("DELETE FROM calibration_samples WHERE ts < ?", (cutoff,))
                con.commit()

        _run_sqlite_write_with_retry(_write)
        _record_calibration_db_write_success(operation, db_path)
    except Exception as e:
        _record_calibration_db_write_failure(operation, db_path, e)
        _log_calibration_db_write_failure(
            "Calibration DB prune failed: %s",
            e,
            operation=operation,
            exc=e,
        )


def insert_calibration_sample(
    db_path: Path,
    lock: threading.Lock,
    *,
    enabled: bool,
    retention_days: int,
    ts: float,
    provider: str,
    marker: str,
    marker_raw: str,
    sig: str,
    st: Dict[str, Any],
    modem: Any,
    computed: Any,
    state_name: str,
) -> None:
    if not enabled:
        return
    operation = "insert"
    _record_calibration_db_write_attempt(operation, db_path)
    try:
        cutoff = (
            float(ts) - float(retention_days) * 86400.0
            if int(retention_days or 0) > 0
            else None
        )

        def _write() -> None:
            with calibration_db_owner_lock(db_path, lock), sqlite_conn(db_path) as con:
                con.execute(
                    _INSERT_SQL,
                    (
                        ts, provider, marker, marker_raw, sig,
                        1 if bool(st.get("needs_calibration", False)) else 0,
                        1 if bool(st.get("baseline_ready", False)) else 0,
                        int(st.get("sample_count", 0) or 0),
                        float(st.get("expected_dl_mbps", 0.0) or 0.0),
                        float(st.get("expected_rtt_ms", 0.0) or 0.0),
                        float(st.get("expected_sinr_db", 0.0) or 0.0),
                        float(st.get("gap_score", 0.0) or 0.0),
                        float(getattr(modem.traffic, 'dl_mbps', 0.0) or 0.0),
                        float(getattr(modem.traffic, 'ul_mbps', 0.0) or 0.0),
                        float(getattr(computed.latency, 'rtt_ms', 0.0) or 0.0),
                        float(getattr(computed.latency, 'rtt_sliding_avg', 0.0) or 0.0),
                        float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0),
                        float(getattr(modem.cellular, 'rsrp_dbm', 0.0) or 0.0),
                        float(getattr(modem.cellular, 'rsrq_db', 0.0) or 0.0),
                        0.0,  # reserved DB schema slot; scheduler truth is register-owned
                        0.0,  # reserved DB schema slot; scheduler truth is register-owned
                        0.0,  # reserved DB schema slot; scheduler truth is register-owned
                        float(getattr(modem.cellular, 'ri', 0.0) or 0.0),
                        1 if bool(getattr(modem.cellular, 'ca_active', False)) else 0,
                        str(getattr(modem.cellular, 'ca_band_signature', "") or ""),
                        int(getattr(modem.cellular, 'carrier_count', 0) or 0),
                        int(getattr(modem.cellular, 'primary_band', 0) or 0),
                        int(getattr(modem.cellular, 'pci', 0) or 0),
                        int(getattr(modem.cellular, 'earfcn_dl', getattr(modem.cellular, 'earfcn_dl', 0)) or 0),
                        int(getattr(modem.cellular, 'enb_id', 0) or 0),
                        str(state_name or ""),
                    ),
                )
                if cutoff is not None:
                    con.execute("DELETE FROM calibration_samples WHERE ts < ?", (cutoff,))
                con.commit()

        _run_sqlite_write_with_retry(_write)
        _record_calibration_db_write_success(operation, db_path)
    except Exception as e:
        _record_calibration_db_write_failure(operation, db_path, e)
        _log_calibration_db_write_failure(
            "Calibration DB insert failed: %s",
            e,
            operation=operation,
            exc=e,
        )


def insert_calibration_observation(
    db_path: Path,
    lock: threading.Lock,
    *,
    enabled: bool,
    retention_days: int,
    observation: Dict[str, Any],
) -> bool:
    """Archive one admitted observation idempotently under its exact scope."""

    if not enabled:
        return False
    operation = "insert_observation"
    _record_calibration_db_write_attempt(operation, db_path)
    data = dict(observation or {})
    sample = data.get("sample") if isinstance(data.get("sample"), dict) else {}
    status = (
        data.get("calibration_status")
        if isinstance(data.get("calibration_status"), dict)
        else {}
    )
    scope = data.get("evidence_scope") if isinstance(data.get("evidence_scope"), dict) else {}
    observation_id = str(data.get("observation_id", "") or "").strip()
    admission_error = calibration_observation_error(data)
    if not observation_id and not admission_error:
        admission_error = "calibration_observation_id_required"
    try:
        canonical_scope = validated_evidence_scope(scope)
        if canonical_scope.formula_vector != calibration_formula_vector():
            admission_error = admission_error or "formula_vector_mismatch"
    except (TypeError, ValueError) as exc:
        canonical_scope = None
        admission_error = admission_error or str(exc)
    ts = float(sample.get("sample_ts", sample.get("ts", 0.0)) or 0.0)
    marker = str(status.get("marker", "") or "C0")
    marker_signature = str(status.get("marker_signature", "") or "")
    inserted = False
    try:
        cutoff = (
            ts - float(retention_days) * 86400.0
            if int(retention_days or 0) > 0
            else None
        )

        def _write() -> None:
            nonlocal inserted
            with calibration_db_owner_lock(db_path, lock), sqlite_conn(db_path) as con:
                con.execute(_CREATE_QUARANTINE_TABLE_SQL)
                if admission_error:
                    con.execute(
                        """
                        INSERT INTO calibration_evidence_quarantine (
                            calibration_sample_id, observation_id,
                            quarantined_at, reason, payload_json
                        ) VALUES (NULL, ?, ?, ?, ?)
                        """,
                        (
                            observation_id,
                            float(time.time()),
                            str(admission_error),
                            json.dumps(data, sort_keys=True, default=str),
                        ),
                    )
                    con.commit()
                    return
                assert canonical_scope is not None
                scope_json = json.dumps(
                    canonical_scope.as_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                cursor = con.execute(
                    """
                    INSERT OR IGNORE INTO calibration_samples (
                        ts, provider_plmn, marker, marker_raw, marker_signature,
                        needs_calibration, baseline_ready, sample_count,
                        expected_dl_mbps, expected_rtt_ms, expected_sinr_db, gap_score,
                        throughput_dl_mbps, throughput_ul_mbps, rtt_ms, rtt_sliding_avg,
                        sinr_db, rsrp_dbm, rsrq_db, mcs_dl, mcs_ul, cqi, ri,
                        ca_active, ca_signature, carrier_count, primary_band, pci,
                        earfcn, enb_id, state, observation_id, identity_key,
                        session_scope_key, formula_scope_key,
                        identity_code, identity_status,
                        identity_catalog_version, identity_catalog_checksum,
                        source_epoch, source_sequence, formula_vector_id,
                        evidence_scope_id, evidence_scope_json, admission_state
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        ts,
                        str(scope.get("provider_plmn", "") or ""),
                        marker,
                        marker,
                        marker_signature,
                        1 if bool(status.get("needs_calibration", False)) else 0,
                        1 if bool(status.get("baseline_ready", False)) else 0,
                        int(status.get("sample_count", 0) or 0),
                        float(status.get("expected_dl_mbps", 0.0) or 0.0),
                        float(status.get("expected_rtt_ms", 0.0) or 0.0),
                        float(status.get("expected_sinr_db", 0.0) or 0.0),
                        float(status.get("gap_score", 0.0) or 0.0),
                        float(sample.get("throughput_dl_mbps", 0.0) or 0.0),
                        float(sample.get("throughput_ul_mbps", 0.0) or 0.0),
                        float(sample.get("rtt_ms", 0.0) or 0.0),
                        float(sample.get("rtt_sliding_avg", sample.get("rtt_ms", 0.0)) or 0.0),
                        float(sample.get("sinr_db", 0.0) or 0.0),
                        float(sample.get("rsrp_dbm", 0.0) or 0.0),
                        float(sample.get("rsrq_db", 0.0) or 0.0),
                        0.0,
                        0.0,
                        0.0,
                        float(sample.get("ri", 0.0) or 0.0),
                        1 if bool(sample.get("ca_active", False)) else 0,
                        str(sample.get("band_signature", "") or ""),
                        int(sample.get("carrier_count", 0) or 0),
                        int(sample.get("primary_band", 0) or 0),
                        int(sample.get("pci", 0) or 0),
                        int(sample.get("earfcn", 0) or 0),
                        int(sample.get("enb_id", 0) or 0),
                        str(sample.get("state", "") or ""),
                        observation_id,
                        str(canonical_scope.identity_code),
                        str(canonical_scope.session_scope_key),
                        str(canonical_scope.formula_vector.vector_id),
                        str(canonical_scope.identity_code),
                        str(canonical_scope.identity_status),
                        str(canonical_scope.identity_catalog_version),
                        str(canonical_scope.identity_catalog_checksum),
                        str(canonical_scope.source_epoch),
                        int(canonical_scope.source_sequence),
                        str(canonical_scope.formula_vector.vector_id),
                        str(canonical_scope.scope_id),
                        scope_json,
                        "admitted",
                    ),
                )
                inserted = bool(cursor.rowcount)
                if cutoff is not None:
                    con.execute("DELETE FROM calibration_samples WHERE ts < ?", (cutoff,))
                con.commit()

        _run_sqlite_write_with_retry(_write)
        _record_calibration_db_write_success(operation, db_path)
        return bool(inserted)
    except Exception as e:
        _record_calibration_db_write_failure(operation, db_path, e)
        _log_calibration_db_write_failure(
            "Calibration observation DB insert failed: %s",
            e,
            operation=operation,
            exc=e,
        )
        raise


def reassign_marker_history(
    db_path: Path,
    lock: threading.Lock,
    *,
    enabled: bool,
    src_marker: str,
    dst_marker: str,
) -> None:
    if not enabled:
        return
    src = str(src_marker or "").strip()
    dst = str(dst_marker or "").strip()
    if not src or not dst or src == dst:
        return
    operation = "reassign_marker"
    _record_calibration_db_write_attempt(operation, db_path)
    try:
        def _write() -> None:
            with calibration_db_owner_lock(db_path, lock), sqlite_conn(db_path) as con:
                con.execute(
                    "UPDATE calibration_samples SET marker = ? WHERE marker = ?",
                    (dst, src),
                )
                con.execute(
                    "UPDATE calibration_samples SET marker = ? WHERE marker_raw = ?",
                    (dst, src),
                )
                con.commit()

        _run_sqlite_write_with_retry(_write)
        _record_calibration_db_write_success(operation, db_path)
    except Exception as e:
        _record_calibration_db_write_failure(operation, db_path, e)
        _log_calibration_db_write_failure(
            "Calibration DB marker reassignment failed %s -> %s: %s",
            src,
            dst,
            e,
            operation=operation,
            exc=e,
        )


def get_hourly_ranges(
    db_path: Path,
    lock: threading.Lock,
    *,
    enabled: bool,
    marker: str,
    lookback_hours: int = 24,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Query hourly min/max snapshots for a calibration marker.
    Used for day/week/month reconstruction dashboards.
    """
    if not enabled:
        return []
    ts_now = time.time() if now is None else float(now)
    since = ts_now - max(1, int(lookback_hours)) * 3600.0
    rows: List[Dict[str, Any]] = []
    try:
        with lock, sqlite_conn(db_path) as con:
            cur = con.execute(_HOURLY_QUERY_SQL, (marker, since))
            for r in cur.fetchall():
                d = dict(r)
                d["bucket_iso"] = time.strftime(
                    "%Y-%m-%dT%H:00:00", time.localtime(float(d["bucket_ts"]))
                )
                rows.append(d)
    except Exception as e:
        log.warning("Calibration DB query failed marker=%s: %s", marker, e)
        return []
    return rows
