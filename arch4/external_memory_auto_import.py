from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from arch4.brain.memory_evidence_scope_owner import current_memory_evidence_scope
from arch4.external_memory_library import build_external_memory_manifest
from arch4.external_memory_evidence_scope import external_projection_scope
from arch4.external_memory_promotion import (
    DEFAULT_PRIOR_PACK_NAME,
    promote_manifest,
    _write_json_atomic,
)
from arch4.external_memory_runtime import external_memory_promotion_workspace_status
from arch4.historical_artifact_reader import (
    FAMILY_PRODUCTION,
    HistoricalReadCooperation,
)
from arch4.operator_profiles import infer_backend_family
from arch4.portable_state_import import _portable_state_library_identity_stages
from arch4.runtime_identity import (
    coerce_provider_name_text,
    coerce_provider_plmn_text,
    runtime_identity_from_capabilities_payload,
)
from arch4.runtime_paths import resolve_app_root


AUTO_IMPORT_STATUS_NAME = "external_memory_auto_import_status.json"
PORTABLE_STATE_IMPORT_MARKER_NAME = ".portable_state_imported.json"
AUTO_MANIFEST_NAME = "external_memory_auto_manifest.json"
AUTO_PROMOTION_PLAN_NAME = "external_memory_auto_promotion_plan.json"
AUTO_IMPORT_SCHEMA = "external_memory_auto_import_status_v1"
AUTO_PROMOTION_AUTHORITY = "automatic_promotion_gate"
DEFAULT_MAX_AGE_DAYS = 14
DEFAULT_MIN_TELEMETRY_ROWS = 100


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _status_path(memory_dir: str | Path) -> Path:
    return Path(memory_dir).resolve() / AUTO_IMPORT_STATUS_NAME


