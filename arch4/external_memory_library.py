from __future__ import annotations

import hashlib
import json
import time
import zipfile
from argparse import ArgumentParser
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from arch4.atomic_io import atomic_write_text
from arch4.evidence_scope import validated_evidence_scope
from arch4.historical_artifact_reader import (
    FAMILY_COMPARE,
    FAMILY_EVENT,
    FAMILY_PRODUCTION,
    HistoricalArtifactReader,
    HistoricalReadCooperation,
)
from arch4.telemetry.production_contract import flatten_production_telemetry_row
from arch4.telemetry.history_contract import telemetry_formula_vector
from arch4.modem_live.compatibility_identity import DecodeCatalogSnapshot

SCHEMA = "external_memory_library_v1"
MANIFEST_NAME = "external_memory_manifest.json"
DEFAULT_LIBRARY_DIR_NAME = "_external_memory_library"
_RUNTIME_DIR = "portable_runtime"
_OUTPUT_DIR = "guc_output"
_MEMORY_DIR = "guc_memory"
_PRIVATE_IDENTITY_KEYS = {
    "imei",
    "imsi",
    "serial",
    "lan_mac",
    "mac",
    "wan_ip",
    "ip",
    "public_ip",
    "iccid",
    "sim_id",
}


def _safe_json_loads(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _json_from_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return _safe_json_loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}


def _json_from_zip(zf: zipfile.ZipFile, name: str) -> dict[str, Any]:
    try:
        return _safe_json_loads(zf.read(name).decode("utf-8", errors="ignore"))
    except Exception:
        return {}


def _first_json(zf: zipfile.ZipFile, names: Iterable[str]) -> dict[str, Any]:
    available = set(zf.namelist())
    for name in names:
        if name in available:
            data = _json_from_zip(zf, name)
            if data:
                return data
    return {}


