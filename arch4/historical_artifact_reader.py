"""Canonical streaming reader for retained runtime evidence.

This owner discovers active and archived output roots, understands JSONL and
JSONL.GZ, deduplicates copied sessions, applies time/scope gates, and exposes a
single retention inventory.  Consumers never infer history by private globs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
import zipfile

from arch4.privacy_scrub import scrub_record
from arch4.evidence_scope import validated_evidence_scope
from arch4.telemetry.forensic_codec import (
    COMPACT_FORENSIC_BASENAME,
    CompactForensicCodec,
)
from arch4.telemetry.forensic_codec import (
    COMPACT_FORENSIC_OWNER,
    COMPACT_FORENSIC_SCHEMA_VERSION,
)
from arch4.telemetry.production_contract import (
    PRODUCTION_TELEMETRY_BASENAME,
    PRODUCTION_TELEMETRY_SCHEMA_VERSION,
    flatten_production_telemetry_row,
)
from arch4.telemetry.trace_matrix import (
    TRACE_MATRIX_BASENAME,
    TRACE_MATRIX_DELTA_SCHEMA,
    TRACE_MATRIX_OWNER,
    TRACE_MATRIX_SCHEMA,
)
from arch4.telemetry.history_contract import telemetry_formula_vector


SCHEMA = "HistoricalArtifactReader.v1"
OWNER = "arch4.historical_artifact_reader"
DEFAULT_RETENTION_DAYS = 14
_OUTPUT_SESSION_SUFFIX = re.compile(
    r"^(?P<base>.+)_(?P<date>[0-9]{8})_(?P<clock>[0-9]{6})"
    r"(?:_(?P<collision>[0-9]{2}))?$"
)

FAMILY_PRODUCTION = "production"
FAMILY_TRACE = "trace"
FAMILY_COMPARE = "compare"
FAMILY_EVENT = "event"
FAMILY_FORENSIC = "forensic"
FAMILY_DECISION_CONTEXT = "decision_context"
ALL_FAMILIES = frozenset(
    {
        FAMILY_PRODUCTION,
        FAMILY_TRACE,
        FAMILY_COMPARE,
        FAMILY_EVENT,
        FAMILY_FORENSIC,
        FAMILY_DECISION_CONTEXT,
    }
)


@dataclass(frozen=True)
class HistoricalArtifact:
    path: Path
    root: Path
    family: str
    compressed: bool
    archived_root: bool
    mtime: float
    size_bytes: int


@dataclass(frozen=True)
class HistoricalRecord:
    payload: dict[str, Any]
    artifact: HistoricalArtifact
    line_number: int
    timestamp: float
    dedupe_key: str
    schema_disposition: str
    scrub_disposition: str
    scope: dict[str, str]


@dataclass(frozen=True)
class HistoricalRejection:
    path: Path
    family: str
    line_number: int
    reason: str


class HistoricalReadCancelled(RuntimeError):
    """The sole background history owner cancelled an isolated scan."""


@dataclass(frozen=True)
class HistoricalReadCooperation:
    """Optional raw-record pacing used only by isolated bulk history work."""

    yield_every_raw_records: int = 0
    yield_s: float = 0.0
    cancel_requested: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        if self.yield_every_raw_records < 0 or self.yield_s < 0.0:
            raise ValueError("historical read cooperation bounds are invalid")


def _family_for_name(name: str) -> str:
    logical = name[:-3] if name.lower().endswith(".gz") else name
    if not logical.lower().endswith(".jsonl"):
        return ""
    base = logical[:-6]
    if base.startswith(f"{PRODUCTION_TELEMETRY_BASENAME}_"):
        return FAMILY_PRODUCTION
    if base.startswith(f"{TRACE_MATRIX_BASENAME}_"):
        return FAMILY_TRACE
    if base.startswith("telemetry_history_"):
        return FAMILY_COMPARE
    if base.startswith("events_"):
        return FAMILY_EVENT
    if base.startswith(f"{COMPACT_FORENSIC_BASENAME}_"):
        return FAMILY_FORENSIC
    if base.startswith("decision_context_"):
        return FAMILY_DECISION_CONTEXT
    return ""


def _validated_families(families: Iterable[str]) -> set[str]:
    allowed = {str(family) for family in families}
    unknown = sorted(allowed - ALL_FAMILIES)
    if unknown:
        raise ValueError("unknown historical artifact families:" + ",".join(unknown))
    return allowed


def _record_timestamp(payload: Mapping[str, Any]) -> float:
    for key in ("ts", "timestamp", "sample_ts", "created_at_ts", "published_at_ts"):
        try:
            value = float(payload.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0.0:
            return value
    for key in ("iso", "timestamp_iso", "created_at"):
        value = str(payload.get(key, "") or "").strip()
        if not value:
            continue
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue
    return 0.0


def _scope_value(payload: Mapping[str, Any], key: str) -> str:
    aliases = {
        "identity_code": ("identity_code", "modem_identity_code"),
        "provider_plmn": ("provider_plmn", "plmn"),
        "session_scope_key": ("session_scope_key", "runtime_session_scope_key"),
        "source_epoch": ("source_epoch",),
        "catalog_version": ("catalog_version", "identity_catalog_version"),
        "catalog_checksum": ("catalog_checksum", "identity_catalog_checksum"),
        "access_type_id": ("access_type_id",),
        "profile_id": ("profile_id", "resolved_profile_id"),
    }
    containers: list[Mapping[str, Any]] = [payload]
    for container_key in (
        "identity",
        "runtime_identity",
        "compatibility_identity",
        "modem_identity",
        "scope",
        "provenance",
        "evidence_scope",
    ):
        value = payload.get(container_key)
        if isinstance(value, Mapping):
            containers.append(value)
    for container in containers:
        for alias in aliases.get(key, (key,)):
            value = container.get(alias)
            if value not in (None, ""):
                return str(value).strip()
    return ""


_SCOPE_KEYS = (
    "identity_code",
    "provider_plmn",
    "session_scope_key",
    "source_epoch",
    "catalog_version",
    "catalog_checksum",
    "access_type_id",
    "profile_id",
)


def _record_scope(payload: Mapping[str, Any], *, family: str) -> dict[str, str]:
    scope_payload = (
        flatten_production_telemetry_row(payload)
        if family == FAMILY_PRODUCTION
        else payload
    )
    scope: dict[str, str] = {}
    for key in _SCOPE_KEYS:
        value = _scope_value(scope_payload, key)
        if not value and scope_payload is not payload:
            value = _scope_value(payload, key)
        if value:
            scope[key] = value
    return scope


def _validated_family_record(
    family: str,
    payload: Mapping[str, Any],
) -> tuple[str, str]:
    """Return ``(schema_disposition, rejection_reason)`` for one row."""

    timestamp = _record_timestamp(payload)
    record_type = str(payload.get("record_type") or "").strip()
    if timestamp <= 0.0:
        return "", "missing_positive_timestamp"
    if family == FAMILY_PRODUCTION:
        if payload.get("schema_version") != PRODUCTION_TELEMETRY_SCHEMA_VERSION:
            return "", "production_schema_mismatch"
        if record_type not in {"tick", "action_regrade"}:
            return "", "production_record_type_mismatch"
        signals = payload.get("signals")
        if not isinstance(signals, Mapping) or not signals:
            return "", "production_signals_missing"
        if any(not isinstance(value, Mapping) for value in signals.values()):
            return "", "production_signal_shape_invalid"
        scope_error = _durable_scope_error(payload, record_type=record_type)
        if scope_error:
            return "", scope_error
        return PRODUCTION_TELEMETRY_SCHEMA_VERSION, ""
    if family == FAMILY_TRACE:
        if payload.get("schema") not in {TRACE_MATRIX_SCHEMA, TRACE_MATRIX_DELTA_SCHEMA}:
            return "", "trace_schema_mismatch"
        if payload.get("owner") != TRACE_MATRIX_OWNER:
            return "", "trace_owner_mismatch"
        if record_type not in {"tick", "action_regrade"}:
            return "", "trace_record_type_mismatch"
        if not isinstance(payload.get("dimensions"), Mapping):
            return "", "trace_dimensions_missing"
        scope_error = _durable_scope_error(payload, record_type=record_type)
        if scope_error:
            return "", scope_error
        return str(payload.get("schema")), ""
    if family == FAMILY_COMPARE:
        if record_type == "tick":
            scope_error = _durable_scope_error(payload, record_type=record_type)
            if scope_error:
                return "", scope_error
            return "telemetry_history_fact_row.v1", ""
        # vB5.3 and early ARCH4 history rows predate the explicit
        # record_type discriminator. Their stable envelope was timestamp,
        # ISO timestamp, and monotonic integer tick; accept that declared
        # legacy contract without treating arbitrary JSON objects as history.
        if record_type == "action_regrade" and all(
            key in payload for key in ("hour", "day_type", "primary_band")
        ):
            scope_error = _durable_scope_error(payload, record_type=record_type)
            if scope_error:
                return "", scope_error
            return "telemetry_history_action_regrade.v1", ""
        return "", "compare_record_contract_mismatch"
    if family == FAMILY_EVENT:
        kind = str(payload.get("kind") or "").strip()
        if payload.get("schema_version") in (None, "") and kind:
            return "arch_event_legacy.v1", ""
        if payload.get("schema_version") != "arch_event_v2":
            return "", "event_schema_mismatch"
        if not kind or str(payload.get("event") or "").strip() != kind:
            return "", "event_envelope_mismatch"
        for key in ("tick", "seq"):
            if not isinstance(payload.get(key), int):
                return "", f"event_{key}_invalid"
        return "arch_event_v2", ""
    if family == FAMILY_FORENSIC:
        compact_type = str(payload.get("t") or "").strip()
        if payload.get("v") != COMPACT_FORENSIC_SCHEMA_VERSION:
            return "", "forensic_schema_mismatch"
        if compact_type not in {"schema", "session", "tick", "event", "truncated"}:
            return "", "forensic_record_type_mismatch"
        if compact_type in {"schema", "truncated"} and payload.get("owner") != COMPACT_FORENSIC_OWNER:
            return "", "forensic_owner_mismatch"
        if compact_type == "tick" and not isinstance(payload.get("d"), Mapping):
            return "", "forensic_tick_delta_missing"
        return COMPACT_FORENSIC_SCHEMA_VERSION, ""
    if family == FAMILY_DECISION_CONTEXT:
        if record_type not in {"capture_meta", "trigger", "tick"}:
            return "", "decision_context_record_type_mismatch"
        if not str(payload.get("capture_id") or "").strip():
            return "", "decision_context_capture_id_missing"
        if record_type == "capture_meta" and not isinstance(payload.get("capture_reasons"), list):
            return "", "decision_context_meta_shape_invalid"
        return "decision_context_capture.v1", ""
    return "", "unsupported_family"


def _durable_scope_error(
    payload: Mapping[str, Any],
    *,
    record_type: str,
) -> str:
    try:
        scope = validated_evidence_scope(
            payload.get("evidence_scope")
            if isinstance(payload.get("evidence_scope"), Mapping)
            else {}
        )
    except (TypeError, ValueError) as exc:
        return f"durable_evidence_scope_invalid:{exc}"
    epochs = dict(scope.formula_vector.epochs)
    if record_type == "tick" and scope.formula_vector != telemetry_formula_vector():
        return "durable_telemetry_formula_vector_mismatch"
    if record_type == "action_regrade" and "crystal_ball" not in epochs:
        return "durable_regrade_formula_vector_mismatch"
    return ""


class HistoricalArtifactReader:
    """One streaming lifecycle owner for runtime historical artifacts."""

    def __init__(
        self,
        current_root: str | Path,
        *,
        extra_roots: Sequence[str | Path] = (),
        retention_days: int = DEFAULT_RETENTION_DAYS,
        cooperation: HistoricalReadCooperation | None = None,
    ) -> None:
        self.current_root = Path(current_root).expanduser().resolve()
        self.extra_roots = tuple(Path(root).expanduser().resolve() for root in extra_roots)
        self.retention_days = max(1, int(retention_days or DEFAULT_RETENTION_DAYS))
        self.cooperation = cooperation or HistoricalReadCooperation()
        self._status = {
            "schema": SCHEMA,
            "owner": OWNER,
            "artifacts_opened": 0,
            "records_read": 0,
            "records_yielded": 0,
            "malformed_records": 0,
            "schema_rejections": 0,
            "duplicate_records": 0,
            "scope_rejections": 0,
            "time_rejections": 0,
        }
        self._rejections: list[HistoricalRejection] = []

    def _cooperate(self, raw_record_number: int, *, force: bool = False) -> None:
        policy = self.cooperation
        if policy.cancel_requested is not None and policy.cancel_requested():
            raise HistoricalReadCancelled("historical read cancelled")
        every = int(policy.yield_every_raw_records or 0)
        if (
            policy.yield_s > 0.0
            and (force or (every > 0 and raw_record_number % every == 0))
        ):
            time.sleep(float(policy.yield_s))

    def _root_family_base(self) -> Path:
        match = _OUTPUT_SESSION_SUFFIX.fullmatch(self.current_root.name)
        if match is None:
            return self.current_root
        return self.current_root.with_name(match.group("base")).resolve()

    @staticmethod
    def _is_root_family_session(candidate: Path, base_root: Path) -> bool:
        match = _OUTPUT_SESSION_SUFFIX.fullmatch(candidate.name)
        return bool(
            match is not None
            and match.group("base") == base_root.name
            and candidate.parent == base_root.parent
        )

    def roots(self) -> tuple[Path, ...]:
        roots: list[Path] = []
        base_root = self._root_family_base()
        for candidate in (self.current_root, base_root):
            if candidate.is_dir() and candidate not in roots:
                roots.append(candidate)
        parent = base_root.parent
        if parent.is_dir():
            for candidate in sorted(parent.glob(f"{base_root.name}_*")):
                resolved = candidate.resolve()
                if (
                    candidate.is_dir()
                    and self._is_root_family_session(resolved, base_root)
                    and resolved not in roots
                ):
                    roots.append(resolved)
        for root in self.extra_roots:
            if root.is_dir() and root not in roots:
                roots.append(root)
        return tuple(roots)

    def discover(
        self,
        *,
        families: Iterable[str] = ALL_FAMILIES,
    ) -> tuple[HistoricalArtifact, ...]:
        allowed = _validated_families(families)
        artifacts: list[HistoricalArtifact] = []
        for root in self.roots():
            for path in sorted(root.iterdir()):
                if not path.is_file():
                    continue
                family = _family_for_name(path.name)
                if family not in allowed:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                artifacts.append(
                    HistoricalArtifact(
                        path=path,
                        root=root,
                        family=family,
                        compressed=path.name.lower().endswith(".jsonl.gz"),
                        archived_root=root != self.current_root,
                        mtime=float(stat.st_mtime),
                        size_bytes=int(stat.st_size),
                    )
                )
        artifacts.sort(key=lambda row: (row.mtime, str(row.path)))
        return tuple(artifacts)

    @staticmethod
    def family_for_name(name: str) -> str:
        """Classify one artifact name without creating a second filename owner."""

        return _family_for_name(Path(str(name)).name)

    @staticmethod
    def _open_text(artifact: HistoricalArtifact):
        if artifact.compressed:
            return gzip.open(artifact.path, "rt", encoding="utf-8", errors="replace")
        return artifact.path.open("r", encoding="utf-8", errors="replace")

    def _iter_text_records(
        self,
        artifact: HistoricalArtifact,
        lines: Iterable[str],
        *,
        seen: set[str],
        since_ts: float,
        until_ts: float,
        required: Mapping[str, str],
        scrub: bool,
    ) -> Iterator[HistoricalRecord]:
        for line_number, line in enumerate(lines, 1):
            self._cooperate(line_number)
            stripped = line.strip()
            if not stripped:
                continue
            self._status["records_read"] += 1
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError:
                self._status["malformed_records"] += 1
                continue
            if not isinstance(raw, dict):
                self._status["malformed_records"] += 1
                continue
            schema_disposition, rejection_reason = _validated_family_record(
                artifact.family,
                raw,
            )
            if rejection_reason:
                self._status["schema_rejections"] += 1
                if len(self._rejections) < 1000:
                    self._rejections.append(
                        HistoricalRejection(
                            path=artifact.path,
                            family=artifact.family,
                            line_number=line_number,
                            reason=rejection_reason,
                        )
                    )
                continue
            timestamp = _record_timestamp(raw)
            if since_ts > 0.0 and (timestamp <= 0.0 or timestamp < since_ts):
                self._status["time_rejections"] += 1
                continue
            if until_ts > 0.0 and (timestamp <= 0.0 or timestamp > until_ts):
                self._status["time_rejections"] += 1
                continue
            scope = _record_scope(raw, family=artifact.family)
            if required and any(
                scope.get(key, "") != expected
                for key, expected in required.items()
            ):
                self._status["scope_rejections"] += 1
                continue
            canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"))
            dedupe_key = hashlib.sha256(
                f"{artifact.family}|{canonical}".encode("utf-8")
            ).hexdigest()
            if dedupe_key in seen:
                self._status["duplicate_records"] += 1
                continue
            seen.add(dedupe_key)
            payload = scrub_record(raw) if scrub else dict(raw)
            self._status["records_yielded"] += 1
            yield HistoricalRecord(
                payload=payload,
                artifact=artifact,
                line_number=line_number,
                timestamp=timestamp,
                dedupe_key=dedupe_key,
                schema_disposition=schema_disposition,
                scrub_disposition=(
                    "privacy_scrubbed" if scrub else "raw_authorized"
                ),
                scope=scope,
            )

    @staticmethod
    def _required_scope(required_scope: Mapping[str, Any] | None) -> dict[str, str]:
        return {
            str(key): str(value).strip()
            for key, value in (required_scope or {}).items()
            if value not in (None, "")
        }

    def iter_paths(
        self,
        paths: Iterable[str | Path],
        *,
        families: Iterable[str] = ALL_FAMILIES,
        since_ts: float = 0.0,
        until_ts: float = 0.0,
        required_scope: Mapping[str, Any] | None = None,
        scrub: bool = False,
        dedupe_keys: set[str] | None = None,
    ) -> Iterator[HistoricalRecord]:
        """Stream explicitly named loose artifacts through the canonical gates."""

        allowed = _validated_families(families)
        required = self._required_scope(required_scope)
        seen = dedupe_keys if dedupe_keys is not None else set()
        artifacts: list[HistoricalArtifact] = []
        for raw_path in paths:
            path = Path(raw_path).expanduser().resolve()
            family = _family_for_name(path.name)
            if family not in allowed or not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            artifacts.append(
                HistoricalArtifact(
                    path=path,
                    root=path.parent,
                    family=family,
                    compressed=path.name.lower().endswith(".jsonl.gz"),
                    archived_root=path.parent != self.current_root,
                    mtime=float(stat.st_mtime),
                    size_bytes=int(stat.st_size),
                )
            )
        for artifact in sorted(artifacts, key=lambda row: (row.mtime, str(row.path))):
            self._status["artifacts_opened"] += 1
            try:
                handle = self._open_text(artifact)
            except OSError:
                continue
            with handle:
                yield from self._iter_text_records(
                    artifact,
                    handle,
                    seen=seen,
                    since_ts=since_ts,
                    until_ts=until_ts,
                    required=required,
                    scrub=scrub,
                )

    def iter_zip_records(
        self,
        zip_path: str | Path,
        *,
        families: Iterable[str] = ALL_FAMILIES,
        since_ts: float = 0.0,
        until_ts: float = 0.0,
        required_scope: Mapping[str, Any] | None = None,
        scrub: bool = False,
        dedupe_keys: set[str] | None = None,
    ) -> Iterator[HistoricalRecord]:
        """Stream JSONL and JSONL.GZ members through the same canonical gates."""

        allowed = _validated_families(families)
        required = self._required_scope(required_scope)
        seen = dedupe_keys if dedupe_keys is not None else set()
        archive = Path(zip_path).expanduser().resolve()
        try:
            bundle = zipfile.ZipFile(archive, "r")
        except (OSError, zipfile.BadZipFile):
            return
        with bundle:
            members = sorted(bundle.infolist(), key=lambda row: row.filename)
            for member in members:
                family = _family_for_name(Path(member.filename).name)
                if member.is_dir() or family not in allowed:
                    continue
                try:
                    mtime = datetime(*member.date_time).timestamp()
                except (TypeError, ValueError, OSError, OverflowError):
                    try:
                        mtime = float(archive.stat().st_mtime)
                    except OSError:
                        mtime = 0.0
                artifact = HistoricalArtifact(
                    path=Path(f"{archive}!/{member.filename}"),
                    root=archive,
                    family=family,
                    compressed=member.filename.lower().endswith(".jsonl.gz"),
                    archived_root=True,
                    mtime=mtime,
                    size_bytes=int(member.file_size),
                )
                self._status["artifacts_opened"] += 1
                try:
                    raw_handle = bundle.open(member, "r")
                except (KeyError, OSError, RuntimeError):
                    continue
                binary_handle = (
                    gzip.GzipFile(fileobj=raw_handle, mode="rb")
                    if artifact.compressed
                    else raw_handle
                )
                try:
                    text_handle = io.TextIOWrapper(
                        binary_handle,
                        encoding="utf-8",
                        errors="replace",
                    )
                    with text_handle:
                        yield from self._iter_text_records(
                            artifact,
                            text_handle,
                            seen=seen,
                            since_ts=since_ts,
                            until_ts=until_ts,
                            required=required,
                            scrub=scrub,
                        )
                except (OSError, EOFError, RuntimeError, zipfile.BadZipFile):
                    try:
                        raw_handle.close()
                    except OSError:
                        pass

    def iter_records(
        self,
        *,
        families: Iterable[str] = ALL_FAMILIES,
        since_ts: float = 0.0,
        until_ts: float = 0.0,
        required_scope: Mapping[str, Any] | None = None,
        scrub: bool = False,
        dedupe_keys: set[str] | None = None,
    ) -> Iterator[HistoricalRecord]:
        required = self._required_scope(required_scope)
        seen = dedupe_keys if dedupe_keys is not None else set()
        for artifact in self.discover(families=families):
            self._status["artifacts_opened"] += 1
            try:
                handle = self._open_text(artifact)
            except OSError:
                continue
            with handle:
                yield from self._iter_text_records(
                    artifact,
                    handle,
                    seen=seen,
                    since_ts=since_ts,
                    until_ts=until_ts,
                    required=required,
                    scrub=scrub,
                )

    def iter_decoded_forensic_records(
        self,
        paths: Iterable[str | Path] | None = None,
        *,
        since_ts: float = 0.0,
        until_ts: float = 0.0,
        required_scope: Mapping[str, Any] | None = None,
        scrub: bool = False,
    ) -> Iterator[HistoricalRecord]:
        """Decode each admitted compact part with independent codec state."""

        required = self._required_scope(required_scope)
        if paths is None:
            artifacts = tuple(
                row
                for row in self.discover(families=(FAMILY_FORENSIC,))
                if row.family == FAMILY_FORENSIC
            )
        else:
            explicit_artifacts: list[HistoricalArtifact] = []
            seen_paths: set[Path] = set()
            for raw_path in paths:
                path = Path(raw_path).expanduser().resolve()
                if (
                    path in seen_paths
                    or not path.is_file()
                    or _family_for_name(path.name) != FAMILY_FORENSIC
                ):
                    continue
                seen_paths.add(path)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                explicit_artifacts.append(
                    HistoricalArtifact(
                        path=path,
                        root=path.parent,
                        family=FAMILY_FORENSIC,
                        compressed=path.name.lower().endswith(".jsonl.gz"),
                        archived_root=path.parent != self.current_root,
                        mtime=float(stat.st_mtime),
                        size_bytes=int(stat.st_size),
                    )
                )
            artifacts = tuple(explicit_artifacts)
        seen: set[str] = set()
        for artifact in sorted(artifacts, key=lambda row: (row.mtime, str(row.path))):
            codec = CompactForensicCodec()
            self._status["artifacts_opened"] += 1
            try:
                handle = self._open_text(artifact)
            except OSError:
                continue
            with handle:
                for record in self._iter_text_records(
                    artifact,
                    handle,
                    seen=seen,
                    since_ts=since_ts,
                    until_ts=until_ts,
                    required={},
                    scrub=False,
                ):
                    decoded = codec.decode_record(record.payload)
                    scope = _record_scope(decoded, family=FAMILY_FORENSIC)
                    if required and any(
                        scope.get(key, "") != expected
                        for key, expected in required.items()
                    ):
                        self._status["scope_rejections"] += 1
                        continue
                    payload = scrub_record(decoded) if scrub else decoded
                    yield HistoricalRecord(
                        payload=payload,
                        artifact=record.artifact,
                        line_number=record.line_number,
                        timestamp=record.timestamp,
                        dedupe_key=record.dedupe_key,
                        schema_disposition=record.schema_disposition,
                        scrub_disposition=(
                            "privacy_scrubbed" if scrub else "raw_authorized"
                        ),
                        scope=scope,
                    )

    def retention_inventory(self, *, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        cutoff = current - float(self.retention_days * 86400)
        artifacts = self.discover()
        expired = [row for row in artifacts if row.mtime < cutoff]
        return {
            "schema": "HistoricalArtifactRetention.v1",
            "owner": OWNER,
            "retention_days": self.retention_days,
            "cutoff_ts": cutoff,
            "artifact_count": len(artifacts),
            "expired_count": len(expired),
            "expired_paths": [str(row.path) for row in expired],
        }

    def enforce_retention(self, *, now: float | None = None) -> dict[str, Any]:
        inventory = self.retention_inventory(now=now)
        removed: list[str] = []
        for raw_path in inventory["expired_paths"]:
            path = Path(raw_path)
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                continue
        base_root = self._root_family_base()
        for root in self.roots():
            if (
                root in {self.current_root, base_root}
                or not self._is_root_family_session(root, base_root)
            ):
                continue
            try:
                if not any(root.iterdir()):
                    shutil.rmtree(root)
            except OSError:
                continue
        inventory["removed_count"] = len(removed)
        inventory["removed_paths"] = removed
        return inventory

    def status(self) -> dict[str, Any]:
        status = dict(self._status)
        status["roots"] = [str(root) for root in self.roots()]
        return status

    def rejections(self) -> tuple[HistoricalRejection, ...]:
        return tuple(self._rejections)


__all__ = [
    "ALL_FAMILIES",
    "DEFAULT_RETENTION_DAYS",
    "FAMILY_COMPARE",
    "FAMILY_DECISION_CONTEXT",
    "FAMILY_EVENT",
    "FAMILY_FORENSIC",
    "FAMILY_PRODUCTION",
    "FAMILY_TRACE",
    "HistoricalArtifact",
    "HistoricalArtifactReader",
    "HistoricalRejection",
    "HistoricalRecord",
    "OWNER",
    "SCHEMA",
]
