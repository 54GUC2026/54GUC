"""Hardware evidence matrix owner.

This module validates exact-profile hardware evidence rows and reduces support
bundles into draft rows.  It is an evidence/certification owner only: it does
not read modem transport, publish runtime capability truth, or feed GUI state.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping


MATRIX_OWNER = "docs.HARDWARE_MATRIX"
ROW_SCHEMA = "54guc.hardware_evidence.v1"
INDEX_SCHEMA = "54guc.hardware_matrix.index.v1"

CERTIFICATION_LEVELS: tuple[str, ...] = (
    "unsupported",
    "detected_read_only",
    "monitor_safe",
    "manual_action_safe",
    "automation_candidate",
    "automation_safe",
    "beta_soak_passed",
)
FIELD_STATE_BUCKETS: tuple[str, ...] = ("supported", "unsupported", "unknown", "failed")
WRITE_ACTION_STATES: frozenset[str] = frozenset({"supported", "unsupported", "unknown", "failed", "degraded"})
PUBLIC_SUPPORT_LABELS: Mapping[str, str] = {
    "unsupported": "Unsupported",
    "detected_read_only": "Detected and Monitored",
    "monitor_safe": "Detected and Monitored",
    "manual_action_safe": "Manual Control Verified",
    "automation_candidate": "Manual Control Verified",
    "automation_safe": "Brain Beta",
    "beta_soak_passed": "Brain Beta",
}


class HardwareEvidenceMatrixError(ValueError):
    """Raised when hardware evidence cannot be trusted as matrix truth."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list_of_text(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted({_text(item) for item in value if _text(item)})
    if _text(value):
        return [_text(value)]
    return []


def _known(value: Any) -> bool:
    text = _text(value).lower()
    return bool(text and text not in {"-", "--", "---", "unknown", "n/a", "na", "none", "null"})


def certification_rank(level: str) -> int:
    token = _text(level)
    if token not in CERTIFICATION_LEVELS:
        raise HardwareEvidenceMatrixError(f"unknown hardware certification level: {level!r}")
    return CERTIFICATION_LEVELS.index(token)


def validate_field_support(field_support: Mapping[str, Any]) -> dict[str, list[str]]:
    if not isinstance(field_support, Mapping):
        raise HardwareEvidenceMatrixError("field_support must be a mapping")
    unknown_buckets = sorted(set(str(key) for key in field_support) - set(FIELD_STATE_BUCKETS))
    if unknown_buckets:
        raise HardwareEvidenceMatrixError(f"unknown field_support buckets: {unknown_buckets}")
    normalized = {bucket: _list_of_text(field_support.get(bucket)) for bucket in FIELD_STATE_BUCKETS}
    seen: dict[str, str] = {}
    for bucket, fields in normalized.items():
        for field in fields:
            if field in seen:
                raise HardwareEvidenceMatrixError(
                    f"field {field!r} appears in both {seen[field]!r} and {bucket!r}"
                )
            seen[field] = bucket
    return normalized


def validate_evidence_row(row: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise HardwareEvidenceMatrixError("hardware evidence row must be a mapping")
    data = dict(row)
    if data.get("schema") != ROW_SCHEMA:
        raise HardwareEvidenceMatrixError(f"unsupported hardware evidence schema: {data.get('schema')!r}")
    if data.get("owner") != MATRIX_OWNER:
        raise HardwareEvidenceMatrixError(f"unsupported hardware evidence owner: {data.get('owner')!r}")
    for key in ("profile_id", "exact_model", "backend", "backend_family", "firmware", "certification_level"):
        if not _text(data.get(key)):
            raise HardwareEvidenceMatrixError(f"missing required hardware evidence field: {key}")
    level = _text(data.get("certification_level"))
    certification_rank(level)
    if level != "unsupported" and not _known(data.get("exact_model")):
        raise HardwareEvidenceMatrixError("non-unsupported rows require exact_model evidence")
    profile_id = _text(data.get("profile_id")).lower()
    exact_model = _text(data.get("exact_model")).lower()
    backend = _text(data.get("backend")).lower()
    family = _text(data.get("backend_family")).lower()
    if level != "unsupported":
        family_only = (
            exact_model in {"huawei", "mikrotik", "routeros", "zte", "hilink", "b535", "b818"}
            or profile_id in {backend, family, "huawei", "mikrotik", "routeros", "zte", "hilink"}
            or profile_id.endswith("_baseline")
        )
        if family_only:
            raise HardwareEvidenceMatrixError("profile certification cannot be inherited from family/baseline")
    data["field_support"] = validate_field_support(_mapping(data.get("field_support")))
    write_actions = _mapping(data.get("write_actions"))
    invalid_actions = {
        str(action): state
        for action, state in write_actions.items()
        if _text(state) not in WRITE_ACTION_STATES
    }
    if invalid_actions:
        raise HardwareEvidenceMatrixError(f"invalid write action states: {invalid_actions}")
    return data


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_evidence_row(path: str | Path) -> dict[str, Any]:
    return validate_evidence_row(load_json(Path(path)))


def matrix_root_from_repo(repo_root: str | Path | None = None) -> Path:
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]
    return root / "docs" / "HARDWARE_MATRIX"


