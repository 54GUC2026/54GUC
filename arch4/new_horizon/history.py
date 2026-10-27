"""Keep New Horizon history in memory first, with disk checkpoints."""

from __future__ import annotations

from collections import OrderedDict, defaultdict, deque
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
from threading import RLock
from time import monotonic, strftime, time
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4
import zlib

from arch4.log_processor import (
    CRITICAL_LANE,
    ROUTINE_LANE,
    MemoryFirstLogProcessor,
    MemoryLogRecord,
)
from arch4.atomic_io import atomic_write_json, fsync_parent_dir
from arch4.sqlite_recovery import quarantine_corrupt_sqlite
from arch4 import prepared_support as _prepared_support


_RETENTION_SECONDS = 14 * 24 * 60 * 60
_OBSERVATION_CACHE_LIMIT = 512
_OBSERVATION_KEY_LIMIT = 4096
_OBSERVATION_SCOPE_CACHE_LIMIT = 64
_OBSERVATION_BATCH_SCHEMA = "NewHorizonObservationBatch.v1"
NEW_HORIZON_HISTORY_PATH_ENV = "ARCH_NEW_HORIZON_HISTORY_PATH"
NEW_HORIZON_HISTORY_DIRNAME = "_new_horizon_history"
NEW_HORIZON_HISTORY_FILENAME = "new_horizon.sqlite3"
LEARNING_CONTEXT_V68_RECEIPT_FILENAME = (
    "new_horizon_learning_context_v68_migration_receipt.json"
)
_LEARNING_CONTEXT_BATCH_SCHEMA = "NewHorizonLearningContextBatchMigration.v1"
_LEARNING_CONTEXT_STORE_RECEIPT_SCHEMA = (
    "NewHorizonLearningContextStoreMigrationReceipt.v1"
)
_LEARNING_MIGRATION_PROOF_KIND = "referee_proof_v12"
_LEARNING_MIGRATION_PROOF_SCHEMA = "NewHorizonRefereeProofEvidence.v12"
_LEARNING_MIGRATION_PROOF_CONTRACTS = {
    _LEARNING_MIGRATION_PROOF_KIND: _LEARNING_MIGRATION_PROOF_SCHEMA,
    "referee_proof_v11": "NewHorizonRefereeProofEvidence.v11",
    "referee_proof_v10": "NewHorizonRefereeProofEvidence.v10",
}
_LEARNING_MIGRATION_DECISION_KIND = "decision_contribution_v8"
_LEARNING_MIGRATION_DECISION_SCHEMA = "NewHorizonDecisionContribution.v8"
_LEARNING_MIGRATION_PENDING_NAME = "pending_proofs_v12"
_LEARNING_MIGRATION_PENDING_SCHEMA = "NewHorizonPendingProofCheckpoint.v12"
_LEARNING_MIGRATION_DERIVED_CHECKPOINTS = frozenset(
    {"posteriors_v15", "adviser_credit_v7"}
)


def evidence_time_equivalent(previous_at: float, now: float, event_times: Iterable[float]) -> bool:
    """Keep a RAM projection only when no previously future evidence became eligible."""
    return now >= previous_at and not any(previous_at < stamp <= now for stamp in event_times)


def portable_history_path(stage_dir: str | Path) -> Path:
    """Return the history path shared by portable releases."""

    stage = Path(stage_dir).expanduser().resolve()
    return (
        stage.parent / NEW_HORIZON_HISTORY_DIRNAME / NEW_HORIZON_HISTORY_FILENAME
    ).resolve()


def resolve_history_path(state_dir: str | Path) -> Path:
    """Resolve the history store. Non-portable runs are isolated by default."""

    selected = str(os.environ.get(NEW_HORIZON_HISTORY_PATH_ENV, "") or "").strip()
    if selected:
        return Path(selected).expanduser().resolve()
    return Path(state_dir).expanduser().resolve() / NEW_HORIZON_HISTORY_FILENAME


def read_confirmed_device_associations(path: str | Path) -> tuple[dict, ...]:
    """Public read-only History adapter to the one continuity projection."""
    from arch4.new_horizon.continuity import read_confirmed_device_associations as project
    return project(path)


def _validated_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    value = json.loads(encoded)
    if not isinstance(value, dict):
        raise ValueError("new_horizon_history_payload_mapping_required")
    return value


def _encode(payload: Mapping[str, Any]) -> bytes:
    raw = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return zlib.compress(raw, level=6)


_PAYLOAD_DELTA = b'NHD1'
_CHECKPOINT_UNLOADED = object()


def _payload_changes(before, after, path=()):
    """Lossless JSON changes against an immutable base, never a prior event."""
    sets, removes = [], []
    if type(before) is dict and type(after) is dict:
        removes.extend([list((*path, key)) for key in before if key not in after])
        for key, value in after.items():
            if key not in before:
                sets.append([list((*path, key)), value])
            else:
                change = _payload_changes(before[key], value, (*path, key))
                sets.extend(change['set']); removes.extend(change['remove'])
    elif type(before) is list and type(after) is list and len(before) == len(after):
        for key, (left, right) in enumerate(zip(before, after)):
            change = _payload_changes(left, right, (*path, key))
            sets.extend(change['set']); removes.extend(change['remove'])
    elif (type(before) is not type(after) or before != after
          or type(before) is float and before == 0 and math.copysign(1, before) != math.copysign(1, after)):
        sets.append([list(path), after])
    return {"set": sets, "remove": removes}


def _payload_base(connection, encoded, bases=None):
    if connection is None or len(encoded) <= 36:
        raise ValueError('history_payload_base_required')
    identity = encoded[4:36].hex()
    row = (bases.get(identity) if bases is not None else
           connection.execute('SELECT slot,payload FROM history_payload_bases WHERE base_id=?', (identity,)).fetchone())
    if row is None:
        raise ValueError('history_payload_base_missing')
    slot, base = row
    if hashlib.sha256(slot.encode() + b'\0' + base).hexdigest() != identity:
        raise ValueError('history_payload_base_corrupt')
    return base


def _snapshot_payload_bases(connection, payloads):
    """Read dependencies with the selected rows, before releasing the owner lock."""
    bases = {}
    for encoded in payloads:
        if encoded.startswith(_PAYLOAD_DELTA):
            identity = encoded[4:36].hex()
            if identity not in bases:
                bases[identity] = connection.execute(
                    'SELECT slot,payload FROM history_payload_bases WHERE base_id=?', (identity,)).fetchone()
    return bases


def _apply_payload_changes(base, patch):
    def parent(path):
        if type(path) is not list or not path:
            raise ValueError('history_payload_delta_path_invalid')
        node = base
        for key in path[:-1]:
            if not (type(node) is dict and type(key) is str and key in node
                    or type(node) is list and type(key) is int and 0 <= key < len(node)):
                raise ValueError('history_payload_delta_path_invalid')
            node = node[key]
        key = path[-1]
        if not (type(node) is dict and type(key) is str
                or type(node) is list and type(key) is int and 0 <= key < len(node)):
            raise ValueError('history_payload_delta_path_invalid')
        return node, key
    if type(patch) is not dict or set(patch) != {'set', 'remove'}:
        raise ValueError('history_payload_delta_invalid')
    for path in patch['remove']:
        node, key = parent(path)
        if type(node) is not dict or key not in node:
            raise ValueError('history_payload_delta_remove_invalid')
        del node[key]
    for path, value in patch['set']:
        if path == []:
            base = value
        else:
            node, key = parent(path); node[key] = value
    return base


