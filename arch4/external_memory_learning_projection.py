from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping
import zipfile

from arch4.brain.memory_band_learning_query_store import external_memory_band_hour_quality
from arch4.brain.historical import historical_formula_vector
from arch4.brain.memory_evidence_scope_owner import (
    memory_formula_vector,
    memory_snapshot_scope_error,
)
from arch4.evidence_scope import EvidenceScope, validated_evidence_scope
from arch4.external_memory_evidence_scope import (
    validated_external_projection_scope,
    validated_external_source_scopes,
)


SCHEMA = "external_memory_learning_prior.v1"
OWNER = "arch4.external_memory_learning_projection"
SEED_SCHEMA = "external_memory_operational_learning_seed.v1"

_SCOPE_WEIGHTS = {
    "generic": 0.20,
    "backend_family": 0.30,
    "provider": 0.45,
    "model": 0.55,
    "model_firmware": 0.65,
    "provider_model": 0.70,
    "provider_band": 0.75,
    "provider_site": 0.82,
    "provider_cell": 0.88,
    "identity": 0.90,
    "identity_provider_band": 0.95,
    "identity_provider_cell": 1.00,
}

_BAND_RE = re.compile(r"B(\d+)", re.IGNORECASE)


def learning_prior_from_external_memory_pack(pack: Mapping[str, Any]) -> dict[str, Any]:
    """Project approved external-memory facts into learning-quality priors.

    External memory is historical evidence only. This projection may seed
    self-learning and Crystal Ball with weighted priors, but it never overrides
    live truth and never emits a command.
    """

    if not isinstance(pack, Mapping) or pack.get("schema") != "external_memory_prior_pack_v1":
        return _empty_prior("missing_prior_pack")
    try:
        target_scope = validated_external_projection_scope(
            _mapping(pack.get("target_evidence_scope"))
        )
    except (TypeError, ValueError):
        return _empty_prior("target_evidence_scope_invalid")
    facts = [dict(row) for row in pack.get("facts", []) if isinstance(row, Mapping)]
    merged: dict[tuple[int, int], dict[str, float]] = {}
    sources: list[dict[str, Any]] = []
    for fact in facts:
        try:
            fact_target_scope = validated_external_projection_scope(
                _mapping(fact.get("target_evidence_scope"))
            )
        except (TypeError, ValueError):
            continue
        if not fact_target_scope.exact_generation_match(target_scope):
            continue
        source_scopes = validated_external_source_scopes(fact)
        if not source_scopes:
            continue
        source_path = str(
            (fact.get("provenance") or {}).get("source_path")
            or fact.get("source_path")
            or ""
        )
        weight, rejection = _fact_weight(fact)
        seed = _mapping(fact.get("operational_learning_seed"))
        if rejection or seed.get("schema") != SEED_SCHEMA or seed.get("owner") != OWNER:
            continue
        try:
            seed_scope = validated_external_projection_scope(
                _mapping(seed.get("evidence_scope"))
            )
        except (TypeError, ValueError):
            continue
        if not seed_scope.exact_generation_match(target_scope):
            continue
        seed_source_scopes = validated_external_source_scopes(seed)
        if {row.scope_id for row in seed_source_scopes} != {
            row.scope_id for row in source_scopes
        }:
            continue
        if not seed.get("present"):
            continue
        sources.append(
            {
                "source_path": str(Path(source_path)),
                "band_hour_count": len(seed.get("band_hour_quality", {}) or {}),
                "raw_sample_count": int(seed.get("sample_count") or 0),
                "promotion_weight": round(weight, 4),
                "age_days": float(fact.get("age_days") or 0.0),
                "confidence": float(fact.get("confidence") or 0.0),
                "scope_tier": str(fact.get("scope_tier") or ""),
                "source_evidence_scope_ids": [row.scope_id for row in source_scopes],
            }
        )
        _merge_band_hour_scores(
            merged,
            quality=_mapping(seed.get("band_hour_quality")),
            counts=_mapping(seed.get("band_hour_counts")),
            evidence_weight=weight,
        )

    if not merged:
        return _empty_prior("no_operational_learning_prior")
    quality: dict[str, float] = {}
    counts: dict[str, int] = {}
    sample_count = 0
    for (band, hour), row in sorted(merged.items()):
        count = int(row.get("count", 0.0) or 0.0)
        if band <= 0 or hour < 0 or count <= 0:
            continue
        key = f"{band}:{hour}"
        quality[key] = round(float(row.get("weighted", 0.0) or 0.0) / max(1.0, float(count)), 4)
        counts[key] = min(200, count)
        sample_count += count
    if not quality:
        return _empty_prior("no_valid_band_hour_prior")
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "present": True,
        "runtime_influence": "weighted_historical_seed",
        "live_truth_override": False,
        "band_hour_quality": quality,
        "band_hour_counts": counts,
        "sample_count": sample_count,
        "profile_confidence": min(0.85, max(0.15, sample_count / 5000.0)),
        "quality_trend": "external_memory_history_prior",
        "evidence_scope": target_scope.as_dict(),
        "formula_vector": target_scope.formula_vector.as_dict(),
        "sources": sources[:12],
    }