def _safe_json_load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_status(memory_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    status = {
        "schema": AUTO_IMPORT_SCHEMA,
        "generated_at_ts": float(payload.get("generated_at_ts") or time.time()),
        "authority": AUTO_PROMOTION_AUTHORITY,
        "runtime_influence": str(payload.get("runtime_influence") or "none"),
        "live_truth_override": False,
        **payload,
    }
    _write_json_atomic(_status_path(memory_dir), status)
    return status


def external_memory_auto_import_status(memory_dir: str | Path) -> dict[str, Any]:
    status = _safe_json_load(_status_path(memory_dir))
    if status.get("schema") != AUTO_IMPORT_SCHEMA:
        return {
            "present": False,
            "status": "not_attempted",
            "authority": AUTO_PROMOTION_AUTHORITY,
            "runtime_influence": "none",
            "live_truth_override": False,
        }
    status["present"] = True
    status.setdefault("live_truth_override", False)
    return status


def _merge_identity(*payloads: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if "backend" not in payload and payload.get("modem_backend") not in ("", None):
            payload = {**payload, "backend": payload.get("modem_backend")}
        for container_key in (
            "runtime_identity",
            "modem_identity_key",
            "accepted_generation",
        ):
            container = payload.get(container_key)
            if isinstance(container, dict):
                merged.update(
                    {
                        str(k): v
                        for k, v in container.items()
                        if v not in ("", None)
                    }
                )
        for key in (
            "backend",
            "backend_family",
            "identity_code",
            "identity_status",
            "identity_catalog_version",
            "identity_catalog_checksum",
            "catalog_version",
            "catalog_checksum",
            "access_type_id",
            "profile_id",
            "model",
            "firmware",
            "provider_name",
            "provider_plmn",
            "session_scope_key",
            "accepted_generation_session_scope_key",
            "runtime_session_scope_key",
            "source_epoch",
            "source_sequence",
            "sample_sequence",
            "generation",
            "sample_ts",
            "ts",
            "sample_age_s",
            "evidence_age_s",
            "selected_band_set_signature",
            "active_band_set_signature",
            "ca_signature",
            "serving_band",
            "primary_band",
            "pci",
            "earfcn",
            "cell_id",
        ):
            value = payload.get(key)
            if value not in ("", None):
                merged[key] = value
        for key in ("evidence_scope", "decode_catalog_snapshot"):
            value = payload.get(key)
            if isinstance(value, dict) and value:
                merged[key] = dict(value)
    backend = _clean_text(merged.get("backend"))
    if backend and not _clean_text(merged.get("backend_family")):
        merged["backend_family"] = infer_backend_family(backend)
    provider_name = coerce_provider_name_text(merged.get("provider_name") or merged.get("provider"))
    provider_plmn = coerce_provider_plmn_text(merged.get("provider_plmn") or merged.get("plmn"))
    if provider_name:
        merged["provider_name"] = provider_name
    else:
        merged.pop("provider_name", None)
    if provider_plmn:
        merged["provider_plmn"] = provider_plmn
    else:
        merged.pop("provider_plmn", None)
    return {key: value for key, value in merged.items() if value not in ("", None)}


def build_external_memory_target_identity(engine: Any) -> dict[str, Any]:
    constraints = getattr(engine, "constraints", None)
    cfg = getattr(engine, "cfg", None)
    caps = getattr(engine, "capabilities", None)
    if not isinstance(caps, dict):
        try:
            caps = engine.get_latest_capabilities_report()
        except Exception:
            caps = {}
    live_status = {}
    status_getter = getattr(engine, "_last_live_status_snapshot", None)
    if callable(status_getter):
        try:
            status = status_getter() or {}
            if isinstance(status, dict):
                live_status = dict(status)
        except Exception:
            live_status = {}
    caps_identity = runtime_identity_from_capabilities_payload(caps if isinstance(caps, dict) else {})
    constraint_identity = {
        "provider_name": getattr(constraints, "provider_name", ""),
        "provider_plmn": getattr(constraints, "provider_plmn", ""),
    }
    cfg_identity = {
        "backend": getattr(cfg, "modem_backend", ""),
    }
    target = _merge_identity(
        cfg_identity,
        caps_identity,
        constraint_identity,
        live_status,
    )
    controller = getattr(engine, "controller", None)
    brain = getattr(controller, "brain", None)
    live_memory_scope = current_memory_evidence_scope(
        getattr(brain, "memory", None)
    )
    if live_memory_scope:
        target["evidence_scope"] = live_memory_scope
        return target
    try:
        target["evidence_scope"] = external_projection_scope(
            target,
            evidence_count=1,
        ).as_dict()
    except (TypeError, ValueError):
        # Exact identity is not available yet.  Callers must retain their
        # waiting state; no weak compatibility scope is published.
        pass
    return target


def _target_identity_ready(identity: dict[str, Any]) -> tuple[bool, list[str]]:
    missing: list[str] = []
    try:
        external_projection_scope(identity, evidence_count=1)
    except (TypeError, ValueError):
        missing.append("exact modem identity/session/generation EvidenceScope")
    if not (_clean_text(identity.get("provider_plmn")) or _clean_text(identity.get("provider_name"))):
        missing.append("provider identity")
    return (not missing), missing


def _candidate_stage_paths(stage_dir: str | Path, target_identity: dict[str, Any]) -> list[Path]:
    stage = Path(stage_dir).resolve()
    candidates: list[Path] = []
    for candidate in _portable_state_library_identity_stages(stage, target_identity):
        if candidate.resolve() == stage:
            continue
        if candidate.is_dir():
            candidates.append(candidate.resolve())
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def _current_stage_has_imported_prior_material(stage: Path) -> bool:
    marker = _safe_json_load(stage / PORTABLE_STATE_IMPORT_MARKER_NAME)
    if not marker:
        return False
    history_files = int(marker.get("history_files") or 0)
    memory_files = int(marker.get("memory_files") or 0)
    merged_sources = marker.get("source_stages_merged")
    has_merged_sources = isinstance(merged_sources, list) and bool(merged_sources)
    return has_merged_sources and (history_files > 0 or memory_files > 0)


def _auto_import_candidate_paths(stage: Path, target_identity: dict[str, Any]) -> list[Path]:
    candidates = _candidate_stage_paths(stage, target_identity)
    if _current_stage_has_imported_prior_material(stage):
        candidates.insert(0, stage)
    candidates.extend(_default_54b_history_candidates(stage))
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def external_memory_candidate_paths(
    stage_dir: str | Path,
    target_identity: dict[str, Any],
    *,
    preferred_paths: Iterable[str | Path] = (),
    include_default_sources: bool,
) -> list[Path]:
    """Return the one bounded candidate inventory used by the import owner."""

    stage = Path(stage_dir).resolve()
    candidates = [
        Path(path).expanduser().resolve()
        for path in preferred_paths
        if str(path or "").strip()
    ]
    if include_default_sources:
        candidates.extend(_auto_import_candidate_paths(stage, target_identity))
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not _path_exists(resolved):
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _default_54b_history_candidates(stage: Path) -> list[Path]:
    """Return bounded local 54b runtime-history candidates.

    The active stage or its explicit import boundary identifies the history
    library. There is no machine-global drive fallback. This function only
    returns existing runtime roots/output dirs; normal identity and promotion
    gates still decide whether a candidate is usable.
    """

    roots: list[Path] = []
    if stage.exists():
        for parent in (stage, *stage.parents):
            if parent.name.lower() == "54b":
                roots.append(parent.resolve())
                break
    out: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen or not _path_exists(resolved):
            return
        seen.add(resolved)
        out.append(resolved)

    for root in roots:
        for item in sorted(root.glob("54GUC_Windows_portable_*"))[-6:]:
            runtime = item / "portable_runtime"
            if runtime.is_dir():
                outputs = sorted(runtime.glob("guc_output_*"))
                add(outputs[-1] if outputs else item)
        for runtime_root in sorted((root / "hp_source_run_runtime").glob("*")) if (root / "hp_source_run_runtime").is_dir() else []:
            if (runtime_root / "guc_memory").is_dir():
                for output_dir in sorted(runtime_root.glob("guc_output_*"))[-4:]:
                    add(output_dir)
    return out[-16:]


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def automatic_external_memory_prior_import(
    *,
    stage_dir: str | Path | None = None,
    memory_dir: str | Path,
    target_identity: dict[str, Any],
    source_paths: Iterable[str | Path] | None = None,
    now_ts: float | None = None,
    min_telemetry_rows: int = DEFAULT_MIN_TELEMETRY_ROWS,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    force: bool = False,
    progress_callback: Callable[[float, str, str], None] | None = None,
    confirmed_handoff_receipt: Mapping[str, Any] | None = None,
    reader_cooperation: HistoricalReadCooperation | None = None,
) -> dict[str, Any]:
    memory = Path(memory_dir).resolve()
    status_generated_at_ts = float(time.time() if now_ts is None else now_ts)
    explicit_source_paths = [Path(path).resolve() for path in source_paths] if source_paths is not None else None
    target = dict(target_identity or {})
    existing = external_memory_promotion_workspace_status(
        memory,
        current_identity=target,
    )
    if (
        existing.get("present")
        and bool((existing.get("activation") or {}).get("active"))
        and not force
    ):
        return _write_status(
            memory,
            {
                "generated_at_ts": status_generated_at_ts,
                "status": "skipped_existing_prior",
                "runtime_influence": "advisory_prior",
                "reason": "external memory prior pack already present",
                "target_identity": dict(target_identity or {}),
                "prior_pack_path": str(memory / DEFAULT_PRIOR_PACK_NAME),
                "max_age_days": int(max_age_days),
                "min_telemetry_rows": int(min_telemetry_rows),
            },
        )

    ready, missing = _target_identity_ready(target)
    if not ready:
        return _write_status(
            memory,
            {
                "generated_at_ts": status_generated_at_ts,
                "status": "waiting_for_identity",
                "reason": "target identity is not ready for automatic import",
                "missing": missing,
                "target_identity": target,
                "candidate_paths": [str(path) for path in explicit_source_paths or []],
                "max_age_days": int(max_age_days),
                "min_telemetry_rows": int(min_telemetry_rows),
            },
        )

    stage = Path(stage_dir).resolve() if stage_dir is not None else resolve_app_root()
    candidates = list(explicit_source_paths) if explicit_source_paths is not None else _auto_import_candidate_paths(stage, target)
    candidates = [path for path in candidates if path.exists()]
    if not candidates:
        return _write_status(
            memory,
            {
                "generated_at_ts": status_generated_at_ts,
                "status": "no_candidates",
                "reason": "no compatible portable-state library candidates found",
                "target_identity": target,
                "candidate_count": 0,
                "candidate_paths": [],
                "max_age_days": int(max_age_days),
                "min_telemetry_rows": int(min_telemetry_rows),
            },
        )

    cutoff_ts = status_generated_at_ts - float(max(1, int(max_age_days)) * 86400)
    if progress_callback is not None:
        progress_callback(2.0, "candidate_inventory", f"{len(candidates)} sources")
    manifest = build_external_memory_manifest(
        candidates,
        since_ts=cutoff_ts,
        confirmed_handoff_receipts=(
            {
                str(Path(str(confirmed_handoff_receipt.get("handoff_path"))).resolve()): dict(
                    confirmed_handoff_receipt
                )
            }
            if confirmed_handoff_receipt
            and str(confirmed_handoff_receipt.get("handoff_path") or "").strip()
            else {}
        ),
        progress_callback=(
            lambda pct, detail: progress_callback(
                5.0 + (0.70 * float(pct)),
                "source_scan",
                detail,
            )
            if progress_callback is not None
            else None
        ),
        reader_cooperation=reader_cooperation,
        telemetry_families=(FAMILY_PRODUCTION,),
    )
    manifest_path = memory / AUTO_MANIFEST_NAME
    _write_json_atomic(manifest_path, manifest)
    if progress_callback is not None:
        progress_callback(80.0, "promotion_gate", "evaluating exact identity and age")
    result = promote_manifest(
        manifest_path,
        target_identity=target,
        output_path=memory / AUTO_PROMOTION_PLAN_NAME,
        memory_dir=memory,
        now_ts=now_ts,
        min_telemetry_rows=min_telemetry_rows,
        max_age_days=max_age_days,
    )
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    imported_pack = _clean_text(result.get("imported_prior_pack"))
    status_name = "imported" if imported_pack else "rejected"
    marker_path = memory / "external_memory_import_marker.json"
    marker = _safe_json_load(marker_path)
    if imported_pack and marker:
        marker["authority"] = AUTO_PROMOTION_AUTHORITY
        marker["auto_import"] = True
        marker["auto_import_status"] = str(_status_path(memory))
        marker["live_truth_override"] = False
        _write_json_atomic(marker_path, marker)
    completed = _write_status(
        memory,
        {
            "generated_at_ts": status_generated_at_ts,
            "status": status_name,
            "runtime_influence": "advisory_prior" if imported_pack else "none",
            "reason": "" if imported_pack else "no approved external memory facts",
            "target_identity": target,
            "candidate_count": len(candidates),
            "candidate_paths": [str(path) for path in candidates],
            "manifest_path": str(manifest_path),
            "promotion_plan": str(result.get("promotion_plan") or ""),
            "prior_pack_path": imported_pack or str(memory / DEFAULT_PRIOR_PACK_NAME),
            "approved_count": int(summary.get("approved_count") or 0),
            "rejected_count": int(summary.get("rejected_count") or 0),
            "approved_observation_count": int(summary.get("approved_observation_count") or 0),
            "max_age_days": int(max_age_days),
            "min_telemetry_rows": int(min_telemetry_rows),
        },
    )
    if progress_callback is not None:
        progress_callback(100.0, "promotion_gate", status_name)
    return completed