def _decode(payload: bytes, connection=None, bases=None) -> dict[str, Any]:
    if payload.startswith(_PAYLOAD_DELTA):
        base = _decode(_payload_base(connection, payload, bases))
        patch = json.loads(zlib.decompress(payload[36:]).decode('utf-8'))
        value = _apply_payload_changes(base, patch)
        if type(value) is not dict:
            raise ValueError('new_horizon_history_payload_mapping_required')
        return value
    value = json.loads(zlib.decompress(payload).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("new_horizon_history_payload_mapping_required")
    return value


def _encode_brain_payload(connection, *, scope_key, observed_at, payload):
    original = _encode(payload)
    if (payload.get('schema') != 'NewHorizonBrainEvaluation.v1'
            or type(payload.get('source_epoch')) is not str or not payload['source_epoch']
            or type(payload.get('lifecycle_generation')) is not int or payload['lifecycle_generation'] <= 0):
        return original
    slot = json.dumps([scope_key, payload['source_epoch'], payload['lifecycle_generation'],
                       int(observed_at // 60)], separators=(',', ':'))
    row = connection.execute('SELECT base_id,payload FROM history_payload_bases WHERE slot=?', (slot,)).fetchone()
    if row is None:
        base_id = hashlib.sha256(slot.encode() + b'\0' + original).hexdigest()
        connection.execute('INSERT INTO history_payload_bases VALUES(?,?,?,?)',
                           (slot, base_id, original, observed_at))
        base = payload
    else:
        base_id, encoded = row
        if hashlib.sha256(slot.encode() + b'\0' + encoded).hexdigest() != base_id:
            raise ValueError('history_payload_base_corrupt')
        base = _decode(encoded)
        connection.execute('UPDATE history_payload_bases SET last_observed_at=MAX(last_observed_at,?) WHERE slot=?',
                           (observed_at, slot))
    encoded = _PAYLOAD_DELTA + bytes.fromhex(base_id) + _encode(_payload_changes(base, payload))
    # A changed candidate population can be cheaper as an ordinary old-format row.
    return encoded if len(encoded) < len(original) else original


def _history_record(
    *,
    event_key: str,
    scope_key: str,
    kind: str,
    observed_at: float,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the complete record returned by History."""

    if not all(
        type(value) is str and value.strip() for value in (event_key, scope_key, kind)
    ):
        raise ValueError("new_horizon_history_return_identity_invalid")
    stamp = float(observed_at)
    if not math.isfinite(stamp) or stamp <= 0.0:
        raise ValueError("new_horizon_history_return_timestamp_invalid")
    if not isinstance(payload, Mapping):
        raise TypeError("new_horizon_history_return_payload_invalid")
    return {
        "event_key": event_key,
        "scope_key": scope_key,
        "kind": kind,
        "observed_at": stamp,
        "payload": dict(payload),
    }


def _observation_state(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"matrix_sequence", "sampled_at", "accepted_at"}
    }


def _compact_observations(
    scope_key: str,
    samples: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Build a minute batch that can be decoded without another batch."""

    runs: list[dict[str, Any]] = []
    prior: dict[str, Any] = {}
    for sample in samples:
        payload = dict(sample["payload"])
        state = _observation_state(payload)
        sequence = int(payload.get("matrix_sequence", 0) or 0)
        stamp = float(sample["observed_at"])
        if runs and state == prior:
            run = runs[-1]
            run["repeat_count"] += 1
            run["last_event_key"] = sample["event_key"]
            run["last_observed_at"] = stamp
            run["last_sequence"] = sequence
            continue
        if not runs:
            change = {"full": state}
        else:
            change = {
                "delta": {
                    key: value
                    for key, value in state.items()
                    if key not in prior or prior[key] != value
                },
                "removed": sorted(set(prior) - set(state)),
            }
        runs.append(
            {
                "first_event_key": sample["event_key"],
                "last_event_key": sample["event_key"],
                "first_observed_at": stamp,
                "last_observed_at": stamp,
                "first_sequence": sequence,
                "last_sequence": sequence,
                "repeat_count": 1,
                **change,
            }
        )
        prior = state
    return {
        "schema": _OBSERVATION_BATCH_SCHEMA,
        "scope_key": scope_key,
        "sample_count": len(samples),
        "first_observed_at": float(samples[0]["observed_at"]),
        "last_observed_at": float(samples[-1]["observed_at"]),
        "runs": runs,
    }


def _decode_observation_batch(
    *,
    event_key: str,
    expected_scope_key: str,
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    if payload.get("schema") != _OBSERVATION_BATCH_SCHEMA:
        raise ValueError("new_horizon_observation_batch_schema_invalid")
    scope_key = str(payload.get("scope_key") or "")
    runs = payload.get("runs")
    if not scope_key or not isinstance(runs, list):
        raise ValueError("new_horizon_observation_batch_contract_invalid")
    if scope_key != expected_scope_key:
        raise ValueError("new_horizon_observation_batch_scope_mismatch")
    state: dict[str, Any] = {}
    decoded: list[dict[str, Any]] = []
    for run_index, run in enumerate(runs):
        if not isinstance(run, Mapping):
            raise ValueError("new_horizon_observation_batch_run_invalid")
        if "full" in run:
            full = run["full"]
            if not isinstance(full, Mapping):
                raise ValueError("new_horizon_observation_batch_anchor_invalid")
            state = dict(full)
        else:
            delta = run.get("delta")
            removed = run.get("removed")
            if not isinstance(delta, Mapping) or not isinstance(removed, list):
                raise ValueError("new_horizon_observation_batch_delta_invalid")
            for key in removed:
                state.pop(str(key), None)
            state.update(dict(delta))
        repeat = max(1, int(run.get("repeat_count", 1)))
        first_at = float(run["first_observed_at"])
        last_at = float(run["last_observed_at"])
        first_sequence = int(run.get("first_sequence", 0) or 0)
        last_sequence = int(run.get("last_sequence", first_sequence) or first_sequence)
        for repeat_index in range(repeat):
            fraction = 0.0 if repeat == 1 else repeat_index / float(repeat - 1)
            observed_at = first_at + ((last_at - first_at) * fraction)
            sequence = round(
                first_sequence + ((last_sequence - first_sequence) * fraction)
            )
            row_payload = dict(state)
            row_payload["matrix_sequence"] = sequence
            row_payload["sampled_at"] = observed_at
            row_payload["accepted_at"] = observed_at
            decoded.append(
                _history_record(
                    event_key=(
                        str(run.get("first_event_key") or "")
                        if repeat_index == 0
                        else str(run.get("last_event_key") or "")
                        if repeat_index == repeat - 1
                        else f"{event_key}:run:{run_index}:repeat:{repeat_index}"
                    ),
                    scope_key=scope_key,
                    kind="observation",
                    observed_at=observed_at,
                    payload=row_payload,
                )
            )
    return tuple(decoded)


def export_history_checkpoints(path: str | Path, *, name: str) -> tuple[dict[str, Any], ...]:
    """Read-only checkpoint boundary for reports and offline diagnostics."""
    source = Path(path).expanduser().resolve()
    if not source.is_file() or not name:
        raise ValueError("new_horizon_checkpoint_export_source_required")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1.0)) as connection:
        rows = connection.execute(
            "SELECT scope_key,updated_at,payload FROM checkpoint WHERE name=?", (name,),
        ).fetchall()
    return tuple({"scope_key": scope, "updated_at": stamp, "payload": _decode(payload)}
                 for scope, stamp, payload in rows)


def export_history_records(
    path: str | Path,
    *,
    kind: str,
    scope_key: str = "",
    since: float = 0.0,
    limit: int = 4096,
) -> tuple[dict[str, Any], ...]:
    """Decode compressed records without opening a runtime writer."""

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if type(kind) is not str or not kind.strip():
        raise ValueError("new_horizon_history_export_kind_required")
    if type(scope_key) is not str:
        raise TypeError("new_horizon_history_export_scope_invalid")
    if type(limit) is not int or not 1 <= limit <= 65536:
        raise ValueError("new_horizon_history_export_limit_invalid")
    connection = sqlite3.connect(
        f"file:{source.as_posix()}?mode=ro",
        uri=True,
        timeout=1.0,
    )
    try:
        connection.execute('BEGIN')
        if kind == "observation":
            if not scope_key:
                raise ValueError(
                    "new_horizon_history_export_observation_scope_required"
                )
            rows = connection.execute(
                "SELECT event_key, scope_key, kind, observed_at, payload FROM evidence "
                "WHERE scope_key=? AND observed_at>=? "
                "AND kind IN ('observation','observation_batch') "
                "ORDER BY observed_at ASC",
                (scope_key, float(since)),
            ).fetchall()
            decoded: list[dict[str, Any]] = []
            for event_key, record_scope, stored_kind, observed_at, encoded in rows:
                payload = _decode(encoded)
                if stored_kind == "observation_batch":
                    decoded.extend(
                        _decode_observation_batch(
                            event_key=event_key,
                            expected_scope_key=record_scope,
                            payload=payload,
                        )
                    )
                else:
                    decoded.append(
                        _history_record(
                            event_key=event_key,
                            scope_key=record_scope,
                            kind="observation",
                            observed_at=observed_at,
                            payload=payload,
                        )
                    )
            return tuple(decoded[-limit:])
        sql = (
            "SELECT event_key, scope_key, observed_at, payload FROM evidence "
            "WHERE kind=? AND observed_at>=?"
        )
        params: list[Any] = [kind, float(since)]
        if scope_key:
            sql += " AND scope_key=?"
            params.append(scope_key)
        sql += " ORDER BY observed_at DESC LIMIT ?"
        params.append(limit)
        rows = connection.execute(sql, params).fetchall()
        return tuple(
            _history_record(
                event_key=event_key,
                scope_key=record_scope,
                kind=kind,
                observed_at=observed_at,
                payload=_decode(encoded, connection),
            )
            for event_key, record_scope, observed_at, encoded in reversed(rows)
        )
    finally:
        connection.close()


SUPPORT_HISTORY_EXPORT_SCHEMA = "NewHorizonSupportHistoryExport.v1"
SUPPORT_HISTORY_EXPORT_OWNER = "54guc.new_horizon.history"
SUPPORT_HISTORY_EXPORT_FILENAME = "new_horizon_history.jsonl.gz"


_SUPPORT_MAX_CHUNK_ROWS=512
_SUPPORT_MAX_CHUNK_BYTES=16*1024*1024


def _install_history_catalog(connection):
    """Prepare constant-size restart counts in the existing writer transaction."""
    connection.execute('CREATE TABLE IF NOT EXISTS history_counts ('
        'singleton INTEGER PRIMARY KEY CHECK(singleton=1),logical_count INTEGER NOT NULL,'
        'evidence_rows INTEGER NOT NULL,checkpoints INTEGER NOT NULL)')
    if connection.execute('SELECT 1 FROM history_counts WHERE singleton=1').fetchone() is None:
        # Once for an older store; subsequent opens read only this catalog row.
        logical, physical = connection.execute('SELECT COALESCE(SUM(logical_count),0),COUNT(*) FROM evidence').fetchone()
        checkpoints = connection.execute('SELECT COUNT(*) FROM checkpoint').fetchone()[0]
        connection.execute('INSERT INTO history_counts VALUES(1,?,?,?)', (logical, physical, checkpoints))
    for table in ('evidence', 'checkpoint'):
        for action in ('INSERT','DELETE','UPDATE'):
            if table == 'evidence':
                update = {'INSERT':'logical_count=logical_count+NEW.logical_count,evidence_rows=evidence_rows+1',
                    'DELETE':'logical_count=logical_count-OLD.logical_count,evidence_rows=evidence_rows-1',
                    'UPDATE':'logical_count=logical_count+NEW.logical_count-OLD.logical_count'}[action]
            else:
                if action == 'UPDATE':continue
                update = 'checkpoints=checkpoints'+('+' if action=='INSERT' else '-')+'1'
            connection.execute(f'CREATE TRIGGER IF NOT EXISTS history_count_{table}_{action.lower()} '
                f'AFTER {action} ON {table} BEGIN UPDATE history_counts SET {update} WHERE singleton=1; END')
    connection.execute('CREATE TABLE IF NOT EXISTS history_payload_bases ('
        'slot TEXT PRIMARY KEY,base_id TEXT UNIQUE NOT NULL,payload BLOB NOT NULL,last_observed_at REAL NOT NULL)')
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='history_observation_keys'").fetchone() is None:
        connection.execute('CREATE TABLE history_observation_keys(event_key TEXT PRIMARY KEY,observed_at REAL NOT NULL)')
        connection.execute('CREATE INDEX history_observation_keys_time ON history_observation_keys(observed_at)')
        # One-time legacy tail preparation. Normal opens never read raw batches.
        keys=list(connection.execute("SELECT event_key,observed_at FROM evidence WHERE kind='observation' ORDER BY observed_at DESC LIMIT 4096"))
        for key,scope,encoded in connection.execute("SELECT event_key,scope_key,payload FROM evidence WHERE kind='observation_batch' ORDER BY observed_at DESC LIMIT 64"):
            keys.extend((row['event_key'],row['observed_at']) for row in _decode_observation_batch(
                event_key=key,expected_scope_key=scope,payload=_decode(encoded)))
        connection.executemany('INSERT OR IGNORE INTO history_observation_keys VALUES(?,?)',
                              sorted(keys,key=lambda row:row[1])[-_OBSERVATION_KEY_LIMIT:])


def _support_install(connection: sqlite3.Connection) -> None:
    connection.execute('CREATE TABLE IF NOT EXISTS support_prepared_rows ('
        'row_key TEXT PRIMARY KEY,source_digest TEXT NOT NULL,policy TEXT NOT NULL,'
        'content BLOB NOT NULL,content_digest TEXT NOT NULL,redactions INTEGER NOT NULL,'
        "secret_fingerprints TEXT NOT NULL DEFAULT '[]',guard_digest TEXT NOT NULL DEFAULT '')")
    columns={row[1] for row in connection.execute('PRAGMA table_info(support_prepared_rows)')}
    for name,default in [('secret_fingerprints',"'[]'"),('guard_digest',"''")]:
        if name not in columns:connection.execute(f'ALTER TABLE support_prepared_rows ADD COLUMN {name} TEXT NOT NULL DEFAULT {default}')
    connection.execute('CREATE TABLE IF NOT EXISTS support_prepared_chunks ('
        'minute INTEGER PRIMARY KEY,source_digest TEXT NOT NULL,policy TEXT NOT NULL,'
        'content BLOB NOT NULL,content_digest TEXT NOT NULL,redactions INTEGER NOT NULL)')
    # Retirement, replacement and identity migrations stay with History. Never
    # keep a detached public row alive after its private source was removed.
    for table,key in [('evidence',"'e:'||OLD.event_key"),
                      ('checkpoint',"'c:'||OLD.scope_key||char(0)||OLD.name")]:
        for action in ('DELETE','UPDATE'):
            connection.execute(f'CREATE TRIGGER IF NOT EXISTS support_prepare_{table}_{action.lower()} '
                f'AFTER {action} ON {table} BEGIN DELETE FROM support_prepared_rows '
                f'WHERE row_key={key}; END')
            if table=='evidence':
                connection.execute(f'CREATE TRIGGER IF NOT EXISTS support_chunk_evidence_{action.lower()} '
                    f'AFTER {action} ON evidence BEGIN DELETE FROM support_prepared_chunks '
                    'WHERE minute=CAST(OLD.observed_at/60 AS INTEGER); END')


def _support_row_key(record: Mapping[str,Any]) -> str:
    return ('e:'+record['event_key'] if record['record_type']=='evidence' else
        'c:'+record['scope_key']+'\0'+record['name'])


def _support_source_digest(record: Mapping[str,Any],encoded: bytes) -> str:
    metadata=json.dumps(dict(record),sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    return hashlib.sha256(metadata+b'\0'+encoded).hexdigest()


def _support_read_prepared(connection: sqlite3.Connection,record: Mapping[str,Any],encoded: bytes):
    if encoded.startswith(_PAYLOAD_DELTA):_payload_base(connection, encoded)
    try:
        row=connection.execute('SELECT source_digest,policy,content,content_digest,redactions '
            'FROM support_prepared_rows WHERE row_key=?',(_support_row_key(record),)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):return None
        raise
    if row is None or row[0]!=_support_source_digest(record,encoded) or row[1]!=_prepared_support.support_preparation_policy():
        return None
    content=bytes(row[2])
    if not content:
        return None  # Its closed minute owns the compressed bytes now.
    if hashlib.sha256(content).hexdigest()!=row[3]:
        raise ValueError('support_prepared_content_corrupt')
    return content,int(row[4])


def _support_write_prepared(connection: sqlite3.Connection,record: Mapping[str,Any],encoded: bytes) -> None:
    original=dict(record,payload=_decode(encoded, connection))
    fingerprints=set()
    content,redactions=_prepared_support.prepare_support_record(original,secret_fingerprints=fingerprints)
    source=_support_source_digest(record,encoded);policy=_prepared_support.support_preparation_policy()
    guards=json.dumps(sorted(fingerprints),separators=(',',':'))
    guard_digest=hashlib.sha256((source+'|'+policy+'|'+guards).encode()).hexdigest()
    connection.execute('INSERT OR REPLACE INTO support_prepared_rows VALUES (?,?,?,?,?,?,?,?)',
        (_support_row_key(record),source,policy,content,hashlib.sha256(content).hexdigest(),redactions,guards,guard_digest))


def _support_prepare_written_rows(connection: sqlite3.Connection,*,evidence_keys=(),checkpoint_keys=()) -> int:
    """Called only inside the existing disk worker's bounded write batch."""
    common={'schema':SUPPORT_HISTORY_EXPORT_SCHEMA,'owner':SUPPORT_HISTORY_EXPORT_OWNER}
    count=0
    for key in dict.fromkeys(evidence_keys):
        row=connection.execute('SELECT event_key,scope_key,kind,observed_at,payload,logical_count FROM evidence WHERE event_key=?',(key,)).fetchone()
        if row is None:continue
        event,scope,kind,stamp,encoded,logical=row
        record=dict(common,record_type='evidence',event_key=event,scope_key=scope,kind=kind,
            observed_at=float(stamp),logical_count=max(1,int(logical or 1)))
        _support_write_prepared(connection,record,encoded);count+=1
    for scope,name in dict.fromkeys(checkpoint_keys):
        row=connection.execute('SELECT updated_at,payload FROM checkpoint WHERE scope_key=? AND name=?',(scope,name)).fetchone()
        if row is None:continue
        record=dict(common,record_type='checkpoint',scope_key=scope,name=name,updated_at=float(row[0]))
        _support_write_prepared(connection,record,row[1]);count+=1
    return count


def _support_evidence_record(row):
    event,scope,kind,stamp,encoded,logical=row
    return dict(schema=SUPPORT_HISTORY_EXPORT_SCHEMA,owner=SUPPORT_HISTORY_EXPORT_OWNER,
        record_type='evidence',event_key=event,scope_key=scope,kind=kind,
        observed_at=float(stamp),logical_count=max(1,int(logical or 1)))


def _support_chunk_digest(rows) -> str:
    digest=hashlib.sha256()
    for row in rows:
        digest.update(_support_source_digest(_support_evidence_record(row),row[4]).encode())
    return digest.hexdigest()


def _support_read_prepared_chunk(connection,rows):
    for source in rows:
        if source[4].startswith(_PAYLOAD_DELTA):_payload_base(connection, source[4])
    try:
        row=connection.execute('SELECT source_digest,policy,content,content_digest,redactions '
            'FROM support_prepared_chunks WHERE minute=?',(int(rows[0][3]//60),)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):return None
        raise
    if row is None or row[0]!=_support_chunk_digest(rows) or row[1]!=_prepared_support.support_preparation_policy():
        return None
    if hashlib.sha256(row[2]).hexdigest()!=row[3]:raise ValueError('support_prepared_chunk_corrupt')
    # The final known-secret scan needs every reused source's verification
    # context. A valid compressed chunk alone does not certify that metadata.
    keys=[('e:'+source[0]) for source in rows]
    guards={value[0]:value[1:] for value in connection.execute(
        'SELECT row_key,source_digest,policy,secret_fingerprints,guard_digest '
        'FROM support_prepared_rows WHERE row_key IN ('+','.join('?' for _ in keys)+')',keys)}
    if len(guards)!=len(rows):return None
    policy=_prepared_support.support_preparation_policy()
    for source in rows:
        source_hash,cached_policy,fingerprints,guard_hash=guards['e:'+source[0]]
        if cached_policy!=policy or source_hash!=_support_source_digest(_support_evidence_record(source),source[4]):
            return None
        if hashlib.sha256((source_hash+'|'+policy+'|'+fingerprints).encode()).hexdigest()!=guard_hash:
            raise ValueError('support_prepared_guard_corrupt')
    return bytes(row[2]),int(row[4])


def _support_compact_prepared_minute(connection,minute: int) -> None:
    """One bounded closed minute, compressed off the modem clock by History."""
    rows=list(connection.execute('SELECT event_key,scope_key,kind,observed_at,payload,logical_count '
        'FROM evidence WHERE observed_at>=? AND observed_at<? ORDER BY observed_at,event_key LIMIT ?',
        (minute*60,(minute+1)*60,_SUPPORT_MAX_CHUNK_ROWS+1)))
    if not rows or len(rows)>_SUPPORT_MAX_CHUNK_ROWS or sum(len(row[4]) for row in rows)>_SUPPORT_MAX_CHUNK_BYTES:
        return
    if _support_read_prepared_chunk(connection,rows) is not None:return
    output=bytearray();redactions=0
    for row in rows:
        record=_support_evidence_record(row);prepared=_support_read_prepared(connection,record,row[4])
        if prepared is None:
            # Regenerate clean bytes AND the guard together when repairing an
            # old/missing row; no detached chunk may lose its privacy context.
            _support_write_prepared(connection,record,row[4])
            prepared=_support_read_prepared(connection,record,row[4])
        body,count=prepared
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
            remaining=_SUPPORT_MAX_CHUNK_BYTES-len(output)
            part=stream.read(remaining+1)
        if len(part)>remaining:return
        output.extend(part);redactions+=count
    content=gzip.compress(bytes(output),compresslevel=6,mtime=0)
    connection.execute('INSERT OR REPLACE INTO support_prepared_chunks VALUES (?,?,?,?,?,?)',
        (minute,_support_chunk_digest(rows),_prepared_support.support_preparation_policy(),content,
            hashlib.sha256(content).hexdigest(),redactions))
    # Keep source fingerprints; don't duplicate each row's compressed content.
    connection.executemany("UPDATE support_prepared_rows SET content=X'',content_digest='' WHERE row_key=?",
        [('e:'+row[0],) for row in rows])


def _support_collect_secret_verification_context(connection,*,evidence_where,evidence_params,
        checkpoint_where,checkpoint_params,secrets: set[str]) -> int:
    """Recover each unique guard from its existing private source once.

    The cache stores hashes/reference metadata, never extra plaintext secrets.
    This retains the final cross-record secret scan without parsing bulk rows.
    """
    from arch4.privacy_scrub import support_known_secret_values
    try:
        columns={row[1] for row in connection.execute('PRAGMA table_info(support_prepared_rows)')}
        if 'secret_fingerprints' not in columns:return 0  # Old/no caches cannot be reused under v2 policy.
        cursors=[('evidence',connection.execute('SELECT e.event_key,e.scope_key,e.kind,e.observed_at,e.payload,e.logical_count,'
            'p.source_digest,p.policy,p.secret_fingerprints,p.guard_digest FROM '
            '(SELECT * FROM evidence'+evidence_where+") e JOIN support_prepared_rows p ON p.row_key='e:'||e.event_key",evidence_params)),
            ('checkpoint',connection.execute('SELECT c.scope_key,c.name,c.updated_at,c.payload,'
            'p.source_digest,p.policy,p.secret_fingerprints,p.guard_digest FROM '
            '(SELECT * FROM checkpoint'+checkpoint_where+") c JOIN support_prepared_rows p ON p.row_key='c:'||c.scope_key||char(0)||c.name",checkpoint_params))]
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):return 0
        raise
    seen=set();decoded=0;policy=_prepared_support.support_preparation_policy()
    for kind,cursor in cursors:
        for row in cursor:
            source,cached_policy,guards,guard_digest=row[-4:]
            if cached_policy!=policy:continue
            if hashlib.sha256((source+'|'+policy+'|'+guards).encode()).hexdigest()!=guard_digest:
                raise ValueError('support_prepared_guard_corrupt')
            missing=set(json.loads(guards))-seen
            if not missing:continue
            if len(seen|missing)>4096:raise ValueError('support_secret_verification_context_limit')
            if kind=='evidence':record=_support_evidence_record(row[:6]);encoded=row[4]
            else:
                scope,name,stamp,encoded=row[:4]
                record=dict(schema=SUPPORT_HISTORY_EXPORT_SCHEMA,owner=SUPPORT_HISTORY_EXPORT_OWNER,
                    record_type='checkpoint',scope_key=scope,name=name,updated_at=float(stamp))
            if _support_source_digest(record,encoded)!=source:continue  # Fallback exporter already scans changed sources.
            found=support_known_secret_values(dict(record,payload=_decode(encoded, connection)))
            hashes={hashlib.sha256(value.encode()).hexdigest() for value in found}
            if not missing.issubset(hashes):raise ValueError('support_prepared_guard_source_mismatch')
            secrets.update(found);seen.update(hashes);decoded+=1
    return decoded


def _support_evidence_groups(cursor):
    """Bounded minute groups; overflow safely uses individual prepared rows."""
    group = []
    size = 0
    for row in cursor:
        if group and (int(group[0][3] // 60) != int(row[3] // 60)
                or len(group) >= _SUPPORT_MAX_CHUNK_ROWS or size + len(row[4]) > _SUPPORT_MAX_CHUNK_BYTES):
            yield group
            group = []
            size = 0
        group.append(row)
        size += len(row[4])
    if group:
        yield group


@dataclass(frozen=True, slots=True)
class NewHorizonSupportHistoryExport:
    """Frozen private diagnostic or byte-attested prepared public History."""

    content: bytes
    evidence_row_count: int
    evidence_logical_count: int
    checkpoint_row_count: int
    known_secret_values: tuple[str, ...]
    filename: str = SUPPORT_HISTORY_EXPORT_FILENAME
    prepared_artifact: Any = None
    preparation: dict[str, Any] | None = None


def capture_support_history_export(
    path: str | Path,
    *,
    since: float | None = None,
    until: float | None = None,
    progress: Callable[[int, int], None] | None = None,
    public: bool = False,
) -> NewHorizonSupportHistoryExport:
    """Freeze the selected rows through the one canonical History selector.

    A read transaction gives one consistent live-WAL snapshot. Payloads are
    Private diagnostics decode original rows. The support button selects public
    mode, reusing prepared clean gzip chunks and projecting only uncached rows
    or the boundary minutes. It supplies explicit session bounds; standalone
    private diagnostics may omit both bounds. Raw learning History is unchanged.
    """

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    bounded = since is not None or until is not None
    evidence_where = checkpoint_where = ""
    evidence_params: tuple = ()
    checkpoint_params: tuple = ()
    if bounded:
        if (type(since) not in (int, float) or type(until) not in (int, float)
                or not math.isfinite(since) or not math.isfinite(until)
                or since <= 0 or until < since):
            raise ValueError("support_history_session_window_invalid")
        # Observation storage is independently decodable minute batches. Include
        # the ending minute's tail, then trim its samples through the same codec.
        evidence_where = (
            " WHERE observed_at>=? AND (observed_at<=? OR "
            "(kind='observation_batch' AND observed_at<?))"
        )
        evidence_params = (since, until, (math.floor(until / 60.0) + 1) * 60.0)
        checkpoint_where = " WHERE updated_at>=? AND updated_at<=?"
        checkpoint_params = (since, until)
    connection = sqlite3.connect(
        f"file:{source.as_posix()}?mode=ro",
        uri=True,
        timeout=5.0,
    )
    try:
        connection.execute("BEGIN")
        evidence_count, evidence_logical_count = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN logical_count IS NULL "
            "OR logical_count < 1 THEN 1 ELSE logical_count END), 0) "
            "FROM evidence" + evidence_where, evidence_params,
        ).fetchone()
        checkpoint_count = connection.execute(
            "SELECT COUNT(*) FROM checkpoint" + checkpoint_where, checkpoint_params,
        ).fetchone()[0]
        edges: dict[str, tuple[dict[str, Any], int, float] | None] = {}
        if bounded:
            # Only the two boundary minutes need decoding before counts are
            # sealed. Interior rows are still decoded/scrubbed exactly once.
            for key, scope, encoded, count in connection.execute(
                "SELECT event_key,scope_key,payload,logical_count FROM evidence"
                + evidence_where
                + " AND kind='observation_batch' AND (observed_at<? OR observed_at>?)",
                (*evidence_params, (math.floor(since / 60.0) + 1) * 60.0, until),
            ):
                rows = _decode_observation_batch(
                    event_key=key, expected_scope_key=scope, payload=_decode(encoded))
                selected = tuple(row for row in rows if since <= row["observed_at"] <= until)
                evidence_logical_count += len(selected) - max(1, int(count or 1))
                if not selected:
                    edges[key] = None
                    evidence_count -= 1
                else:
                    compact = _compact_observations(scope, selected)
                    edges[key] = (compact, len(selected), compact["last_observed_at"])
        manifest = {
            "schema": SUPPORT_HISTORY_EXPORT_SCHEMA,
            "owner": SUPPORT_HISTORY_EXPORT_OWNER,
            "record_type": "manifest",
            "storage": "deduplicated_payload_rows",
            "complete": True,
            "evidence_row_count": int(evidence_count),
            "evidence_logical_count": int(evidence_logical_count),
            "checkpoint_row_count": int(checkpoint_count),
        }
        if bounded:
            manifest["window"] = {"from_at": float(since), "to_at": float(until)}
        secrets: set[str] = set()
        processed = 0
        output = io.BytesIO()
        prepared_stats = dict(reused_rows=0, fallback_rows=0, redactions=0)
        with (gzip.GzipFile(
            fileobj=output,
            mode="wb",
            compresslevel=6,
            mtime=0,
        ) if not public else nullcontext(None)) as compressed:
            def write_row(row: Mapping[str, Any], encoded: bytes | None = None) -> None:
                if public:
                    from arch4 import prepared_support

                    cached = _support_read_prepared(connection, row, encoded) if encoded is not None else None
                    if cached is not None:
                        body, redactions = cached
                        prepared_stats["reused_rows"] += 1
                    else:
                        original = dict(row, payload=_decode(encoded, connection)) if encoded is not None else row
                        body, redactions = prepared_support.prepare_support_record(original, known_secret_values=secrets)
                        if row["record_type"] != "manifest":
                            prepared_stats["fallback_rows"] += 1
                    prepared_stats["redactions"] += redactions
                    output.write(body)
                    return
                if encoded is not None:
                    row = dict(row, payload=_decode(encoded, connection))
                compressed.write(
                    (
                        json.dumps(
                            dict(row),
                            # Match the canonical support scrubber's JSONL
                            # rendering so its final privacy pass is idempotent.
                            ensure_ascii=False,
                            allow_nan=False,
                        )
                        + "\n"
                    ).encode("utf-8")
                )

            write_row(manifest)
            evidence_cursor = connection.execute(
                "SELECT event_key,scope_key,kind,observed_at,payload,logical_count "
                "FROM evidence" + evidence_where + " ORDER BY observed_at ASC,event_key ASC",
                evidence_params,
            )
            for group in _support_evidence_groups(evidence_cursor):
                cached_chunk = None
                if public and not any(row[0] in edges for row in group):

                    cached_chunk = _support_read_prepared_chunk(connection, group)
                if cached_chunk is not None:
                    body, redactions = cached_chunk
                    output.write(body)
                    prepared_stats["reused_rows"] += len(group)
                    prepared_stats["redactions"] += redactions
                    prepared_stats["chunk_reused_rows"] = prepared_stats.get("chunk_reused_rows", 0) + len(group)
                    processed += len(group)
                else:
                    for event_key, scope_key, kind, observed_at, encoded, logical_count in group:
                        if event_key in edges:
                            edge = edges[event_key]
                            if edge is None:
                                continue
                            payload, logical_count, observed_at = edge
                        else:
                            payload = None
                        write_row(dict(schema=SUPPORT_HISTORY_EXPORT_SCHEMA, owner=SUPPORT_HISTORY_EXPORT_OWNER,
                            record_type="evidence", event_key=event_key, scope_key=scope_key, kind=kind,
                            observed_at=float(observed_at), logical_count=max(1, int(logical_count or 1)),
                            **({"payload": payload} if payload is not None else {})), encoded if payload is None else None)
                        processed += 1
                if progress is not None:
                    progress(processed, int(evidence_count))
            for scope_key, name, updated_at, encoded in connection.execute(
                "SELECT scope_key,name,updated_at,payload "
                "FROM checkpoint" + checkpoint_where + " ORDER BY updated_at ASC,scope_key ASC,name ASC",
                checkpoint_params,
            ):
                write_row(
                    {
                        "schema": SUPPORT_HISTORY_EXPORT_SCHEMA,
                        "owner": SUPPORT_HISTORY_EXPORT_OWNER,
                        "record_type": "checkpoint",
                        "scope_key": scope_key,
                        "name": name,
                        "updated_at": float(updated_at),
                    }, encoded,
                )
        if progress is not None:
            progress(int(evidence_count), int(evidence_count))
        content = output.getvalue()
        prepared_artifact = None
        if public:
            from arch4.prepared_support import seal_prepared_artifact

            prepared_stats["secret_context_rows_decoded"] = _support_collect_secret_verification_context(connection,
                evidence_where=evidence_where, evidence_params=evidence_params,
                checkpoint_where=checkpoint_where, checkpoint_params=checkpoint_params, secrets=secrets)
            prepared_artifact = seal_prepared_artifact(SUPPORT_HISTORY_EXPORT_FILENAME, content,
                redaction_count=prepared_stats["redactions"])
        return NewHorizonSupportHistoryExport(
            content=content,
            evidence_row_count=int(evidence_count),
            evidence_logical_count=int(evidence_logical_count),
            checkpoint_row_count=int(checkpoint_count),
            known_secret_values=tuple(sorted(secrets)),
            prepared_artifact=prepared_artifact,
            preparation=prepared_stats if public else None,
        )
    finally:
        connection.close()


_SUPPORT_HISTORY_RECORD_BYTES = 32 * 1024 * 1024
_SUPPORT_HISTORY_RECORDS = 262144


def iter_support_history_export(source: bytes | str | Path, *, expand_observations: bool = False):
    """Sole gzip/JSONL reader; bound records instead of rejecting long sessions."""
    opener = (gzip.GzipFile(fileobj=io.BytesIO(source), mode="rb")
              if isinstance(source, bytes) else gzip.open(Path(source), "rb"))
    with opener as stream:
        manifest = None
        evidence_count = checkpoint_count = logical_count = 0
        physical_count = 0
        while raw := stream.readline(_SUPPORT_HISTORY_RECORD_BYTES + 1):
            physical_count += 1
            if physical_count > _SUPPORT_HISTORY_RECORDS + 1:  # Include the manifest.
                raise ValueError("support_history_record_count_limit")
            if len(raw) > _SUPPORT_HISTORY_RECORD_BYTES:
                raise ValueError("support_history_record_bytes_limit")
            if not raw.strip():
                continue
            row = json.loads(raw)
            if (not isinstance(row, dict) or row.get("schema") != SUPPORT_HISTORY_EXPORT_SCHEMA
                    or row.get("owner") != SUPPORT_HISTORY_EXPORT_OWNER):
                raise ValueError("new_horizon_support_history_contract_invalid")
            if manifest is None:
                if row.get("record_type") != "manifest":
                    raise ValueError("new_horizon_support_history_manifest_missing")
                for name in ("evidence_row_count", "checkpoint_row_count", "evidence_logical_count"):
                    if type(row.get(name)) is not int or not 0 <= row[name] <= _SUPPORT_HISTORY_RECORDS:
                        raise ValueError("support_history_record_count_limit")
                manifest = row
                yield row
                continue
            kind = row.get("record_type")
            if kind == "checkpoint":
                checkpoint_count += 1
                decoded = (row,)
            elif kind == "evidence":
                evidence_count += 1
                count = row.get("logical_count", 1)
                if type(count) is not int or count < 1:
                    raise ValueError("support_history_logical_count_invalid")
                logical_count += count
                if logical_count > _SUPPORT_HISTORY_RECORDS:
                    raise ValueError("support_history_record_count_limit")
                if expand_observations and row.get("kind") == "observation_batch":
                    payload = row["payload"]
                    runs = payload.get("runs")
                    if (not isinstance(runs, list) or payload.get("sample_count") != count
                            or any(not isinstance(r, dict) or type(r.get("repeat_count")) is not int
                                   or r["repeat_count"] < 1 for r in runs)
                            or sum(r["repeat_count"] for r in runs) != count):
                        raise ValueError("support_history_batch_count_invalid")
                    observations = _decode_observation_batch(event_key=str(row["event_key"]),
                        expected_scope_key=str(row["scope_key"]), payload=payload)
                    # The batch codec emits logical observations. The public
                    # stream supplies their export envelope in this one place.
                    decoded = ({"schema": SUPPORT_HISTORY_EXPORT_SCHEMA,
                                "owner": SUPPORT_HISTORY_EXPORT_OWNER,
                                "record_type": "evidence", **item} for item in observations)
                else:
                    decoded = (row,)
            else:
                raise ValueError("new_horizon_support_history_record_type_invalid")
            if evidence_count + checkpoint_count > _SUPPORT_HISTORY_RECORDS:
                raise ValueError("support_history_record_count_limit")
            yield from decoded
        if manifest is None:
            raise ValueError("new_horizon_support_history_manifest_missing")
        if (evidence_count != manifest["evidence_row_count"]
                or checkpoint_count != manifest["checkpoint_row_count"]
                or logical_count != manifest["evidence_logical_count"]):
            raise ValueError("new_horizon_support_history_count_mismatch")


def _collect_support_history_export(records):
    manifest, evidence, checkpoints = None, [], []
    for row in records:
        if manifest is None:
            manifest = row
        elif row["record_type"] == "checkpoint":
            checkpoints.append(row)
        else:
            evidence.append(row)
    return {"manifest": manifest, "evidence": tuple(evidence), "checkpoints": tuple(checkpoints)}


def read_support_history_export(
    path: str | Path,
    *,
    expand_observations: bool = False,
) -> dict[str, Any]:
    """Read a support History export and optionally rebuild observations."""

    return _collect_support_history_export(iter_support_history_export(
        Path(path).expanduser().resolve(), expand_observations=expand_observations))


def decode_support_history_export(content: bytes, *, expand_observations: bool = False) -> dict[str, Any]:
    """Canonical bytes reader for frozen ZIP inputs; no temporary database."""
    return _collect_support_history_export(iter_support_history_export(
        content, expand_observations=expand_observations))


def _legacy_learning_context_key(components: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(components),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _migrate_learning_payload(
    payload: Mapping[str, Any],
    *,
    require_key: bool,
) -> tuple[dict[str, Any], bool]:
    from arch4.new_horizon.learning_context import (
        learning_context_key,
        migrate_legacy_learning_context,
        require_learning_context,
    )

    row = dict(payload)
    raw_context = row.get("learning_context")
    if not isinstance(raw_context, Mapping):
        raise ValueError("new_horizon_history_learning_context_missing")
    migrated = migrate_legacy_learning_context(
        raw_context, observed_at=row.get("opened_at", row.get("observed_at")))
    changed = dict(raw_context) != migrated
    if not changed:
        require_learning_context(migrated)
    if require_key:
        stored_key = row.get("learning_context_key")
        if type(stored_key) is not str or not stored_key:
            raise ValueError("new_horizon_history_learning_context_key_missing")
        expected_old_key = (
            _legacy_learning_context_key(raw_context)
            if changed
            else learning_context_key(migrated)
        )
        if stored_key != expected_old_key:
            raise ValueError("new_horizon_history_learning_context_key_mismatch")
        row["learning_context_key"] = learning_context_key(migrated)
    row["learning_context"] = migrated
    return row, changed


def _hourly_context_payload(value: Any, *, original_at: float) -> Any:
    """Named History-boundary adapter; never use import time for old keys.

    AQ/metrics are not regraded. Old expectations remain factual audit records,
    but cannot train the new hourly forecast's confidence.
    """
    from arch4.new_horizon.learning_context import migrate_legacy_learning_context, learning_context_key
    if isinstance(value, list):
        return [_hourly_context_payload(item, original_at=original_at) for item in value]
    if not isinstance(value, dict):
        return value
    stamp = original_at
    frame = value.get("decision_frame")
    if isinstance(frame, dict) and type(frame.get("accepted_at")) in {int, float}:
        stamp = frame["accepted_at"]
    for name in ("opened_at", "accepted_at", "observed_at"):
        candidate = value.get(name)
        if type(candidate) in {int, float} and math.isfinite(candidate) and candidate > 0:
            stamp = candidate
            break
    row = {key: _hourly_context_payload(item, original_at=stamp)
           if key not in {"learning_context", "learning_context_migration"} else item
           for key, item in value.items()}
    old = row.get("learning_context")
    legacy = isinstance(old, dict) and (
        old.get("schema") == "NewHorizonLearningContext.v1"
        or set(old) == {"exact_scope_key", "day_class", "hour", "load_class"})
    if legacy:
        context = migrate_legacy_learning_context(old, observed_at=stamp)
        key = row.get("learning_context_key")
        if key and key != _legacy_learning_context_key(old):
            raise ValueError("new_horizon_history_learning_context_key_mismatch")
        row["learning_context"] = context
        if "learning_context_key" in row:
            row["learning_context_key"] = learning_context_key(context)
        row["learning_context_migration"] = {
            "from": old, "original_at": stamp, "to": "hourly_v2"}
        if isinstance(row.get("forecast"), dict):
            row["forecast"] = {**row["forecast"], "time_basis": "legacy_six_hour"}
    return row


def _migrate_hourly_context_store(connection: sqlite3.Connection, path: Path) -> None:
    """Atomic, once-per-store conversion before the owner's caches are read."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == 3:
        return
    if version > 3:
        raise ValueError("new_horizon_history_newer_time_schema")
    if version == 2:
        # Prepared catalogs and independent payload deltas need the new reader.
        # Previous readers reject this revision before attempting restoration.
        with connection:
            connection.execute("PRAGMA user_version=3")
        return
    changes = []
    for key, stamp, encoded in connection.execute(
        "SELECT event_key, observed_at, payload FROM evidence "
        "WHERE kind LIKE 'referee_%' OR kind LIKE 'decision_%' OR kind LIKE 'proposal_%'"
    ).fetchall():
        old = _decode(encoded)
        new = _hourly_context_payload(old, original_at=stamp)
        if old != new:
            changes.append(("evidence", key, None, new))
    for scope, name, stamp, encoded in connection.execute(
        "SELECT scope_key, name, updated_at, payload FROM checkpoint"
    ).fetchall():
        old = _decode(encoded)
        new = _hourly_context_payload(old, original_at=stamp)
        if old != new:
            changes.append(("checkpoint", scope, name, new))
    if changes:
        backup = path.with_name(path.name + ".pre-hourly-context-" + uuid4().hex + ".bak")
        saved = sqlite3.connect(backup)
        try:
            connection.backup(saved)
        finally:
            saved.close()
    with connection:
        for table, key, name, payload in changes:
            if table == "evidence":
                connection.execute("UPDATE evidence SET payload=? WHERE event_key=?", (_encode(payload), key))
            else:
                connection.execute("UPDATE checkpoint SET payload=? WHERE scope_key=? AND name=?", (_encode(payload), key, name))
        if changes:
            connection.execute("DELETE FROM checkpoint WHERE name IN ('posteriors_v15','adviser_credit_v7')")
        connection.execute("PRAGMA user_version=3")


def _inspect_or_migrate_learning_context(
    connection: sqlite3.Connection,
    *,
    mutate: bool,
) -> dict[str, int]:
    evidence_before = int(
        connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    )
    logical_before = int(
        connection.execute(
            "SELECT COALESCE(SUM(logical_count), 0) FROM evidence"
        ).fetchone()[0]
    )
    checkpoints_before = int(
        connection.execute("SELECT COUNT(*) FROM checkpoint").fetchone()[0]
    )
    transformed_evidence = 0
    legacy_contexts = 0
    target_kinds = (
        *tuple(_LEARNING_MIGRATION_PROOF_CONTRACTS),
        _LEARNING_MIGRATION_DECISION_KIND,
    )
    placeholders = ",".join("?" for _ in target_kinds)
    evidence_rows = connection.execute(
        f"SELECT event_key, kind, payload FROM evidence WHERE kind IN ({placeholders})",
        target_kinds,
    ).fetchall()
    for event_key, kind, encoded in evidence_rows:
        payload = _decode(encoded)
        expected_schema = _LEARNING_MIGRATION_PROOF_CONTRACTS.get(
            str(kind),
            _LEARNING_MIGRATION_DECISION_SCHEMA,
        )
        if payload.get("schema") != expected_schema:
            raise ValueError("new_horizon_history_learning_schema_invalid")
        if kind in _LEARNING_MIGRATION_PROOF_CONTRACTS and (
            not isinstance(payload.get("learning_context"), Mapping)
            or not payload.get("learning_context")
        ):
            if (
                payload.get("status") == "graded"
                and payload.get("final_assessment") is True
            ):
                raise ValueError("new_horizon_history_learning_context_missing")
            continue
        migrated, changed = _migrate_learning_payload(
            payload,
            require_key=kind in _LEARNING_MIGRATION_PROOF_CONTRACTS,
        )
        if not changed:
            continue
        legacy_contexts += 1
        transformed_evidence += 1
        if mutate:
            connection.execute(
                "UPDATE evidence SET payload=? WHERE event_key=?",
                (_encode(migrated), str(event_key)),
            )

    transformed_pending_rows = 0
    pending_rows = connection.execute(
        "SELECT scope_key, payload FROM checkpoint WHERE name=?",
        (_LEARNING_MIGRATION_PENDING_NAME,),
    ).fetchall()
    for scope_key, encoded in pending_rows:
        payload = _decode(encoded)
        if payload.get("schema") != _LEARNING_MIGRATION_PENDING_SCHEMA:
            raise ValueError("new_horizon_history_pending_schema_invalid")
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise ValueError("new_horizon_history_pending_rows_invalid")
        migrated_rows: list[dict[str, Any]] = []
        checkpoint_changed = False
        for pending in rows:
            if not isinstance(pending, Mapping):
                raise ValueError("new_horizon_history_pending_row_invalid")
            migrated, changed = _migrate_learning_payload(
                pending,
                require_key=True,
            )
            migrated_rows.append(migrated)
            if changed:
                legacy_contexts += 1
                transformed_pending_rows += 1
                checkpoint_changed = True
        if mutate and checkpoint_changed:
            connection.execute(
                "UPDATE checkpoint SET payload=? WHERE scope_key=? AND name=?",
                (
                    _encode({**payload, "rows": migrated_rows}),
                    str(scope_key),
                    _LEARNING_MIGRATION_PENDING_NAME,
                ),
            )

    removed_derived = 0
    if legacy_contexts:
        placeholders = ",".join("?" for _ in _LEARNING_MIGRATION_DERIVED_CHECKPOINTS)
        removed_derived = int(
            connection.execute(
                f"SELECT COUNT(*) FROM checkpoint WHERE name IN ({placeholders})",
                tuple(sorted(_LEARNING_MIGRATION_DERIVED_CHECKPOINTS)),
            ).fetchone()[0]
        )
        if mutate and removed_derived:
            connection.execute(
                f"DELETE FROM checkpoint WHERE name IN ({placeholders})",
                tuple(sorted(_LEARNING_MIGRATION_DERIVED_CHECKPOINTS)),
            )

    if mutate:
        evidence_after = int(
            connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        )
        logical_after = int(
            connection.execute(
                "SELECT COALESCE(SUM(logical_count), 0) FROM evidence"
            ).fetchone()[0]
        )
        if (evidence_after, logical_after) != (evidence_before, logical_before):
            raise RuntimeError("new_horizon_history_migration_evidence_loss")
    return {
        "evidence_rows": evidence_before,
        "logical_evidence": logical_before,
        "checkpoint_rows": checkpoints_before,
        "legacy_contexts": legacy_contexts,
        "transformed_evidence": transformed_evidence,
        "transformed_pending_rows": transformed_pending_rows,
        "removed_derived_checkpoints": removed_derived,
    }


def migrate_learning_context_history(
    path: str | Path,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Inspect or migrate one offline history store to six-hour keys."""

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)

    if not apply:
        connection = sqlite3.connect(
            f"file:{source.as_posix()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        try:
            receipt = _inspect_or_migrate_learning_context(
                connection,
                mutate=False,
            )
        finally:
            connection.close()
        return {
            "schema": "NewHorizonLearningContextMigration.v1",
            "applied": False,
            **receipt,
        }

    owner = sqlite3.connect(source, timeout=0.0, isolation_level=None)
    try:
        try:
            checkpoint = owner.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise sqlite3.OperationalError("learning_history_checkpoint_busy")
            owner.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                "new_horizon_history_migration_requires_offline_store"
            ) from exc
        wal = source.with_name(source.name + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            raise RuntimeError("new_horizon_history_migration_requires_offline_store")

        token = uuid4().hex
        staging = source.with_name(source.name + f".migration-{token}.tmp")
        backup = source.with_name(
            source.name + ".pre-six-hour-" + strftime("%Y%m%d-%H%M%S") + ".bak"
        )
        shutil.copy2(source, staging)
        connection = sqlite3.connect(staging, timeout=1.0)
        try:
            connection.execute("BEGIN EXCLUSIVE")
            receipt = _inspect_or_migrate_learning_context(
                connection,
                mutate=True,
            )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise RuntimeError("new_horizon_history_migration_integrity_failed")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
        except Exception:
            connection.rollback()
            connection.close()
            staging.unlink(missing_ok=True)
            raise
        else:
            connection.close()

        if not receipt["legacy_contexts"]:
            staging.unlink(missing_ok=True)
            return {
                "schema": "NewHorizonLearningContextMigration.v1",
                "applied": False,
                "reason": "already_current",
                **receipt,
            }
        shutil.copy2(source, backup)
    finally:
        try:
            owner.rollback()
        finally:
            owner.close()

    try:
        os.replace(staging, source)
    except Exception:
        staging.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        raise
    fsync_parent_dir(source)
    return {
        "schema": "NewHorizonLearningContextMigration.v1",
        "applied": True,
        "backup": str(backup),
        **receipt,
    }


def discover_learning_context_histories(
    roots: tuple[str | Path, ...],
) -> tuple[Path, ...]:
    """Find history stores below the selected portable roots."""

    if not isinstance(roots, tuple) or not roots:
        raise TypeError("new_horizon_history_migration_roots_required")
    discovered: set[Path] = set()
    for raw_root in roots:
        root = Path(raw_root).expanduser().resolve()
        if not root.exists():
            raise FileNotFoundError(root)
        if root.is_file():
            if (
                root.name != NEW_HORIZON_HISTORY_FILENAME
                or root.parent.name != NEW_HORIZON_HISTORY_DIRNAME
            ):
                raise ValueError("new_horizon_history_migration_store_not_canonical")
            discovered.add(root)
            continue
        if not root.is_dir():
            raise ValueError("new_horizon_history_migration_root_not_directory")

        candidates = [root / NEW_HORIZON_HISTORY_DIRNAME / NEW_HORIZON_HISTORY_FILENAME]
        if root.name == NEW_HORIZON_HISTORY_DIRNAME:
            candidates.append(root / NEW_HORIZON_HISTORY_FILENAME)
        for child in root.iterdir():
            if child.is_dir():
                candidates.append(
                    child / NEW_HORIZON_HISTORY_DIRNAME / NEW_HORIZON_HISTORY_FILENAME
                )
        for candidate in candidates:
            if candidate.is_file():
                discovered.add(candidate.resolve())
    return tuple(sorted(discovered, key=lambda path: str(path).casefold()))


def _validated_store_migration_receipt(path: Path) -> dict[str, Any] | None:
    receipt_path = path.with_name(LEARNING_CONTEXT_V68_RECEIPT_FILENAME)
    if not receipt_path.exists():
        return None
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("new_horizon_history_migration_receipt_invalid") from exc
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != _LEARNING_CONTEXT_STORE_RECEIPT_SCHEMA
        or receipt.get("release") != "CGF_v6.8"
        or receipt.get("history_path") != str(path)
        or receipt.get("completed") is not True
        or not isinstance(receipt.get("result"), dict)
        or receipt["result"].get("schema") != "NewHorizonLearningContextMigration.v1"
    ):
        raise ValueError("new_horizon_history_migration_receipt_invalid")
    return receipt


def migrate_learning_context_histories(
    roots: tuple[str | Path, ...],
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Check and migrate every selected store in one batch."""

    paths = discover_learning_context_histories(roots)
    received: list[dict[str, Any]] = []
    pending: list[Path] = []
    for path in paths:
        existing = _validated_store_migration_receipt(path)
        if existing is None:
            pending.append(path)
        else:
            received.append(
                {
                    "history_path": str(path),
                    "status": "receipt_current",
                    "result": dict(existing["result"]),
                }
            )

    inspected: list[tuple[Path, dict[str, Any]]] = []
    for path in pending:
        inspected.append((path, migrate_learning_context_history(path)))

    if not apply:
        return {
            "schema": _LEARNING_CONTEXT_BATCH_SCHEMA,
            "applied": False,
            "discovered_stores": len(paths),
            "pending_stores": len(pending),
            "stores": [
                *received,
                *(
                    {
                        "history_path": str(path),
                        "status": "validated",
                        "result": result,
                    }
                    for path, result in inspected
                ),
            ],
        }

    migrated: list[dict[str, Any]] = []
    for path, _dry_run in inspected:
        result = migrate_learning_context_history(path, apply=True)
        receipt = {
            "schema": _LEARNING_CONTEXT_STORE_RECEIPT_SCHEMA,
            "release": "CGF_v6.8",
            "history_path": str(path),
            "completed": True,
            "completed_at": time(),
            "result": result,
        }
        atomic_write_json(
            path.with_name(LEARNING_CONTEXT_V68_RECEIPT_FILENAME),
            receipt,
            sort_keys=True,
            trailing_newline=True,
        )
        migrated.append(
            {
                "history_path": str(path),
                "status": "migrated" if result.get("applied") else "already_current",
                "result": result,
            }
        )
    return {
        "schema": _LEARNING_CONTEXT_BATCH_SCHEMA,
        "applied": any(row["result"].get("applied") is True for row in migrated),
        "receipts_written": len(migrated),
        "discovered_stores": len(paths),
        "pending_stores": 0,
        "stores": [*received, *migrated],
    }


class AdvisoryHistory:
    """Semantic history store with memory-first intake and typed disk batches."""

    def __init__(
        self,
        path: str | Path,
        *,
        retention_seconds: int = _RETENTION_SECONDS,
    ) -> None:
        startup_started = monotonic()
        self.path = Path(path).resolve()
        if type(retention_seconds) is not int or retention_seconds <= 0:
            raise ValueError("new_horizon_history_retention_invalid")
        self.retention_seconds = retention_seconds
        self._lock = RLock()
        self._recovered_corruption = ""
        self._closed = False
        self._retired_evidence: dict[str, float] = {}
        self._checkpoint_retirements: dict[tuple[str, str], set[str]] = {}
        self._identity_migration_checks: set[tuple[str, str]] = set()
        self._provisional_device_session = uuid4().hex
        existed_at_open = self.path.is_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            startup = self._open_database_and_read()
        except sqlite3.DatabaseError as exc:
            quarantine_corrupt_sqlite(self.path, exc)
            self._recovered_corruption = f"{type(exc).__name__}:{exc}"
            startup = self._open_database_and_read()
        (
            self._connection,
            self._opened_evidence_count,
            self._opened_durable_row_count,
            self._opened_checkpoint_count,
            checkpoint_rows,
            recent_observation_keys,
            recent_observation_batches,
        ) = startup
        self._checkpoint_cache = {(str(scope), str(name)): _CHECKPOINT_UNLOADED
                                  for scope, name in checkpoint_rows}
        self._recent_observation_keys: OrderedDict[str, None] = OrderedDict(
            (str(row[0]), None) for row in reversed(recent_observation_keys)
        )
        self._observation_cache: OrderedDict[
            str,
            deque[dict[str, Any]],
        ] = OrderedDict()
        self._warmed_observation_scopes: set[str] = set()
        self._recent_kind_cache: dict[
            str,
            deque[dict[str, Any]],
        ] = {}
        self._warmed_recent_kinds: set[str] = set()
        self._evidence_count = self._opened_evidence_count
        self._checkpoint_count = self._opened_checkpoint_count
        self._durable_row_count = self._opened_durable_row_count
        self._support_prepared_rows = 0
        self._support_preparation_failures = 0
        self._support_open_minutes: set[int] = set()
        self._support_latest_minute = 0
        self._continuity_state = (
            "recovered_corruption"
            if self._recovered_corruption
            else "continued"
            if existed_at_open
            and (self._opened_evidence_count or self._opened_checkpoint_count)
            else "existing_empty"
            if existed_at_open
            else "new_store"
        )
        self._log_processor = MemoryFirstLogProcessor(
            owner="54guc.new_horizon.history",
            critical_sink=self._write_critical_batch,
            routine_sink=self._write_observation_batches,
        )
        self._startup_elapsed_s = monotonic() - startup_started

    def _open_database_and_read(
        self,
    ) -> tuple[
        sqlite3.Connection,
        int,
        int,
        int,
        list[tuple[Any, ...]],
        list[tuple[Any, ...]],
        list[tuple[Any, ...]],
    ]:
        connection = self._open_database()
        try:
            _migrate_hourly_context_store(connection, self.path)
            with connection:
                counts = connection.execute('SELECT logical_count,evidence_rows,checkpoints FROM history_counts WHERE singleton=1').fetchone()
                if counts is None or any(type(n) is not int or n < 0 for n in counts):
                    raise ValueError('history_startup_counts_invalid')
                evidence_count, durable_row_count, checkpoint_count = counts
                checkpoint_rows = connection.execute(
                    "SELECT scope_key, name FROM checkpoint"
                ).fetchall()
                recent_keys = connection.execute('SELECT event_key FROM history_observation_keys ORDER BY observed_at DESC LIMIT 4096').fetchall()
            return (
                connection,
                evidence_count,
                durable_row_count,
                checkpoint_count,
                checkpoint_rows,
                recent_keys,
                [],
            )
        except Exception:
            connection.close()
            raise

    def _remember_observation_key_locked(self, event_key: str) -> None:
        self._recent_observation_keys.pop(event_key, None)
        self._recent_observation_keys[event_key] = None
        while len(self._recent_observation_keys) > _OBSERVATION_KEY_LIMIT:
            self._recent_observation_keys.popitem(last=False)

    def _scope_cache_locked(
        self,
        scope_key: str,
    ) -> deque[dict[str, Any]]:
        cache = self._observation_cache.pop(scope_key, None)
        if cache is None:
            cache = deque(maxlen=_OBSERVATION_CACHE_LIMIT)
        self._observation_cache[scope_key] = cache
        while len(self._observation_cache) > _OBSERVATION_SCOPE_CACHE_LIMIT:
            evicted, _rows = self._observation_cache.popitem(last=False)
            self._warmed_observation_scopes.discard(evicted)
        return cache

    def _open_database(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=1.0,
            check_same_thread=False,
        )
        try:
            if connection.execute('PRAGMA user_version').fetchone()[0] > 3:
                raise ValueError('new_horizon_history_newer_time_schema')
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute('PRAGMA recursive_triggers=ON')
            connection.execute(
                "CREATE TABLE IF NOT EXISTS evidence ("
                "event_key TEXT PRIMARY KEY, scope_key TEXT NOT NULL, "
                "kind TEXT NOT NULL, observed_at REAL NOT NULL, payload BLOB NOT NULL, "
                "logical_count INTEGER NOT NULL DEFAULT 1)"
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(evidence)").fetchall()
            }
            if "logical_count" not in columns:
                connection.execute(
                    "ALTER TABLE evidence ADD COLUMN logical_count "
                    "INTEGER NOT NULL DEFAULT 1"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS evidence_scope_time "
                "ON evidence(scope_key, observed_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS evidence_time ON evidence(observed_at)"
            )
            connection.execute('CREATE INDEX IF NOT EXISTS evidence_scope_kind_time ON evidence(scope_key,kind,observed_at)')
            connection.execute('CREATE INDEX IF NOT EXISTS evidence_kind_time ON evidence(kind,observed_at)')
            connection.execute(
                "CREATE TABLE IF NOT EXISTS checkpoint ("
                "scope_key TEXT NOT NULL, name TEXT NOT NULL, "
                "updated_at REAL NOT NULL, payload BLOB NOT NULL, "
                "PRIMARY KEY(scope_key, name))"
            )

            _install_history_catalog(connection)
            _support_install(connection)
            connection.commit()
            return connection
        except Exception:
            connection.close()
            raise

    @contextmanager
    def _disk_transaction(self):
        """Keep live cached accounting aligned with an atomic SQLite batch."""
        with self._lock:
            before = (self._durable_row_count, self._evidence_count,
                      self._support_prepared_rows, self._support_latest_minute,
                      set(self._support_open_minutes))
            try:
                with self._connection:
                    yield
            except BaseException:
                (self._durable_row_count, self._evidence_count,
                 self._support_prepared_rows, self._support_latest_minute,
                 self._support_open_minutes) = before
                raise

    def _write_critical_batch(self, batch: tuple[MemoryLogRecord, ...]) -> None:
        with self._disk_transaction():
            for item in batch:
                record_type = str(item.payload["record_type"])
                if record_type == "checkpoint_scope_replacement":
                    source = (
                        str(item.payload["source_scope_key"]),
                        str(item.payload["source_name"]),
                    )
                    target = (
                        str(item.payload["target_scope_key"]),
                        str(item.payload["target_name"]),
                    )
                    current_source = self._connection.execute(
                        "SELECT payload FROM checkpoint WHERE scope_key=? AND name=?",
                        source,
                    ).fetchone()
                    current_target = self._connection.execute(
                        "SELECT payload FROM checkpoint WHERE scope_key=? AND name=?",
                        target,
                    ).fetchone()
                    source_value = None if current_source is None else _decode(current_source[0])
                    target_value = None if current_target is None else _decode(current_target[0])
                    if source_value != item.payload["expected_source_payload"]:
                        raise RuntimeError("new_horizon_checkpoint_migration_source_changed")
                    if target_value != item.payload["expected_target_payload"]:
                        raise RuntimeError("new_horizon_checkpoint_migration_target_changed")
                    self._connection.execute(
                        "INSERT INTO checkpoint VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(scope_key, name) DO UPDATE SET "
                        "updated_at=excluded.updated_at, payload=excluded.payload",
                        (*target, float(item.observed_at), _encode(item.payload["target_payload"])),
                    )
                    self._connection.execute(
                        "DELETE FROM checkpoint WHERE scope_key=? AND name=?", source,
                    )
                    self._connection.execute(
                        "INSERT INTO checkpoint VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(scope_key, name) DO UPDATE SET "
                        "updated_at=excluded.updated_at, payload=excluded.payload",
                        (
                            str(item.payload["source_scope_key"]),
                            str(item.payload["tombstone_name"]),
                            float(item.observed_at),
                            _encode(item.payload["tombstone_payload"]),
                        ),
                    )
                    continue
                if record_type == "checkpoint":
                    self._connection.execute(
                        "INSERT INTO checkpoint VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(scope_key, name) DO UPDATE SET "
                        "updated_at=excluded.updated_at, payload=excluded.payload",
                        (
                            str(item.payload["scope_key"]),
                            str(item.payload["name"]),
                            float(item.observed_at),
                            _encode(item.payload["payload"]),
                        ),
                    )
                    removed = 0
                    for event_key in item.payload.get("retire_event_keys", ()):
                        cursor = self._connection.execute(
                            "DELETE FROM evidence WHERE event_key=? AND scope_key=?",
                            (event_key, str(item.payload["scope_key"])),
                        )
                        removed += max(0, cursor.rowcount)
                    self._durable_row_count = max(0, self._durable_row_count - removed)
                    continue
                if str(item.payload["event_key"]) in self._retired_evidence:
                    continue
                cursor = self._connection.execute(
                    "INSERT OR IGNORE INTO evidence("
                    "event_key, scope_key, kind, observed_at, payload, logical_count"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(item.payload["event_key"]),
                        str(item.payload["scope_key"]),
                        str(item.payload["kind"]),
                        float(item.observed_at),
                        (_encode_brain_payload(self._connection,
                            scope_key=str(item.payload['scope_key']), observed_at=float(item.observed_at),
                            payload=item.payload['payload'])
                         if item.payload['kind']=='brain_evaluation_v1' else _encode(item.payload["payload"])),
                        1,
                    ),
                )
                if cursor.rowcount == 1:
                    self._durable_row_count += 1
            evidence_keys = [str(i.payload["event_key"]) for i in batch
                if i.payload["record_type"] == "evidence"]
            checkpoints = [(str(i.payload["scope_key"]), str(i.payload["name"])) for i in batch
                if i.payload["record_type"] == "checkpoint"]
            # Scope migration rows intentionally fall back to fresh projection;
            # update/delete triggers invalidate their former public copies.
            self._prepare_support_rows(evidence_keys=evidence_keys, checkpoint_keys=checkpoints)

    def _prepare_support_rows(self, *, evidence_keys=(), checkpoint_keys=()) -> None:
        transaction_open = self._connection.in_transaction
        try:
            self._support_prepared_rows += _support_prepare_written_rows(self._connection,
                evidence_keys=evidence_keys, checkpoint_keys=checkpoint_keys)
            for key in evidence_keys:
                row = self._connection.execute("SELECT observed_at FROM evidence WHERE event_key=?", (key,)).fetchone()
                if row is not None:
                    minute = int(row[0] // 60)
                    self._support_open_minutes.add(minute)
                    self._support_latest_minute = max(self._support_latest_minute, minute)
            for minute in sorted(self._support_open_minutes):
                if minute >= self._support_latest_minute:
                    continue
                _support_compact_prepared_minute(self._connection, minute)
                self._support_open_minutes.discard(minute)
        except Exception:
            # Projection errors remain optional. SQLite FULL/ROLLBACK can abort
            # the whole transaction; that must reach the writer's retry queue.
            self._support_preparation_failures += 1
            if transaction_open and not self._connection.in_transaction:
                raise

    def _write_observation_batches(
        self,
        batch: tuple[MemoryLogRecord, ...],
    ) -> None:
        grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        for item in batch:
            row = dict(item.payload)
            scope_key = str(row["scope_key"])
            minute = int(float(row["observed_at"]) // 60.0)
            grouped[(scope_key, minute)].append(row)
        cache_updates: list[tuple[str, tuple[dict[str, Any], ...]]] = []
        support_keys: list[str] = []
        with self._disk_transaction():
            for (scope_key, _minute), rows in sorted(grouped.items()):
                samples = tuple(sorted(rows, key=lambda row: float(row["observed_at"])))
                compact = _compact_observations(scope_key, samples)
                material = "|".join(str(row["event_key"]) for row in samples)
                batch_key = (
                    "observation_batch:"
                    + hashlib.sha256(material.encode("utf-8")).hexdigest()
                )
                cursor = self._connection.execute(
                    "INSERT OR IGNORE INTO evidence("
                    "event_key, scope_key, kind, observed_at, payload, logical_count"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        batch_key,
                        scope_key,
                        "observation_batch",
                        float(compact["last_observed_at"]),
                        _encode(compact),
                        int(compact["sample_count"]),
                    ),
                )
                if cursor.rowcount == 1:
                    self._durable_row_count += 1
                    support_keys.append(batch_key)
                cache_updates.append(
                    (
                        scope_key,
                        tuple(
                            _history_record(
                                event_key=str(row["event_key"]),
                                scope_key=scope_key,
                                kind="observation",
                                observed_at=float(row["observed_at"]),
                                payload=dict(row["payload"]),
                            )
                            for row in samples
                        ),
                    )
                )
                self._connection.executemany('INSERT OR IGNORE INTO history_observation_keys VALUES(?,?)',
                    ((row['event_key'],row['observed_at']) for row in samples))
            self._connection.execute('DELETE FROM history_observation_keys WHERE event_key IN '
                '(SELECT event_key FROM history_observation_keys ORDER BY observed_at DESC LIMIT -1 OFFSET 4096)')
            self._prepare_support_rows(evidence_keys=support_keys)
            cutoff = max(item.observed_at for item in batch) - self.retention_seconds
            expired = self._connection.execute(
                "SELECT logical_count FROM evidence WHERE observed_at < ?",
                (cutoff,),
            ).fetchall()
            cursor = self._connection.execute(
                "DELETE FROM evidence WHERE observed_at < ?",
                (cutoff,),
            )
            self._connection.execute('DELETE FROM history_payload_bases WHERE last_observed_at < ?', (cutoff,))
            removed_rows = max(0, cursor.rowcount)
            removed_evidence = sum(int(row[0]) for row in expired)
            self._durable_row_count = max(
                0,
                self._durable_row_count - removed_rows,
            )
            self._evidence_count = max(
                0,
                self._evidence_count - removed_evidence,
            )
        with self._lock:
            for scope_key, rows in cache_updates:
                cache = self._scope_cache_locked(scope_key)
                known = {str(row["event_key"]) for row in cache}
                for row in rows:
                    if (
                        float(row["observed_at"]) >= cutoff
                        and row["event_key"] not in known
                    ):
                        cache.append(row)
                        known.add(str(row["event_key"]))
        self._log_processor.discard_hot_before(
            observed_at=cutoff,
            lane=ROUTINE_LANE,
        )

    def record(
        self,
        *,
        event_key: str,
        scope_key: str,
        kind: str,
        observed_at: float,
        payload: Mapping[str, Any],
    ) -> bool:
        self._require_active_scope(scope_key)
        if kind == "observation":
            raise RuntimeError("new_horizon_observation_requires_log_processor_lane")
        if not all(
            type(item) is str and item.strip() for item in (event_key, scope_key, kind)
        ):
            raise ValueError("new_horizon_history_identity_required")
        stamp = float(observed_at)
        if stamp <= 0.0:
            raise ValueError("new_horizon_history_timestamp_invalid")
        clean = _validated_payload(_hourly_context_payload(dict(payload), original_at=stamp))
        if event_key in self._retired_evidence:
            return False
        if self.contains(event_key=event_key):
            return False
        accepted = self._log_processor.submit(
            key="evidence:" + event_key,
            lane=CRITICAL_LANE,
            observed_at=stamp,
            payload={
                "record_type": "evidence",
                "event_key": event_key,
                "scope_key": scope_key,
                "kind": kind,
                "payload": clean,
            },
        )
        if accepted:
            with self._lock:
                self._evidence_count += 1
                if kind in self._warmed_recent_kinds:
                    cache = self._recent_kind_cache.setdefault(
                        kind,
                        deque(maxlen=16),
                    )
                    cache.append(
                        _history_record(
                            event_key=event_key,
                            scope_key=scope_key,
                            kind=kind,
                            observed_at=stamp,
                            payload=clean,
                        )
                    )
        return accepted

    def admit_observation(
        self,
        *,
        event_key: str,
        scope_key: str,
        observed_at: float,
        payload: Mapping[str, Any],
    ) -> bool:
        self._require_active_scope(scope_key)
        if not all(
            type(item) is str and item.strip() for item in (event_key, scope_key)
        ):
            raise ValueError("new_horizon_history_identity_required")
        stamp = float(observed_at)
        if stamp <= 0.0:
            raise ValueError("new_horizon_history_timestamp_invalid")
        clean = _validated_payload(payload)
        with self._lock:
            if event_key in self._recent_observation_keys:
                return False
        accepted = self._log_processor.submit(
            key="observation:" + event_key,
            lane=ROUTINE_LANE,
            observed_at=stamp,
            payload={
                "event_key": event_key,
                "scope_key": scope_key,
                "observed_at": stamp,
                "payload": clean,
            },
        )
        if accepted:
            with self._lock:
                self._remember_observation_key_locked(event_key)
                self._evidence_count += 1
        return accepted

    def _warm_observation_scope(self, scope_key: str) -> None:
        with self._lock:
            if scope_key in self._warmed_observation_scopes:
                self._scope_cache_locked(scope_key)
                return
            rows = self._connection.execute(
                "SELECT event_key, scope_key, kind, observed_at, payload FROM evidence "
                "WHERE scope_key=? AND kind IN ('observation','observation_batch') "
                "ORDER BY observed_at DESC LIMIT 256",
                (scope_key,),
            ).fetchall()
            decoded: list[dict[str, Any]] = []
            for event_key, record_scope, kind, observed_at, encoded in reversed(rows):
                payload = _decode(encoded)
                if kind == "observation_batch":
                    decoded.extend(
                        _decode_observation_batch(
                            event_key=event_key,
                            expected_scope_key=record_scope,
                            payload=payload,
                        )
                    )
                else:
                    decoded.append(
                        _history_record(
                            event_key=event_key,
                            scope_key=record_scope,
                            kind="observation",
                            observed_at=observed_at,
                            payload=payload,
                        )
                    )
            cache = self._scope_cache_locked(scope_key)
            merged = {str(row["event_key"]): row for row in (*decoded, *tuple(cache))}
            cache.clear()
            cache.extend(
                sorted(
                    merged.values(),
                    key=lambda row: float(row["observed_at"]),
                )[-_OBSERVATION_CACHE_LIMIT:]
            )
            self._warmed_observation_scopes.add(scope_key)

    def query(
        self,
        *,
        scope_key: str,
        kind: str | None = None,
        since: float = 0.0,
        limit: int = 256,
    ) -> tuple[dict[str, Any], ...]:
        self._require_active_scope(scope_key)
        if type(scope_key) is not str or not scope_key.strip():
            raise ValueError("new_horizon_history_scope_required")
        if kind is not None and (type(kind) is not str or not kind.strip()):
            raise ValueError("new_horizon_history_semantic_kind_required")
        if type(limit) is not int or not 1 <= limit <= 8192:
            raise ValueError("new_horizon_history_query_limit_invalid")
        if kind == "observation":
            self._warm_observation_scope(scope_key)
            with self._lock:
                rows = list(self._observation_cache.get(scope_key, ()))
            for item in self._log_processor.hot_records(lane=ROUTINE_LANE):
                row = item.payload
                if row.get("scope_key") == scope_key:
                    rows.append(
                        _history_record(
                            event_key=str(row["event_key"]),
                            scope_key=str(row["scope_key"]),
                            kind="observation",
                            observed_at=float(row["observed_at"]),
                            payload=dict(row["payload"]),
                        )
                    )
            deduped = {
                str(row["event_key"]): row
                for row in rows
                if float(row["observed_at"]) >= float(since)
            }
            return tuple(
                sorted(deduped.values(), key=lambda row: float(row["observed_at"]))[
                    -limit:
                ]
            )
        sql = (
            "SELECT event_key, scope_key, kind, observed_at, payload FROM evidence "
            "WHERE scope_key=? AND observed_at>=?"
        )
        params: list[Any] = [scope_key, float(since)]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY observed_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            disk_rows = self._connection.execute(sql, params).fetchall()
            bases = _snapshot_payload_bases(self._connection, (row[4] for row in disk_rows))
        rows = [
            _history_record(
                event_key=row[0],
                scope_key=row[1],
                kind=row[2],
                observed_at=row[3],
                payload=_decode(row[4], self._connection, bases),
            )
            for row in reversed(disk_rows)
        ]
        for item in self._log_processor.hot_records(lane=CRITICAL_LANE):
            row = item.payload
            if (
                row.get("record_type") == "evidence"
                and row.get("scope_key") == scope_key
                and (kind is None or row.get("kind") == kind)
                and item.observed_at >= float(since)
            ):
                rows.append(
                    _history_record(
                        event_key=str(row["event_key"]),
                        scope_key=str(row["scope_key"]),
                        kind=str(row["kind"]),
                        observed_at=item.observed_at,
                        payload=dict(row["payload"]),
                    )
                )
        deduped = {str(row["event_key"]): row for row in rows
                   if row["event_key"] not in self._retired_evidence}
        return tuple(
            sorted(deduped.values(), key=lambda row: float(row["observed_at"]))[-limit:]
        )

    def query_kind(
        self,
        *,
        kind: str,
        since: float = 0.0,
        limit: int = 4096,
    ) -> tuple[dict[str, Any], ...]:
        """Read one evidence kind across its defined scopes."""

        if type(kind) is not str or not kind.strip() or kind == "observation":
            raise ValueError("new_horizon_history_semantic_kind_required")
        if type(limit) is not int or not 1 <= limit <= 8192:
            raise ValueError("new_horizon_history_query_limit_invalid")
        with self._lock:
            disk_rows = self._connection.execute(
                "SELECT event_key, scope_key, observed_at, payload FROM evidence "
                "WHERE kind=? AND observed_at>=? "
                "ORDER BY observed_at DESC LIMIT ?",
                (kind, float(since), limit),
            ).fetchall()
            bases = _snapshot_payload_bases(self._connection, (row[3] for row in disk_rows))
        rows = [
            _history_record(
                event_key=event_key,
                scope_key=scope_key,
                kind=kind,
                observed_at=observed_at,
                payload=_decode(payload, self._connection, bases),
            )
            for event_key, scope_key, observed_at, payload in reversed(disk_rows)
        ]
        for item in self._log_processor.hot_records(lane=CRITICAL_LANE):
            row = item.payload
            if (
                row.get("record_type") == "evidence"
                and row.get("kind") == kind
                and item.observed_at >= float(since)
            ):
                rows.append(
                    _history_record(
                        event_key=str(row["event_key"]),
                        scope_key=str(row["scope_key"]),
                        kind=kind,
                        observed_at=item.observed_at,
                        payload=dict(row["payload"]),
                    )
                )
        deduped = {str(row["event_key"]): row for row in rows
                   if row["event_key"] not in self._retired_evidence}
        return tuple(
            sorted(deduped.values(), key=lambda row: float(row["observed_at"]))[-limit:]
        )

    def query_scoped_kinds(
        self,
        *,
        scope_key: str,
        kinds: tuple[str, ...],
        limit_per_kind: int | None,
        since: float | None = None,
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        """Read one exact multi-kind population for a canonical owner.

        The disk reads share the history lock and the in-memory critical lane
        is captured once. This is the storage-boundary adapter for owners that
        must bind several related evidence kinds as one generation.
        None reads all retained semantic rows; it is never an observation query.
        """

        self._require_active_scope(scope_key)

        if type(scope_key) is not str or not scope_key.strip():
            raise ValueError("new_horizon_history_scope_required")
        if (
            type(kinds) is not tuple
            or not kinds
            or len(set(kinds)) != len(kinds)
            or any(type(kind) is not str or not kind.strip() for kind in kinds)
            or "observation" in kinds
        ):
            raise ValueError("new_horizon_history_semantic_kinds_required")
        if limit_per_kind is not None and (
            type(limit_per_kind) is not int or not 1 <= limit_per_kind <= 8192
        ):
            raise ValueError("new_horizon_history_query_limit_invalid")
        if since is not None and (
            type(since) not in {int, float} or not math.isfinite(since)
        ):
            raise ValueError("new_horizon_history_query_since_invalid")

        durable_by_kind: dict[str, list[tuple[Any, ...]]] = {}
        with self._lock:
            for kind in kinds:
                durable_by_kind[kind] = self._connection.execute(
                    "SELECT event_key, scope_key, observed_at, payload "
                    "FROM evidence WHERE scope_key=? AND kind=? "
                    "AND (? IS NULL OR observed_at >= ?) "
                    "ORDER BY observed_at DESC LIMIT ?",
                    (
                        scope_key, kind, since, since,
                        -1 if limit_per_kind is None else limit_per_kind + 1,
                    ),
                ).fetchall()
            bases = _snapshot_payload_bases(self._connection,
                (row[3] for group in durable_by_kind.values() for row in group))
        hot = self._log_processor.hot_records(lane=CRITICAL_LANE)

        result: dict[str, tuple[dict[str, Any], ...]] = {}
        for kind in kinds:
            rows = [
                _history_record(
                    event_key=event_key,
                    scope_key=record_scope,
                    kind=kind,
                    observed_at=observed_at,
                    payload=_decode(payload, self._connection, bases),
                )
                for event_key, record_scope, observed_at, payload in reversed(
                    durable_by_kind[kind]
                )
            ]
            for item in hot:
                row = item.payload
                if (
                    row.get("record_type") == "evidence"
                    and row.get("scope_key") == scope_key
                    and row.get("kind") == kind
                    and (since is None or item.observed_at >= since)
                ):
                    rows.append(
                        _history_record(
                            event_key=str(row["event_key"]),
                            scope_key=scope_key,
                            kind=kind,
                            observed_at=item.observed_at,
                            payload=dict(row["payload"]),
                        )
                    )
            deduped = {str(row["event_key"]): row for row in rows
                       if row["event_key"] not in self._retired_evidence}
            if limit_per_kind is not None and len(deduped) > limit_per_kind:
                raise RuntimeError(
                    "new_horizon_history_scoped_kind_capacity_exceeded:" + kind
                )
            result[kind] = tuple(
                sorted(
                    deduped.values(),
                    key=lambda row: (float(row["observed_at"]), row["event_key"]),
                )
            )
        return result

    def continuity_scopes_for_identity(
        self,
        *,
        kind: str,
        identity_code: str,
    ) -> tuple[str, ...]:
        raise RuntimeError("history_firmware_identity_lookup_retired_use_confirmed_device")

    def confirmed_legacy_scopes(self, *, continuity_key: str) -> tuple[str, ...]:
        self._require_active_scope(continuity_key)
        with self._lock:
            return tuple(sorted(scope for (scope, name) in self._checkpoint_cache
                if name == "confirmed_device_history_migration_v1"
                and self._checkpoint_payload((scope,name))["target_scope"] == continuity_key))

    def read_confirmed_factual_checkpoint(self, *, continuity_key: str, source_scope: str,
                                          name: str) -> dict[str, Any] | None:
        """Only this boundary may read a retired device's factual summary."""
        from arch4.new_horizon.history_identity_migration import route_factual_checkpoint
        if name not in {f"band_evidence_summary_v{version}" for version in range(2, 13)}:
            raise ValueError("history_retired_checkpoint_kind_forbidden")
        self._require_active_scope(continuity_key)
        with self._lock:
            receipt = self._checkpoint_payload((source_scope, "confirmed_device_history_migration_v1"))
            if receipt is None or receipt["target_scope"] != continuity_key:
                raise ValueError("history_retired_checkpoint_physical_proof_required")
            payload = self._checkpoint_payload((source_scope, name))
            return route_factual_checkpoint(payload, receipt["scope_map"]) if payload else None

    def recent_kind(
        self,
        *,
        kind: str,
        limit: int = 8,
    ) -> tuple[dict[str, Any], ...]:
        """Return recent records without scanning SQLite again."""

        if type(kind) is not str or not kind.strip() or kind == "observation":
            raise ValueError("new_horizon_history_semantic_kind_required")
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("new_horizon_history_recent_limit_invalid")
        with self._lock:
            if kind in self._warmed_recent_kinds:
                return tuple(row for row in self._recent_kind_cache.get(kind, ())
                             if row["event_key"] not in self._retired_evidence)[-limit:]
            # Records received during the initial disk read are added to this
            # cache and merged below. This avoids losing data during warm-up.
            self._warmed_recent_kinds.add(kind)
            cache = self._recent_kind_cache.setdefault(
                kind,
                deque(maxlen=16),
            )
        try:
            durable = self.query_kind(kind=kind, limit=16)
        except BaseException:
            with self._lock:
                self._warmed_recent_kinds.discard(kind)
                self._recent_kind_cache.pop(kind, None)
            raise
        with self._lock:
            merged = {str(row["event_key"]): row for row in (*durable, *tuple(cache))
                      if row["event_key"] not in self._retired_evidence}
            cache.clear()
            cache.extend(
                sorted(
                    merged.values(),
                    key=lambda row: float(row["observed_at"]),
                )[-16:]
            )
            return tuple(cache)[-limit:]

    def contains(self, *, event_key: str) -> bool:
        if type(event_key) is not str or not event_key.strip():
            raise ValueError("new_horizon_history_event_key_required")
        if event_key in self._retired_evidence:
            return False
        if self._log_processor.contains("evidence:" + event_key):
            return True
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM evidence WHERE event_key=?",
                (event_key,),
            ).fetchone()
        return row is not None

    def load_evidence(self, *, event_key: str) -> dict[str, Any] | None:
        """Read one record from memory first, then from disk."""

        if type(event_key) is not str or not event_key.strip():
            raise ValueError("new_horizon_history_event_key_required")
        if event_key in self._retired_evidence:
            return None
        for item in reversed(self._log_processor.hot_records(lane=CRITICAL_LANE)):
            row = item.payload
            if (
                row.get("record_type") == "evidence"
                and row.get("event_key") == event_key
            ):
                return _history_record(
                    event_key=event_key,
                    scope_key=str(row["scope_key"]),
                    kind=str(row["kind"]),
                    observed_at=item.observed_at,
                    payload=dict(row["payload"]),
                )
        with self._lock:
            row = self._connection.execute(
                "SELECT scope_key, kind, observed_at, payload FROM evidence "
                "WHERE event_key=?",
                (event_key,),
            ).fetchone()
            decoded = None if row is None else _decode(row[3], self._connection)
        if row is None:
            return None
        return _history_record(
            event_key=event_key,
            scope_key=row[0],
            kind=row[1],
            observed_at=row[2],
            payload=decoded,
        )

    def save_checkpoint(
        self,
        *,
        scope_key: str,
        name: str,
        updated_at: float,
        payload: Mapping[str, Any],
        retire_event_keys: tuple[str, ...] = (),
    ) -> None:
        self._require_active_scope(scope_key)
        if not all(type(item) is str and item.strip() for item in (scope_key, name)):
            raise ValueError("new_horizon_checkpoint_identity_required")
        stamp = float(updated_at)
        if stamp <= 0.0:
            raise ValueError("new_horizon_checkpoint_timestamp_invalid")
        clean = _validated_payload(_hourly_context_payload(dict(payload), original_at=stamp))
        cache_key = (scope_key, name)
        if type(retire_event_keys) is not tuple or any(
            type(key) is not str or not key for key in retire_event_keys
        ):
            raise ValueError("new_horizon_checkpoint_retire_keys_invalid")
        retired = {}
        for key in set(retire_event_keys) - self._retired_evidence.keys():
            row = self.load_evidence(event_key=key)
            if row is None or row["scope_key"] != scope_key:
                raise ValueError("new_horizon_checkpoint_retire_scope_invalid")
            retired[key] = float(row["observed_at"])
        with self._lock:
            retirements = self._checkpoint_retirements.get(cache_key, set()) | set(retire_event_keys)
            self._log_processor.submit(
                key=f"checkpoint:{scope_key}:{name}",
                lane=CRITICAL_LANE,
                observed_at=stamp,
                payload={
                    "record_type": "checkpoint",
                    "scope_key": scope_key,
                    "name": name,
                    "payload": clean,
                    "retire_event_keys": sorted(retirements),
                },
                replace=True,
            )
            self._checkpoint_retirements[cache_key] = retirements
            # Checkpoint replacement and source retirement form one disk
            # transaction in the existing critical lane. Memory readers switch
            # immediately; an old hot record cannot resurrect retired evidence.
            self._retired_evidence.update(retired)
            self._evidence_count = max(0, self._evidence_count - len(retired))
            is_new = cache_key not in self._checkpoint_cache
            self._checkpoint_cache[cache_key] = clean
            if is_new:
                self._checkpoint_count += 1

    def replace_checkpoint_scope(
        self,
        *,
        source_scope_key: str,
        target_scope_key: str,
        name: str,
        updated_at: float,
        target_payload: Mapping[str, Any],
        tombstone_name: str,
        tombstone_payload: Mapping[str, Any],
    ) -> None:
        """Atomically move one checkpoint and retire its former scope."""

        strings = (
            source_scope_key, target_scope_key, name, tombstone_name,
        )
        if not all(type(item) is str and item.strip() for item in strings):
            raise ValueError("new_horizon_checkpoint_migration_identity_required")
        if source_scope_key == target_scope_key or name == tombstone_name:
            raise ValueError("new_horizon_checkpoint_migration_distinct_identity_required")
        stamp = float(updated_at)
        if stamp <= 0.0:
            raise ValueError("new_horizon_checkpoint_migration_timestamp_invalid")
        target_clean = _validated_payload(target_payload)
        tombstone_clean = _validated_payload(tombstone_payload)
        source_key = (source_scope_key, name)
        target_key = (target_scope_key, name)
        tombstone_key = (source_scope_key, tombstone_name)
        with self._lock:
            source = self._checkpoint_payload(source_key)
            target = self._checkpoint_payload(target_key)
            if source is None:
                raise ValueError("new_horizon_checkpoint_migration_source_missing")
            if tombstone_key in self._checkpoint_cache:
                raise RuntimeError("new_horizon_checkpoint_migration_source_resurrected")
            expected_source = dict(source)
            expected_target = None if target is None else dict(target)
            existed = {source_key, target_key, tombstone_key} & self._checkpoint_cache.keys()
            self._log_processor.submit(
                key=f"checkpoint-migration:{source_scope_key}:{target_scope_key}:{name}",
                lane=CRITICAL_LANE,
                observed_at=stamp,
                payload={
                    "record_type": "checkpoint_scope_replacement",
                    "source_scope_key": source_scope_key,
                    "source_name": name,
                    "target_scope_key": target_scope_key,
                    "target_name": name,
                    "expected_source_payload": expected_source,
                    "expected_target_payload": expected_target,
                    "target_payload": target_clean,
                    "tombstone_name": tombstone_name,
                    "tombstone_payload": tombstone_clean,
                },
                replace=True,
            )
            self._checkpoint_cache.pop(source_key)
            self._checkpoint_cache[target_key] = target_clean
            self._checkpoint_cache[tombstone_key] = tombstone_clean
            current = {source_key, target_key, tombstone_key} & self._checkpoint_cache.keys()
            self._checkpoint_count += len(current) - len(existed)

    def load_checkpoint(
        self,
        *,
        scope_key: str,
        name: str,
    ) -> dict[str, Any] | None:
        if name != "confirmed_device_history_migration_v1":
            self._require_active_scope(scope_key)
        with self._lock:
            value = self._checkpoint_payload((scope_key, name))
            return None if value is None else dict(value)

    def _checkpoint_payload(self, key):
        value = self._checkpoint_cache.get(key)
        if value is _CHECKPOINT_UNLOADED:
            row = self._connection.execute('SELECT payload FROM checkpoint WHERE scope_key=? AND name=?', key).fetchone()
            if row is None:
                raise ValueError('history_checkpoint_catalog_entry_missing')
            value = _decode(row[0]); self._checkpoint_cache[key] = value
        return value

    def verify_integrity(self) -> None:
        """Explicit full-store diagnostic; never on the startup critical path."""
        with self._lock:
            if self._connection.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise sqlite3.DatabaseError('new_horizon_history_integrity_failed')

    def _require_active_scope(self, scope_key: str) -> None:
        if (scope_key, "confirmed_device_history_migration_v1") in self._checkpoint_cache:
            raise RuntimeError("new_horizon_history_scope_retired_use_physical_identity")

    def adopt_legacy_device_checkpoint(self, *, scope_key: str, name: str,
                                       legacy_keys: tuple[str, ...]) -> dict[str, Any] | None:
        """Named baseline/peak adapter. Callers supply only proven device aliases."""
        if name not in {"signal_baselines_v1", "passive_dl_peak_v2"}:
            raise ValueError("new_horizon_device_checkpoint_adapter_kind_invalid")
        previous = self.load_checkpoint(scope_key=scope_key, name=name)
        sources = [(key, self.load_checkpoint(scope_key=key, name=name))
                   for key in set(legacy_keys) - {scope_key}]
        sources = [(key, value) for key, value in sources if value is not None]
        if not sources:
            return previous
        values = ([previous] if previous is not None else []) + [value for _,value in sources]
        expected = "NewHorizonSignalBaselines.v1" if name == "signal_baselines_v1" else "NewHorizonPassiveDlPeak.v2"
        if any(value.get("schema") != expected for value in values):
            raise ValueError("new_horizon_device_checkpoint_adapter_schema_invalid")
        result = dict(values[0])
        if name == "signal_baselines_v1":
            if len({value.get("formula_generation") for value in values}) != 1:
                raise ValueError("new_horizon_device_baseline_formula_mismatch")
            for field, choose in (("rsrq_baseline_db", max), ("rtt_baseline_ms", min)):
                available = [value[field] for value in values if value.get(field) is not None]
                result[field] = choose(available) if available else None
            result["learned_at"] = max(value["learned_at"] for value in values)
        else:
            result["dl_mbps"] = max(value["dl_mbps"] for value in values)
        self.flush()
        with self._lock:
            self._backup_before_identity_migration()
            with self._connection:
                for key,_ in sources:
                    self._connection.execute("DELETE FROM checkpoint WHERE scope_key=? AND name=?", (key,name))
                self._connection.execute("INSERT INTO checkpoint(scope_key,name,updated_at,payload) VALUES(?,?,?,?) ON CONFLICT(scope_key,name) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload",
                                         (scope_key,name,time(),_encode(result)))
            for key,_ in sources:
                self._checkpoint_cache.pop((key,name), None)
            self._checkpoint_cache[(scope_key,name)] = result
            self._checkpoint_count = len(self._checkpoint_cache)
        return dict(result)

    def _backup_before_identity_migration(self) -> None:
        backup = self.path.with_name(self.path.stem + ".before_physical_identity.sqlite3")
        if not backup.exists():
            temporary = backup.with_name(backup.name + "." + uuid4().hex + ".tmp")
            try:
                destination = sqlite3.connect(temporary)
                try:
                    self._connection.backup(destination)
                    if destination.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise RuntimeError("new_horizon_identity_backup_integrity_failed")
                finally:
                    destination.close()
                temporary.replace(backup)
            finally:
                temporary.unlink(missing_ok=True)

    def prune(self, *, now: float) -> int:
        """Maintenance operation. Normal live intake does not call it."""

        self.flush()
        cutoff = float(now) - self.retention_seconds
        with self._disk_transaction():
            expired = self._connection.execute(
                "SELECT logical_count FROM evidence WHERE observed_at < ?",
                (cutoff,),
            ).fetchall()
            cursor = self._connection.execute(
                "DELETE FROM evidence WHERE observed_at < ?",
                (cutoff,),
            )
            self._connection.execute('DELETE FROM history_payload_bases WHERE last_observed_at < ?', (cutoff,))
            removed = max(0, cursor.rowcount)
            self._durable_row_count = max(0, self._durable_row_count - removed)
            removed_evidence = sum(int(row[0]) for row in expired)
            self._evidence_count = max(
                0,
                self._evidence_count - removed_evidence,
            )
            for scope_key, rows in tuple(self._observation_cache.items()):
                self._observation_cache[scope_key] = deque(
                    (row for row in rows if float(row["observed_at"]) >= cutoff),
                    maxlen=_OBSERVATION_CACHE_LIMIT,
                )
            expired_keys = {key for key, stamp in self._retired_evidence.items()
                            if stamp < cutoff}
            for key in expired_keys:
                del self._retired_evidence[key]
            for keys in self._checkpoint_retirements.values():
                keys.difference_update(expired_keys)
            for kind, rows in tuple(self._recent_kind_cache.items()):
                self._recent_kind_cache[kind] = deque(
                    (row for row in rows if float(row["observed_at"]) >= cutoff),
                    maxlen=16)
        self._log_processor.discard_hot_before(observed_at=cutoff, lane=CRITICAL_LANE)
        self._log_processor.discard_hot_before(
            observed_at=cutoff,
            lane=ROUTINE_LANE,
        )
        return removed

    def flush(self) -> None:
        self._log_processor.flush()

    def flush_for_support(self, *, cutoff: float, timeout_s: float) -> None:
        self._log_processor.flush_through(cutoff=cutoff, timeout_s=timeout_s)

    def close(self, *, timeout_s: float = 2.0) -> None:
        if self._closed:
            return
        self._log_processor.close(timeout_s=timeout_s)
        with self._lock:
            self._connection.close()
            self._closed = True

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "owner": "54guc.new_horizon.history",
                "startup": {"duration_s": self._startup_elapsed_s,
                    "validation": "targeted_catalog_and_requested_payloads",
                    "whole_database_integrity_checked": False,
                    "loaded_checkpoint_payloads": sum(v is not _CHECKPOINT_UNLOADED for v in self._checkpoint_cache.values())},
                "support_preparation": dict(prepared_rows=self._support_prepared_rows,
                    failures=self._support_preparation_failures),
                "evidence_count": self._evidence_count,
                "checkpoint_count": self._checkpoint_count,
                "durable_row_count": self._durable_row_count,
                "continuity_state": self._continuity_state,
                "opened_evidence_count": self._opened_evidence_count,
                "opened_durable_row_count": self._opened_durable_row_count,
                "opened_checkpoint_count": self._opened_checkpoint_count,
                "recent_observation_key_count": len(self._recent_observation_keys),
                "cached_observation_scope_count": len(self._observation_cache),
                "retention_days": self.retention_seconds / 86400.0,
                "recovered_corruption": self._recovered_corruption,
                "log_processor": self._log_processor.status(),
            }

    def provisional_device_key(self, scope: str) -> str:
        """No permanent ID: keep only this runtime's separate evidence."""
        return "unconfirmed-session:" + self._provisional_device_session + ":" + scope

    def checkpoints_named(self, name: str) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(dict(scope_key=scope, payload=dict(self._checkpoint_payload((scope,key))))
                         for (scope, key) in self._checkpoint_cache if key == name)

    def migrate_confirmed_device_scope(self, *, source: Mapping[str, Any], target: Mapping[str, Any],
                                       observed_at: float) -> None:
        """One durable transaction; never run an independent GUI/adviser import."""
        from arch4.new_horizon.continuity import continuity_digest
        from arch4.new_horizon.history_identity_migration import (
            MIGRATION_RECEIPT, MIGRATION_SCHEMA, plan_device_scope_migration,
        )
        old, new = continuity_digest(source), continuity_digest(target)
        pair = (old, new)
        if pair in self._identity_migration_checks:
            return
        receipt = self.load_checkpoint(scope_key=old, name=MIGRATION_RECEIPT)
        if receipt is not None:
            if receipt.get("schema") != MIGRATION_SCHEMA or receipt.get("target_scope") != new:
                raise ValueError("new_horizon_legacy_scope_physical_owner_conflict")
            self._identity_migration_checks.add(pair)
            return
        self.flush()
        with self._lock:
            rows = self._connection.execute(
                "SELECT event_key,kind,observed_at,payload FROM evidence WHERE scope_key=? AND kind NOT IN ('observation','observation_batch')",
                (old,)).fetchall()
            source_hours = self._checkpoint_payload((old, "referee_hourly_aq_v1"))
            factual = tuple(self._checkpoint_payload((key,name)) for (key,name) in self._checkpoint_cache
                            if key == old and name.startswith("band_evidence_summary_v"))
            if not rows and source_hours is None and not factual:
                self._identity_migration_checks.add(pair)
                return
            records = tuple(dict(event_key=k,scope_key=old,kind=kind,observed_at=ts,payload=_decode(raw, self._connection))
                            for k,kind,ts,raw in rows)
            plan = plan_device_scope_migration(source=source, target=target, records=records,
                source_hours=source_hours, target_hours=self._checkpoint_payload((new,"referee_hourly_aq_v1")),
                factual_checkpoints=factual)
            self._backup_before_identity_migration()
            receipt = dict(schema=MIGRATION_SCHEMA, source_scope=old, target_scope=new,
                physical_scope=target["history_device_key"], source_identity_code=source["identity_code"],
                scope_map=plan["scope_map"],
                record_count=len(rows), hour_count=len(plan["hours"]["hours"]), observed_at=observed_at)
            with self._connection:
                for row in plan["records"]:
                    self._connection.execute("UPDATE evidence SET scope_key=?,payload=? WHERE event_key=?",
                                             (new,_encode(row["payload"]),row["event_key"]))
                # Retain originals, but the receipt below hard-fails old active
                # reads. Only the named factual-summary adapter may read them.
                # Newly joined legacy rows must pass the existing formula adapter.
                self._connection.execute("DELETE FROM checkpoint WHERE scope_key=? AND name LIKE 'service_reward_import_recalculation_%'", (new,))
                self._connection.execute("DELETE FROM checkpoint WHERE scope_key=? AND name=?",
                    (new, "band_evidence_factual_continuity_to_v12_v1"))
                for key,name,payload in ((new,"referee_hourly_aq_v1",plan["hours"]), (old,MIGRATION_RECEIPT,receipt)):
                    self._connection.execute("INSERT INTO checkpoint(scope_key,name,updated_at,payload) VALUES(?,?,?,?) ON CONFLICT(scope_key,name) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload",
                                             (key,name,observed_at,_encode(payload)))
            self._checkpoint_cache = {(str(scope), str(name)): _CHECKPOINT_UNLOADED for scope,name in
                self._connection.execute("SELECT scope_key,name FROM checkpoint")}
            self._checkpoint_count = len(self._checkpoint_cache)
            self._recent_kind_cache.clear()
            self._warmed_recent_kinds.clear()
            # Everything in this lane was flushed before the transaction.
            # Old hot copies must not override the newly routed durable rows.
            self._log_processor.discard_hot_before(observed_at=float("inf"), lane=CRITICAL_LANE)
        self._identity_migration_checks.add(pair)


__all__ = (
    "capture_support_history_export",
    "read_support_history_export",
    "iter_support_history_export",
    "decode_support_history_export",
    "AdvisoryHistory",
    "discover_learning_context_histories",
    "LEARNING_CONTEXT_V68_RECEIPT_FILENAME",
    "migrate_learning_context_histories",
    "NEW_HORIZON_HISTORY_DIRNAME",
    "NEW_HORIZON_HISTORY_FILENAME",
    "NEW_HORIZON_HISTORY_PATH_ENV",
    "export_history_records",
    "migrate_learning_context_history",
    "portable_history_path",
    "resolve_history_path",
    "read_confirmed_device_associations",
)