def materialize_external_memory_learning_seed(
    fact: Mapping[str, Any],
    *,
    target_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Read an approved source once, at promotion, and freeze its learning seed.

    Runtime activation must never reopen removable source paths.  The explicit
    promotion boundary validates and materializes the operational facts into
    the signed/atomic prior pack; later refreshes consume only that pack.
    """

    try:
        target_scope = validated_external_projection_scope(
            _mapping(fact.get("target_evidence_scope"))
        )
    except (TypeError, ValueError):
        return _materialized_empty_seed("target_evidence_scope_invalid")
    source_scopes = validated_external_source_scopes(fact)
    if not source_scopes:
        return _materialized_empty_seed("source_evidence_scope_invalid")
    source_path = str(
        (_mapping(fact.get("provenance"))).get("source_path")
        or fact.get("source_path")
        or ""
    ).strip()
    legacy_handoff_binding = bool(
        fact.get("legacy_scope_binding") == "confirmed_handoff_exact_identity"
        and isinstance(fact.get("confirmed_handoff_admission"), Mapping)
        and fact.get("confirmed_handoff_admission", {}).get("admitted") is True
    )
    if not source_path:
        source = _empty_prior("approved_fact_source_path_missing")
    elif legacy_handoff_binding:
        source = _confirmed_handoff_legacy_learning_prior(
            Path(source_path),
            target_identity=target_identity,
        )
    else:
        source = _source_learning_prior(
            Path(source_path),
            target_identity=target_identity,
            source_scopes=source_scopes,
        )
    return {
        "schema": SEED_SCHEMA,
        "owner": OWNER,
        "present": bool(source.get("present")),
        "reason": str(source.get("reason") or ""),
        "band_hour_quality": _mapping(source.get("band_hour_quality")),
        "band_hour_counts": _mapping(source.get("band_hour_counts")),
        "sample_count": int(source.get("sample_count") or 0),
        "evidence_scope": target_scope.as_dict(),
        "formula_vector": target_scope.formula_vector.as_dict(),
        "source_evidence_scopes": [row.as_dict() for row in source_scopes],
        "materialization": (
            "confirmed_handoff_exact_identity_legacy_projection"
            if legacy_handoff_binding
            else "promotion_boundary_source_read_once"
        ),
        "legacy_owner_files_restored": False,
    }


def _materialized_empty_seed(reason: str) -> dict[str, Any]:
    return {
        "schema": SEED_SCHEMA,
        "owner": OWNER,
        "present": False,
        "reason": str(reason or "scoped_source_evidence_unavailable"),
        "band_hour_quality": {},
        "band_hour_counts": {},
        "sample_count": 0,
        "materialization": "promotion_boundary_source_rejected",
    }


def _fact_weight(fact: Mapping[str, Any]) -> tuple[float, str]:
    if str(fact.get("promotion_status") or "") != "approved":
        return 0.0, "fact_not_approved"
    try:
        confidence = float(fact["confidence"])
        age_days = float(fact["age_days"])
    except (KeyError, TypeError, ValueError):
        return 0.0, "promotion_quality_facts_missing"
    scope_tier = str(fact.get("scope_tier") or "").strip()
    scope_weight = _SCOPE_WEIGHTS.get(scope_tier)
    if scope_weight is None:
        return 0.0, "promotion_scope_unknown"
    if not 0.0 < confidence <= 1.0 or not 0.0 <= age_days <= 14.0:
        return 0.0, "promotion_quality_facts_out_of_range"
    age_weight = max(0.10, 1.0 - (age_days / 15.0))
    return max(0.01, min(1.0, confidence * age_weight * scope_weight)), ""


def _source_learning_prior(
    source_path: Path,
    *,
    target_identity: Mapping[str, Any],
    source_scopes: tuple[EvidenceScope, ...],
) -> dict[str, Any]:
    zipped = _zip_memory_payloads(source_path)
    if zipped is not None:
        historical_payloads, memory_payloads = zipped
        merged: dict[tuple[int, int], dict[str, float]] = {}
        for historical in historical_payloads:
            _merge_band_hour_scores(
                merged,
                **_historical_band_hour_quality(
                    historical,
                    target_identity=target_identity,
                    source_scopes=source_scopes,
                ),
            )
        for memory in memory_payloads:
            _merge_band_hour_scores(
                merged,
                **_memory_band_hour_quality(
                    memory,
                    source_scopes=source_scopes,
                ),
            )
        return _finalize_source_prior(merged)
    memory_dir = _memory_dir_for_source(source_path)
    if memory_dir is None:
        return _empty_prior("source_memory_dir_missing")
    merged: dict[tuple[int, int], dict[str, float]] = {}
    historical = _safe_json(memory_dir / "historical.json")
    _merge_band_hour_scores(
        merged,
        **_historical_band_hour_quality(
            historical,
            target_identity=target_identity,
            source_scopes=source_scopes,
        ),
    )
    memory = _safe_json(memory_dir / "memory.json")
    _merge_band_hour_scores(
        merged,
        **_memory_band_hour_quality(memory, source_scopes=source_scopes),
    )
    return _finalize_source_prior(merged)


def _confirmed_handoff_legacy_learning_prior(
    source_path: Path,
    *,
    target_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Project legacy memory only after protected exact-handoff admission.

    This adapter deliberately bypasses missing legacy EvidenceScope envelopes;
    the exact scope is supplied by the validated handoff receipt.  It never
    restores the legacy files or their former owners into the active runtime.
    """

    zipped = _zip_memory_payloads(source_path)
    if zipped is None:
        return _empty_prior("confirmed_handoff_memory_payloads_missing")
    historical_payloads, memory_payloads = zipped
    merged: dict[tuple[int, int], dict[str, float]] = {}
    for historical in historical_payloads:
        _merge_band_hour_scores(
            merged,
            **_legacy_handoff_historical_band_hour_quality(
                historical,
                target_identity=target_identity,
            ),
        )
    for memory in memory_payloads:
        _merge_band_hour_scores(
            merged,
            **external_memory_band_hour_quality(memory),
        )
    return _finalize_source_prior(merged)


def _finalize_source_prior(
    merged: Mapping[tuple[int, int], Mapping[str, float]],
) -> dict[str, Any]:
    if not merged:
        return _empty_prior("source_has_no_learning_prior")
    quality: dict[str, float] = {}
    counts: dict[str, int] = {}
    sample_count = 0
    for (band, hour), row in sorted(merged.items()):
        count = int(row.get("count", 0.0) or 0.0)
        if count <= 0:
            continue
        key = f"{band}:{hour}"
        quality[key] = round(float(row.get("weighted", 0.0) or 0.0) / max(1.0, float(count)), 4)
        counts[key] = min(200, count)
        sample_count += count
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "present": bool(quality),
        "band_hour_quality": quality,
        "band_hour_counts": counts,
        "sample_count": sample_count,
    }


