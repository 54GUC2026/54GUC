"""Current-session action scope, separate from durable learning evidence.

An operator/recovery/maintenance action needs a fresh accepted modem generation,
session identity, capability proof and safety admission.  It does not need an
invented cross-session modem code.  Durable learners continue to require
``EvidenceScope.v1`` and must never accept this contract as a substitute.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import re
from typing import Any, Mapping

from arch4.controller_frames import CellularIntelligenceGeneration
from arch4.core_contracts.accepted_runtime_view import (
    CoreAcceptedRuntimeView,
)


SCHEMA = "CurrentSessionActionScope.v1"
OWNER = "arch4.action_session_scope"
_HEX64_RE = re.compile(r"^0x[0-9A-Fa-f]{16}$")
_SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")


def _digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
    return sha256(raw.encode("utf-8")).hexdigest()


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


@dataclass(frozen=True, slots=True)
class CurrentSessionActionScope:
    session_scope_key: str
    source_epoch: str
    source_sequence: int
    observed_at: float
    evidence_age_s: float
    identity_code: str = "0x0000000000000000"
    identity_status: str = "provisional"
    identity_catalog_version: str = ""
    identity_catalog_checksum: str = ""
    access_type_id: str = "0x0000000000000000"
    profile_id: str = ""
    provider_plmn: str = ""
    serving_band: int = 0

    def __post_init__(self) -> None:
        if not str(self.session_scope_key or "").strip():
            raise ValueError("action_session_scope_session_required")
        if not str(self.source_epoch or "").strip() or int(self.source_sequence) <= 0:
            raise ValueError("action_session_scope_generation_required")
        if not math.isfinite(float(self.observed_at)) or float(self.observed_at) <= 0.0:
            raise ValueError("action_session_scope_observed_at_invalid")
        if not math.isfinite(float(self.evidence_age_s)) or float(self.evidence_age_s) < 0.0:
            raise ValueError("action_session_scope_age_invalid")
        code = str(self.identity_code or "0x0000000000000000").strip()
        access_id = str(self.access_type_id or "0x0000000000000000").strip()
        if not _HEX64_RE.fullmatch(code) or not _HEX64_RE.fullmatch(access_id):
            raise ValueError("action_session_scope_identity_format_invalid")
        identity_status = str(self.identity_status or "").strip().lower()
        if identity_status not in {"exact", "provisional", "unknown"}:
            raise ValueError("action_session_scope_identity_status_invalid")
        if identity_status == "exact":
            if int(code[2:], 16) == 0 or int(access_id[2:], 16) == 0:
                raise ValueError("action_session_scope_exact_identity_invalid")
            if not str(self.identity_catalog_version or "").strip() or not _SHA256_RE.fullmatch(
                str(self.identity_catalog_checksum or "").strip()
            ):
                raise ValueError("action_session_scope_exact_catalog_reference_required")
        elif int(code[2:], 16) != 0:
            raise ValueError("action_session_scope_nonexact_identity_must_be_zero")
        plmn = str(self.provider_plmn or "").strip()
        if plmn and not plmn.isdigit():
            raise ValueError("action_session_scope_provider_plmn_invalid")
        if int(self.serving_band) < 0:
            raise ValueError("action_session_scope_serving_band_invalid")

    def _content(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            "session_scope_key": self.session_scope_key,
            "source_epoch": self.source_epoch,
            "source_sequence": int(self.source_sequence),
            "observed_at": float(self.observed_at),
            "evidence_age_s": float(self.evidence_age_s),
            "identity_code": self.identity_code,
            "identity_status": self.identity_status,
            "identity_catalog_version": self.identity_catalog_version,
            "identity_catalog_checksum": self.identity_catalog_checksum,
            "access_type_id": self.access_type_id,
            "profile_id": str(self.profile_id or "").strip(),
            "provider_plmn": self.provider_plmn,
            "serving_band": int(self.serving_band),
        }

    @property
    def scope_id(self) -> str:
        return _digest(self._content())

    def as_dict(self) -> dict[str, Any]:
        return {**self._content(), "action_session_scope_id": self.scope_id}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CurrentSessionActionScope":
        row = dict(raw or {})
        if row.get("schema") != SCHEMA or row.get("owner") != OWNER:
            raise ValueError("action_session_scope_owner_schema_invalid")
        value = cls(
            session_scope_key=str(row.get("session_scope_key") or ""),
            source_epoch=str(row.get("source_epoch") or ""),
            source_sequence=_int(row.get("source_sequence")),
            observed_at=_float(row.get("observed_at")),
            evidence_age_s=_float(row.get("evidence_age_s")),
            identity_code=str(row.get("identity_code") or "0x0000000000000000"),
            identity_status=str(row.get("identity_status") or "provisional"),
            identity_catalog_version=str(row.get("identity_catalog_version") or ""),
            identity_catalog_checksum=str(row.get("identity_catalog_checksum") or ""),
            access_type_id=str(row.get("access_type_id") or "0x0000000000000000"),
            profile_id=str(row.get("profile_id") or ""),
            provider_plmn=str(row.get("provider_plmn") or ""),
            serving_band=_int(row.get("serving_band")),
        )
        if str(row.get("action_session_scope_id") or "") != value.scope_id:
            raise ValueError("action_session_scope_checksum_mismatch")
        return value


def current_session_action_scope_from_generation(
    frame: CellularIntelligenceGeneration,
    *,
    now: float,
) -> CurrentSessionActionScope:
    """Adapt one immutable accepted generation into current action scope."""

    if not isinstance(frame, CellularIntelligenceGeneration):
        raise TypeError(
            "current_session_action_scope_requires_cellular_intelligence_generation"
        )
    now_ts = float(now)
    observed_at = float(
        getattr(frame.generation, "sample_ts", 0.0)
        or getattr(frame.generation, "accepted_ts", 0.0)
        or 0.0
    )
    evidence_age_s = max(0.0, now_ts - observed_at)
    return CurrentSessionActionScope(
        session_scope_key=str(
            getattr(frame.generation, "session_scope_key", "") or ""
        ),
        source_epoch=str(getattr(frame.generation, "source_epoch", "") or ""),
        source_sequence=_int(getattr(frame.generation, "generation", 0)),
        observed_at=observed_at,
        evidence_age_s=evidence_age_s,
        identity_code=str(
            getattr(frame.identity, "identity_code", "")
            or "0x0000000000000000"
        ),
        identity_status=str(
            getattr(frame.identity, "identity_status", "") or "unknown"
        ),
        identity_catalog_version=str(
            getattr(frame.identity, "identity_catalog_version", "") or ""
        ),
        identity_catalog_checksum=str(
            getattr(frame.identity, "identity_catalog_checksum", "") or ""
        ),
        access_type_id=str(
            getattr(frame.identity, "access_type_id", "")
            or "0x0000000000000000"
        ),
        profile_id=str(getattr(frame.identity, "profile_id", "") or ""),
        provider_plmn=str(getattr(frame.identity, "provider_plmn", "") or ""),
        serving_band=_int(
            getattr(
                frame.cellular,
                "serving_band",
                getattr(frame.cellular, "primary_band", 0),
            )
        ),
    )


def current_session_action_scope_from_runtime_view(
    view: CoreAcceptedRuntimeView,
    *,
    now: float,
) -> CurrentSessionActionScope:
    """Adapt the one accepted Core interpretation into action scope."""

    if not isinstance(view, CoreAcceptedRuntimeView):
        raise TypeError(
            "current_session_action_scope_requires_runtime_view"
        )
    facts = view.capability_facts.to_mapping()
    source_status = str(facts.get("identity_status") or "").strip().lower()
    identity_status = (
        "exact"
        if source_status == "exact"
        else "provisional"
        if source_status == "provisional"
        else "unknown"
    )
    observed_at = view.sampled_at
    return CurrentSessionActionScope(
        session_scope_key=view.matrix_identity.session_scope_key,
        source_epoch=view.matrix_identity.source_epoch,
        source_sequence=view.matrix_identity.matrix_sequence,
        observed_at=observed_at,
        evidence_age_s=max(0.0, float(now) - observed_at),
        identity_code=(
            view.identity_code
            if identity_status == "exact"
            else "0x0000000000000000"
        ),
        identity_status=identity_status,
        identity_catalog_version=str(facts.get("catalog_version") or ""),
        identity_catalog_checksum=str(facts.get("catalog_checksum") or ""),
        access_type_id=str(
            facts.get("access_type_id") or "0x0000000000000000"
        ),
        profile_id=str(facts.get("profile_id") or ""),
        provider_plmn=str(facts.get("provider_plmn") or ""),
        serving_band=view.radio_target.primary_band,
    )


def current_action_context_key(
    frame: CellularIntelligenceGeneration,
    *,
    scope: CurrentSessionActionScope,
) -> str:
    """Return the canonical current-session Brain context key."""

    if not isinstance(frame, CellularIntelligenceGeneration):
        raise TypeError(
            "current_action_context_key_requires_cellular_intelligence_generation"
        )
    if not isinstance(scope, CurrentSessionActionScope):
        raise TypeError("current_action_context_key_requires_action_scope")
    return current_action_context_key_from_values(
        identity_code=scope.identity_code,
        provider_plmn=str(getattr(frame.identity, "provider_plmn", "") or ""),
        cell_id=_int(getattr(frame.cellular, "cell_id", 0)),
        pci=_int(getattr(frame.cellular, "pci", 0)),
        earfcn=_int(getattr(frame.cellular, "earfcn_dl", 0)),
        session_scope_key=scope.session_scope_key,
    )


def current_action_context_key_from_values(
    *,
    identity_code: str,
    provider_plmn: str,
    cell_id: int,
    pci: int,
    earfcn: int,
    session_scope_key: str,
) -> str:
    """Build the sole physical-action context key at a register boundary."""

    identity = str(identity_code or "").strip()
    session = str(session_scope_key or "").strip()
    provider = str(provider_plmn or "").strip()
    if not identity:
        raise ValueError("action_context_identity_required")
    if not session:
        raise ValueError("action_context_session_required")
    if provider and not provider.isdigit():
        raise ValueError("action_context_provider_plmn_invalid")
    return "|".join(
        (
            identity,
            provider,
            str(_int(cell_id)),
            str(_int(pci)),
            str(_int(earfcn)),
            session,
        )
    )


__all__ = [
    "CurrentSessionActionScope",
    "OWNER",
    "SCHEMA",
    "current_action_context_key",
    "current_action_context_key_from_values",
    "current_session_action_scope_from_generation",
    "current_session_action_scope_from_runtime_view",
]
