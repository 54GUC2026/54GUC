"""Delivered-load-speed-test memory contract.

DLST is nullable evidence attached to memory records that can influence future
band/cell/pool ranking.  It is populated only from controlled speed-test
capacity evidence; ordinary live traffic is not accepted as proof.
"""

from __future__ import annotations

from typing import Any, Mapping

from arch4.controller_frames import (
    CellularIntelligenceGeneration,
    DerivedSignalFrames,
)
from arch4.evidence_scope import (
    EvidenceScope,
    FormulaVector,
    evidence_scope_from_mapping,
    validated_evidence_scope,
)
from arch4.interfaces import ARCH_QUALITY_FORMULA_EPOCH, band_signature, normalize_band_list


DLST_SCHEMA_VERSION = "dlst_memory_v1"
DLST_OWNER = "brain.delivered_load_memory"
DLST_MISSING_EVIDENCE = "controlled_speedtest_dl_samples"
DLST_FORMULA_EPOCH = "controlled_speedtest_best_of_3_v1"


def dlst_formula_vector() -> FormulaVector:
    return FormulaVector.build(
        {
            "delivered_capacity": DLST_FORMULA_EPOCH,
            "quality": str(ARCH_QUALITY_FORMULA_EPOCH or ""),
        }
    )


def dlst_evidence_scope_from_modem(
    modem: CellularIntelligenceGeneration,
    *,
    provider_plmn: str = "",
    evidence_count: int = 1,
) -> EvidenceScope:
    return evidence_scope_from_mapping(
        {
            "identity_code": str(getattr(modem.identity, 'identity_code', "") or ""),
            "identity_status": str(getattr(modem.identity, 'identity_status', "") or ""),
            "identity_catalog_version": str(
                getattr(modem.identity, 'identity_catalog_version', "") or ""
            ),
            "identity_catalog_checksum": str(
                getattr(modem.identity, 'identity_catalog_checksum', "") or ""
            ),
            "session_scope_key": str(
                getattr(modem.generation, 'session_scope_key', "") or ""
            ),
            "source_epoch": str(getattr(modem.generation, 'source_epoch', "") or ""),
            "source_sequence": int(getattr(modem.generation, 'generation', 0) or 0),
            "sample_ts": float(
                getattr(modem.generation, 'sample_ts', getattr(modem.generation, 'sample_ts', 0.0)) or 0.0
            ),
            "sample_age_s": float(getattr(modem.generation, 'sample_age_s', 0.0) or 0.0),
            "provider_plmn": str(provider_plmn or getattr(modem.identity, 'provider_plmn', "") or ""),
            "access_type": str(getattr(modem.identity, 'access_type', "") or ""),
            "serving_band": int(
                getattr(modem.cellular, 'serving_band', getattr(modem.cellular, 'primary_band', 0))
                or 0
            ),
            "earfcn": int(
                getattr(modem.cellular, 'earfcn_dl', getattr(modem.cellular, 'earfcn_dl', 0)) or 0
            ),
            "pci": int(getattr(modem.cellular, 'pci', 0) or 0),
        },
        formula_vector=dlst_formula_vector(),
        evidence_count=max(1, int(evidence_count or 1)),
    )


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _text(value: Any) -> str:
    return str(value or "").strip()


def dlst_scope_from_modem(
    modem: CellularIntelligenceGeneration,
    *,
    provider_plmn: str = "",
) -> dict[str, Any]:
    """Return the required provider/cell/band/pool DLST scope."""

    bands = normalize_band_list(list(getattr(modem.cellular, 'band_set', []) or []))
    if not bands:
        bands = normalize_band_list(list(getattr(modem.cellular, 'active_band_set', []) or []))
    if not bands:
        bands = normalize_band_list(list(getattr(modem.cellular, 'selected_band_set', []) or []))
    primary_band = _int(getattr(modem.cellular, 'primary_band', getattr(modem.cellular, 'serving_band', 0)))
    if not bands and primary_band > 0:
        bands = [primary_band]

    pool_sig = (
        _text(getattr(modem.cellular, 'active_band_set_signature', ""))
        or _text(getattr(modem.cellular, 'ca_band_signature', ""))
        or band_signature(bands)
    )
    band_sig = band_signature(bands)
    pci = _int(getattr(modem.cellular, 'pci', 0))
    cell_id = _int(getattr(modem.cellular, 'cell_id', 0))
    earfcn = _int(getattr(modem.cellular, 'earfcn_dl', getattr(modem.cellular, 'earfcn_dl', 0)))
    provider = _text(provider_plmn)
    scope_key = (
        f"plmn={provider or '*'};"
        f"earfcn={earfcn};pci={pci};cell={cell_id};"
        f"band=B{primary_band if primary_band > 0 else 0};pool={pool_sig or band_sig or 'none'}"
    )
    return {
        "provider_plmn": provider,
        "earfcn": earfcn,
        "pci": pci,
        "cell_id": cell_id,
        "band": primary_band,
        "band_set_signature": band_sig,
        "pool_signature": pool_sig or band_sig,
        "scope_key": scope_key,
    }


