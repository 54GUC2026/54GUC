from __future__ import annotations

import json
import time
from argparse import ArgumentParser
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from arch4.atomic_io import atomic_write_json
from arch4.evidence_scope import EvidenceScope
from arch4.external_memory_identity_recoder import recode_external_memory_payload, recode_identity_mapping
from arch4.external_memory_evidence_scope import (
    external_projection_scope,
    validated_external_projection_scope,
    validated_external_source_scopes,
)
from arch4.external_memory_learning_projection import (
    materialize_external_memory_learning_seed,
)
from arch4.modem_live.compatibility_identity import UNKNOWN_IDENTITY_CODE
from arch4.portable_state_import import (
    validate_confirmed_handoff_continuity_receipt,
)
from arch4.telemetry.history_contract import telemetry_formula_vector


PROMOTION_SCHEMA = "external_memory_promotion_v1"
PRIOR_PACK_SCHEMA = "external_memory_prior_pack_v1"
IMPORT_MARKER_SCHEMA = "external_memory_prior_import_marker_v1"
DEFAULT_PRIOR_PACK_NAME = "external_memory_promoted_priors.json"
DEFAULT_IMPORT_MARKER_NAME = "external_memory_import_marker.json"


def _safe_json_load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload, indent=2, sort_keys=True, trailing_newline=True)


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now_dt(now_ts: float | None = None) -> datetime:
    if now_ts is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(float(now_ts), tz=timezone.utc)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _identity_from_target_payload(payload: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key in ("runtime_identity", "identity", "resolved_identity_card", "modem_identity"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            merged.update({str(k): v for k, v in inner.items()})
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
        "profile_id",
        "resolved_profile_id",
        "model",
        "firmware",
        "hardware_version",
        "software_version",
        "webui",
        "provider_name",
        "provider_plmn",
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
            merged.setdefault(key, value)
    catalog = payload.get("decode_catalog_snapshot")
    if not isinstance(catalog, dict):
        for key in ("runtime_identity", "identity", "modem_identity"):
            inner = payload.get(key)
            if isinstance(inner, dict) and isinstance(inner.get("decode_catalog_snapshot"), dict):
                catalog = inner.get("decode_catalog_snapshot")
                break
    cleaned = {key: value for key, value in merged.items() if value not in ("", None)}
    if isinstance(catalog, dict):
        cleaned["decode_catalog_snapshot"] = dict(catalog)
    evidence_scope = payload.get("evidence_scope")
    if isinstance(evidence_scope, dict):
        cleaned["evidence_scope"] = dict(evidence_scope)
    return recode_identity_mapping(
        cleaned,
        decode_catalog_snapshot=catalog if isinstance(catalog, dict) else None,
    )


def load_target_identity(path_or_json: str | Path | None) -> dict[str, Any]:
    if path_or_json is None:
        return {}
    text = str(path_or_json or "").strip()
    if not text:
        return {}
    path = Path(text)
    if path.exists():
        return _identity_from_target_payload(_safe_json_load(path))
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return _identity_from_target_payload(data if isinstance(data, dict) else {})


def _field_conflict(source: dict[str, Any], target: dict[str, Any], key: str) -> bool:
    source_value = _clean_text(source.get(key)).lower()
    target_value = _clean_text(target.get(key)).lower()
    return bool(source_value and target_value and source_value != target_value)


def _provider_conflict(source: dict[str, Any], target: dict[str, Any]) -> bool:
    if _field_conflict(source, target, "provider_plmn"):
        return True
    source_plmn = _clean_text(source.get("provider_plmn"))
    target_plmn = _clean_text(target.get("provider_plmn"))
    if source_plmn or target_plmn:
        return False
    return _field_conflict(source, target, "provider_name")


def _scope_from_identity(identity: dict[str, Any]) -> dict[str, Any]:
    identity_code = _clean_text(identity.get("identity_code"))
    if identity_code == UNKNOWN_IDENTITY_CODE:
        identity_code = ""
    scope = {
        "identity_code": identity_code,
        "identity_status": _clean_text(identity.get("identity_status")),
        "backend": _clean_text(identity.get("backend")),
        "backend_family": _clean_text(identity.get("backend_family")),
        "model": _clean_text(identity.get("model")),
        "firmware": _clean_text(identity.get("firmware")),
        "provider_name": _clean_text(identity.get("provider_name")),
        "provider_plmn": _clean_text(identity.get("provider_plmn")),
        "selected_band_set_signature": _clean_text(identity.get("selected_band_set_signature")),
        "active_band_set_signature": _clean_text(identity.get("active_band_set_signature")),
        "ca_signature": _clean_text(identity.get("ca_signature")),
        "serving_band": identity.get("serving_band"),
        "primary_band": identity.get("primary_band"),
        "pci": identity.get("pci"),
        "earfcn": identity.get("earfcn"),
        "cell_id": identity.get("cell_id"),
    }
    return {key: value for key, value in scope.items() if value not in ("", None)}


def _scope_tier(scope: dict[str, Any]) -> str:
    if scope.get("identity_code") and scope.get("provider_plmn") and scope.get("serving_band") and scope.get("earfcn") and scope.get("pci"):
        return "identity_provider_cell"
    if scope.get("identity_code") and scope.get("provider_plmn") and scope.get("serving_band"):
        return "identity_provider_band"
    if scope.get("identity_code"):
        return "identity"
    if scope.get("provider_plmn") and scope.get("serving_band") and scope.get("earfcn") and scope.get("pci"):
        return "provider_cell"
    if scope.get("provider_plmn") and scope.get("serving_band") and scope.get("earfcn"):
        return "provider_site"
    if scope.get("provider_plmn") and scope.get("serving_band"):
        return "provider_band"
    if scope.get("provider_plmn") and scope.get("model"):
        return "provider_model"
    if scope.get("model") and scope.get("firmware"):
        return "model_firmware"
    if scope.get("model"):
        return "model"
    if scope.get("provider_plmn"):
        return "provider"
    if scope.get("backend_family"):
        return "backend_family"
    return "generic"


def _confidence_score(source: dict[str, Any], age_days: float | None) -> float:
    coverage = source.get("coverage") if isinstance(source.get("coverage"), dict) else {}
    identity = source.get("identity") if isinstance(source.get("identity"), dict) else {}
    telemetry_rows = max(0, int(coverage.get("telemetry_rows") or 0))
    score = 0.15
    if identity.get("backend"):
        score += 0.08
    if identity.get("model"):
        score += 0.12
    if identity.get("firmware"):
        score += 0.08
    if identity.get("provider_plmn") or identity.get("provider_name"):
        score += 0.12
    score += min(0.22, telemetry_rows / 50000.0 * 0.22)
    # Event rows remain operator context until they carry their own exact
    # EvidenceScope contract; they must not increase learning confidence.
    if age_days is not None:
        if age_days <= 2:
            score += 0.15
        elif age_days <= 7:
            score += 0.10
        elif age_days <= 14:
            score += 0.05
    return round(max(0.0, min(1.0, score)), 3)


def _confidence_label(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.50:
        return "medium"
    return "low"


def evaluate_external_memory_source(
    source: dict[str, Any],
    *,
    target_identity: dict[str, Any] | None = None,
    now_ts: float | None = None,
    min_telemetry_rows: int = 100,
    max_age_days: int = 14,
) -> dict[str, Any]:
    target = target_identity if isinstance(target_identity, dict) else {}
    catalog = (
        target.get("decode_catalog_snapshot")
        if isinstance(target.get("decode_catalog_snapshot"), dict)
        else None
    )
    source = recode_external_memory_payload(source, decode_catalog_snapshot=catalog)
    identity = source.get("identity") if isinstance(source.get("identity"), dict) else {}
    coverage = source.get("coverage") if isinstance(source.get("coverage"), dict) else {}
    version = source.get("version") if isinstance(source.get("version"), dict) else {}
    decoded_catalogs = [
        dict(row)
        for row in source.get("decoded_identity_catalogs", []) or []
        if isinstance(row, dict)
    ]
    source_scope_spans = [
        dict(row)
        for row in coverage.get("evidence_scope_spans", []) or []
        if isinstance(row, dict)
    ]
    reasons: list[str] = []
    contamination_notes: list[str] = []
    telemetry_rows = max(0, int(coverage.get("telemetry_rows") or 0))
    event_rows = max(0, int(coverage.get("event_rows") or 0))
    quarantined_rows = max(0, int(coverage.get("evidence_quarantined_rows") or 0))
    source_scopes = validated_external_source_scopes(source)
    handoff_admission: dict[str, Any] = {}
    raw_handoff_receipt = source.get("confirmed_handoff_receipt")
    if isinstance(raw_handoff_receipt, dict):
        handoff_admission = dict(
            validate_confirmed_handoff_continuity_receipt(
                raw_handoff_receipt,
                target_identity=target,
                now_ts=now_ts,
                max_age_days=max_age_days,
            )
        )
        if handoff_admission.get("admitted") is True:
            try:
                source_scopes = (
                    replace(
                        EvidenceScope.from_mapping(
                        dict(
                            handoff_admission.get("confirmed_evidence_scope")
                            or {}
                        ),
                        ),
                        formula_vector=telemetry_formula_vector(),
                        evidence_count=max(
                            1,
                            int(
                                (source.get("assets") or {}).get(
                                    "memory_files", 0
                                )
                                or 0
                            ),
                        ),
                    ),
                )
            except (TypeError, ValueError):
                reasons.append("confirmed handoff EvidenceScope invalid")
                source_scopes = ()
        else:
            reasons.extend(
                str(reason)
                for reason in handoff_admission.get("rejection_reasons", [])
                if str(reason or "").strip()
            )
    elif source.get("source_type") == "next_start_handoff":
        reasons.append("next-start handoff lacks confirmed-login admission receipt")
    confirmed_handoff_admitted = handoff_admission.get("admitted") is True
    target_scope = None
    try:
        target_scope = external_projection_scope(
            target,
            evidence_count=max(1, telemetry_rows),
        )
    except (TypeError, ValueError):
        reasons.append("target exact identity/session/generation EvidenceScope unavailable")
    end_dt = _parse_iso(coverage.get("telemetry_end_iso"))
    age_days: float | None = None
    if confirmed_handoff_admitted:
        try:
            age_days = float(handoff_admission.get("age_days"))
        except (TypeError, ValueError):
            age_days = None
    elif end_dt is not None:
        age_days = max(0.0, (_now_dt(now_ts) - end_dt).total_seconds() / 86400.0)

    if source.get("runtime_influence") != "none":
        reasons.append("source already has runtime influence")
    if source.get("import_status") not in ("candidate_prior", "promoted_prior", None):
        reasons.append("source import status is not a prior candidate")
    if (
        not confirmed_handoff_admitted
        and telemetry_rows < max(0, int(min_telemetry_rows))
    ):
        reasons.append("insufficient telemetry rows")
    if not source_scopes:
        reasons.append("source has no exact scoped telemetry evidence")
    if quarantined_rows:
        contamination_notes.append(
            f"{quarantined_rows} unscoped or invalid telemetry rows quarantined"
        )
    if not (identity.get("backend") or identity.get("model")):
        reasons.append("missing modem identity")
    if not (identity.get("provider_plmn") or identity.get("provider_name")):
        contamination_notes.append("provider identity missing; provider-scoped promotion unavailable")
    if max_age_days > 0:
        if age_days is None:
            reasons.append("missing telemetry end timestamp")
        elif age_days > float(max_age_days):
            reasons.append("source is older than promotion age window")

    for key in ("backend", "model"):
        if _field_conflict(identity, target, key):
            reasons.append(f"target {key} mismatch")
    if _provider_conflict(identity, target):
        reasons.append("target provider mismatch")
    if _field_conflict(identity, target, "firmware"):
        contamination_notes.append("firmware differs from target; firmware-specific facts require lower weight")

    if source_scopes:
        physical_namespaces = {
            (
                row.identity_code,
                row.identity_catalog_version,
                row.identity_catalog_checksum,
            )
            for row in source_scopes
        }
        if len(physical_namespaces) != 1:
            reasons.append("source contains mixed exact modem identity/catalog namespaces")
        source_scope = source_scopes[0]
        if decoded_catalogs and not any(
            str(row.get("catalog_checksum") or "")
            == source_scope.identity_catalog_checksum
            and str(row.get("catalog_version") or "")
            == source_scope.identity_catalog_version
            for row in decoded_catalogs
        ):
            reasons.append("source decoded identity catalog reference mismatch")
        identity["identity_code"] = source_scope.identity_code
        identity["identity_status"] = source_scope.identity_status
        identity["identity_catalog_version"] = source_scope.identity_catalog_version
        identity["identity_catalog_checksum"] = source_scope.identity_catalog_checksum
        identity.pop("identity_quarantine_reason", None)
        identity.setdefault("provider_plmn", source_scope.provider_plmn)
        if target_scope is not None:
            if source_scope.identity_code != target_scope.identity_code:
                reasons.append("target exact modem identity mismatch")
            if (
                source_scope.identity_catalog_version
                != target_scope.identity_catalog_version
                or source_scope.identity_catalog_checksum
                != target_scope.identity_catalog_checksum
            ):
                reasons.append("target decoded identity catalog mismatch")
            if (
                source_scope.provider_plmn
                and target_scope.provider_plmn
                and source_scope.provider_plmn != target_scope.provider_plmn
            ):
                reasons.append("target provider mismatch")

    scope = _scope_from_identity(identity)
    score = _confidence_score(source, age_days)
    admitted_memory_units = max(
        1,
        int((source.get("assets") or {}).get("memory_files") or 0),
    ) if confirmed_handoff_admitted else telemetry_rows
    status = "approved" if not reasons else "rejected"
    return {
        "schema": "external_memory_source_promotion_v1",
        "source_path": _clean_text(source.get("source_path")),
        "source_name": _clean_text(source.get("source_name")),
        "source_type": _clean_text(source.get("source_type")),
        "promotion_status": status,
        "runtime_influence": "advisory_prior" if status == "approved" else "none",
        "authority": (
            "confirmed_handoff_exact_identity_adapter"
            if status == "approved" and confirmed_handoff_admitted
            else "explicit_promotion_gate"
            if status == "approved"
            else "offline_evidence_only"
        ),
        "rejection_reasons": reasons,
        "contamination_notes": contamination_notes,
        "identity": dict(identity),
        "source_version": dict(version),
        "decoded_identity_catalogs": decoded_catalogs,
        "scope": scope,
        "scope_tier": _scope_tier(scope),
        "observation_count": admitted_memory_units,
        "telemetry_rows": telemetry_rows,
        "event_rows": event_rows,
        "event_rows_disposition": "context_only_not_learning_evidence",
        "source_evidence_scopes": [row.as_dict() for row in source_scopes],
        "source_evidence_scope_spans": source_scope_spans,
        "source_evidence_scope_count": len(source_scopes),
        "confirmed_handoff_admission": handoff_admission,
        "legacy_scope_binding": (
            "confirmed_handoff_exact_identity"
            if confirmed_handoff_admitted
            else ""
        ),
        "evidence_quarantined_rows": quarantined_rows,
        "coverage": {
            "telemetry_start_iso": _clean_text(coverage.get("telemetry_start_iso")),
            "telemetry_end_iso": _clean_text(coverage.get("telemetry_end_iso")),
            "event_start_iso": _clean_text(coverage.get("event_start_iso")),
            "event_end_iso": _clean_text(coverage.get("event_end_iso")),
            "evidence_scopes": [row.as_dict() for row in source_scopes],
            "evidence_scope_spans": source_scope_spans,
            "evidence_quarantined_rows": quarantined_rows,
        },
        "age_days": None if age_days is None else round(age_days, 3),
        "confidence": score,
        "confidence_label": _confidence_label(score),
        "provenance": {
            "source_manifest_schema": _clean_text(source.get("schema")),
            "source_path": _clean_text(source.get("source_path")),
            "source_type": _clean_text(source.get("source_type")),
            "source_version": dict(version),
            "decoded_identity_catalogs": decoded_catalogs,
        },
    }


def build_promotion_plan(
    manifest: dict[str, Any],
    *,
    target_identity: dict[str, Any] | None = None,
    now_ts: float | None = None,
    min_telemetry_rows: int = 100,
    max_age_days: int = 14,
) -> dict[str, Any]:
    sources = manifest.get("sources") if isinstance(manifest.get("sources"), list) else []
    evaluated = [
        evaluate_external_memory_source(
            src if isinstance(src, dict) else {},
            target_identity=target_identity or {},
            now_ts=now_ts,
            min_telemetry_rows=min_telemetry_rows,
            max_age_days=max_age_days,
        )
        for src in sources
    ]
    approved = [item for item in evaluated if item.get("promotion_status") == "approved"]
    rejected = [item for item in evaluated if item.get("promotion_status") != "approved"]
    approved_observation_count = sum(
        int(item.get("observation_count") or 0) for item in approved
    )
    target_scope: dict[str, Any] = {}
    try:
        target_scope = external_projection_scope(
            target_identity or {},
            evidence_count=max(1, approved_observation_count),
        ).as_dict()
    except (TypeError, ValueError):
        target_scope = {}
    return {
        "schema": PROMOTION_SCHEMA,
        "generated_at_ts": float(time.time() if now_ts is None else now_ts),
        "runtime_influence": "none",
        "authority": "explicit_promotion_gate",
        "target_identity": dict(target_identity or {}),
        "target_evidence_scope": target_scope,
        "rules": {
            "min_telemetry_rows": max(0, int(min_telemetry_rows)),
            "max_age_days": max(0, int(max_age_days)),
            "source_runtime_influence_required": "none",
            "cross_model_provider_conflicts": "reject",
            "unscoped_or_foreign_rows": "quarantine_and_exclude",
            "decoded_identity_catalog": "read_only_evidence",
        },
        "summary": {
            "source_count": len(evaluated),
            "approved_count": len(approved),
            "rejected_count": len(rejected),
            "approved_observation_count": approved_observation_count,
            "quarantined_evidence_rows": sum(
                int(item.get("evidence_quarantined_rows") or 0)
                for item in evaluated
            ),
        },
        "approved": approved,
        "rejected": rejected,
    }


def build_prior_pack(promotion_plan: dict[str, Any]) -> dict[str, Any]:
    approved = promotion_plan.get("approved") if isinstance(promotion_plan.get("approved"), list) else []
    target = promotion_plan.get("target_identity") if isinstance(promotion_plan.get("target_identity"), dict) else {}
    catalog = target.get("decode_catalog_snapshot") if isinstance(target.get("decode_catalog_snapshot"), dict) else None
    target_scope = validated_external_projection_scope(
        promotion_plan.get("target_evidence_scope")
        if isinstance(promotion_plan.get("target_evidence_scope"), dict)
        else {}
    )
    facts: list[dict[str, Any]] = []
    for item in approved:
        if not isinstance(item, dict):
            continue
        source_scopes = validated_external_source_scopes(item)
        if not source_scopes:
            continue
        fact = recode_external_memory_payload(item, decode_catalog_snapshot=catalog)
        source_scope = source_scopes[0]
        for key in ("identity", "scope"):
            scoped_identity = (
                dict(fact.get(key)) if isinstance(fact.get(key), dict) else {}
            )
            scoped_identity.update(
                {
                    "identity_code": source_scope.identity_code,
                    "identity_status": source_scope.identity_status,
                    "identity_catalog_version": source_scope.identity_catalog_version,
                    "identity_catalog_checksum": source_scope.identity_catalog_checksum,
                }
            )
            scoped_identity.pop("identity_quarantine_reason", None)
            fact[key] = scoped_identity
        fact["source_evidence_scopes"] = [
            row.as_dict() for row in source_scopes
        ]
        fact["target_evidence_scope"] = target_scope.as_dict()
        fact["operational_learning_seed"] = materialize_external_memory_learning_seed(
            fact,
            target_identity=target,
        )
        facts.append(fact)
    return {
        "schema": PRIOR_PACK_SCHEMA,
        "generated_at_ts": float(promotion_plan.get("generated_at_ts") or time.time()),
        "runtime_influence": "advisory_prior",
        "authority": "imported_prior_advisory",
        "target_identity": recode_identity_mapping(
            target,
            decode_catalog_snapshot=catalog,
        ),
        "target_evidence_scope": target_scope.as_dict(),
        "formula_vector": target_scope.formula_vector.as_dict(),
        "allowed_consumers": [
            "operator_diagnostics",
            "self_learning_prior",
            "self_calibration_advisory",
            "crystal_ball_advisory",
            "ngui_chart_history",
        ],
        "disallowed_consumers": [
            "live_truth",
            "direct_modem_actuation",
            "support_tier_promotion",
        ],
        "summary": {
            "fact_count": len(facts),
            "observation_count": sum(int(item.get("observation_count") or 0) for item in facts),
            "scope_tiers": sorted({str(item.get("scope_tier") or "") for item in facts if item.get("scope_tier")}),
        },
        "facts": facts,
    }


def write_promotion_plan(
    manifest_path: str | Path,
    output_path: str | Path,
    *,
    target_identity: dict[str, Any] | None = None,
    now_ts: float | None = None,
    min_telemetry_rows: int = 100,
    max_age_days: int = 14,
) -> Path:
    manifest = _safe_json_load(Path(manifest_path).resolve())
    plan = build_promotion_plan(
        manifest,
        target_identity=target_identity,
        now_ts=now_ts,
        min_telemetry_rows=min_telemetry_rows,
        max_age_days=max_age_days,
    )
    path = Path(output_path).resolve()
    _write_json_atomic(path, plan)
    return path


def import_promoted_prior_pack(
    promotion_plan_path: str | Path,
    memory_dir: str | Path,
    *,
    pack_name: str = DEFAULT_PRIOR_PACK_NAME,
) -> Path:
    plan_path = Path(promotion_plan_path).resolve()
    plan = _safe_json_load(plan_path)
    if plan.get("schema") != PROMOTION_SCHEMA:
        raise ValueError(f"Not an external memory promotion plan: {plan_path}")
    pack = build_prior_pack(plan)
    if int(pack.get("summary", {}).get("fact_count") or 0) <= 0:
        raise ValueError("Promotion plan has no approved facts to import.")
    target_dir = Path(memory_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    pack_path = target_dir / pack_name
    marker_path = target_dir / DEFAULT_IMPORT_MARKER_NAME
    _write_json_atomic(pack_path, pack)
    _write_json_atomic(
        marker_path,
        {
            "schema": IMPORT_MARKER_SCHEMA,
            "imported_at_ts": float(time.time()),
            "runtime_influence": "advisory_prior",
            "authority": "explicit_promotion_gate",
            "promotion_plan": str(plan_path),
            "prior_pack": str(pack_path),
            "fact_count": int(pack.get("summary", {}).get("fact_count") or 0),
            "observation_count": int(pack.get("summary", {}).get("observation_count") or 0),
            "target_identity": dict(pack.get("target_identity") or {}),
            "target_evidence_scope": dict(pack.get("target_evidence_scope") or {}),
            "formula_vector": dict(pack.get("formula_vector") or {}),
            "live_truth_override": False,
        },
    )
    return pack_path


def _read_manifest(path: str | Path) -> dict[str, Any]:
    manifest = _safe_json_load(Path(path).resolve())
    if manifest.get("schema") != "external_memory_library_v1":
        raise ValueError(f"Not an external memory library manifest: {path}")
    return manifest


def promote_manifest(
    manifest_path: str | Path,
    *,
    target_identity: dict[str, Any] | None = None,
    output_path: str | Path | None = None,
    memory_dir: str | Path | None = None,
    now_ts: float | None = None,
    min_telemetry_rows: int = 100,
    max_age_days: int = 14,
) -> dict[str, Any]:
    manifest_path = Path(manifest_path).resolve()
    manifest = _read_manifest(manifest_path)
    plan = build_promotion_plan(
        manifest,
        target_identity=target_identity,
        now_ts=now_ts,
        min_telemetry_rows=min_telemetry_rows,
        max_age_days=max_age_days,
    )
    if output_path is None:
        output_path = manifest_path.parent / "external_memory_promotion_plan.json"
    plan_path = Path(output_path).resolve()
    _write_json_atomic(plan_path, plan)
    imported_path = ""
    if memory_dir is not None and int(plan["summary"]["approved_count"]) > 0:
        imported_path = str(import_promoted_prior_pack(plan_path, memory_dir))
    return {
        "promotion_plan": str(plan_path),
        "imported_prior_pack": imported_path,
        "summary": dict(plan.get("summary") or {}),
    }


def _format_summary(result: dict[str, Any]) -> str:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    return json.dumps(
        {
            "promotion_plan": result.get("promotion_plan"),
            "imported_prior_pack": result.get("imported_prior_pack"),
            "approved_count": summary.get("approved_count"),
            "rejected_count": summary.get("rejected_count"),
            "approved_observation_count": summary.get("approved_observation_count"),
        },
        indent=2,
        sort_keys=True,
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = ArgumentParser(description="Promote/import external memory evidence into advisory prior packs.")
    parser.add_argument("--manifest", required=True, help="external_memory_manifest.json to evaluate.")
    parser.add_argument("--target-identity", default="", help="current_state/status JSON path or JSON object.")
    parser.add_argument("--out", default="", help="Promotion-plan output path.")
    parser.add_argument("--memory-dir", default="", help="Optional guc_memory dir for explicit advisory-prior import.")
    parser.add_argument("--min-telemetry-rows", type=int, default=100)
    parser.add_argument("--max-age-days", type=int, default=14)
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = promote_manifest(
        args.manifest,
        target_identity=load_target_identity(args.target_identity),
        output_path=args.out or None,
        memory_dir=args.memory_dir or None,
        min_telemetry_rows=args.min_telemetry_rows,
        max_age_days=args.max_age_days,
    )
    print(_format_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