def load_matrix_index(matrix_root: str | Path | None = None) -> dict[str, Any]:
    root = Path(matrix_root) if matrix_root is not None else matrix_root_from_repo()
    index = load_json(root / "index.json")
    if index.get("schema") != INDEX_SCHEMA:
        raise HardwareEvidenceMatrixError(f"unsupported hardware matrix index schema: {index.get('schema')!r}")
    if index.get("owner") != MATRIX_OWNER:
        raise HardwareEvidenceMatrixError(f"unsupported hardware matrix index owner: {index.get('owner')!r}")
    rows = index.get("rows")
    if not isinstance(rows, list) or not rows:
        raise HardwareEvidenceMatrixError("hardware matrix index must list rows")
    return index


def load_matrix_rows(matrix_root: str | Path | None = None) -> list[dict[str, Any]]:
    root = Path(matrix_root) if matrix_root is not None else matrix_root_from_repo()
    index = load_matrix_index(root)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rel in index["rows"]:
        rel_text = _text(rel)
        if not rel_text or Path(rel_text).is_absolute() or ".." in Path(rel_text).parts:
            raise HardwareEvidenceMatrixError(f"invalid matrix row path: {rel!r}")
        row = load_evidence_row(root / rel_text)
        key = _text(row.get("profile_id"))
        if key in seen:
            raise HardwareEvidenceMatrixError(f"duplicate hardware evidence profile_id: {key}")
        seen.add(key)
        out.append(row)
    return out


def _row_filename(row: Mapping[str, Any]) -> str:
    token = "_".join(
        part
        for part in (
            _text(row.get("profile_id")),
            _text(row.get("firmware")),
        )
        if part
    ).lower()
    safe = "".join(ch if ch.isalnum() else "_" for ch in token).strip("_")
    safe = "_".join(part for part in safe.split("_") if part)
    return f"{safe or 'hardware_evidence'}.json"