def empty_dlst(scope: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return explicit nullable DLST evidence for unmeasured memory rows."""

    scoped = dict(scope or {})
    return {
        "schema_version": DLST_SCHEMA_VERSION,
        "owner": DLST_OWNER,
        "measured": False,
        "missing_evidence": [DLST_MISSING_EVIDENCE],
        "source": "not_measured",
        "sample_count": 0,
        "best_dl_mbps": None,
        "ceiling_mbps": None,
        "ratio": None,
        "last_measured_ts": 0.0,
        "scope": scoped,
        "evidence_scope": {},
    }


def normalize_dlst(raw: Any, scope: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a JSON-safe DLST record with the canonical keys present."""

    if not isinstance(raw, Mapping):
        return empty_dlst(scope)
    measured = bool(raw.get("measured", False))
    scoped = dict(raw.get("scope") if isinstance(raw.get("scope"), Mapping) else (scope or {}))
    if not measured:
        out = empty_dlst(scoped)
        out["source"] = _text(raw.get("source")) or "not_measured"
        if _text(raw.get("quarantine_reason")):
            out["quarantine_reason"] = _text(raw.get("quarantine_reason"))
        return out
    try:
        evidence_scope = validated_evidence_scope(
            raw.get("evidence_scope")
            if isinstance(raw.get("evidence_scope"), Mapping)
            else {}
        )
        if evidence_scope.formula_vector != dlst_formula_vector():
            raise ValueError("dlst_formula_vector_mismatch")
    except (TypeError, ValueError) as exc:
        out = empty_dlst(scoped)
        out["source"] = "quarantined_unscoped_evidence"
        out["quarantine_reason"] = str(exc)
        return out
    return {
        "schema_version": DLST_SCHEMA_VERSION,
        "owner": DLST_OWNER,
        "measured": True,
        "missing_evidence": [],
        "source": _text(raw.get("source")) or "controlled_speedtest",
        "sample_count": max(0, _int(raw.get("sample_count", 0))),
        "best_dl_mbps": max(0.0, _float(raw.get("best_dl_mbps", 0.0))),
        "ceiling_mbps": max(0.0, _float(raw.get("ceiling_mbps", 0.0))),
        "ratio": max(0.0, min(1.0, _float(raw.get("ratio", 0.0)))),
        "last_measured_ts": max(0.0, _float(raw.get("last_measured_ts", 0.0))),
        "scope": scoped,
        "evidence_scope": evidence_scope.as_dict(),
    }


def build_dlst_from_computed(
    modem: CellularIntelligenceGeneration,
    computed: DerivedSignalFrames,
    *,
    provider_plmn: str = "",
    now: float = 0.0,
) -> dict[str, Any]:
    """Build DLST memory evidence from controlled speed-test computed fields."""

    scope = dlst_scope_from_modem(modem, provider_plmn=provider_plmn)
    if not bool(getattr(computed.quality, 'delivered_capacity_valid', False)):
        return empty_dlst(scope)
    sample_count = _int(getattr(computed.quality, 'delivered_capacity_sample_count', 0))
    try:
        evidence_scope = dlst_evidence_scope_from_modem(
            modem,
            provider_plmn=provider_plmn,
            evidence_count=sample_count,
        ).as_dict()
    except (TypeError, ValueError) as exc:
        out = empty_dlst(scope)
        out["source"] = "quarantined_unscoped_evidence"
        out["quarantine_reason"] = str(exc)
        return out
    return normalize_dlst(
        {
            "measured": True,
            "source": _text(getattr(computed.quality, 'delivered_capacity_source', ""))
            or "controlled_speedtest",
            "sample_count": sample_count,
            "best_dl_mbps": _float(getattr(computed.quality, 'delivered_capacity_best_dl_mbps', 0.0)),
            "ceiling_mbps": _float(getattr(computed.quality, 'delivered_capacity_ceiling_mbps', 0.0)),
            "ratio": _float(getattr(computed.quality, 'delivered_capacity_ratio', 0.0)),
            "last_measured_ts": float(now or 0.0),
            "scope": scope,
            "evidence_scope": evidence_scope,
        },
        scope,
    )


def merge_dlst(existing: Any, incoming: Any) -> tuple[dict[str, Any], bool]:
    """Merge measured DLST evidence, keeping the best scoped proof."""

    old = normalize_dlst(existing)
    new = normalize_dlst(incoming)
    if not bool(new.get("measured", False)):
        if not isinstance(existing, Mapping):
            return new, True
        return old, False
    if not bool(old.get("measured", False)):
        return new, True

    try:
        old_scope = validated_evidence_scope(old.get("evidence_scope", {}))
        new_scope = validated_evidence_scope(new.get("evidence_scope", {}))
    except (TypeError, ValueError):
        return old, False
    if not old_scope.exact_runtime_match(new_scope):
        return old, False

    old_score = (
        _int(old.get("sample_count", 0)),
        _float(old.get("ratio", 0.0)),
        _float(old.get("best_dl_mbps", 0.0)),
        _float(old.get("last_measured_ts", 0.0)),
    )
    new_score = (
        _int(new.get("sample_count", 0)),
        _float(new.get("ratio", 0.0)),
        _float(new.get("best_dl_mbps", 0.0)),
        _float(new.get("last_measured_ts", 0.0)),
    )
    if new_score > old_score:
        return new, True
    return old, False


def attach_dlst(target: dict[str, Any], dlst: Any, scope: Mapping[str, Any] | None = None) -> bool:
    """Attach canonical `DLST` to a mutable memory bucket."""

    merged, changed = merge_dlst(target.get("DLST"), normalize_dlst(dlst, scope))
    if not changed and "DLST" in target:
        target["DLST"] = normalize_dlst(target.get("DLST"), scope)
        return False
    target["DLST"] = merged
    return True