def _scrub_identity(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        k = str(key or "").strip()
        if not k or k.lower() in _PRIVATE_IDENTITY_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[k] = value
    return out


def _identity_from_payloads(*payloads: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for container_key in (
            "runtime_identity",
            "identity",
            "resolved_identity_card",
            "modem_identity",
            "evidence_scope",
        ):
            inner = payload.get(container_key)
            if isinstance(inner, dict):
                merged.update(_scrub_identity(inner))
        portable_library = payload.get("portable_state_library")
        if isinstance(portable_library, dict):
            library_identity = portable_library.get("identity")
            if isinstance(library_identity, dict):
                merged.update(_scrub_identity(library_identity))
        for key in (
            "backend",
            "backend_family",
            "modem_backend",
            "model",
            "firmware",
            "software_version",
            "provider_name",
            "provider_plmn",
            "profile_id",
            "resolved_profile_id",
            "selected_band_set_signature",
            "active_band_set_signature",
            "ca_signature",
            "serving_band",
            "primary_band",
            "pci",
            "earfcn",
            "cell_id",
            "identity_code",
            "identity_status",
            "identity_catalog_version",
            "identity_catalog_checksum",
            "access_type_id",
            "session_scope_key",
            "source_epoch",
            "source_sequence",
        ):
            if key in payload and payload.get(key) not in ("", None):
                merged[key] = payload.get(key)
    if "backend" not in merged and "modem_backend" in merged:
        merged["backend"] = merged.get("modem_backend")
    return _scrub_identity(merged)


def _decoded_catalogs_from_payloads(*payloads: dict[str, Any]) -> list[dict[str, Any]]:
    catalogs: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        candidates: list[Any] = [payload.get("decode_catalog_snapshot")]
        candidates.extend(payload.get("decoded_identity_catalogs") or [])
        for key in ("runtime_identity", "identity", "modem_identity"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                candidates.append(nested.get("decode_catalog_snapshot"))
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            try:
                catalog = DecodeCatalogSnapshot.from_payload(candidate).as_dict()
            except (TypeError, ValueError):
                continue
            catalogs[str(catalog["catalog_checksum"])] = catalog
    return list(catalogs.values())


def _version_from_payloads(*payloads: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in ("version", "release_tag", "release_channel", "build_date", "commit", "source_commit"):
            value = payload.get(key)
            if value not in ("", None):
                out[key] = value
        provenance = payload.get("provenance")
        if isinstance(provenance, dict):
            for key in ("version", "release_channel", "build_date", "commit"):
                value = provenance.get(key)
                if value not in ("", None):
                    out.setdefault(key, value)
    return out


def _summarize_jsonl_records(
    records: Iterable[dict[str, Any]],
    *,
    require_evidence_scope: bool = False,
) -> dict[str, Any]:
    count = 0
    first_iso = ""
    last_iso = ""
    first_ts: float | None = None
    last_ts: float | None = None
    identity: dict[str, Any] = {}
    evidence_scope_groups: dict[str, dict[str, Any]] = {}
    evidence_quarantined_rows = 0
    decoded_catalogs: dict[str, dict[str, Any]] = {}
    for _scanned_rows, row in enumerate(records, 1):
        if not isinstance(row, dict):
            continue
        if require_evidence_scope:
            try:
                evidence_scope = validated_evidence_scope(
                    row.get("evidence_scope")
                    if isinstance(row.get("evidence_scope"), dict)
                    else {}
                )
                record_type = str(row.get("record_type", "tick") or "tick")
                epochs = dict(evidence_scope.formula_vector.epochs)
                if (
                    record_type == "tick"
                    and evidence_scope.formula_vector != telemetry_formula_vector()
                ) or (
                    record_type == "action_regrade"
                    and "crystal_ball" not in epochs
                ):
                    raise ValueError("external_memory_formula_vector_mismatch")
            except (TypeError, ValueError):
                evidence_quarantined_rows += 1
                continue
            group_key = _evidence_scope_group_key(evidence_scope)
            group = evidence_scope_groups.setdefault(
                group_key,
                {
                    "representative_scope": evidence_scope.as_dict(),
                    "source_sequence_start": evidence_scope.source_sequence,
                    "source_sequence_end": evidence_scope.source_sequence,
                    "observed_at_start": evidence_scope.observed_at,
                    "observed_at_end": evidence_scope.observed_at,
                    "admitted_rows": 0,
                },
            )
            group["representative_scope"] = evidence_scope.as_dict()
            group["source_sequence_start"] = min(
                int(group["source_sequence_start"]), evidence_scope.source_sequence
            )
            group["source_sequence_end"] = max(
                int(group["source_sequence_end"]), evidence_scope.source_sequence
            )
            group["observed_at_start"] = min(
                float(group["observed_at_start"]), evidence_scope.observed_at
            )
            group["observed_at_end"] = max(
                float(group["observed_at_end"]), evidence_scope.observed_at
            )
            group["admitted_rows"] = int(group["admitted_rows"]) + 1
        count += 1
        row_identity = _identity_from_payloads(row)
        if row_identity:
            identity.update(row_identity)
        for catalog in _decoded_catalogs_from_payloads(row):
            decoded_catalogs[str(catalog["catalog_checksum"])] = catalog
        iso = str(row.get("iso") or "")
        try:
            ts = float(row.get("ts"))
        except Exception:
            ts = None
        if count == 1:
            first_iso = iso
            first_ts = ts
        if iso:
            last_iso = iso
        if ts is not None:
            last_ts = ts
    scope_spans = [_evidence_scope_span(row) for row in evidence_scope_groups.values()]
    evidence_scopes = [dict(row["representative_scope"]) for row in scope_spans]
    evidence_scope_counts = {
        str(row["representative_scope"]["evidence_scope_id"]): int(
            row["admitted_rows"]
        )
        for row in scope_spans
    }
    return {
        "rows": count,
        "start_iso": first_iso,
        "end_iso": last_iso,
        "start_ts": first_ts,
        "end_ts": last_ts,
        "identity": _scrub_identity(identity),
        "evidence_scopes": evidence_scopes,
        "evidence_scope_counts": dict(evidence_scope_counts),
        "evidence_scope_spans": scope_spans,
        "evidence_quarantined_rows": int(evidence_quarantined_rows),
        "decoded_identity_catalogs": list(decoded_catalogs.values()),
    }


def _evidence_scope_group_key(scope: Any) -> str:
    row = scope.as_dict()
    for key in (
        "source_sequence",
        "observed_at",
        "evidence_age_s",
        "confidence",
        "evidence_count",
        "evidence_scope_id",
    ):
        row.pop(key, None)
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _evidence_scope_span(raw: dict[str, Any]) -> dict[str, Any]:
    content = {
        "schema": "ExternalEvidenceScopeSpan.v1",
        "owner": "arch4.external_memory_library",
        "representative_scope": dict(raw["representative_scope"]),
        "source_sequence_start": int(raw["source_sequence_start"]),
        "source_sequence_end": int(raw["source_sequence_end"]),
        "observed_at_start": float(raw["observed_at_start"]),
        "observed_at_end": float(raw["observed_at_end"]),
        "admitted_rows": int(raw["admitted_rows"]),
    }
    return {
        **content,
        "span_id": hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest(),
    }


def _combine_coverage(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sum(int(item.get("rows") or 0) for item in summaries)
    starts = [item for item in summaries if item.get("start_iso") or item.get("start_ts") is not None]
    ends = [item for item in summaries if item.get("end_iso") or item.get("end_ts") is not None]
    start = starts[0] if starts else {}
    end = ends[-1] if ends else {}
    scope_by_id: dict[str, dict[str, Any]] = {}
    scope_counts: dict[str, int] = {}
    quarantined_rows = 0
    decoded_catalogs: dict[str, dict[str, Any]] = {}
    scope_spans: list[dict[str, Any]] = []
    for item in summaries:
        quarantined_rows += int(item.get("evidence_quarantined_rows", 0) or 0)
        for raw_scope in item.get("evidence_scopes", []) or []:
            if not isinstance(raw_scope, dict):
                continue
            scope_id = str(raw_scope.get("evidence_scope_id", "") or "")
            if scope_id:
                scope_by_id[scope_id] = dict(raw_scope)
        for catalog in item.get("decoded_identity_catalogs", []) or []:
            if isinstance(catalog, dict) and catalog.get("catalog_checksum"):
                decoded_catalogs[str(catalog["catalog_checksum"])] = dict(catalog)
        scope_spans.extend(
            dict(row)
            for row in item.get("evidence_scope_spans", []) or []
            if isinstance(row, dict)
        )
        for scope_id, count in dict(item.get("evidence_scope_counts") or {}).items():
            if str(scope_id or ""):
                scope_counts[str(scope_id)] = int(scope_counts.get(str(scope_id), 0)) + int(
                    count or 0
                )
    return {
        "rows": rows,
        "start_iso": str(start.get("start_iso") or ""),
        "end_iso": str(end.get("end_iso") or ""),
        "start_ts": start.get("start_ts"),
        "end_ts": end.get("end_ts"),
        "evidence_scopes": list(scope_by_id.values()),
        "evidence_scope_counts": scope_counts,
        "evidence_scope_spans": scope_spans,
        "evidence_quarantined_rows": quarantined_rows,
        "decoded_identity_catalogs": list(decoded_catalogs.values()),
    }


def _runtime_root_from_source_dir(path: Path) -> Path:
    if (path.name == _OUTPUT_DIR or path.name.startswith(f"{_OUTPUT_DIR}_")) and path.parent.name == _RUNTIME_DIR:
        return path.parent
    if path.name == _MEMORY_DIR and path.parent.name == _RUNTIME_DIR:
        return path.parent
    if path.name == _RUNTIME_DIR:
        return path
    return path / _RUNTIME_DIR


def _output_dirs_for_runtime_root(runtime_root: Path, *, preferred: Path | None = None) -> list[Path]:
    """Return active and rotated output dirs in deterministic oldest-to-newest order."""
    dirs: list[Path] = []
    if preferred is not None and preferred.is_dir():
        dirs.append(preferred)
    active = runtime_root / _OUTPUT_DIR
    if active.is_dir() and active not in dirs:
        dirs.append(active)
    for item in sorted(runtime_root.glob(f"{_OUTPUT_DIR}_*")):
        if item.is_dir() and item not in dirs:
            dirs.append(item)
    return dirs


def _json_payloads_from_output_dirs(output_dirs: list[Path], filename: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for output_dir in output_dirs:
        payload = _json_from_file(output_dir / filename)
        if payload:
            payloads.append(payload)
    return payloads


def _source_file_name(path: Path, *, output_dir_count: int) -> str:
    if output_dir_count <= 1:
        return path.name
    return f"{path.parent.name}/{path.name}"


def _scan_directory_source(
    path: Path,
    *,
    since_ts: float = 0.0,
    reader_cooperation: HistoricalReadCooperation | None = None,
    telemetry_families: tuple[str, ...] = (FAMILY_PRODUCTION, FAMILY_COMPARE),
    dedupe_keys: set[str] | None = None,
) -> dict[str, Any]:
    runtime_root = _runtime_root_from_source_dir(path)
    if runtime_root.is_dir():
        if path.parent == runtime_root and (path.name == _OUTPUT_DIR or path.name.startswith(f"{_OUTPUT_DIR}_")):
            output_dirs = [path]
        else:
            output_dirs = _output_dirs_for_runtime_root(runtime_root)
        memory_dir = runtime_root / _MEMORY_DIR
        stage_dir = runtime_root.parent
    else:
        output_dirs = [path]
        memory_dir = path.parent / _MEMORY_DIR
        stage_dir = path

    current_states = _json_payloads_from_output_dirs(output_dirs, "current_state.json")
    capabilities_payloads = _json_payloads_from_output_dirs(output_dirs, "capabilities.json")
    version = _json_from_file(stage_dir / "version_manifest.json")
    current_output = runtime_root / _OUTPUT_DIR if runtime_root.is_dir() else output_dirs[0]
    historical_reader = HistoricalArtifactReader(
        current_output,
        extra_roots=tuple(output_dirs),
        cooperation=reader_cooperation,
    )
    telemetry_artifacts = historical_reader.discover(
        families=telemetry_families,
    )
    event_artifacts = historical_reader.discover(families=(FAMILY_EVENT,))
    telemetry_files = [artifact.path for artifact in telemetry_artifacts]
    event_files = [artifact.path for artifact in event_artifacts]
    telemetry_root_count = len({artifact.root for artifact in telemetry_artifacts})
    event_root_count = len({artifact.root for artifact in event_artifacts})
    telemetry_summaries = [
        _summarize_jsonl_records(
            (
                flatten_production_telemetry_row(record.payload)
                if record.artifact.family == FAMILY_PRODUCTION
                else record.payload
                for record in historical_reader.iter_records(
                    families=telemetry_families,
                    since_ts=since_ts,
                    dedupe_keys=dedupe_keys,
                )
            ),
            require_evidence_scope=True,
        )
    ]
    reader_status = historical_reader.status()
    telemetry_reader_rejections = int(
        reader_status.get("schema_rejections", 0) or 0
    ) + int(reader_status.get("malformed_records", 0) or 0)
    telemetry_summaries[0]["evidence_quarantined_rows"] = int(
        telemetry_summaries[0].get("evidence_quarantined_rows", 0) or 0
    ) + telemetry_reader_rejections
    event_summaries = [
        _summarize_jsonl_records(
            record.payload
            for record in historical_reader.iter_records(
                families=(FAMILY_EVENT,),
                since_ts=since_ts,
                dedupe_keys=dedupe_keys,
            )
        )
    ]
    memory_files = [item for item in memory_dir.glob("*") if item.is_file()] if memory_dir.is_dir() else []
    identity_payloads = [version, *current_states, *capabilities_payloads, *telemetry_summaries]
    return _source_record(
        path=path,
        source_type="portable_runtime" if runtime_root.is_dir() else "log_directory",
        identity=_identity_from_payloads(*identity_payloads),
        version=_version_from_payloads(*identity_payloads),
        telemetry_files=[
            _source_file_name(item, output_dir_count=telemetry_root_count)
            for item in telemetry_files
        ],
        telemetry_summary=_combine_coverage(telemetry_summaries),
        event_files=[
            _source_file_name(item, output_dir_count=event_root_count)
            for item in event_files
        ],
        event_summary=_combine_coverage(event_summaries),
        memory_files=[item.name for item in memory_files],
        decoded_identity_catalogs=_decoded_catalogs_from_payloads(*identity_payloads),
    )


def _scan_zip_source(
    path: Path,
    *,
    since_ts: float = 0.0,
    confirmed_handoff_receipt: Mapping[str, Any] | None = None,
    reader_cooperation: HistoricalReadCooperation | None = None,
    telemetry_families: tuple[str, ...] = (FAMILY_PRODUCTION, FAMILY_COMPARE),
    dedupe_keys: set[str] | None = None,
) -> dict[str, Any]:
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        output_payload_names = [
            name for name in sorted(names)
            if f"{_RUNTIME_DIR}/{_OUTPUT_DIR}" in name
        ]
        current_states = [_json_from_zip(zf, name) for name in output_payload_names if Path(name).name == "current_state.json"]
        capabilities_payloads = [_json_from_zip(zf, name) for name in output_payload_names if Path(name).name == "capabilities.json"]
        if not current_states:
            current_states = [_first_json(zf, ["current_state.json"])]
        if not capabilities_payloads:
            capabilities_payloads = [_first_json(zf, ["capabilities.json"])]
        bundle_manifest = _first_json(zf, ["bundle_manifest.json"])
        handoff_manifest = _first_json(zf, ["handoff_manifest.json"])
        version_manifest = _first_json(zf, ["version_manifest.json"])
        telemetry_reader = HistoricalArtifactReader(
            path.parent,
            cooperation=reader_cooperation,
        )
        telemetry_names: set[str] = set()

        def telemetry_records() -> Iterable[dict[str, Any]]:
            for record in telemetry_reader.iter_zip_records(
                path,
                families=telemetry_families,
                since_ts=since_ts,
                dedupe_keys=dedupe_keys,
            ):
                telemetry_names.add(record.artifact.path.name)
                yield (
                    flatten_production_telemetry_row(record.payload)
                    if record.artifact.family == FAMILY_PRODUCTION
                    else record.payload
                )

        telemetry_summary = _summarize_jsonl_records(
            telemetry_records(),
            require_evidence_scope=True,
        )
        telemetry_status = telemetry_reader.status()
        telemetry_summary["evidence_quarantined_rows"] = int(
            telemetry_summary.get("evidence_quarantined_rows", 0) or 0
        ) + int(telemetry_status.get("schema_rejections", 0) or 0) + int(
            telemetry_status.get("malformed_records", 0) or 0
        )

        event_reader = HistoricalArtifactReader(
            path.parent,
            cooperation=reader_cooperation,
        )
        event_names: set[str] = set()

        def event_records() -> Iterable[dict[str, Any]]:
            for record in event_reader.iter_zip_records(
                path,
                families=(FAMILY_EVENT,),
                since_ts=since_ts,
                dedupe_keys=dedupe_keys,
            ):
                event_names.add(record.artifact.path.name)
                yield record.payload

        event_summary = _summarize_jsonl_records(event_records())
        event_status = event_reader.status()
        event_summary["evidence_quarantined_rows"] = int(
            event_status.get("schema_rejections", 0) or 0
        ) + int(event_status.get("malformed_records", 0) or 0)
        memory_files = sorted(
            Path(name).name
            for name in names
            if f"/{_MEMORY_DIR}/" in name and not name.endswith("/")
        )
        source_type = (
            "next_start_handoff"
            if handoff_manifest.get("schema") == "portable_next_start_handoff_v1"
            else "support_bundle"
            if bundle_manifest
            else "zip_runtime_archive"
        )
        identity_payloads = [
            version_manifest,
            bundle_manifest,
            handoff_manifest,
            *[item for item in current_states if item],
            *[item for item in capabilities_payloads if item],
            telemetry_summary,
        ]
        record = _source_record(
            path=path,
            source_type=source_type,
            identity=_identity_from_payloads(*identity_payloads),
            version=_version_from_payloads(*identity_payloads),
            telemetry_files=sorted(telemetry_names),
            telemetry_summary=_combine_coverage([telemetry_summary]),
            event_files=sorted(event_names),
            event_summary=_combine_coverage([event_summary]),
            memory_files=memory_files,
            decoded_identity_catalogs=_decoded_catalogs_from_payloads(*identity_payloads),
        )
        if source_type == "next_start_handoff":
            record["handoff_manifest"] = {
                "schema": str(handoff_manifest.get("schema") or ""),
                "created_at_ts": float(
                    handoff_manifest.get("created_at_ts") or 0.0
                ),
                "predictive_history_days": int(
                    handoff_manifest.get("predictive_history_days") or 0
                ),
                "memory_file_count": len(
                    handoff_manifest.get("memory_files") or []
                ),
                "history_file_count": len(
                    handoff_manifest.get("history_files") or []
                ),
            }
            if confirmed_handoff_receipt:
                record["confirmed_handoff_receipt"] = dict(
                    confirmed_handoff_receipt
                )
        return record


def _scan_loose_file(
    path: Path,
    *,
    since_ts: float = 0.0,
    reader_cooperation: HistoricalReadCooperation | None = None,
    telemetry_families: tuple[str, ...] = (FAMILY_PRODUCTION, FAMILY_COMPARE),
    dedupe_keys: set[str] | None = None,
) -> dict[str, Any]:
    telemetry_summary = {"rows": 0, "start_iso": "", "end_iso": "", "start_ts": None, "end_ts": None}
    event_summary = {"rows": 0, "start_iso": "", "end_iso": "", "start_ts": None, "end_ts": None}
    telemetry_files: list[str] = []
    event_files: list[str] = []
    family = HistoricalArtifactReader.family_for_name(path.name)
    reader = HistoricalArtifactReader(path.parent, cooperation=reader_cooperation)
    if family in set(telemetry_families):
        telemetry_files = [path.name]
        telemetry_summary = _summarize_jsonl_records(
            (
                flatten_production_telemetry_row(record.payload)
                if record.artifact.family == FAMILY_PRODUCTION
                else record.payload
                for record in reader.iter_paths(
                    (path,),
                    families=telemetry_families,
                    since_ts=since_ts,
                    dedupe_keys=dedupe_keys,
                )
            ),
            require_evidence_scope=True,
        )
        reader_status = reader.status()
        telemetry_summary["evidence_quarantined_rows"] = int(
            telemetry_summary.get("evidence_quarantined_rows", 0) or 0
        ) + int(reader_status.get("schema_rejections", 0) or 0) + int(
            reader_status.get("malformed_records", 0) or 0
        )
    elif family == FAMILY_EVENT:
        event_files = [path.name]
        event_summary = _summarize_jsonl_records(
            record.payload
            for record in reader.iter_paths(
                (path,),
                families=(FAMILY_EVENT,),
                since_ts=since_ts,
                dedupe_keys=dedupe_keys,
            )
        )
        reader_status = reader.status()
        event_summary["evidence_quarantined_rows"] = int(
            reader_status.get("schema_rejections", 0) or 0
        ) + int(reader_status.get("malformed_records", 0) or 0)
    return _source_record(
        path=path,
        source_type="telemetry_file" if telemetry_files else "event_file" if event_files else "file",
        identity=_identity_from_payloads(telemetry_summary, event_summary),
        version={},
        telemetry_files=telemetry_files,
        telemetry_summary=telemetry_summary,
        event_files=event_files,
        event_summary=event_summary,
        memory_files=[],
        decoded_identity_catalogs=list(
            telemetry_summary.get("decoded_identity_catalogs") or []
        ),
    )


def _source_record(
    *,
    path: Path,
    source_type: str,
    identity: dict[str, Any],
    version: dict[str, Any],
    telemetry_files: list[str],
    telemetry_summary: dict[str, Any],
    event_files: list[str],
    event_summary: dict[str, Any],
    memory_files: list[str],
    decoded_identity_catalogs: list[dict[str, Any]],
) -> dict[str, Any]:
    has_identity = bool(identity.get("model") or identity.get("provider_plmn") or identity.get("backend"))
    memory_set = set(memory_files)
    limitations: list[str] = []
    if not telemetry_files:
        limitations.append("no production or compare telemetry jsonl files found")
    if telemetry_files and not telemetry_summary.get("evidence_scopes"):
        limitations.append("telemetry has no admitted exact EvidenceScope rows")
    if not has_identity:
        limitations.append("identity unavailable or incomplete")
    return {
        "schema": "external_memory_source_v1",
        "source_path": str(path.resolve()),
        "source_name": path.name,
        "source_type": source_type,
        "import_status": "candidate_prior",
        "runtime_influence": "none",
        "authority": "offline_evidence_only",
        "confidence": "medium" if has_identity and telemetry_files else "low",
        "identity": identity,
        "version": version,
        "decoded_identity_catalogs": [
            dict(row) for row in decoded_identity_catalogs if isinstance(row, dict)
        ],
        "coverage": {
            "telemetry_files": len(telemetry_files),
            "telemetry_file_names": telemetry_files,
            "telemetry_rows": int(telemetry_summary.get("rows") or 0),
            "telemetry_start_iso": str(telemetry_summary.get("start_iso") or ""),
            "telemetry_end_iso": str(telemetry_summary.get("end_iso") or ""),
            "evidence_scopes": list(telemetry_summary.get("evidence_scopes") or []),
            "evidence_scope_counts": dict(
                telemetry_summary.get("evidence_scope_counts") or {}
            ),
            "evidence_scope_spans": list(
                telemetry_summary.get("evidence_scope_spans") or []
            ),
            "evidence_quarantined_rows": int(
                telemetry_summary.get("evidence_quarantined_rows") or 0
            ),
            "event_files": len(event_files),
            "event_file_names": event_files,
            "event_rows": int(event_summary.get("rows") or 0),
            "event_start_iso": str(event_summary.get("start_iso") or ""),
            "event_end_iso": str(event_summary.get("end_iso") or ""),
        },
        "assets": {
            "memory_files": len(memory_files),
            "has_memory_json": "memory.json" in memory_set,
            "has_ltm_snapshot": "ltm_snapshot.json" in memory_set,
            "has_historical_json": "historical.json" in memory_set,
            "has_calibration_db": "calibration.db" in memory_set,
            "has_calibration_authority": "calibration_authority.json" in memory_set,
        },
        "usable_for": [
            "offline_log_analysis",
            "prior_candidate_generation",
            "calibration_evidence_candidate",
        ],
        "limitations": limitations,
    }


def scan_external_memory_source(
    source_path: str | Path,
    *,
    since_ts: float = 0.0,
    confirmed_handoff_receipt: Mapping[str, Any] | None = None,
    reader_cooperation: HistoricalReadCooperation | None = None,
    telemetry_families: tuple[str, ...] = (FAMILY_PRODUCTION, FAMILY_COMPARE),
    dedupe_keys: set[str] | None = None,
) -> dict[str, Any]:
    path = Path(source_path).resolve()
    if path.is_dir():
        return _scan_directory_source(
            path,
            since_ts=since_ts,
            reader_cooperation=reader_cooperation,
            telemetry_families=telemetry_families,
            dedupe_keys=dedupe_keys,
        )
    if path.is_file() and path.suffix.lower() == ".zip":
        return _scan_zip_source(
            path,
            since_ts=since_ts,
            confirmed_handoff_receipt=confirmed_handoff_receipt,
            reader_cooperation=reader_cooperation,
            telemetry_families=telemetry_families,
            dedupe_keys=dedupe_keys,
        )
    if path.is_file():
        return _scan_loose_file(
            path,
            since_ts=since_ts,
            reader_cooperation=reader_cooperation,
            telemetry_families=telemetry_families,
            dedupe_keys=dedupe_keys,
        )
    return _source_record(
        path=path,
        source_type="missing",
        identity={},
        version={},
        telemetry_files=[],
        telemetry_summary={"rows": 0},
        event_files=[],
        event_summary={"rows": 0},
        memory_files=[],
        decoded_identity_catalogs=[],
    )


def build_external_memory_manifest(
    source_paths: Iterable[str | Path],
    *,
    since_ts: float = 0.0,
    progress_callback: Callable[[float, str], None] | None = None,
    confirmed_handoff_receipts: Mapping[str, Mapping[str, Any]] | None = None,
    reader_cooperation: HistoricalReadCooperation | None = None,
    telemetry_families: tuple[str, ...] = (FAMILY_PRODUCTION, FAMILY_COMPARE),
) -> dict[str, Any]:
    paths = tuple(source_paths)
    sources: list[dict[str, Any]] = []
    receipts = dict(confirmed_handoff_receipts or {})
    dedupe_keys: set[str] = set()
    for index, path in enumerate(paths):
        resolved = Path(path).expanduser().resolve()
        sources.append(
            scan_external_memory_source(
                resolved,
                since_ts=since_ts,
                confirmed_handoff_receipt=receipts.get(str(resolved)),
                reader_cooperation=reader_cooperation,
                telemetry_families=telemetry_families,
                dedupe_keys=dedupe_keys,
            )
        )
        if progress_callback is not None:
            progress_callback(
                100.0 * float(index + 1) / float(max(1, len(paths))),
                f"scanned source {index + 1}/{len(paths)}",
            )
    telemetry_rows = sum(int(src.get("coverage", {}).get("telemetry_rows") or 0) for src in sources)
    event_rows = sum(int(src.get("coverage", {}).get("event_rows") or 0) for src in sources)
    identities = {
        "|".join(
            str(src.get("identity", {}).get(key) or "")
            for key in ("backend", "model", "firmware", "provider_plmn")
        )
        for src in sources
        if isinstance(src.get("identity"), dict) and src.get("identity")
    }
    return {
        "schema": SCHEMA,
        "generated_at_ts": float(time.time()),
        "runtime_influence": "none",
        "authority": "offline_evidence_library",
        "summary": {
            "source_count": len(sources),
            "identity_count": len(identities),
            "telemetry_rows": telemetry_rows,
            "event_rows": event_rows,
        },
        "sources": sources,
    }


def write_external_memory_manifest(
    source_paths: Iterable[str | Path],
    library_dir: str | Path,
) -> Path:
    target = Path(library_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    manifest = build_external_memory_manifest(source_paths)
    path = target / MANIFEST_NAME
    atomic_write_text(path, json.dumps(manifest, indent=2, sort_keys=True))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description="Build an offline 54GUC external memory evidence manifest.")
    parser.add_argument("sources", nargs="+", help="Support bundles, portable runtime folders, or telemetry JSONL files.")
    parser.add_argument(
        "--out",
        default=DEFAULT_LIBRARY_DIR_NAME,
        help=f"Output library directory. Default: {DEFAULT_LIBRARY_DIR_NAME}",
    )
    args = parser.parse_args(argv)
    manifest_path = write_external_memory_manifest(args.sources, args.out)
    print(str(manifest_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
