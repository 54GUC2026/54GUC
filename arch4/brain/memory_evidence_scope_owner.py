"""Canonical EvidenceScope owner for Memory STM/LTM aggregate state.

Action rows carry their own action/ROI formula vector.  This owner binds the
remaining mutable Memory aggregates to one exact modem/catalog/session/source
namespace and prevents a loaded or live foreign namespace from being merged.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Mapping

from arch4.brain.band_learning import BandConfidenceStore
from arch4.brain.memory_io import record_to_dict
from arch4.brain.memory_namespaces import bind_namespace, new_provider_namespace
from arch4.evidence_scope import (
    EvidenceScope,
    FormulaVector,
    evidence_scope_from_mapping,
    validated_evidence_scope,
)
from arch4.interfaces import ARCH_QUALITY_FORMULA_EPOCH


OWNER = "arch4.brain.memory_evidence_scope_owner"
MEMORY_AGGREGATE_FORMULA_EPOCH = "memory_stm_ltm_exact_scope_v1"
DELIVERED_CAPACITY_FORMULA_EPOCH = "controlled_speedtest_best_of_3_v1"


@dataclass(frozen=True, slots=True)
class MemoryCheckpointScopeAdmission:
    status: str
    reason: str
    evidence_scope: Mapping[str, Any]

    @property
    def admitted(self) -> bool:
        return self.status == "admitted"

    @property
    def deferred(self) -> bool:
        return self.status == "deferred"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "MemoryCheckpointScopeAdmission.v1",
            "owner": OWNER,
            "status": self.status,
            "reason": self.reason,
            "evidence_scope": dict(self.evidence_scope),
        }


def memory_formula_vector() -> FormulaVector:
    return FormulaVector.build(
        {
            "delivered_capacity": DELIVERED_CAPACITY_FORMULA_EPOCH,
            "memory_aggregate": MEMORY_AGGREGATE_FORMULA_EPOCH,
            "quality": str(ARCH_QUALITY_FORMULA_EPOCH or ""),
        }
    )


def memory_evidence_scope(modem: Any) -> dict[str, Any]:
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
            "provider_plmn": str(getattr(modem.identity, 'provider_plmn', "") or ""),
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
        formula_vector=memory_formula_vector(),
    ).as_dict()


def memory_snapshot_scope_error(raw: Mapping[str, Any] | None) -> str:
    try:
        scope = validated_evidence_scope(raw or {})
    except (TypeError, ValueError) as exc:
        return str(exc)
    if scope.formula_vector != memory_formula_vector():
        return "memory_formula_vector_mismatch"
    return ""


def current_memory_evidence_scope(memory: Any) -> dict[str, Any]:
    """Return the validated live aggregate scope without exposing mutable state."""

    try:
        scope = validated_evidence_scope(
            dict(getattr(memory, "_current_memory_evidence_scope", {}) or {})
        )
    except (TypeError, ValueError):
        return {}
    return scope.as_dict()


def memory_checkpoint_scope_admission(
    memory: Any,
) -> MemoryCheckpointScopeAdmission:
    """Classify checkpoint scope without synthesizing modem identity."""

    raw = dict(getattr(memory, "_current_memory_evidence_scope", {}) or {})
    if not raw:
        return MemoryCheckpointScopeAdmission(
            status="deferred",
            reason="identity_scope_not_bound",
            evidence_scope={},
        )
    error = memory_snapshot_scope_error(raw)
    if error:
        return MemoryCheckpointScopeAdmission(
            status="rejected",
            reason=str(error),
            evidence_scope={},
        )
    return MemoryCheckpointScopeAdmission(
        status="admitted",
        reason="exact_scope_admitted",
        evidence_scope=current_memory_evidence_scope(memory),
    )


def bind_memory_evidence_scope(memory: Any, modem: Any) -> bool:
    """Bind mutable STM/LTM to one exact runtime namespace.

    A namespace transition does not reinterpret prior aggregates.  The old
    namespace is reduced to a bounded quarantine receipt, active aggregates
    are cleared, and only exact-scoped ActionRecords remain eligible.
    """

    try:
        incoming = validated_evidence_scope(memory_evidence_scope(modem))
    except (TypeError, ValueError) as exc:
        _quarantine(memory, "memory_live_scope_invalid", {"error": str(exc)})
        return False

    try:
        current = validated_evidence_scope(
            dict(getattr(memory, "_current_memory_evidence_scope", {}) or {})
        )
    except (TypeError, ValueError):
        current = None

    if current is not None and not current.same_physical_namespace(incoming):
        _quarantine(
            memory,
            "memory_runtime_namespace_changed",
            _aggregate_inventory(memory, evidence_scope=current.as_dict()),
        )
        _reset_mutable_aggregates(memory, provider_plmn=incoming.provider_plmn)

    _quarantine_foreign_action_rows(memory, incoming)
    memory._current_memory_evidence_scope = incoming.as_dict()
    return True


def quarantine_unscoped_snapshot(memory: Any, data: Mapping[str, Any], reason: str) -> None:
    _quarantine(
        memory,
        str(reason or "memory_snapshot_scope_invalid"),
        {
            "version": str(data.get("version", "") or ""),
            "action_count": len(list(data.get("action_history", []) or [])),
            "band_count": len(dict(data.get("bands", {}) or {})),
            "cell_count": len(dict(data.get("cells", {}) or {})),
            "baseline_count": len(dict(data.get("baselines", {}) or {})),
            "provider_count": len(dict(data.get("providers", {}) or {})),
            "decoded_identity_catalog_disposition": "read_only_reference_only",
        },
    )


def _aggregate_inventory(memory: Any, *, evidence_scope: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "evidence_scope": dict(evidence_scope or {}),
        "band_count": len(getattr(memory, "bands", {}) or {}),
        "cell_count": len(getattr(memory, "cells", {}) or {}),
        "baseline_count": len(getattr(memory, "baselines", {}) or {}),
        "provider_count": len(getattr(memory, "_provider_namespaces", {}) or {}),
        "reward_count": len(getattr(memory, "_reward_ledger", {}) or {}),
        "decoded_identity_catalog_disposition": "read_only_reference_only",
    }


def _reset_mutable_aggregates(memory: Any, *, provider_plmn: str) -> None:
    provider = str(provider_plmn or "")
    namespace = new_provider_namespace()
    memory._provider_namespaces = {provider: namespace}
    memory._current_provider_plmn = provider
    bind_namespace(memory, namespace)
    memory._band_confidence_store = BandConfidenceStore()
    memory._congestion_hourly = {}
    memory._dwell_tracking = {}
    memory._dwell_current_band = 0
    memory._dwell_current_band_since = 0.0
    memory._dwell_current_episode_id = ""
    memory._dwell_current_selection_source = "attribution_unavailable"
    memory._congestion_precursors = []
    memory._reward_ledger = {}
    memory._reward_trajectory_feedback = {}
    memory._reward_projection_outbox = {}
    memory._reward_projection_effect_keys = set()
    memory._mark_dirty(events=1)


def _quarantine_foreign_action_rows(memory: Any, incoming: EvidenceScope) -> None:
    store = getattr(memory, "_action_history_store", None)
    if store is None:
        return
    admitted = []
    for record in list(store.records):
        raw_scope = getattr(record, "evidence_scope", {})
        try:
            scope = validated_evidence_scope(raw_scope)
        except (TypeError, ValueError) as exc:
            _quarantine(
                memory,
                "action_record_scope_invalid",
                {"error": str(exc), "record": record_to_dict(record)},
            )
            continue
        if not scope.same_physical_namespace(incoming):
            _quarantine(
                memory,
                "action_record_foreign_runtime_namespace",
                {"record": record_to_dict(record)},
            )
            continue
        admitted.append(record)
    if len(admitted) != len(store.records):
        store.set_records(admitted)
        memory._mark_dirty(events=1)


def _quarantine(memory: Any, reason: str, payload: Mapping[str, Any]) -> None:
    row = {
        "schema": "DurableEvidenceQuarantine.v1",
        "owner": OWNER,
        "reason": str(reason or "evidence_scope_invalid"),
        "quarantined_at": float(time.time()),
        "payload": dict(payload or {}),
    }
    quarantine = getattr(memory, "_evidence_quarantine", None)
    if not isinstance(quarantine, list):
        quarantine = []
        memory._evidence_quarantine = quarantine
    quarantine.append(row)
    if len(quarantine) > 256:
        del quarantine[:-256]


__all__ = [
    "DELIVERED_CAPACITY_FORMULA_EPOCH",
    "MEMORY_AGGREGATE_FORMULA_EPOCH",
    "MemoryCheckpointScopeAdmission",
    "OWNER",
    "bind_memory_evidence_scope",
    "current_memory_evidence_scope",
    "memory_checkpoint_scope_admission",
    "memory_evidence_scope",
    "memory_formula_vector",
    "memory_snapshot_scope_error",
    "quarantine_unscoped_snapshot",
]