def _zip_memory_payloads(
    source_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    for candidate in _source_path_variants(source_path):
        try:
            if not candidate.is_file() or not zipfile.is_zipfile(candidate):
                continue
            with zipfile.ZipFile(candidate) as bundle:
                historical_names = sorted(
                    name
                    for name in bundle.namelist()
                    if name.replace("\\", "/").endswith("/guc_memory/historical.json")
                    or Path(name).name == "historical.json"
                )
                memory_names = sorted(
                    name
                    for name in bundle.namelist()
                    if name.replace("\\", "/").endswith("/guc_memory/memory.json")
                    or Path(name).name == "memory.json"
                )
                historical = [_safe_zip_json(bundle, name) for name in historical_names]
                memory = [_safe_zip_json(bundle, name) for name in memory_names]
                return (
                    [row for row in historical if row],
                    [row for row in memory if row],
                )
        except (OSError, zipfile.BadZipFile):
            continue
    return None


def _safe_zip_json(bundle: zipfile.ZipFile, name: str) -> dict[str, Any]:
    try:
        raw = json.loads(bundle.read(name).decode("utf-8", errors="replace"))
    except Exception:
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _historical_band_hour_quality(
    historical: Mapping[str, Any],
    *,
    target_identity: Mapping[str, Any],
    source_scopes: tuple[EvidenceScope, ...],
) -> dict[str, dict[str, float]]:
    if not isinstance(historical, Mapping):
        return {"quality": {}, "counts": {}}
    try:
        scope = validated_evidence_scope(_mapping(historical.get("evidence_scope")))
    except (TypeError, ValueError):
        return {"quality": {}, "counts": {}}
    if scope.formula_vector != historical_formula_vector() or not any(
        row.same_physical_namespace(scope) for row in source_scopes
    ):
        return {"quality": {}, "counts": {}}
    return _historical_payload_band_hour_quality(
        historical,
        target_identity=target_identity,
    )


def _legacy_handoff_historical_band_hour_quality(
    historical: Mapping[str, Any],
    *,
    target_identity: Mapping[str, Any],
) -> dict[str, dict[str, float]]:
    if not isinstance(historical, Mapping):
        return {"quality": {}, "counts": {}}
    return _historical_payload_band_hour_quality(
        historical,
        target_identity=target_identity,
    )


def _historical_payload_band_hour_quality(
    historical: Mapping[str, Any],
    *,
    target_identity: Mapping[str, Any],
) -> dict[str, dict[str, float]]:
    provider_plmn = str(target_identity.get("provider_plmn") or "").strip()
    namespaces: list[Mapping[str, Any]] = []
    providers = historical.get("providers") if isinstance(historical.get("providers"), Mapping) else {}
    if provider_plmn and isinstance(providers.get(provider_plmn), Mapping):
        namespaces.append(providers[provider_plmn])
    namespaces.append(historical)
    raw: dict[tuple[int, int], dict[str, float]] = {}
    hour_max: dict[int, float] = {}
    for namespace in namespaces:
        band_perf = namespace.get("band_perf") if isinstance(namespace, Mapping) else {}
        if not isinstance(band_perf, Mapping):
            continue
        for hour_raw, perf in band_perf.items():
            try:
                hour = int(hour_raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(perf, Mapping):
                continue
            for signature, values in perf.items():
                band = _anchor_band_from_signature(signature)
                if band <= 0:
                    continue
                try:
                    total, count = values
                    total_f = float(total or 0.0)
                    count_i = int(count or 0)
                except Exception:
                    continue
                if count_i < 3 or total_f <= 0.0:
                    continue
                avg = total_f / max(1, count_i)
                key = (band, hour)
                row = raw.setdefault(key, {"total": 0.0, "count": 0.0})
                row["total"] += total_f
                row["count"] += float(count_i)
                hour_max[hour] = max(hour_max.get(hour, 0.0), avg)
    quality: dict[str, float] = {}
    counts: dict[str, int] = {}
    for (band, hour), row in raw.items():
        count = int(row.get("count", 0.0) or 0.0)
        avg = float(row.get("total", 0.0) or 0.0) / max(1, count)
        best = max(0.0, float(hour_max.get(hour, 0.0) or 0.0))
        if best <= 0.0:
            continue
        score = 0.25 + (0.70 * max(0.0, min(1.0, avg / best)))
        if best < 0.20:
            score = min(score, 0.60)
        quality[f"{band}:{hour}"] = round(max(0.0, min(0.95, score)), 4)
        counts[f"{band}:{hour}"] = min(200, count)
    return {"quality": quality, "counts": counts}


def _memory_band_hour_quality(
    memory: Mapping[str, Any],
    *,
    source_scopes: tuple[EvidenceScope, ...],
) -> dict[str, dict[str, float]]:
    if memory_snapshot_scope_error(memory.get("evidence_scope")):
        return {"quality": {}, "counts": {}}
    try:
        scope = validated_evidence_scope(_mapping(memory.get("evidence_scope")))
    except (TypeError, ValueError):
        return {"quality": {}, "counts": {}}
    if scope.formula_vector != memory_formula_vector() or not any(
        row.same_physical_namespace(scope) for row in source_scopes
    ):
        return {"quality": {}, "counts": {}}
    return external_memory_band_hour_quality(memory)


def _merge_band_hour_scores(
    merged: dict[tuple[int, int], dict[str, float]],
    *,
    quality: Mapping[str, Any],
    counts: Mapping[str, Any],
    evidence_weight: float = 1.0,
) -> None:
    for key, raw_score in quality.items():
        band, hour = _split_band_hour(key)
        if band <= 0 or hour < 0:
            continue
        raw_count = max(1, _int(counts.get(key), 1))
        count = max(1, int(round(raw_count * max(0.0, min(1.0, evidence_weight)))))
        score = max(0.0, min(1.0, _float(raw_score, 0.0)))
        row = merged.setdefault((band, hour), {"weighted": 0.0, "count": 0.0})
        row["weighted"] += score * count
        row["count"] += float(count)


def _memory_dir_for_source(source_path: Path) -> Path | None:
    candidates: list[Path] = []
    for path in _source_path_variants(source_path):
        candidates.extend(
            [
                path / "portable_runtime" / "guc_memory",
                path / "guc_memory",
                path.parent / "guc_memory",
            ]
        )
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_dir():
            return candidate
    return None


def _source_path_variants(source_path: Path) -> tuple[Path, ...]:
    text = str(source_path)
    variants = [source_path]
    drive_match = re.match(r"^([A-Za-z]):[\\/](.+)$", text)
    if drive_match and os.name != "nt":
        drive = drive_match.group(1).lower()
        rest = drive_match.group(2).replace("\\", "/")
        variants.insert(0, Path(f"/mnt/{drive}/{rest}"))
    mnt_match = re.match(r"^/mnt/([A-Za-z])/(.+)$", text)
    if mnt_match and os.name == "nt":
        drive = mnt_match.group(1).upper()
        rest = mnt_match.group(2).replace("/", "\\")
        variants.insert(0, Path(f"{drive}:\\{rest}"))
    return tuple(variants)


def _safe_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _anchor_band_from_signature(value: Any) -> int:
    match = _BAND_RE.search(str(value or ""))
    if not match:
        return 0
    return _int(match.group(1), 0)


def _split_band_hour(value: Any) -> tuple[int, int]:
    parts = str(value or "").split(":", 1)
    if len(parts) != 2:
        return 0, -1
    return _int(parts[0], 0), _int(parts[1], -1)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _empty_prior(reason: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "present": False,
        "runtime_influence": "none",
        "live_truth_override": False,
        "reason": str(reason or "no_prior"),
        "band_hour_quality": {},
        "band_hour_counts": {},
        "sample_count": 0,
        "profile_confidence": 0.0,
    }


__all__ = [
    "OWNER",
    "SCHEMA",
    "SEED_SCHEMA",
    "learning_prior_from_external_memory_pack",
    "materialize_external_memory_learning_seed",
]
