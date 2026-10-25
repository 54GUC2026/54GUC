"""Choose Foundation runtime artifacts for a support bundle. This is the only manifest for
the light product. It does not scan or import Intelligence history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import gzip
import io
import json
import math
import os
from pathlib import Path
from threading import RLock
import time
from uuid import uuid4
from typing import Any, Iterable, Mapping

from arch4.architecture_contracts.application_lifecycle import ApplicationLeaseIdentity
from arch4.application_lease import current_process_started_at_unix_ms

from arch4.foundation.evidence_snapshot import (
    FOUNDATION_EVIDENCE_FAILURE_RECEIPTS,
    FOUNDATION_EVIDENCE_SNAPSHOT_FILENAME,
    FOUNDATION_EVIDENCE_SNAPSHOT_OWNER,
    FOUNDATION_EVIDENCE_SNAPSHOT_SCHEMA,
    capture_foundation_evidence_snapshot,
    foundation_evidence_snapshot_contract_errors,
)
from arch4.foundation.live_artifact_reader import read_live_bytes
from arch4.foundation.runtime_artifacts import (
    SHUTDOWN_RECEIPT_FILENAME,
    SHUTDOWN_RECEIPT_OWNER,
    SHUTDOWN_RECEIPT_SCHEMA,
)
from arch4.foundation.operator_settings import (
    FILENAME as FOUNDATION_SETTINGS_FILENAME,
    OWNER as FOUNDATION_SETTINGS_OWNER,
    SCHEMA as FOUNDATION_SETTINGS_SCHEMA,
    PREVIOUS_SCHEMA as FOUNDATION_SETTINGS_PREVIOUS_SCHEMA,
)
from arch4.foundation.traffic_accounting import (
    FOUNDATION_TRAFFIC_ACCOUNTING_FILENAME,
    FOUNDATION_TRAFFIC_ACCOUNTING_MIGRATION_FILENAME,
    FOUNDATION_TRAFFIC_ACCOUNTING_MIGRATION_SCHEMA,
    FOUNDATION_TRAFFIC_ACCOUNTING_OWNER,
    FOUNDATION_TRAFFIC_ACCOUNTING_SCHEMA,
    resolve_traffic_accounting_root,
)
from arch4.interfaces import ARCH_NGUI_PAGE_TRUTH_FILENAME
from arch4.ngui.fault_evidence import (
    FILENAME as NGUI_FAULTS_FILENAME,
    OWNER as NGUI_FAULTS_OWNER,
    SCHEMA as NGUI_FAULTS_SCHEMA,
    ngui_fault_evidence_record_errors,
)
from arch4.ngui.debug_surface import (
    NGUI_PAGE_TRUTH_OWNER,
    NGUI_PAGE_TRUTH_SCHEMA,
)
from arch4.ngui.settings_store import (
    FILENAME as NGUI_SETTINGS_FILENAME,
    OWNER as NGUI_SETTINGS_OWNER,
    SCHEMA as NGUI_SETTINGS_SCHEMA,
)
from arch4.privacy_scrub import (
    SupportArtifactScrubResult, scrub_support_artifact,
)
from arch4.runtime_paths import CGF_STATE_DIRNAME
from arch4.modem_live.startup_trace import TRACE_FILENAME, TRACE_SCHEMA, TRACE_OWNER


OWNER = "arch4.foundation.support_bundle"
SCHEMA = "FoundationLightSupportManifest.v1"


def scrub_support_export_artifact(path: str | Path, raw: bytes, *,
        known_secret_values: set[str] | None = None) -> SupportArtifactScrubResult:
    """Final privacy boundary. Never reseal redacted diagnostic data as live truth."""
    if Path(path).parent.name=='modem_profiles'and Path(path).suffix=='.json':
        from arch4.modem_live.profile_definition.privacy import checked_definition
        from arch4.privacy_scrub import excluded_support_artifact
        try:
            checked=checked_definition(raw,known_secret_values or())
            if checked.profile_id!=Path(path).stem:raise ValueError('profile_definition_filename_mismatch')
        except (ValueError,TypeError,KeyError,RecursionError,UnicodeError):
            return excluded_support_artifact(source_kind='json',source_size_bytes=len(raw),
                disposition='excluded_private_or_invalid_definition',reason='profile_definition_not_shareable')
        return SupportArtifactScrubResult(content=raw,disposition='included_profile_definition_checked',
            source_kind='json',source_size_bytes=len(raw),staged_size_bytes=len(raw),redaction_count=0)
    from arch4.support_evidence import project_support_records, SNAPSHOT_SCHEMA
    from arch4.privacy_scrub import scrub_record_with_secrets, scrub_text
    if str(path).lower().endswith(".jsonl.gz"):
        output = io.BytesIO()
        count = 0
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as source, gzip.GzipFile(
                fileobj=output, mode="wb", mtime=0, compresslevel=6) as target:
            for line in source:
                if not line.strip():
                    continue
                original = json.loads(line)
                prepared = project_support_records(original)
                clean, secrets = scrub_record_with_secrets(prepared)
                if known_secret_values is not None:
                    known_secret_values.update(secrets)
                count += bool(secrets)
                target.write((json.dumps(clean, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
        content = output.getvalue()
        return SupportArtifactScrubResult(content=content, disposition="included_structured_scrubbed",
            source_kind="jsonl.gz", source_size_bytes=len(raw), staged_size_bytes=len(content),
            redaction_count=count)
    is_snapshot = Path(path).name == FOUNDATION_EVIDENCE_SNAPSHOT_FILENAME
    payload = None
    if is_snapshot:
        payload = json.loads(raw)
        errors = foundation_evidence_snapshot_contract_errors(payload)
        if errors:
            raise ValueError("foundation_support_source_snapshot_invalid:" + ",".join(errors))
        payload = {k: v for k, v in payload.items() if k not in {"content_sha256", "snapshot_id"}}
        payload["source_status"] = {k: {n: v for n,v in row.items() if n != "sha256"}
            for k,row in payload["source_status"].items()}
        payload = {**payload, "schema": SNAPSHOT_SCHEMA, "owner": OWNER}
    elif Path(path).suffix == ".json":
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            pass  # The privacy owner declares malformed optional artifacts.
    if payload is not None:
        payload = project_support_records(payload) if not is_snapshot else {
            k: project_support_records(v) for k, v in payload.items()}
        if isinstance(payload, dict) and Path(path).name != "open_source_manifest.json":
            clean, secrets = scrub_record_with_secrets(payload)
            if known_secret_values is not None:
                known_secret_values.update(secrets)
            content = (json.dumps(clean, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)+"\n").encode("utf-8")
            return SupportArtifactScrubResult(content=content, disposition="included_structured_scrubbed",
                source_kind="json", source_size_bytes=len(raw), staged_size_bytes=len(content),
                redaction_count=int(clean != payload))
        raw = (json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    if Path(path).suffix == ".jsonl":
        try:
            rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
            if all(isinstance(row, dict) for row in rows):
                clean_rows, count = [], 0
                for row in rows:
                    prepared = project_support_records(row)
                    clean, secrets = scrub_record_with_secrets(prepared)
                    if known_secret_values is not None:
                        known_secret_values.update(secrets)
                    count += clean != prepared
                    clean_rows.append(json.dumps(clean, ensure_ascii=False, allow_nan=False))
                content = ("\n".join(clean_rows) + ("\n" if rows else "")).encode("utf-8")
                return SupportArtifactScrubResult(content=content, disposition="included_structured_scrubbed",
                    source_kind="jsonl", source_size_bytes=len(raw), staged_size_bytes=len(content), redaction_count=count)
        except (ValueError, UnicodeError):
            pass  # Malformed optional lines use the existing explicit privacy markers.
    if Path(path).suffix.lower() in {".txt", ".log"}:
        try:
            text = raw.decode("utf-8-sig")
            if not any(ord(c)<32 and c not in "\t\r\n" for c in text):
                clean = scrub_text(text)
                content = clean.encode("utf-8")
                return SupportArtifactScrubResult(content=content, disposition="included_text_scrubbed",
                    source_kind="text", source_size_bytes=len(raw), staged_size_bytes=len(content),
                    redaction_count=int(clean != text))
        except UnicodeError:
            pass
    return scrub_support_artifact(path, raw)


def finalize_support_artifacts(originals: Mapping[str, bytes], manifest: Mapping[str, Any], *,
        known_secret_values: set[str] | None = None,
        prepared_artifacts: Mapping[str, Any] | None = None) -> dict[str, bytes]:
    """Last content-changing phase: privacy once, then serialize final metadata."""
    files: dict[str, bytes] = {}
    dispositions = []
    prepared_artifacts = dict(prepared_artifacts or {})
    if set(prepared_artifacts).difference(originals):
        raise ValueError("support_prepared_member_absent")
    for name, raw in originals.items():
        receipt = prepared_artifacts.get(name)
        if receipt is not None:
            from arch4.prepared_support import PreparedSupportArtifact
            if (type(receipt) is not PreparedSupportArtifact
                    or receipt.filename != Path(name).name or not receipt.verify(raw)):
                raise ValueError("support_prepared_artifact_receipt_invalid")
            result = SupportArtifactScrubResult(content=raw, disposition="included_structured_scrubbed",
                source_kind="jsonl.gz", source_size_bytes=len(raw), staged_size_bytes=len(raw),
                redaction_count=receipt.redaction_count)
        else:
            result = scrub_support_export_artifact(name, raw, known_secret_values=known_secret_values)
        row = result.manifest_record(name)
        row.pop("staged_sha256", None)  # Ordinary diagnostic files need no extra digest.
        dispositions.append(row)
        if result.included:
            files[name] = result.content
    names = sorted([*files, "BUNDLE_FILE_LIST.txt", "bundle_manifest.json"])
    files["BUNDLE_FILE_LIST.txt"] = ("54GUC support bundle files\n\n" + "\n".join(names) + "\n").encode()
    prepared = dict(manifest)
    profile=prepared.get('portable_profile')
    if isinstance(profile,dict)and profile.get('included')and profile.get('member')not in files:
        prepared['portable_profile']={**profile,'included':False,'reason':'definition_omitted_by_privacy_or_validation'}
    prepared["privacy_scrub"] = dict(policy="deny_by_default_v1", complete=True,
        status="complete_with_redactions" if any(r["redaction_count"] for r in dispositions) else "complete",
        included_count=len(files), excluded_count=sum(not r["included"] for r in dispositions),
        redacted_record_count=sum(r["redaction_count"] for r in dispositions), artifacts=dispositions)
    prepared["archive_contract"] = dict(members=[dict(name=n, size_bytes=len(b)) for n,b in sorted(files.items())])
    prepared["included"] = names
    raw = json.dumps(prepared, ensure_ascii=False, allow_nan=False).encode()
    result = scrub_support_export_artifact("bundle_manifest.json", raw)
    if not result.included:
        raise ValueError("support_manifest_privacy_failed")
    files["bundle_manifest.json"] = result.content
    return files

SUPPORT_BUNDLE_VERIFICATION_RECEIPT_FILENAME = (
    "support_bundle_verification_receipt.json"
)
from arch4.foundation.read_diagnostic_report import ROUTER_CHECK_FILENAME

LIGHT_RUNTIME_ARTIFACT_FILENAMES: tuple[str, ...] = (
    ROUTER_CHECK_FILENAME,
    TRACE_FILENAME,
    FOUNDATION_EVIDENCE_SNAPSHOT_FILENAME,
    NGUI_FAULTS_FILENAME,
    ARCH_NGUI_PAGE_TRUTH_FILENAME,
    FOUNDATION_SETTINGS_FILENAME,
    FOUNDATION_TRAFFIC_ACCOUNTING_FILENAME,
    FOUNDATION_TRAFFIC_ACCOUNTING_MIGRATION_FILENAME,
    NGUI_SETTINGS_FILENAME,
    SHUTDOWN_RECEIPT_FILENAME,
    *FOUNDATION_EVIDENCE_FAILURE_RECEIPTS,
)
JSON_ARTIFACT_CONTRACTS: dict[str, tuple[str, str]] = {
    FOUNDATION_EVIDENCE_SNAPSHOT_FILENAME: (
        FOUNDATION_EVIDENCE_SNAPSHOT_SCHEMA,
        FOUNDATION_EVIDENCE_SNAPSHOT_OWNER,
    ),
    ARCH_NGUI_PAGE_TRUTH_FILENAME: (
        NGUI_PAGE_TRUTH_SCHEMA,
        NGUI_PAGE_TRUTH_OWNER,
    ),
    FOUNDATION_SETTINGS_FILENAME: (
        FOUNDATION_SETTINGS_SCHEMA,
        FOUNDATION_SETTINGS_OWNER,
    ),
    FOUNDATION_TRAFFIC_ACCOUNTING_FILENAME: (
        FOUNDATION_TRAFFIC_ACCOUNTING_SCHEMA,
        FOUNDATION_TRAFFIC_ACCOUNTING_OWNER,
    ),
    FOUNDATION_TRAFFIC_ACCOUNTING_MIGRATION_FILENAME: (
        FOUNDATION_TRAFFIC_ACCOUNTING_MIGRATION_SCHEMA,
        FOUNDATION_TRAFFIC_ACCOUNTING_OWNER,
    ),
    NGUI_SETTINGS_FILENAME: (NGUI_SETTINGS_SCHEMA, NGUI_SETTINGS_OWNER),
    SHUTDOWN_RECEIPT_FILENAME: (
        SHUTDOWN_RECEIPT_SCHEMA,
        SHUTDOWN_RECEIPT_OWNER,
    ),
    "site_report_failure_receipt.json": (
        "SiteReportFailureReceipt.v1",
        "arch4.foundation.site_report_job",
    ),
    "support_bundle_failure_receipt.json": (
        "SupportBundleFailureReceipt.v1",
        "arch4.support_bundle_job",
    ),
}
_SUPPORT_CAPTURE_LOCK = RLock()


@dataclass(frozen=True, slots=True)
class FoundationSupportArtifact:
    name: str
    content: bytes = field(repr=False)
    captured_at: float
    contract: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class FoundationSupportExportSnapshot:
    snapshot_id: str
    captured_at: float
    foundation_evidence_snapshot_id: str
    artifacts: tuple[FoundationSupportArtifact, ...]
    manifest: Mapping[str, Any]
    known_secret_values: tuple[str, ...] = field(repr=False, compare=False)


def profile_definition_support_artifact(snapshot,registry):
    """Select the definition of the frozen accepted root, never a GUI draft."""
    from arch4.core_contracts.accepted_current_facts import CoreAcceptedCurrentFacts
    artifact=next((a for a in snapshot.artifacts if a.name==FOUNDATION_EVIDENCE_SNAPSHOT_FILENAME
        and a.contract.get('valid')is True),None)
    if artifact is None:return {'included':False,'reason':'accepted_definition_not_recorded'},None
    try:
        state=json.loads(artifact.content)['current_state']['core_snapshot']
        facts=CoreAcceptedCurrentFacts.from_payload(state['accepted_current_facts'])
        fact=facts.exact('identity.modem_live_access_profile')
        profile=fact.value.get('value')if fact.disposition=='available'else None
        admitted=profile.get('profile_definition')if isinstance(profile,dict)else None
        if not isinstance(admitted,dict)or admitted.get('schema')!='PortableProfileProvenance.v1':
            return {'included':False,'reason':'accepted_definition_not_recorded'},None
        definition=registry.select(admitted['profile_id'])
        if definition.content_hash!=admitted['content_hash']or definition.interpretation_hash!=admitted['interpretation_hash']:
            return {'included':False,'reason':'profile_file_differs_from_admitted_definition'},None
        member='modem_profiles/'+definition.profile_id+'.json'
        return {'included':True,'member':member,**definition.provenance()},registry.definition_bytes(definition.profile_id)
    except (ValueError,KeyError,TypeError):
        return {'included':False,'reason':'admitted_definition_unavailable'},None


def _json_contract_content(name: str, raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return {
            "valid": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, dict):
        return {"valid": False, "error": "top_level_object_required"}
    schema = str(payload.get("schema") or "")
    owner = str(payload.get("owner") or "")
    expected = JSON_ARTIFACT_CONTRACTS.get(name)
    if expected is None:
        return {
            "valid": False,
            "schema": schema,
            "owner": owner,
            "error": "json_artifact_contract_undeclared",
        }
    expected_schema, expected_owner = expected
    errors: list[str] = []
    prior_operator_schema = name == FOUNDATION_SETTINGS_FILENAME and schema == FOUNDATION_SETTINGS_PREVIOUS_SCHEMA
    if schema != expected_schema and not prior_operator_schema:
        errors.append("schema_invalid")
    if owner != expected_owner:
        errors.append("owner_invalid")
    if name == FOUNDATION_EVIDENCE_SNAPSHOT_FILENAME:
        errors.extend(foundation_evidence_snapshot_contract_errors(payload))
    return {
        "valid": not errors,
        "schema": schema,
        "owner": owner,
        **({"errors": errors[:20]} if errors else {}),
    }


def _ngui_fault_contract_content(raw: bytes) -> dict[str, Any]:
    errors: list[str] = []
    record_count = 0
    last_sequence = 0
    try:
        lines = raw.decode("utf-8").splitlines()
    except Exception as exc:
        return {
            "valid": False,
            "schema": NGUI_FAULTS_SCHEMA,
            "owner": NGUI_FAULTS_OWNER,
            "record_count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        record_count += 1
        try:
            row = json.loads(line)
        except Exception as exc:
            errors.append(
                f"line_{line_number}:invalid_json:{type(exc).__name__}:{exc}"
            )
            continue
        row_errors = ngui_fault_evidence_record_errors(
            row,
            previous_sequence=last_sequence,
            record_bytes=len((line + "\n").encode("utf-8")),
        )
        errors.extend(f"line_{line_number}:{error}" for error in row_errors)
        if isinstance(row, dict):
            sequence = row.get("sequence")
            if type(sequence) is int and sequence > 0:
                last_sequence = sequence
    result: dict[str, Any] = {
        "valid": not errors,
        "schema": NGUI_FAULTS_SCHEMA,
        "owner": NGUI_FAULTS_OWNER,
        "record_count": record_count,
        "last_sequence": last_sequence,
    }
    if errors:
        result["errors"] = errors[:20]
    return result


def _artifact_contract(name: str, raw: bytes) -> dict[str, Any]:
    if name == ROUTER_CHECK_FILENAME:
        text = raw.decode("utf-8")
        return {"valid": text.startswith("54GUC - Router check\n"), "owner": "arch4.foundation.read_diagnostic_report", "schema": "RouterCheckText.v1"}
    if name == TRACE_FILENAME:
        try:
            records = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
            valid = bool(records) and all(
                isinstance(row, dict) and row.get("schema") == TRACE_SCHEMA
                and row.get("owner") == TRACE_OWNER for row in records)
            return {"valid": valid, "schema": TRACE_SCHEMA, "owner": TRACE_OWNER,
                    "record_count": len(records)}
        except (ValueError, UnicodeError) as exc:
            return {"valid": False, "schema": TRACE_SCHEMA, "owner": TRACE_OWNER,
                    "error": type(exc).__name__}
    if name == NGUI_FAULTS_FILENAME:
        return _ngui_fault_contract_content(raw)
    return _json_contract_content(name, raw)


def capture_light_runtime_support_snapshot(
    runtime_dir: str | Path,
    *,
    memory_dir: str | Path | None = None,
) -> FoundationSupportExportSnapshot:
    """Capture every allowed runtime artifact exactly once for one export."""

    root = Path(runtime_dir).resolve()
    captured_at = time.time()
    with _SUPPORT_CAPTURE_LOCK:
        evidence = capture_foundation_evidence_snapshot(
            root,
            now=captured_at,
            require_complete=False,
        )
        evidence_content = (
            json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        captured: list[FoundationSupportArtifact] = [
            FoundationSupportArtifact(
                name=FOUNDATION_EVIDENCE_SNAPSHOT_FILENAME,
                content=evidence_content,
                captured_at=float(evidence.get("captured_at") or captured_at),
                contract=_artifact_contract(
                    FOUNDATION_EVIDENCE_SNAPSHOT_FILENAME,
                    evidence_content,
                ),
            )
        ]
        unavailable: list[dict[str, str]] = []
        explicit_sources = {}
        if memory_dir not in (None, ""):
            accounting_root = resolve_traffic_accounting_root(
                Path(memory_dir).resolve() / CGF_STATE_DIRNAME
            )
            for accounting_name in (
                FOUNDATION_TRAFFIC_ACCOUNTING_FILENAME,
                FOUNDATION_TRAFFIC_ACCOUNTING_MIGRATION_FILENAME,
            ):
                explicit_sources[accounting_name] = (
                    accounting_root / accounting_name
                )
        for name in LIGHT_RUNTIME_ARTIFACT_FILENAMES:
            if name == FOUNDATION_EVIDENCE_SNAPSHOT_FILENAME:
                continue
            path = explicit_sources.get(name, root / name)
            if not path.is_file():
                continue
            try:
                content = read_live_bytes(path)
            except Exception as exc:
                unavailable.append(
                    {
                        "name": name,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            captured.append(
                FoundationSupportArtifact(
                    name=name,
                    content=content,
                    captured_at=time.time(),
                    contract=_artifact_contract(name, content),
                )
            )
        artifact_rows = [
            {
                "name": artifact.name,
                "source_size_bytes": len(artifact.content),
                "captured_at": artifact.captured_at,
                **dict(artifact.contract),
            }
            for artifact in captured
        ]
        snapshot_id = 'foundation-support:' + uuid4().hex

        present = {artifact.name for artifact in captured}
        manifest = {
            "schema": SCHEMA,
            "owner": OWNER,
            "export_snapshot_id": snapshot_id,
            "captured_at": captured_at,
            "foundation_evidence_snapshot_id": str(
                evidence.get("snapshot_id") or ""
            ),
            "capture_semantics": (
                "coherent_foundation_evidence_plus_independently_versioned_"
                "boundary_artifacts"
            ),
            "declared_artifacts": list(LIGHT_RUNTIME_ARTIFACT_FILENAMES),
            "selected_artifacts": [artifact.name for artifact in captured],
            "missing_artifacts": [
                name
                for name in LIGHT_RUNTIME_ARTIFACT_FILENAMES
                if name not in present
            ],
            "unavailable_artifacts": unavailable,
            "artifacts": artifact_rows,
        }
        return FoundationSupportExportSnapshot(
            snapshot_id=snapshot_id,
            captured_at=captured_at,
            foundation_evidence_snapshot_id=str(
                evidence.get("snapshot_id") or ""
            ),
            artifacts=tuple(captured),
            manifest=manifest,
            known_secret_values=(),  # Discovered during the sole final privacy walk.
        )


def select_light_runtime_artifacts(runtime_dir: str | Path) -> tuple[Path, ...]:
    """Removed path-based export. Support capture must use an immutable snapshot."""

    del runtime_dir
    raise RuntimeError(
        "foundation_raw_support_path_selection_deleted_use_capture_snapshot"
    )


def support_session_selection(snapshot: FoundationSupportExportSnapshot) -> dict[str, Any] | None:
    """Bound selected-session evidence by its frozen, original GUI lease."""
    artifact = next((row for row in snapshot.artifacts
                     if row.name == ARCH_NGUI_PAGE_TRUTH_FILENAME), None)
    if artifact is None or artifact.contract.get("valid") is not True:
        return None
    try:
        truth = json.loads(artifact.content)
        if not isinstance(truth.get("application_lease"), dict):
            return None
        lease = ApplicationLeaseIdentity(**truth["application_lease"])
        published = truth["published_at"]
        if type(published) not in (int, float) or not math.isfinite(published):
            raise ValueError("invalid_publication_time")
        end = min(float(published), snapshot.captured_at)
        started = lease.process_started_at_unix_ms / 1000.0
        if end < started:
            raise ValueError("publication_before_session")
        # Page publication can lag durable History. Only this exact GUI may
        # use capture time; an old folder or reused PID keeps its saved bound.
        live_owner = (lease.process_id == os.getpid()
                and lease.process_started_at_unix_ms == current_process_started_at_unix_ms())
        if live_owner:
            end = snapshot.captured_at
        # A stopped-session export must include final History flushes after the
        # last GUI frame. Never use an earlier run's or future-dated stop.
        stop = next((row for row in snapshot.artifacts
                     if row.name == SHUTDOWN_RECEIPT_FILENAME
                     and row.contract.get("valid") is True), None)
        if stop is not None:
            receipt = json.loads(stop.content)
            stop_start, stop_end = receipt.get("started_at"), receipt.get("finished_at")
            if (receipt.get("status") in {"complete", "degraded"}
                    and type(stop_start) in (int, float) and type(stop_end) in (int, float)
                    and math.isfinite(stop_start) and math.isfinite(stop_end)
                    and started <= stop_start <= stop_end <= snapshot.captured_at):
                end = max(end, float(stop_end))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("support_history_session_identity_invalid") from exc
    return {
        "scope": "session", "application_lease_id": lease.lease_id,
        "application_generation": lease.generation,
        "session_started_at": started, "from_at": max(started, end - 86400.0),
        "to_at": end, "truncated_to_last_24_hours": end - started > 86400.0,
        "live_owner_required": live_owner,
    }


def light_runtime_artifact_manifest(
    selected_runtime_files: Iterable[Path],
) -> dict[str, Any]:
    """Removed live-path manifest. Support capture must use an immutable snapshot."""

    del selected_runtime_files
    raise RuntimeError(
        "foundation_live_support_manifest_deleted_use_capture_snapshot"
    )


__all__ = [
    "finalize_support_artifacts",
    "scrub_support_export_artifact",
    "LIGHT_RUNTIME_ARTIFACT_FILENAMES",
    "JSON_ARTIFACT_CONTRACTS",
    "FoundationSupportArtifact",
    "FoundationSupportExportSnapshot",
    "NGUI_FAULTS_FILENAME",
    "OWNER",
    "SCHEMA",
    "SHUTDOWN_RECEIPT_FILENAME",
    "SUPPORT_BUNDLE_VERIFICATION_RECEIPT_FILENAME",
    "capture_light_runtime_support_snapshot",
    "support_session_selection",
    "light_runtime_artifact_manifest",
    "select_light_runtime_artifacts",
]
