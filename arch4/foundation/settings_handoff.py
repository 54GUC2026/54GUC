"""Archive settings for the next start. The writer receives confirmed Core identity and
saves only approved Foundation, NGUI, and license settings. The reader applies them only
after the live modem identity matches. History and Intelligence data are not included.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import time
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from arch4.atomic_io import atomic_write_json
from arch4.core_contracts.observations import CoreObservationFrame
from arch4.customer_contracts.handoff_identity import (
    confirmed_handoff_identity_matches,
    handoff_identity_token,
)
from arch4.customer_contracts.settings import (
    FoundationOperatorSettings,
    TrafficPlan,
    traffic_plan_scope_id,
)
from arch4.modem_live.profile_catalog import modem_profile_selection
from arch4.external_settings_decoder import decode_previous_handoff_settings_xml
from arch4.foundation.operational_service_contracts import (
    LicenseImportRequest,
    LicenseServicePort,
)
from arch4.foundation.operator_settings import FoundationOperatorSettingsStore
from arch4.foundation.billing_plan_adoption import adopt_r145_billing_plan
from arch4.license_credential_store import load_license_credentials
from arch4.ngui.settings_store import NGUISettingsStore
from arch4.protected_operator_context import (
    PROTECTED_OPERATOR_CONTEXT_ARTIFACT,
    build_protected_operator_context,
    build_protected_operator_context_from_slices,
    decode_protected_operator_context,
    encode_protected_operator_context,
)
from arch4.versioning import parse_version_key


HANDOFF_DIR = "_portable_next_start_handoff"
SETTINGS_HANDOFF_FILENAME_PREFIX = "next_start_settings_handoff__"
SETTINGS_HANDOFF_FILENAME_SUFFIX = ".zip"
PREVIOUS_VERSION_HANDOFF_FILENAME = "next_start_handoff.zip"
SETTINGS_HANDOFF_SCHEMA = "portable_next_start_settings_handoff_v1"
PREVIOUS_VERSION_HANDOFF_SCHEMA = "portable_next_start_handoff_v1"
SETTINGS_HANDOFF_ENV = "ARCH_PORTABLE_NEXT_START_SETTINGS_HANDOFF"
PREVIOUS_VERSION_HANDOFF_ENV = "ARCH_PORTABLE_NEXT_START_HANDOFF"
SETTINGS_IMPORT_MARKER_FILENAME = (
    ".portable_confirmed_handoff_settings_imported.json"
)
SETTINGS_IMPORT_RECEIPT_SCHEMA = "portable_confirmed_handoff_settings_import_v1"
SETTINGS_IMPORT_RECEIPT_OWNER = "54guc.foundation.settings_handoff"
SETTINGS_IMPORT_TRANSACTION_FILENAME = (
    ".portable_confirmed_handoff_settings_import_transaction.json"
)
SETTINGS_IMPORT_TRANSACTION_SCHEMA = (
    "portable_confirmed_handoff_settings_import_transaction_v1"
)
MANIFEST_FILENAME = "version_manifest.json"
PREVIOUS_VERSION_CONFIG_MEMBER = "guc_config.xml"


@dataclass(frozen=True, slots=True)
class FoundationSettingsHandoffPublishResult:
    source_stage: Path
    handoff_path: Path
    protected_context_published: bool


@dataclass(frozen=True, slots=True)
class FoundationSettingsHandoffImportResult:
    target_stage: Path
    handoff_path: Path
    source_identity: Mapping[str, object]
    merged_items: int
    operator_settings_updated: bool


@dataclass(frozen=True, slots=True)
class _SettingsArchivePayload:
    manifest: Mapping[str, object]
    protected_context: bytes | None
    previous_config: bytes | None
    disposition: str


_IMPORT_PHASES = (
    "intent",
    "license_pending",
    "license_committed",
    "operator_pending",
    "operator_committed",
    "ngui_hint_pending",
    "ngui_hint_committed",
    "receipt_committed",
)


def _transaction_id(
    *,
    source_context_checksum: str,
    confirmed_profile_id: str,
    confirmed_identity: Mapping[str, object],
) -> str:
    identity = "|".join(
        handoff_identity_token(confirmed_identity.get(key))
        for key in ("serial", "imei", "imsi", "provider_plmn")
    )
    material = (
        f"{source_context_checksum}|{confirmed_profile_id}|{identity}"
    ).encode("utf-8")
    return "settings-import:" + hashlib.sha256(material).hexdigest()[:24]


def _load_import_transaction(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            "confirmed_handoff_import_transaction_unreadable"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != SETTINGS_IMPORT_TRANSACTION_SCHEMA
        or payload.get("owner") != SETTINGS_IMPORT_RECEIPT_OWNER
        or payload.get("phase") not in _IMPORT_PHASES
    ):
        raise RuntimeError("confirmed_handoff_import_transaction_invalid")
    return payload


def _commit_import_transaction(
    path: Path,
    payload: Mapping[str, object],
    *,
    phase: str,
    phase_result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if phase not in _IMPORT_PHASES:
        raise ValueError("confirmed_handoff_import_transaction_phase_invalid")
    candidate = dict(payload)
    current_phase = str(candidate.get("phase") or "")
    if current_phase:
        current_index = _IMPORT_PHASES.index(current_phase)
        next_index = _IMPORT_PHASES.index(phase)
        if next_index < current_index or next_index > current_index + 1:
            raise RuntimeError(
                "confirmed_handoff_import_transaction_phase_transition_invalid"
            )
    results = dict(candidate.get("phase_results") or {})
    if phase_result is not None:
        results[phase] = dict(phase_result)
    candidate.update(
        {
            "schema": SETTINGS_IMPORT_TRANSACTION_SCHEMA,
            "owner": SETTINGS_IMPORT_RECEIPT_OWNER,
            "phase": phase,
            "phase_results": results,
            "updated_at_ts": float(time.time()),
        }
    )
    atomic_write_json(
        path,
        candidate,
        sort_keys=True,
        trailing_newline=True,
    )
    return candidate


def _transaction_phase_at_least(
    transaction: Mapping[str, object],
    phase: str,
) -> bool:
    return _IMPORT_PHASES.index(str(transaction.get("phase") or "")) >= (
        _IMPORT_PHASES.index(phase)
    )


def _portable_path(value: str | Path) -> Path:
    raw = str(value or "").strip()
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
    if match and os.name != "nt":
        return Path("/mnt") / match.group(1).lower() / str(
            match.group(2) or ""
        ).replace("\\", "/")
    return Path(raw)


def _handoff_root(
    stage_dir: str | Path | None = None,
    *,
    runtime_memory_dir: str | Path | None = None,
) -> Path | None:
    """One publication/admission location for the explicit deployment context."""
    if runtime_memory_dir not in (None, ""):
        memory = _portable_path(str(runtime_memory_dir)).resolve()
        for parent in (memory, *memory.parents):
            if parent.name.lower() == "54b":
                return parent / HANDOFF_DIR
        if (memory.name == "guc_memory" and memory.parent.name.startswith("cgf-run-")
                and memory.parent.parent.name == "runs"
                and re.fullmatch(r"54GUC_R[0-9]+(?:\.[0-9]+)+-source", memory.parents[2].name)):
            # Permanent boundary adapter for the protected versioned source lane:
            # router/build/runs/cgf-run-*/guc_memory shares the router archive
            # with the versioned portable lane, never worker state.
            return memory.parents[3] / HANDOFF_DIR
        if stage_dir in (None, "") or memory.parent.name != "portable_runtime":
            return memory.parent / HANDOFF_DIR
    if stage_dir in (None, ""):
        return None
    stage = Path(stage_dir).resolve()
    if stage.name.startswith("guc_output") and stage.parent.name == "portable_runtime":
        stage = stage.parent.parent
    elif stage.name == "portable_runtime":
        stage = stage.parent
    return stage.parent / HANDOFF_DIR


def _current_settings_handoff_filename(
    identity: Mapping[str, object],
) -> str:
    """Return one stable current-generation archive name per physical modem."""

    backend = handoff_identity_token(identity.get("backend")) or "modem"
    immutable_id = handoff_identity_token(identity.get("serial")) or (
        handoff_identity_token(identity.get("imei"))
    )
    if not immutable_id:
        raise ValueError("settings_handoff_immutable_identity_required")
    return (
        f"{SETTINGS_HANDOFF_FILENAME_PREFIX}{backend}__{immutable_id}"
        f"{SETTINGS_HANDOFF_FILENAME_SUFFIX}"
    )


def _add_candidate(
    candidates: list[Path],
    raw: str | Path | None,
    *,
    archive_name: str,
) -> None:
    if raw in (None, ""):
        return
    path = _portable_path(str(raw))
    if path.is_dir() or path.suffix.lower() != ".zip":
        path = path / archive_name
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    if resolved not in candidates:
        candidates.append(resolved)


def _add_archive_scope(
    candidates: list[Path],
    raw: str | Path | None,
    *,
    confirmed_identity: Mapping[str, object] | None,
) -> None:
    """Add both legal archive generations for one bounded location scope."""

    if isinstance(confirmed_identity, Mapping):
        _add_candidate(
            candidates,
            raw,
            archive_name=_current_settings_handoff_filename(
                confirmed_identity
            ),
        )
    _add_candidate(
        candidates,
        raw,
        archive_name=PREVIOUS_VERSION_HANDOFF_FILENAME,
    )


def _next_start_settings_handoff_candidates(
    *,
    stage_dir: str | Path | None = None,
    runtime_memory_dir: str | Path | None = None,
    explicit_path: str | Path | None = None,
    confirmed_identity: Mapping[str, object] | None = None,
) -> tuple[Path, ...]:
    """Build the one bounded, scope-first Foundation archive inventory."""

    candidates: list[Path] = []
    if explicit_path not in (None, ""):
        _add_archive_scope(
            candidates,
            explicit_path,
            confirmed_identity=confirmed_identity,
        )
        return tuple(candidates)

    configured_current = os.environ.get(SETTINGS_HANDOFF_ENV, "")
    configured_previous = os.environ.get(PREVIOUS_VERSION_HANDOFF_ENV, "")
    if configured_current or configured_previous:
        if configured_current and isinstance(confirmed_identity, Mapping):
            _add_candidate(
                candidates,
                configured_current,
                archive_name=_current_settings_handoff_filename(
                    confirmed_identity
                ),
            )
        _add_candidate(
            candidates,
            configured_previous,
            archive_name=PREVIOUS_VERSION_HANDOFF_FILENAME,
        )
        return tuple(candidates)

    _add_archive_scope(
        candidates,
        _handoff_root(stage_dir, runtime_memory_dir=runtime_memory_dir),
        confirmed_identity=confirmed_identity,
    )
    return tuple(candidates)


def resolve_next_start_settings_handoff_path(
    *,
    stage_dir: str | Path | None = None,
    runtime_memory_dir: str | Path | None = None,
    explicit_path: str | Path | None = None,
    confirmed_identity: Mapping[str, object] | None = None,
) -> Path | None:
    """Resolve the owner archive plus the named previous-version input adapter."""

    return next(
        (
            path
            for path in _next_start_settings_handoff_candidates(
                stage_dir=stage_dir,
                runtime_memory_dir=runtime_memory_dir,
                explicit_path=explicit_path,
                confirmed_identity=confirmed_identity,
            )
            if path.is_file()
        ),
        None,
    )


def confirmed_history_identity_links(
    handoff_root: str | Path, *, confirmed_identity: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    """Read identity proof only, not settings, from the declared handoff folder.

    All three published filename generations are explicit inputs here. The
    settings importer may choose one settings archive; identity continuity
    needs every matching version. No credential member is opened.
    """
    root = Path(handoff_root).resolve()
    if not root.is_dir():
        return ()
    candidates = {
        root / "next_start_settings_handoff.zip",
        root / PREVIOUS_VERSION_HANDOFF_FILENAME,
        *root.glob(SETTINGS_HANDOFF_FILENAME_PREFIX + "*" + SETTINGS_HANDOFF_FILENAME_SUFFIX),
    }
    manifests: list[tuple[Mapping[str, object], str]] = []
    for path in sorted(candidates):
        if not path.is_file():
            continue
        with zipfile.ZipFile(path) as archive:
            members = [info for info in archive.infolist() if info.filename == "handoff_manifest.json"]
            if len(members) != 1 or members[0].file_size > 2 * 1024 * 1024:
                raise ValueError("confirmed_history_handoff_manifest_invalid")
            raw = archive.read(members[0])
        manifest = json.loads(raw)
        if not isinstance(manifest, dict) or manifest.get("schema") not in {
            SETTINGS_HANDOFF_SCHEMA, PREVIOUS_VERSION_HANDOFF_SCHEMA,
        }:
            raise ValueError("confirmed_history_handoff_schema_invalid")
        identity = manifest.get("identity")
        if not isinstance(identity, Mapping):
            continue
        manifests.append((identity, hashlib.sha256(raw).hexdigest()))
    links: list[dict[str, object]] = []
    for identity, manifest_sha256 in manifests:
        if not confirmed_handoff_identity_matches(identity, confirmed_identity):
            continue
        alias = (identity.get("identity_code"), identity.get("profile_id"))
        if all(alias) and any(
            (other.get("identity_code"), other.get("profile_id")) == alias
            and (other.get("serial") or other.get("imei"))
            and not confirmed_handoff_identity_matches(other, confirmed_identity)
            for other, _ in manifests
        ):
            raise ValueError("confirmed_history_legacy_alias_physical_owner_ambiguous")
        links.append({
            "identity": {key: identity[key] for key in (
                "backend", "model", "serial", "imei", "profile_id", "identity_code", "access_type",
            ) if key in identity},
            "manifest_sha256": manifest_sha256,
        })
    return tuple(links)


def _read_version(stage: Path) -> tuple[str, tuple[int, ...]]:
    manifest = stage / MANIFEST_FILENAME
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            version = str(payload.get("version") or "").strip()
            if version:
                return version, parse_version_key(version)
    version_file = stage / "VERSION"
    if version_file.is_file():
        version = version_file.read_text(encoding="utf-8", errors="ignore").strip()
        if version:
            return version, parse_version_key(version)
    return "", parse_version_key("")


def settings_handoff_identity_from_observation(
    frame: CoreObservationFrame | None,
) -> dict[str, object]:
    """Build archive identity from the accepted Core observation."""

    if frame is None:
        return {}
    if not isinstance(frame, CoreObservationFrame):
        raise TypeError("settings_handoff_core_observation_required")
    if frame.identity_status != "confirmed":
        return {}
    facts = frame.capability_facts.to_mapping()
    access_profile = facts.get("modem_live_access_profile")
    profile = access_profile if isinstance(access_profile, Mapping) else {}
    return {
        "profile_id": str(facts.get("profile_id") or ""),
        "backend": str(facts.get("backend") or ""),
        "backend_family": str(profile.get("backend_family") or ""),
        "model": str(facts.get("model") or ""),
        "firmware": str(facts.get("firmware_version") or ""),
        "provider_name": str(facts.get("provider_name") or ""),
        "provider_plmn": str(facts.get("provider_plmn") or ""),
        "serial": str(facts.get("serial") or ""),
        "imei": str(facts.get("imei") or ""),
        "imsi": str(facts.get("imsi") or ""),
        "identity_code": frame.identity_code,
        "access_type": frame.access_type,
    }


def settings_handoff_identity_from_current_state(
    payload: Mapping[str, object] | None,
) -> dict[str, object]:
    """Project confirmed handoff identity from the real runtime snapshot shape."""

    if not isinstance(payload, Mapping):
        return {}
    base: dict[str, object] = {}
    direct = payload.get("runtime_identity")
    if isinstance(direct, Mapping):
        base.update(dict(direct))
    core = payload.get("core_snapshot")
    if not isinstance(core, Mapping):
        return base
    registers = core.get("registers")
    if not isinstance(registers, Mapping):
        return base
    matrix_register = registers.get("modem_live_matrix")
    if not isinstance(matrix_register, Mapping):
        return base
    matrix = matrix_register.get("value")
    if not isinstance(matrix, Mapping):
        return base
    lines = matrix.get("lines")
    if not isinstance(lines, Mapping):
        return base

    def cell_value(line: str, field: str) -> object:
        raw_line = lines.get(line)
        if not isinstance(raw_line, Mapping):
            return None
        cells = raw_line.get("cells")
        if not isinstance(cells, Mapping):
            return None
        cell = cells.get(field)
        if not isinstance(cell, Mapping):
            return None
        return cell.get("value")

    runtime_identity = cell_value("identity", "runtime_identity")
    if isinstance(runtime_identity, Mapping):
        base.update(dict(runtime_identity))
    modem_identity_key = cell_value("identity", "modem_identity_key")
    if isinstance(modem_identity_key, Mapping):
        for key in ("profile_id", "identity_code", "access_type"):
            value = modem_identity_key.get(key)
            if value not in (None, ""):
                base.setdefault(key, value)
    access_profile = cell_value("identity", "modem_live_access_profile")
    if isinstance(access_profile, Mapping):
        value = access_profile.get("access_type")
        if value not in (None, ""):
            base.setdefault("access_type", value)
        backend = access_profile.get("backend")
        if backend not in (None, ""):
            base.setdefault("backend", backend)
    capability = cell_value("capability", "runtime_capabilities")
    if isinstance(capability, Mapping):
        for source_key, target_key in (
            ("profile_id", "profile_id"),
            ("backend", "backend"),
        ):
            value = capability.get(source_key)
            if value not in (None, ""):
                base.setdefault(target_key, value)
        contract = capability.get("capability_contract")
        if isinstance(contract, Mapping):
            value = contract.get("profile_id")
            if value not in (None, ""):
                base.setdefault("profile_id", value)
    command_context = core.get("command_context")
    if isinstance(command_context, Mapping):
        value = command_context.get("identity_code")
        if value not in (None, ""):
            base.setdefault("identity_code", value)

    allowed = {
        "profile_id",
        "backend",
        "backend_family",
        "model",
        "firmware",
        "firmware_version",
        "provider_name",
        "provider_plmn",
        "serial",
        "imei",
        "imsi",
        "identity_code",
        "access_type",
    }
    return {
        str(key): value
        for key, value in base.items()
        if key in allowed and value not in (None, "")
    }


def publish_next_start_settings_handoff(
    source_stage_dir: str | Path,
    *,
    confirmed_identity: Mapping[str, object],
    runtime_memory_dir: str | Path | None = None,
) -> FoundationSettingsHandoffPublishResult | None:
    """Publish protected settings from explicit confirmed Core identity."""

    source_stage = Path(source_stage_dir).resolve()
    identity = dict(confirmed_identity or {})
    if not source_stage.is_dir():
        raise FileNotFoundError(f"settings handoff stage missing: {source_stage}")
    if not any(
        handoff_identity_token(identity.get(key)) for key in ("serial", "imei")
    ):
        return None
    protected_context = build_protected_operator_context(
        source_stage,
        profile_id=str(identity.get("profile_id") or ""),
    )
    if protected_context is None:
        return None

    root = _handoff_root(source_stage, runtime_memory_dir=runtime_memory_dir)
    root.mkdir(parents=True, exist_ok=True)
    handoff_path = root / _current_settings_handoff_filename(identity)
    staging_path = root / (
        f".handoff-{uuid.uuid4().hex}.tmp"
    )
    version_text, version_key = _read_version(source_stage)
    manifest = {
        "schema": SETTINGS_HANDOFF_SCHEMA,
        "owner": SETTINGS_IMPORT_RECEIPT_OWNER,
        "source_stage": str(source_stage),
        "source_stage_name": str(source_stage.name or ""),
        "version": version_text,
        "version_key": list(version_key),
        "created_at_ts": float(time.time()),
        "identity": identity,
        "settings_only": True,
        "history_included": False,
        "raw_config_included": False,
        "protected_operator_context": {
            "artifact": PROTECTED_OPERATOR_CONTEXT_ARTIFACT,
            "included": True,
            "apply_gate": "same_physical_modem_identity_confirmed",
        },
    }
    try:
        with zipfile.ZipFile(staging_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(
                "handoff_manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True),
            )
            bundle.writestr(
                PROTECTED_OPERATOR_CONTEXT_ARTIFACT,
                encode_protected_operator_context(protected_context),
            )
            version_manifest = source_stage / MANIFEST_FILENAME
            if version_manifest.is_file():
                bundle.write(version_manifest, MANIFEST_FILENAME)
        staging_path.replace(handoff_path)
    finally:
        staging_path.unlink(missing_ok=True)
    return FoundationSettingsHandoffPublishResult(
        source_stage=source_stage,
        handoff_path=handoff_path,
        protected_context_published=True,
    )


def _read_settings_archive(path: Path) -> _SettingsArchivePayload:
    """Read manifest and the one legal settings member in one ZIP snapshot."""

    try:
        with zipfile.ZipFile(path, "r") as bundle:
            manifest = json.loads(
                bundle.read("handoff_manifest.json").decode("utf-8")
            )
            if not isinstance(manifest, dict) or manifest.get("schema") not in {
                SETTINGS_HANDOFF_SCHEMA,
                PREVIOUS_VERSION_HANDOFF_SCHEMA,
            }:
                return _SettingsArchivePayload({}, None, None, "manifest_invalid")
            try:
                protected = bundle.read(PROTECTED_OPERATOR_CONTEXT_ARTIFACT)
            except KeyError:
                protected = None
            previous = None
            if (
                protected is None
                and manifest.get("schema") == PREVIOUS_VERSION_HANDOFF_SCHEMA
            ):
                try:
                    previous = bundle.read(PREVIOUS_VERSION_CONFIG_MEMBER)
                except KeyError:
                    return _SettingsArchivePayload(
                        manifest,
                        None,
                        None,
                        "previous_adapter_config_absent",
                    )
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return _SettingsArchivePayload({}, None, None, "rejected_unreadable")
    return _SettingsArchivePayload(
        manifest,
        protected,
        previous,
        "loaded",
    )


def _decode_settings_context(
    archive: _SettingsArchivePayload,
    *,
    confirmed_profile_id: str,
    confirmed_identity: Mapping[str, object],
) -> tuple[dict[str, object] | None, str]:
    """Read one current context or the named previous-version adapter."""

    if archive.protected_context is not None:
        try:
            return (
                decode_protected_operator_context(archive.protected_context),
                "validated",
            )
        except (TypeError, ValueError):
            return None, "rejected_invalid"
    previous_xml = archive.previous_config
    if previous_xml is None:
        return None, archive.disposition
    previous = decode_previous_handoff_settings_xml(
        previous_xml,
        confirmed_profile_id=confirmed_profile_id,
        confirmed_identity=confirmed_identity,
    )
    context = build_protected_operator_context_from_slices(
        operator_settings=previous.foundation,
        connection_hint=previous.connection_hint,
        license_fields=previous.license_fields,
    )
    if context is None:
        return None, "previous_adapter_no_allowlisted_settings"
    return context, "validated_previous_version_adapter"


def import_confirmed_next_start_handoff_settings(
    target_stage_dir: str | Path,
    *,
    license_service: LicenseServicePort,
    confirmed_profile_id: str,
    confirmed_identity: Mapping[str, object],
    runtime_memory_dir: str | Path | None = None,
    handoff_path: str | Path | None = None,
) -> FoundationSettingsHandoffImportResult | None:
    """Apply protected settings only after the live modem identity matches."""

    if license_service is None:
        raise TypeError("confirmed handoff import requires LicenseService")
    profile_contract = modem_profile_selection(str(confirmed_profile_id or "").strip())
    if profile_contract is None:
        raise ValueError("confirmed handoff import requires exact profile_id")
    exact_profile_id = str(profile_contract.profile_id)
    target_stage = Path(target_stage_dir).resolve()
    if Path(license_service.storage_root).resolve() != target_stage:
        raise RuntimeError("confirmed handoff LicenseService root mismatch")
    candidates = _next_start_settings_handoff_candidates(
        stage_dir=target_stage,
        runtime_memory_dir=runtime_memory_dir,
        explicit_path=handoff_path,
        confirmed_identity=confirmed_identity,
    )
    selected: tuple[
        Path,
        _SettingsArchivePayload,
        Mapping[str, object],
    ] | None = None
    for candidate in candidates:
        if not candidate.is_file():
            continue
        archive = _read_settings_archive(candidate)
        source_identity = archive.manifest.get("identity")
        if not isinstance(source_identity, Mapping):
            continue
        if not confirmed_handoff_identity_matches(
            source_identity,
            confirmed_identity,
        ):
            continue
        selected = (candidate, archive, source_identity)
        break
    if selected is None:
        return None
    resolved, archive, source_identity = selected

    protected_context, protected_context_disposition = _decode_settings_context(
        archive,
        confirmed_profile_id=exact_profile_id,
        confirmed_identity=confirmed_identity,
    )
    if protected_context is None:
        return None
    source_context_checksum = str(protected_context["content_sha256"])
    transaction_path = target_stage / SETTINGS_IMPORT_TRANSACTION_FILENAME
    transaction_identity = _transaction_id(
        source_context_checksum=source_context_checksum,
        confirmed_profile_id=exact_profile_id,
        confirmed_identity=confirmed_identity,
    )
    transaction = _load_import_transaction(transaction_path)
    if transaction is not None and (
        str(transaction.get("transaction_id") or "") != transaction_identity
        and transaction.get("phase") != "receipt_committed"
    ):
        raise RuntimeError(
            "confirmed_handoff_import_transaction_conflict"
        )
    if transaction is None or (
        str(transaction.get("transaction_id") or "") != transaction_identity
    ):
        transaction = _commit_import_transaction(
            transaction_path,
            {
                "transaction_id": transaction_identity,
                "source_handoff": str(resolved),
                "source_context_checksum": source_context_checksum,
                "confirmed_profile_id": exact_profile_id,
                "phase_results": {},
                "created_at_ts": float(time.time()),
            },
            phase="intent",
        )

    text_tags = dict(protected_context["fields"])
    protected_license = {
        name: str(text_tags.pop(name, "") or "").strip()
        for name in ("license_key", "license_nonce", "license_email", "license_tier")
    }
    operator_store = FoundationOperatorSettingsStore(target_stage)
    ngui_store = NGUISettingsStore(target_stage)
    before_operator = operator_store.load()
    phase_results = dict(transaction.get("phase_results") or {})
    license_result = dict(phase_results.get("license_committed") or {})
    if license_result:
        license_disposition = str(
            license_result.get("license_credential_disposition") or "absent"
        )
        license_committed = bool(
            license_result.get("license_credential_committed", False)
        )
    else:
        if not _transaction_phase_at_least(transaction, "license_pending"):
            transaction = _commit_import_transaction(
                transaction_path,
                transaction,
                phase="license_pending",
                phase_result={
                    "credential_present": bool(
                        protected_license["license_key"]
                        and protected_license["license_nonce"]
                    ),
                    "identity_kind": (
                        "serial"
                        if str(confirmed_identity.get("serial") or "").strip()
                        else "imei"
                    ),
                },
            )
        license_disposition = "absent"
        license_committed = False
        if protected_license["license_key"] and protected_license["license_nonce"]:
            immutable_identity = str(
                confirmed_identity.get("serial")
                or confirmed_identity.get("imei")
                or ""
            ).strip()
            try:
                imported_license = license_service.import_if_missing(
                    LicenseImportRequest(
                        identity=immutable_identity,
                        nonce=protected_license["license_nonce"],
                        key=protected_license["license_key"],
                        email=protected_license["license_email"],
                        tier=protected_license["license_tier"],
                    )
                )
            except Exception as exc:
                license_disposition = (
                    f"rejected_by_license_service:{type(exc).__name__}"
                )
            else:
                license_committed = bool(imported_license.committed)
                license_disposition = imported_license.disposition
        elif any(protected_license.values()):
            license_disposition = "incomplete_protected_credentials_rejected"
        transaction = _commit_import_transaction(
            transaction_path,
            transaction,
            phase="license_committed",
            phase_result={
                "license_credential_disposition": license_disposition,
                "license_credential_committed": license_committed,
            },
        )

    merged_fields = dict(before_operator.operator_fields)
    merged_item_count = 0
    for name, value in sorted(text_tags.items()):
        clean = str(value or "").strip()
        if clean and name not in merged_fields:
            merged_fields[name] = clean
            merged_item_count += 1

    merged_plans = dict(before_operator.traffic_plans)
    rejected_traffic_scopes = 0
    for raw_scope in protected_context["traffic_plan_scopes"]:
        subscription = str(raw_scope.get("subscription_key") or "")
        if not subscription:
            adopted = adopt_r145_billing_plan(raw_scope, source_identity=source_identity,
                confirmed_identity=confirmed_identity, profile_id=exact_profile_id)
            if adopted is not None and adopted.scope_id not in merged_plans:
                merged_plans[adopted.scope_id] = adopted
                merged_item_count += 1
            elif adopted is None:
                rejected_traffic_scopes += 1
            continue
        source_profile_id = str(raw_scope.get("profile_id") or "").strip()
        if source_profile_id != exact_profile_id:
            rejected_traffic_scopes += 1
            continue
        backend = str(raw_scope.get("modem_backend") or "").strip().lower()
        provider_name = str(raw_scope.get("provider_name") or "").strip()
        provider_plmn = str(raw_scope.get("provider_plmn") or "").strip()
        scope_id = traffic_plan_scope_id(
            subscription_key=subscription,
            modem_backend=backend,
            provider_name=provider_name,
            provider_plmn=provider_plmn,
            profile_id=exact_profile_id,
        )
        if not scope_id:
            rejected_traffic_scopes += 1
            continue
        override = str(raw_scope.get("override_enabled") or "").strip().lower()
        if override not in {"true", "false"}:
            raise ValueError("protected traffic-plan override_enabled is invalid")
        plan = TrafficPlan(
            subscription_key=subscription,
            scope_id=scope_id,
            modem_backend=backend,
            provider_name=provider_name,
            provider_plmn=provider_plmn,
            profile_id=exact_profile_id,
            monthly_allowance_gb=raw_scope.get("monthly_allowance_gb") or 1000.0,
            cycle_start_day=raw_scope.get("cycle_start_day") or 1,
            override_enabled=override == "true",
        )
        if plan.scope_id not in merged_plans:
            merged_plans[plan.scope_id] = plan
            merged_item_count += 1

    from arch4.customer_contracts.apn_profiles import SavedApnProfile
    merged_profiles = dict(before_operator.apn_profiles)
    for raw_profile in protected_context.get("apn_profiles", []):
        profile = SavedApnProfile.from_mapping(raw_profile)
        if profile.profile_id not in merged_profiles:
            merged_profiles[profile.profile_id] = profile
            merged_item_count += 1
    after_operator = FoundationOperatorSettings(
        unassigned_traffic_plans=before_operator.unassigned_traffic_plans,
        operator_fields=merged_fields,
        traffic_plans=merged_plans,
        apn_profiles=merged_profiles,
    )
    operator_settings_updated = (
        dict(after_operator.operator_fields) != dict(before_operator.operator_fields)
        or dict(after_operator.traffic_plans) != dict(before_operator.traffic_plans)
        or dict(after_operator.apn_profiles) != dict(before_operator.apn_profiles)
    )
    operator_result = dict(
        dict(transaction.get("phase_results") or {}).get(
            "operator_committed"
        )
        or {}
    )
    if not operator_result:
        operator_pending = dict(
            dict(transaction.get("phase_results") or {}).get(
                "operator_pending"
            )
            or {}
        )
        if not operator_pending:
            operator_pending = {
                "operator_settings_updated": operator_settings_updated,
                "merged_item_count": merged_item_count,
                "rejected_traffic_scope_count": rejected_traffic_scopes,
            }
            transaction = _commit_import_transaction(
                transaction_path,
                transaction,
                phase="operator_pending",
                phase_result=operator_pending,
            )
        if operator_settings_updated:
            operator_store.save(after_operator)
        operator_settings_updated = bool(
            operator_pending.get("operator_settings_updated", False)
        )
        merged_item_count = int(
            operator_pending.get("merged_item_count", 0) or 0
        )
        rejected_traffic_scopes = int(
            operator_pending.get("rejected_traffic_scope_count", 0) or 0
        )
        transaction = _commit_import_transaction(
            transaction_path,
            transaction,
            phase="operator_committed",
            phase_result={
                "operator_settings_updated": operator_settings_updated,
                "merged_item_count": merged_item_count,
                "rejected_traffic_scope_count": rejected_traffic_scopes,
            },
        )
    else:
        operator_settings_updated = bool(
            operator_result.get("operator_settings_updated", False)
        )
        merged_item_count = int(
            operator_result.get("merged_item_count", 0) or 0
        )
        rejected_traffic_scopes = int(
            operator_result.get("rejected_traffic_scope_count", 0) or 0
        )

    hint_disposition = "absent"
    protected_hint = dict(protected_context.get("ngui_connection_hint") or {})
    hint_result = dict(
        dict(transaction.get("phase_results") or {}).get(
            "ngui_hint_committed"
        )
        or {}
    )
    if hint_result:
        hint_disposition = str(
            hint_result.get("ngui_connection_hint_disposition") or "absent"
        )
        hint_merged = int(hint_result.get("merged_item_count", 0) or 0)
    else:
        hint_merged = 0
        hint_pending = dict(
            dict(transaction.get("phase_results") or {}).get(
                "ngui_hint_pending"
            )
            or {}
        )
        if not hint_pending:
            if protected_hint:
                if (
                    str(protected_hint.get("profile_id") or "").strip()
                    != exact_profile_id
                ):
                    pending_disposition = "rejected_profile_mismatch"
                    pending_merged = 0
                else:
                    pending_disposition = "committed_exact_profile_hint"
                    pending_merged = 1
            else:
                pending_disposition = "absent"
                pending_merged = 0
            hint_pending = {
                "ngui_connection_hint_disposition": pending_disposition,
                "merged_item_count": pending_merged,
            }
            transaction = _commit_import_transaction(
                transaction_path,
                transaction,
                phase="ngui_hint_pending",
                phase_result=hint_pending,
            )
        if protected_hint:
            if (
                str(protected_hint.get("profile_id") or "").strip()
                != exact_profile_id
            ):
                hint_disposition = "rejected_profile_mismatch"
            else:
                ngui_store.save_connection_hint(
                    profile_id=exact_profile_id,
                    address=str(protected_hint.get("address") or "").strip(),
                    username=str(protected_hint.get("username") or "").strip(),
                    make_last=True,
                )
                hint_disposition = "committed_exact_profile_hint"
                hint_merged = 1
        hint_disposition = str(
            hint_pending.get("ngui_connection_hint_disposition")
            or hint_disposition
        )
        hint_merged = int(
            hint_pending.get("merged_item_count", hint_merged) or 0
        )
        transaction = _commit_import_transaction(
            transaction_path,
            transaction,
            phase="ngui_hint_committed",
            phase_result={
                "ngui_connection_hint_disposition": hint_disposition,
                "merged_item_count": hint_merged,
            },
        )
    merged_item_count += hint_merged

    merged = int(merged_item_count) + (1 if license_committed else 0)
    payload = {
        "schema": SETTINGS_IMPORT_RECEIPT_SCHEMA,
        "owner": SETTINGS_IMPORT_RECEIPT_OWNER,
        "decision": "imported" if merged else "no_missing_operator_fields",
        "same_modem_identity_confirmed": True,
        "immutable_match_fields": [
            key
            for key in ("serial", "imei")
            if handoff_identity_token(source_identity.get(key))
            and handoff_identity_token(source_identity.get(key))
            == handoff_identity_token(confirmed_identity.get(key))
        ],
        "source_handoff": str(resolved),
        "protected_operator_context_disposition": protected_context_disposition,
        "source_context_schema": str(protected_context["schema"]),
        "source_context_checksum": source_context_checksum,
        "merged_items": merged,
        "license_credential_disposition": license_disposition,
        "license_credential_committed": license_committed,
        "operator_settings_updated": operator_settings_updated,
        "ngui_connection_hint_disposition": hint_disposition,
        "rejected_traffic_scope_count": rejected_traffic_scopes,
        "raw_config_imported": False,
        "connection_credentials_imported": False,
        "password_imported": False,
        "decided_at_ts": float(time.time()),
        "target_stage": str(target_stage),
    }
    atomic_write_json(
        target_stage / SETTINGS_IMPORT_MARKER_FILENAME,
        payload,
        sort_keys=True,
        trailing_newline=True,
    )
    _commit_import_transaction(
        transaction_path,
        transaction,
        phase="receipt_committed",
        phase_result={
            "decision": payload["decision"],
            "merged_items": merged,
            "receipt": SETTINGS_IMPORT_MARKER_FILENAME,
        },
    )
    return FoundationSettingsHandoffImportResult(
        target_stage=target_stage,
        handoff_path=resolved,
        source_identity=dict(source_identity),
        merged_items=merged,
        operator_settings_updated=operator_settings_updated,
    )


def foundation_settings_handoff_contract_errors(
    stage_dir: str | Path,
) -> list[str]:
    """Check a saved settings receipt against its owner stores."""

    stage = Path(stage_dir).resolve()
    errors: list[str] = []
    transaction_path = stage / SETTINGS_IMPORT_TRANSACTION_FILENAME
    try:
        transaction = _load_import_transaction(transaction_path)
    except RuntimeError as exc:
        errors.append(str(exc))
        transaction = None
    marker_path = stage / SETTINGS_IMPORT_MARKER_FILENAME
    if not marker_path.is_file():
        if transaction is not None and transaction.get("phase") == "receipt_committed":
            errors.append("confirmed handoff transaction lacks terminal receipt")
        return errors
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(
            f"confirmed handoff receipt cannot be read: {type(exc).__name__}"
        )
        return errors
    if not isinstance(marker, dict):
        errors.append("confirmed handoff receipt must be an object")
        return errors
    expected = {
        "schema", "owner", "decision", "same_modem_identity_confirmed",
        "immutable_match_fields", "source_handoff",
        "protected_operator_context_disposition", "source_context_schema",
        "source_context_checksum", "merged_items",
        "license_credential_disposition", "license_credential_committed",
        "operator_settings_updated", "ngui_connection_hint_disposition",
        "rejected_traffic_scope_count", "raw_config_imported",
        "connection_credentials_imported", "password_imported",
        "decided_at_ts", "target_stage",
    }
    if transaction is None:
        errors.append("confirmed handoff receipt lacks import transaction")
    elif transaction.get("phase") != "receipt_committed":
        errors.append("confirmed handoff import transaction is not terminal")
    elif (
        str(transaction.get("source_context_checksum") or "")
        != str(marker.get("source_context_checksum") or "")
    ):
        errors.append("confirmed handoff transaction source checksum mismatch")
    if set(marker) != expected:
        errors.append("confirmed handoff receipt fields mismatch")
    if marker.get("schema") != SETTINGS_IMPORT_RECEIPT_SCHEMA:
        errors.append("confirmed handoff receipt schema mismatch")
    if marker.get("owner") != SETTINGS_IMPORT_RECEIPT_OWNER:
        errors.append("confirmed handoff receipt owner mismatch")
    if marker.get("same_modem_identity_confirmed") is not True:
        errors.append("confirmed handoff receipt lacks immutable modem proof")
    for field in (
        "raw_config_imported", "connection_credentials_imported", "password_imported"
    ):
        if marker.get(field) is not False:
            errors.append(f"confirmed handoff receipt illegally reports {field}")
    if marker.get("operator_settings_updated") is True:
        store = FoundationOperatorSettingsStore(stage)
        if not store.exists:
            errors.append("confirmed operator settings commit is missing")
        else:
            try:
                store.load()
            except Exception as exc:
                errors.append(
                    f"confirmed operator settings commit is invalid:{type(exc).__name__}"
                )
    if marker.get("license_credential_committed") is True:
        if load_license_credentials(stage) is None:
            errors.append("confirmed license credential commit is missing or invalid")
        if not (stage / "license.dat").is_file():
            errors.append("confirmed license.dat output adapter is missing")
    if str(marker.get("ngui_connection_hint_disposition") or "").strip() == (
        "committed_exact_profile_hint"
    ):
        store = NGUISettingsStore(stage)
        if not store.exists:
            errors.append("confirmed NGUI connection-hint commit is missing")
        else:
            try:
                settings = store.load()
            except Exception as exc:
                errors.append(
                    f"confirmed NGUI connection-hint commit is invalid:{type(exc).__name__}"
                )
            else:
                if not settings.last_profile_id or (
                    settings.last_profile_id not in settings.connection_hints
                ):
                    errors.append("confirmed NGUI connection-hint commit has no exact profile")
    return errors


__all__ = (
    "FoundationSettingsHandoffImportResult",
    "FoundationSettingsHandoffPublishResult",
    "SETTINGS_IMPORT_RECEIPT_OWNER",
    "SETTINGS_IMPORT_TRANSACTION_FILENAME",
    "SETTINGS_IMPORT_TRANSACTION_SCHEMA",
    "confirmed_handoff_identity_matches",
    "foundation_settings_handoff_contract_errors",
    "import_confirmed_next_start_handoff_settings",
    "publish_next_start_settings_handoff",
    "resolve_next_start_settings_handoff_path",
    "settings_handoff_identity_from_current_state",
    "settings_handoff_identity_from_observation",
)