def _load_index_or_empty(root: Path) -> dict[str, Any]:
    path = root / "index.json"
    if path.is_file():
        return load_matrix_index(root)
    return {
        "owner": MATRIX_OWNER,
        "schema": INDEX_SCHEMA,
        "summary": "Exact-profile hardware evidence rows. Do not promote modem support by family name.",
        "rows": [],
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _field_state_index(field_support: Mapping[str, Iterable[str]]) -> dict[str, str]:
    return {
        _text(field): bucket
        for bucket in FIELD_STATE_BUCKETS
        for field in field_support.get(bucket, ())
        if _text(field)
    }


def _merge_field_support(
    existing: Mapping[str, Iterable[str]],
    incoming: Mapping[str, Iterable[str]],
) -> dict[str, list[str]]:
    """Merge later observation without hiding exact-profile contradictions."""
    states = _field_state_index(existing)
    for field, state in _field_state_index(incoming).items():
        prior = states.get(field)
        if prior is None or prior == "unknown":
            states[field] = state
        elif state == "unknown" or state == prior:
            continue
        else:
            raise HardwareEvidenceMatrixError(
                f"conflicting hardware field evidence: {field!r} is {prior!r} and {state!r}"
            )
    return {
        bucket: sorted(field for field, state in states.items() if state == bucket)
        for bucket in FIELD_STATE_BUCKETS
    }


def merge_evidence_rows(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Merge reduced bundle evidence into an existing exact-profile row."""
    base = validate_evidence_row(existing)
    new = validate_evidence_row(incoming)
    if _text(base.get("profile_id")) != _text(new.get("profile_id")):
        raise HardwareEvidenceMatrixError("cannot merge hardware evidence rows with different profile_id")
    for key in ("exact_model", "backend", "backend_family"):
        old_value = _text(base.get(key)).lower()
        new_value = _text(new.get(key)).lower()
        if old_value and new_value and old_value != new_value:
            raise HardwareEvidenceMatrixError(f"cannot merge mismatched hardware evidence field: {key}")
    merged = dict(base)
    merged["field_support"] = _merge_field_support(base["field_support"], new["field_support"])
    if _known(new.get("firmware")) and not _known(merged.get("firmware")):
        merged["firmware"] = new["firmware"]
    if _known(new.get("webui")) and not _known(merged.get("webui")):
        merged["webui"] = new["webui"]
    merged["source_supported"] = bool(base.get("source_supported")) or bool(new.get("source_supported"))
    soak = dict(_mapping(base.get("hardware_soak")))
    new_soak = _mapping(new.get("hardware_soak"))
    if _text(new_soak.get("last_bundle")):
        soak["last_bundle"] = new_soak["last_bundle"]
        soak["status"] = "bundle_observed"
    notes = sorted({_text(item) for item in (*tuple(soak.get("notes", [])), *tuple(new_soak.get("notes", []))) if _text(item)})
    soak["notes"] = notes
    merged["hardware_soak"] = soak
    merged["evidence_sources"] = sorted(
        {
            _text(item)
            for item in (*tuple(base.get("evidence_sources", [])), *tuple(new.get("evidence_sources", [])))
            if _text(item)
        }
    )
    return validate_evidence_row(merged)


def upsert_support_bundle_evidence(
    bundle: str | Path,
    *,
    matrix_root: str | Path | None = None,
    certification_level: str = "detected_read_only",
) -> dict[str, Any]:
    """Reduce a support bundle and write/update one exact-profile matrix row."""
    root = Path(matrix_root) if matrix_root is not None else matrix_root_from_repo()
    root.mkdir(parents=True, exist_ok=True)
    incoming = reduce_support_bundle_to_evidence_row(bundle, certification_level=certification_level)
    index = _load_index_or_empty(root)
    rows = list(index.get("rows") or [])
    target_profile = _text(incoming.get("profile_id"))
    target_rel = ""
    existing_row: dict[str, Any] | None = None
    for rel in rows:
        rel_text = _text(rel)
        if not rel_text:
            continue
        row_path = root / rel_text
        if not row_path.is_file():
            continue
        row = load_evidence_row(row_path)
        if _text(row.get("profile_id")) == target_profile:
            target_rel = rel_text
            existing_row = row
            break
    row = merge_evidence_rows(existing_row, incoming) if existing_row is not None else incoming
    if not target_rel:
        target_rel = _row_filename(row)
        if target_rel not in rows:
            rows.append(target_rel)
    index["rows"] = sorted(rows)
    _write_json(root / target_rel, row)
    _write_json(root / "index.json", index)
    return {
        "owner": MATRIX_OWNER,
        "updated_row": target_rel,
        "profile_id": target_profile,
        "certification_level": row["certification_level"],
    }


def release_claim_for_row(row: Mapping[str, Any]) -> str:
    data = validate_evidence_row(row)
    model = _text(data.get("exact_model"))
    profile = _text(data.get("profile_id"))
    level = _text(data.get("certification_level"))
    source = "source-supported" if bool(data.get("source_supported")) else "source-not-supported"
    public_label = PUBLIC_SUPPORT_LABELS[level]
    return (
        f"{model} ({profile}) is {source}; public support is {public_label}; "
        f"hardware evidence level is {level}."
    )


def release_profile_status(matrix_root: str | Path | None = None) -> list[dict[str, Any]]:
    return [
        {
            "profile_id": row["profile_id"],
            "exact_model": row["exact_model"],
            "source_supported": bool(row.get("source_supported")),
            "certification_level": row["certification_level"],
            "public_support_label": PUBLIC_SUPPORT_LABELS[row["certification_level"]],
            "release_claim": release_claim_for_row(row),
        }
        for row in load_matrix_rows(matrix_root)
    ]


def validate_release_profiles_have_matrix_rows(
    profile_ids: Iterable[str],
    *,
    matrix_root: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    rows = {str(row["profile_id"]): row for row in load_matrix_rows(matrix_root)}
    missing = sorted({_text(profile_id) for profile_id in profile_ids if _text(profile_id) and _text(profile_id) not in rows})
    if missing:
        raise HardwareEvidenceMatrixError(f"release profiles missing hardware evidence rows: {missing}")
    return {profile_id: rows[profile_id] for profile_id in rows if profile_id in set(_text(item) for item in profile_ids)}


def hardware_profile_gate_report(
    profile_id: str,
    *,
    matrix_root: str | Path | None = None,
    min_level: str = "monitor_safe",
    require_soak_status: str = "",
) -> dict[str, Any]:
    """Return exact-profile hardware gate status for release/build decisions."""
    target = _text(profile_id)
    if not target:
        raise HardwareEvidenceMatrixError("profile_id is required")
    required_rank = certification_rank(min_level)
    rows = {str(row["profile_id"]): row for row in load_matrix_rows(matrix_root)}
    row = rows.get(target)
    blockers: list[str] = []
    if row is None:
        return {
            "owner": MATRIX_OWNER,
            "profile_id": target,
            "eligible": False,
            "blockers": ["missing_hardware_evidence_row"],
            "certification_level": "",
            "required_level": min_level,
        }
    row_rank = certification_rank(row["certification_level"])
    if row_rank < required_rank:
        blockers.append("hardware_evidence_level_below_required")
    if not bool(row.get("source_supported")):
        blockers.append("source_not_supported")
    required_soak = _text(require_soak_status)
    soak_status = _text(_mapping(row.get("hardware_soak")).get("status"))
    if required_soak and soak_status != required_soak:
        blockers.append("hardware_soak_status_below_required")
    return {
        "owner": MATRIX_OWNER,
        "profile_id": target,
        "exact_model": row["exact_model"],
        "eligible": not blockers,
        "blockers": blockers,
        "certification_level": row["certification_level"],
        "required_level": min_level,
        "hardware_soak_status": soak_status,
        "release_claim": release_claim_for_row(row),
    }


def _read_bundle_json(bundle: str | Path, basename: str) -> dict[str, Any]:
    path = Path(bundle)
    if path.is_dir():
        for candidate in path.rglob(basename):
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except Exception as exc:  # pragma: no cover - included in error context
                raise HardwareEvidenceMatrixError(f"cannot parse {basename} from {candidate}: {exc}") from exc
        return {}
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            matches = [name for name in zf.namelist() if Path(name).name == basename]
            if not matches:
                return {}
            with zf.open(matches[0]) as fh:
                try:
                    return json.loads(fh.read().decode("utf-8"))
                except Exception as exc:  # pragma: no cover - included in error context
                    raise HardwareEvidenceMatrixError(f"cannot parse {basename} from {path}: {exc}") from exc
    raise HardwareEvidenceMatrixError(f"support bundle is neither directory nor zip: {path}")


def _identity_value(*sources: Mapping[str, Any], keys: Iterable[str]) -> str:
    for source in sources:
        mapping = _mapping(source)
        for key in keys:
            value = _text(mapping.get(key))
            if value:
                return value
    return ""


def _page_truth_field_name(source_path: str) -> str:
    line, _separator, field = _text(source_path).partition(".")
    if not field:
        return line
    if line == "nr":
        return f"nr_{field}"
    return {
        "temperature_4g_c": "temperature_modem_4g_c",
        "temperature_5g_c": "temperature_modem_5g_c",
    }.get(field, field)


def _field_support_from_page_truth(page_truth: Mapping[str, Any]) -> dict[str, list[str]]:
    buckets: dict[str, set[str]] = {bucket: set() for bucket in FIELD_STATE_BUCKETS}
    if page_truth.get("schema") != "NGUIPageTruth.v1":
        raise HardwareEvidenceMatrixError("support bundle PageTruth has unsupported schema")
    fields = _mapping(page_truth.get("fields"))
    for key, raw_cell in fields.items():
        cell = _mapping(raw_cell)
        if _text(cell.get("owner")) != "modem_live.truth_matrix":
            continue
        field = _page_truth_field_name(_text(cell.get("source_path")) or _text(key))
        state = _text(cell.get("state")).lower()
        if state == "valid":
            bucket = "supported"
        elif state == "unsupported":
            bucket = "unsupported"
        elif state in {"missing", "not_applicable"}:
            bucket = "unknown"
        else:
            bucket = "failed"
        prior = next((name for name in FIELD_STATE_BUCKETS if field in buckets[name]), "")
        if prior and prior != bucket:
            if "unknown" in {prior, bucket}:
                buckets[prior].discard(field)
                buckets[bucket if prior == "unknown" else prior].add(field)
                continue
            raise HardwareEvidenceMatrixError(
                f"conflicting PageTruth hardware field evidence: {field!r} is {prior!r} and {bucket!r}"
            )
        buckets[bucket].add(field)
    return {bucket: sorted(fields) for bucket, fields in buckets.items()}


def _find_live_access_profile(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    found: list[Mapping[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            if {
                "profile_id",
                "fast_live_interval_s",
                "slow_live_interval_s",
                "event_write_min_quiet_s",
            }.issubset(value):
                found.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(snapshot)
    if not found:
        raise HardwareEvidenceMatrixError("support bundle is missing Modem Live access-profile evidence")
    normalized = {json.dumps(dict(item), sort_keys=True, default=str) for item in found}
    if len(normalized) != 1:
        raise HardwareEvidenceMatrixError("support bundle contains conflicting Modem Live access profiles")
    return found[0]


def reduce_support_bundle_to_evidence_row(
    bundle: str | Path,
    *,
    certification_level: str = "detected_read_only",
) -> dict[str, Any]:
    """Build a draft exact-profile hardware evidence row from a support bundle.

    The reducer preserves missing/unsupported/unknown/failed distinctions and
    rejects family-level promotion through `validate_evidence_row`.
    """
    manifest = _read_bundle_json(bundle, "bundle_manifest.json")
    note = _read_bundle_json(bundle, "SUPPORT_BUNDLE_NOTE.json")
    page_truth = _read_bundle_json(bundle, "ngui_page_truth.json")
    evidence_snapshot = _read_bundle_json(bundle, "foundation_evidence_snapshot.json")
    if not manifest:
        raise HardwareEvidenceMatrixError("support bundle is missing bundle_manifest.json")
    if manifest.get("schema") != "FoundationLightSupportBundleManifest.v1":
        raise HardwareEvidenceMatrixError("support bundle manifest is not the current light-bundle contract")
    if note.get("schema") != "FoundationLightSupportBundleNote.v1":
        raise HardwareEvidenceMatrixError("support bundle is missing the current SUPPORT_BUNDLE_NOTE contract")
    identity = _mapping(note.get("modem_identity"))
    profile_id = _identity_value(identity, note, keys=("profile_id", "profile"))
    if profile_id != _text(note.get("profile")):
        raise HardwareEvidenceMatrixError("support bundle profile and modem identity disagree")
    exact_model = _identity_value(identity, keys=("model", "router_model", "hardware_version"))
    backend = _identity_value(identity, keys=("backend", "modem_backend"))
    backend_family = _identity_value(identity, keys=("backend_family", "family"))
    firmware = _identity_value(identity, keys=("firmware", "software_version", "os_version"))
    access = _find_live_access_profile(evidence_snapshot)
    row = {
        "schema": ROW_SCHEMA,
        "owner": MATRIX_OWNER,
        "profile_id": profile_id,
        "exact_model": exact_model,
        "vendor": _identity_value(identity, keys=("vendor",)),
        "backend": backend,
        "backend_family": backend_family or "unknown",
        "firmware": firmware or "unknown",
        "webui": _identity_value(identity, keys=("webui", "os_name")),
        "hardware_sku": _identity_value(identity, keys=("hardware_sku", "config_version")),
        "provider": {
            "name": _identity_value(identity, keys=("provider_name",)),
            "plmn": _identity_value(identity, keys=("provider_plmn", "plmn")),
            "country": "",
        },
        "access_profile": {
            "profile_id": _text(access.get("profile_id")),
            "access_type": _text(access.get("access_type")),
            "fast_read_cadence_s": float(access.get("fast_live_interval_s", 0.0) or 0.0),
            "slow_read_cadence_s": float(access.get("slow_live_interval_s", 0.0) or 0.0),
            "write_min_quiet_s": float(access.get("event_write_min_quiet_s", 0.0) or 0.0),
            "post_write_readback_delay_s": float(access.get("post_write_readback_delay_s", 0.0) or 0.0),
        },
        "source_supported": _text(note.get("runtime_mode")).lower() == "live",
        "certification_level": certification_level,
        "source_completion": {
            "status": "bundle_reduced",
            "reason": "draft row reduced from current Foundation light support bundle",
        },
        "hardware_soak": {
            "status": "bundle_observed",
            "last_bundle": Path(bundle).name,
            "notes": [],
        },
        "field_support": _field_support_from_page_truth(page_truth),
        "write_actions": {},
        "timing_evidence": {},
        "smartness_evidence": {},
        "known_quirks": [],
        "evidence_sources": [
            "support_bundle:bundle_manifest.json",
            "support_bundle:SUPPORT_BUNDLE_NOTE.json",
            "support_bundle:foundation_evidence_snapshot.json",
            "support_bundle:ngui_page_truth.json",
        ],
    }
    return validate_evidence_row(row)
