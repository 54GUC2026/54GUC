"""Exact durable evidence scope shared by learning and prediction owners.

This contract describes where evidence came from; it does not decide how much
that evidence is worth.  A consumer must validate the checksum, exact modem
identity, accepted physical generation, and formula vector before admitting a
row.  Optional context narrows portability and may never widen it implicitly.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


FORMULA_SCHEMA = "FormulaVector.v1"
FORMULA_OWNER = "arch4.evidence_scope"
EVIDENCE_SCHEMA = "EvidenceScope.v1"
EVIDENCE_OWNER = "arch4.evidence_scope"
_EXACT_IDENTITY_RE = re.compile(r"^0x[0-9a-fA-F]{16}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class EvidencePortability(str, Enum):
    NONE = "none"
    EXACT_IDENTITY = "exact_identity"
    SAME_ACCESS_TYPE = "same_access_type"
    SAME_MODEL_FAMILY = "same_model_family"


@dataclass(frozen=True, slots=True)
class FormulaVector:
    epochs: tuple[tuple[str, str], ...]

    @classmethod
    def build(cls, epochs: Mapping[str, Any]) -> "FormulaVector":
        rows = tuple(
            sorted(
                (
                    (str(name).strip(), str(epoch).strip())
                    for name, epoch in dict(epochs or {}).items()
                    if str(name).strip() and str(epoch).strip()
                ),
                key=lambda item: item[0],
            )
        )
        if not rows:
            raise ValueError("formula_vector_epochs_required")
        if len(rows) != len(dict(epochs or {})):
            raise ValueError("formula_vector_blank_epoch_forbidden")
        return cls(epochs=rows)

    @property
    def vector_id(self) -> str:
        return _digest(
            {
                "schema": FORMULA_SCHEMA,
                "owner": FORMULA_OWNER,
                "epochs": dict(self.epochs),
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": FORMULA_SCHEMA,
            "owner": FORMULA_OWNER,
            "epochs": dict(self.epochs),
            "formula_vector_id": self.vector_id,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "FormulaVector":
        row = dict(raw or {})
        if row.get("schema") != FORMULA_SCHEMA or row.get("owner") != FORMULA_OWNER:
            raise ValueError("formula_vector_owner_schema_invalid")
        epochs = row.get("epochs")
        if not isinstance(epochs, Mapping):
            raise ValueError("formula_vector_epochs_required")
        value = cls.build(epochs)
        if str(row.get("formula_vector_id") or "") != value.vector_id:
            raise ValueError("formula_vector_checksum_mismatch")
        return value


@dataclass(frozen=True, slots=True)
class EvidenceScope:
    identity_code: str
    identity_status: str
    identity_catalog_version: str
    identity_catalog_checksum: str
    session_scope_key: str
    source_epoch: str
    source_sequence: int
    observed_at: float
    evidence_age_s: float
    confidence: float
    evidence_count: int
    formula_vector: FormulaVector
    portability: EvidencePortability = EvidencePortability.EXACT_IDENTITY
    access_type_id: str = ""
    model_family_id: str = ""
    provider_plmn: str = ""
    sim_scope_hash: str = ""
    site_scope_id: str = ""
    cell_scope_key: str = ""
    serving_band: int = 0
    action_shape_key: str = ""

    def __post_init__(self) -> None:
        code = str(self.identity_code or "").strip()
        if not _EXACT_IDENTITY_RE.fullmatch(code) or int(code[2:], 16) == 0:
            raise ValueError("evidence_scope_exact_identity_required")
        if str(self.identity_status or "").strip().lower() != "exact":
            raise ValueError("evidence_scope_exact_identity_status_required")
        if not str(self.identity_catalog_version or "").strip() or not _SHA256_RE.fullmatch(
            str(self.identity_catalog_checksum or "").strip()
        ):
            raise ValueError("evidence_scope_identity_catalog_reference_required")
        if not str(self.session_scope_key or "").strip():
            raise ValueError("evidence_scope_session_required")
        if not str(self.source_epoch or "").strip() or int(self.source_sequence) <= 0:
            raise ValueError("evidence_scope_generation_required")
        if not math.isfinite(float(self.observed_at)) or float(self.observed_at) <= 0.0:
            raise ValueError("evidence_scope_observed_at_invalid")
        if not math.isfinite(float(self.evidence_age_s)) or float(self.evidence_age_s) < 0.0:
            raise ValueError("evidence_scope_age_invalid")
        if not math.isfinite(float(self.confidence)) or not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("evidence_scope_confidence_invalid")
        if int(self.evidence_count) <= 0:
            raise ValueError("evidence_scope_count_invalid")
        plmn = str(self.provider_plmn or "").strip()
        if plmn and not plmn.isdigit():
            raise ValueError("evidence_scope_provider_plmn_invalid")
        if int(self.serving_band) < 0:
            raise ValueError("evidence_scope_serving_band_invalid")
        if not isinstance(self.formula_vector, FormulaVector):
            raise ValueError("evidence_scope_formula_vector_required")
        if self.portability is not EvidencePortability.EXACT_IDENTITY:
            if self.portability is EvidencePortability.SAME_ACCESS_TYPE and not str(
                self.access_type_id or ""
            ).strip():
                raise ValueError("evidence_scope_access_type_required_for_portability")
            if self.portability is EvidencePortability.SAME_MODEL_FAMILY and not str(
                self.model_family_id or ""
            ).strip():
                raise ValueError("evidence_scope_model_family_required_for_portability")

    @property
    def scope_id(self) -> str:
        return _digest(self._content())

    def _content(self) -> dict[str, Any]:
        return {
            "schema": EVIDENCE_SCHEMA,
            "owner": EVIDENCE_OWNER,
            "identity_code": self.identity_code,
            "identity_status": self.identity_status,
            "identity_catalog_version": self.identity_catalog_version,
            "identity_catalog_checksum": self.identity_catalog_checksum,
            "session_scope_key": self.session_scope_key,
            "source_epoch": self.source_epoch,
            "source_sequence": int(self.source_sequence),
            "observed_at": float(self.observed_at),
            "evidence_age_s": float(self.evidence_age_s),
            "confidence": float(self.confidence),
            "evidence_count": int(self.evidence_count),
            "formula_vector": self.formula_vector.as_dict(),
            "portability": self.portability.value,
            "access_type_id": self.access_type_id,
            "model_family_id": self.model_family_id,
            "provider_plmn": self.provider_plmn,
            "sim_scope_hash": self.sim_scope_hash,
            "site_scope_id": self.site_scope_id,
            "cell_scope_key": self.cell_scope_key,
            "serving_band": int(self.serving_band),
            "action_shape_key": self.action_shape_key,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._content(), "evidence_scope_id": self.scope_id}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "EvidenceScope":
        row = dict(raw or {})
        if row.get("schema") != EVIDENCE_SCHEMA or row.get("owner") != EVIDENCE_OWNER:
            raise ValueError("evidence_scope_owner_schema_invalid")
        try:
            portability = EvidencePortability(str(row.get("portability") or ""))
        except ValueError as exc:
            raise ValueError("evidence_scope_portability_invalid") from exc
        value = cls(
            identity_code=str(row.get("identity_code") or ""),
            identity_status=str(row.get("identity_status") or ""),
            identity_catalog_version=str(
                row.get("identity_catalog_version") or ""
            ),
            identity_catalog_checksum=str(
                row.get("identity_catalog_checksum") or ""
            ),
            session_scope_key=str(row.get("session_scope_key") or ""),
            source_epoch=str(row.get("source_epoch") or ""),
            source_sequence=_int(row.get("source_sequence")),
            observed_at=_float(row.get("observed_at")),
            evidence_age_s=_float(row.get("evidence_age_s")),
            confidence=_float(row.get("confidence"), float("nan")),
            evidence_count=_int(row.get("evidence_count")),
            formula_vector=FormulaVector.from_mapping(_mapping(row.get("formula_vector"))),
            portability=portability,
            access_type_id=str(row.get("access_type_id") or ""),
            model_family_id=str(row.get("model_family_id") or ""),
            provider_plmn=str(row.get("provider_plmn") or ""),
            sim_scope_hash=str(row.get("sim_scope_hash") or ""),
            site_scope_id=str(row.get("site_scope_id") or ""),
            cell_scope_key=str(row.get("cell_scope_key") or ""),
            serving_band=_int(row.get("serving_band")),
            action_shape_key=str(row.get("action_shape_key") or ""),
        )
        if str(row.get("evidence_scope_id") or "") != value.scope_id:
            raise ValueError("evidence_scope_checksum_mismatch")
        return value

    def exact_runtime_match(self, current: "EvidenceScope") -> bool:
        """Return whether two rows are safe to merge into one live learner."""

        return bool(
            isinstance(current, EvidenceScope)
            and self.identity_code == current.identity_code
            and self.identity_status == current.identity_status
            and self.identity_catalog_version == current.identity_catalog_version
            and self.identity_catalog_checksum == current.identity_catalog_checksum
            and self.session_scope_key == current.session_scope_key
            and self.source_epoch == current.source_epoch
            and self.formula_vector == current.formula_vector
            and (not self.provider_plmn or self.provider_plmn == current.provider_plmn)
            and (not self.sim_scope_hash or self.sim_scope_hash == current.sim_scope_hash)
            and (not self.site_scope_id or self.site_scope_id == current.site_scope_id)
            and (not self.cell_scope_key or self.cell_scope_key == current.cell_scope_key)
            and (not self.serving_band or self.serving_band == current.serving_band)
            and (
                not self.action_shape_key
                or self.action_shape_key == current.action_shape_key
            )
        )

    def same_runtime_namespace(self, current: "EvidenceScope") -> bool:
        """Return whether evidence belongs to the same exact runtime namespace."""

        return bool(
            isinstance(current, EvidenceScope)
            and self.identity_code == current.identity_code
            and self.identity_status == current.identity_status
            and self.identity_catalog_version == current.identity_catalog_version
            and self.identity_catalog_checksum == current.identity_catalog_checksum
            and self.session_scope_key == current.session_scope_key
            and self.source_epoch == current.source_epoch
            and self.formula_vector == current.formula_vector
        )

    def same_physical_namespace(self, current: "EvidenceScope") -> bool:
        """Match exact modem, catalog, session, and generation namespace."""

        return bool(
            isinstance(current, EvidenceScope)
            and self.identity_code == current.identity_code
            and self.identity_status == current.identity_status
            and self.identity_catalog_version == current.identity_catalog_version
            and self.identity_catalog_checksum == current.identity_catalog_checksum
            and self.session_scope_key == current.session_scope_key
            and self.source_epoch == current.source_epoch
        )

    def exact_generation_match(self, current: "EvidenceScope") -> bool:
        """Return whether two envelopes describe the same physical generation."""

        return bool(
            self.exact_runtime_match(current)
            and self.source_sequence == current.source_sequence
        )


def evidence_scope_from_mapping(
    raw: Mapping[str, Any],
    *,
    formula_vector: FormulaVector,
    confidence: float = 1.0,
    evidence_count: int = 1,
    portability: EvidencePortability = EvidencePortability.EXACT_IDENTITY,
    observed_at: float | None = None,
) -> EvidenceScope:
    """Build one strict scope from an accepted-generation fact mapping."""

    row = dict(raw or {})
    identity_key = _mapping(row.get("modem_identity_key"))
    runtime_identity = _mapping(row.get("runtime_identity"))
    runtime_identity_key = _mapping(runtime_identity.get("modem_identity_key"))
    identity_code = str(
        row.get("identity_code")
        or row.get("modem_identity_code")
        or identity_key.get("identity_code")
        or runtime_identity.get("identity_code")
        or runtime_identity_key.get("identity_code")
        or ""
    )
    identity_status = str(
        row.get("identity_status")
        or identity_key.get("identity_status")
        or runtime_identity.get("identity_status")
        or runtime_identity_key.get("identity_status")
        or ""
    )
    catalog_version = str(
        row.get("identity_catalog_version")
        or row.get("catalog_version")
        or identity_key.get("catalog_version")
        or runtime_identity.get("catalog_version")
        or runtime_identity_key.get("catalog_version")
        or ""
    )
    catalog_checksum = str(
        row.get("identity_catalog_checksum")
        or row.get("catalog_checksum")
        or identity_key.get("catalog_checksum")
        or runtime_identity.get("catalog_checksum")
        or runtime_identity_key.get("catalog_checksum")
        or ""
    )
    session_scope_key = str(
        row.get("accepted_generation_session_scope_key")
        or row.get("session_scope_key")
        or row.get("runtime_session_scope_key")
        or ""
    )
    source_epoch = str(row.get("source_epoch") or "")
    source_sequence = _int(
        row.get("source_sequence")
        or row.get("sample_sequence")
        or row.get("generation")
    )
    sample_ts = _float(
        observed_at
        if observed_at is not None
        else row.get("sample_ts") or row.get("ts") or row.get("timestamp")
    )
    age = _float(row.get("evidence_age_s", row.get("sample_age_s", 0.0)))
    return EvidenceScope(
        identity_code=identity_code,
        identity_status=identity_status,
        identity_catalog_version=catalog_version,
        identity_catalog_checksum=catalog_checksum,
        session_scope_key=session_scope_key,
        source_epoch=source_epoch,
        source_sequence=source_sequence,
        observed_at=sample_ts,
        evidence_age_s=age,
        confidence=float(confidence),
        evidence_count=int(evidence_count),
        formula_vector=formula_vector,
        portability=portability,
        access_type_id=str(
            row.get("access_type_id") or row.get("access_type") or ""
        ),
        provider_plmn=str(row.get("provider_plmn") or row.get("plmn") or ""),
        sim_scope_hash=str(row.get("sim_scope_hash") or ""),
        site_scope_id=str(row.get("site_scope_id") or ""),
        cell_scope_key=str(
            row.get("cell_scope_key")
            or (
                f"{_int(row.get('serving_band') or row.get('primary_band'))}:"
                f"{_int(row.get('earfcn'))}:{_int(row.get('pci'))}"
                if _int(row.get("serving_band") or row.get("primary_band")) > 0
                and _int(row.get("pci")) > 0
                else ""
            )
        ),
        serving_band=_int(row.get("serving_band") or row.get("primary_band")),
        action_shape_key=str(row.get("action_shape_key") or ""),
    )


def validated_evidence_scope(
    raw: Mapping[str, Any] | EvidenceScope,
) -> EvidenceScope:
    if isinstance(raw, EvidenceScope):
        return raw
    if not isinstance(raw, Mapping):
        raise ValueError("evidence_scope_mapping_required")
    return EvidenceScope.from_mapping(raw)


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


__all__ = [
    "EVIDENCE_OWNER",
    "EVIDENCE_SCHEMA",
    "FORMULA_OWNER",
    "FORMULA_SCHEMA",
    "EvidencePortability",
    "EvidenceScope",
    "FormulaVector",
    "evidence_scope_from_mapping",
    "validated_evidence_scope",
]
