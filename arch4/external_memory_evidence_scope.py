"""EvidenceScope contract for external-memory promotion and activation."""

from __future__ import annotations

from typing import Any, Mapping

from arch4.evidence_scope import (
    EvidenceScope,
    FormulaVector,
    evidence_scope_from_mapping,
    validated_evidence_scope,
)
from arch4.interfaces import ARCH_QUALITY_FORMULA_EPOCH
from arch4.telemetry.history_contract import telemetry_formula_vector


OWNER = "arch4.external_memory_evidence_scope"
EXTERNAL_MEMORY_FORMULA_EPOCH = "external_memory_weighted_prior_v1"


def external_memory_formula_vector() -> FormulaVector:
    return FormulaVector.build(
        {
            "external_memory_projection": EXTERNAL_MEMORY_FORMULA_EPOCH,
            "quality": str(ARCH_QUALITY_FORMULA_EPOCH or ""),
        }
    )


def external_projection_scope(
    target: Mapping[str, Any],
    *,
    evidence_count: int,
) -> EvidenceScope:
    raw_scope = (
        target.get("evidence_scope")
        if isinstance(target.get("evidence_scope"), Mapping)
        else target
    )
    try:
        source = validated_evidence_scope(raw_scope)
    except (TypeError, ValueError):
        source = evidence_scope_from_mapping(
            raw_scope,
            formula_vector=external_memory_formula_vector(),
            evidence_count=max(1, int(evidence_count or 1)),
        )
    return EvidenceScope(
        identity_code=source.identity_code,
        identity_status=source.identity_status,
        identity_catalog_version=source.identity_catalog_version,
        identity_catalog_checksum=source.identity_catalog_checksum,
        session_scope_key=source.session_scope_key,
        source_epoch=source.source_epoch,
        source_sequence=source.source_sequence,
        observed_at=source.observed_at,
        evidence_age_s=source.evidence_age_s,
        confidence=source.confidence,
        evidence_count=max(1, int(evidence_count or 1)),
        formula_vector=external_memory_formula_vector(),
        portability=source.portability,
        access_type_id=source.access_type_id,
        model_family_id=source.model_family_id,
        provider_plmn=source.provider_plmn,
        sim_scope_hash=source.sim_scope_hash,
        site_scope_id=source.site_scope_id,
        cell_scope_key=source.cell_scope_key,
        serving_band=source.serving_band,
    )


def validated_external_projection_scope(raw: Mapping[str, Any]) -> EvidenceScope:
    scope = validated_evidence_scope(raw)
    if scope.formula_vector != external_memory_formula_vector():
        raise ValueError("external_memory_formula_vector_mismatch")
    return scope


def validated_external_source_scopes(source: Mapping[str, Any]) -> tuple[EvidenceScope, ...]:
    coverage = source.get("coverage") if isinstance(source.get("coverage"), Mapping) else {}
    raw_scopes = (
        source.get("source_evidence_scopes")
        if isinstance(source.get("source_evidence_scopes"), list)
        else coverage.get("evidence_scopes")
        if isinstance(coverage.get("evidence_scopes"), list)
        else []
    )
    admitted: list[EvidenceScope] = []
    seen: set[str] = set()
    for raw in raw_scopes:
        try:
            scope = validated_evidence_scope(raw if isinstance(raw, Mapping) else {})
        except (TypeError, ValueError):
            continue
        epochs = dict(scope.formula_vector.epochs)
        if scope.formula_vector != telemetry_formula_vector() and "crystal_ball" not in epochs:
            continue
        if scope.scope_id in seen:
            continue
        seen.add(scope.scope_id)
        admitted.append(scope)
    return tuple(admitted)


__all__ = [
    "EXTERNAL_MEMORY_FORMULA_EPOCH",
    "OWNER",
    "external_memory_formula_vector",
    "external_projection_scope",
    "validated_external_projection_scope",
    "validated_external_source_scopes",
]
