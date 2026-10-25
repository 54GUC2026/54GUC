"""Delivered-load-speed-test memory owner."""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import time
from typing import Any, Dict, Optional

from arch4.brain.delivered_load_memory import (
    attach_dlst,
    build_dlst_from_computed,
    dlst_scope_from_modem,
    dlst_evidence_scope_from_modem,
    empty_dlst,
    merge_dlst,
    normalize_dlst,
)
from arch4.evidence_scope import validated_evidence_scope
from arch4.brain.memory_namespaces import set_provider_context as memory_set_provider_context
from arch4.brain.memory_evidence_scope_owner import bind_memory_evidence_scope
from arch4.interfaces import parse_band_signature

OWNER = "arch4.brain.memory_delivered_load_owner"


def lookup_dlst_for_modem(
    memory: Any,
    modem: Optional[CellularIntelligenceGeneration],
) -> Dict[str, Any]:
    if modem is None:
        return empty_dlst()
    scope = dlst_scope_from_modem(modem, provider_plmn=getattr(memory, "_current_provider_plmn", ""))
    key = str(scope.get("scope_key", "") or "")
    if key and key in getattr(memory, "_delivered_load_speed_tests", {}):
        record = normalize_dlst(memory._delivered_load_speed_tests.get(key), scope)
        if not bool(record.get("measured", False)):
            return record
        try:
            stored_scope = validated_evidence_scope(record.get("evidence_scope", {}))
            live_scope = dlst_evidence_scope_from_modem(
                modem,
                provider_plmn=getattr(memory, "_current_provider_plmn", ""),
            )
        except (TypeError, ValueError):
            return empty_dlst(scope)
        if stored_scope.same_physical_namespace(live_scope):
            return record
        return empty_dlst(scope)
    return empty_dlst(scope)


def dlst_from_modem_computed(
    memory: Any,
    modem: Optional[CellularIntelligenceGeneration],
    computed: Optional[DerivedSignalFrames],
    *,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    if modem is None:
        return empty_dlst()
    if computed is not None and bool(getattr(computed.quality, 'delivered_capacity_valid', False)):
        return build_dlst_from_computed(
            modem,
            computed,
            provider_plmn=getattr(memory, "_current_provider_plmn", ""),
            now=float(time.time() if now is None else now),
        )
    return lookup_dlst_for_modem(memory, modem)


def record_delivered_load_speed_test(
    memory: Any,
    modem: CellularIntelligenceGeneration,
    computed: DerivedSignalFrames,
    *,
    provider_plmn: str = "",
    now: Optional[float] = None,
    append_journal: bool = True,
) -> Dict[str, Any]:
    ts = float(time.time() if now is None else now)
    if not bind_memory_evidence_scope(memory, modem):
        return {
            "recorded": False,
            "reason": "exact_evidence_scope_required",
            "DLST": empty_dlst(),
        }
    provider = str(provider_plmn or getattr(memory, "_current_provider_plmn", "") or "")
    record = build_dlst_from_computed(modem, computed, provider_plmn=provider, now=ts)
    if not bool(record.get("measured", False)):
        return {
            "recorded": False,
            "reason": str(record.get("quarantine_reason", "") or "missing_controlled_speedtest_evidence"),
            "DLST": record,
        }
    changed = apply_delivered_load_speed_test_record(memory, record)
    if changed and append_journal:
        memory._append_journal_event(
            {
                "kind": "delivered_load_speed_test",
                "ts": ts,
                "record": record,
            }
        )
        memory._mark_dirty(events=1)
    return {"recorded": bool(changed), "reason": "recorded" if changed else "unchanged", "DLST": record}


def learning_bucket_matches_dlst_scope(key: str, dlst: Dict[str, Any]) -> bool:
    scope = dict(dlst.get("scope") or {})
    band_sig = str(scope.get("band_set_signature", "") or "")
    pool_sig = str(scope.get("pool_signature", "") or "")
    band = int(scope.get("band", 0) or 0)
    tail = str(key or "").split("|", 1)[-1]
    bucket_sig = tail.split(":", 1)[-1] if ":" in tail else tail
    if bucket_sig in {band_sig, pool_sig}:
        return True
    if band > 0 and bucket_sig == f"B{band}_PRIMARY":
        return True
    return False


def apply_delivered_load_speed_test_record(memory: Any, record: Dict[str, Any]) -> bool:
    dlst = normalize_dlst(record)
    if not bool(dlst.get("measured", False)):
        return False
    try:
        record_scope = validated_evidence_scope(dlst.get("evidence_scope", {}))
        memory_scope = validated_evidence_scope(
            getattr(memory, "_current_memory_evidence_scope", {}) or {}
        )
    except (TypeError, ValueError):
        return False
    if not record_scope.same_physical_namespace(memory_scope):
        return False
    scope = dict(dlst.get("scope") or {})
    provider = str(scope.get("provider_plmn", "") or getattr(memory, "_current_provider_plmn", "") or "")
    if provider != getattr(memory, "_current_provider_plmn", ""):
        memory_set_provider_context(memory, provider)
    key = str(scope.get("scope_key", "") or "")
    if not key:
        return False
    merged, changed = merge_dlst(memory._delivered_load_speed_tests.get(key), dlst)
    memory._delivered_load_speed_tests[key] = merged

    pci = int(scope.get("pci", 0) or 0)
    cell_id = int(scope.get("cell_id", 0) or 0)
    for cell_key in (cell_id, pci):
        ck = memory.cells.get(cell_key)
        if ck is not None:
            cell_merged, cell_changed = merge_dlst(getattr(ck, "DLST", None), merged)
            ck.DLST = cell_merged
            changed = changed or cell_changed

    bands = parse_band_signature(scope.get("band_set_signature", ""))
    if not bands:
        band = int(scope.get("band", 0) or 0)
        bands = [band] if band > 0 else []
    for band in bands:
        bk = memory.bands.get(int(band))
        if bk is not None:
            band_merged, band_changed = merge_dlst(getattr(bk, "DLST", None), merged)
            bk.DLST = band_merged
            changed = changed or band_changed

    for store in (memory.band_learning_stm, memory.band_learning_ltm):
        for bucket_key, bucket in list(store.items()):
            if isinstance(bucket, dict) and learning_bucket_matches_dlst_scope(bucket_key, merged):
                changed = attach_dlst(bucket, merged) or changed
    confidence_store = getattr(memory, "band_confidence_ltm", {})
    band_sig = str(scope.get("band_set_signature", "") or "")
    pool_sig = str(scope.get("pool_signature", "") or "")
    for sig in {band_sig, pool_sig}:
        if sig and sig in confidence_store:
            changed = attach_dlst(confidence_store[sig], merged) or changed
    return bool(changed)


__all__ = [
    "OWNER",
    "apply_delivered_load_speed_test_record",
    "dlst_from_modem_computed",
    "learning_bucket_matches_dlst_scope",
    "lookup_dlst_for_modem",
    "record_delivered_load_speed_test",
]
