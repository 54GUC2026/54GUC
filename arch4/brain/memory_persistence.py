"""
arch4/brain/memory_persistence.py
───────────────────────────────────────────────────────────────────────────────
Persistence and journal helpers for Memory.

This module owns the operational persistence lane:
  - checkpoint scheduling
  - memory meta reads
  - runtime persistence status
  - journal compaction
  - journal replay/apply

The Memory class remains the public facade. These helpers are intentionally
state-light: they operate on the existing Memory instance and reuse its
existing typed/domain helpers instead of redefining learning semantics here.
───────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from arch4.atomic_io import atomic_write_json, fsync_parent_dir
from arch4.intelligence.checkpoint_store import (
    IntelligenceCheckpointSelection,
    checkpoint_manifest_exists,
    select_checkpoint,
)
from arch4.interfaces import ARCH4_VERSION, ARCH_QUALITY_FORMULA_EPOCH
from arch4.brain.band_learning import BandConfidenceStore
from arch4.brain.delivered_load_memory import normalize_dlst
from arch4.brain.memory_io import band_to_dict, cell_to_dict, dict_to_record, record_to_dict
from arch4.brain.memory_calibration_context_owner import (
    compact_calibration_alias_graph,
    load_calibration_context_state,
    resolve_marker_alias,
)
from arch4.brain.memory_calibration_store import calibration_db_write_health
from arch4.brain.memory_evidence_scope_owner import (
    memory_checkpoint_scope_admission,
    memory_snapshot_scope_error,
    quarantine_unscoped_snapshot,
)
from arch4.brain.memory_band_operational_history_owner import (
    apply_anchor_stability_record,
    apply_band_attempt_record,
    apply_blacklist_band_record,
)
from arch4.brain.memory_delivered_load_owner import apply_delivered_load_speed_test_record
from arch4.brain.memory_objective_lifecycle_owner import apply_lifecycle_journal_record
from arch4.brain.reward_ledger import (
    MILESTONE_PROJECTION_RECEIPT_EVENT_KIND,
    PROJECTION_RECEIPT_EVENT_KIND,
    TERMINAL_EVENT_KIND,
    apply_nonterminal_trajectory_entry,
    apply_trajectory_projection_receipt,
    apply_terminal_reward_entry,
    apply_reward_projection_receipt,
    quarantine_reward_evidence,
    restore_reward_ledger_snapshot,
    reward_evidence_quarantine_snapshot,
    reward_ledger_snapshot,
    reward_projection_effect_keys_snapshot,
    reward_projection_outbox_snapshot,
    reward_trajectory_snapshot,
)

log = logging.getLogger("arch4.brain.memory")
QUALITY_FORMULA_EPOCH = str(ARCH_QUALITY_FORMULA_EPOCH or "bq_cq_split_v1")
OPERATIONAL_COMPACTION_TRANSACTION_SCHEMA = "OperationalJournalCompaction.v1"
OPERATIONAL_COMPACTION_TRANSACTION_FILE = "memory_compaction_transaction.json"


def _reset_bucket_quality_fields(bucket_map: Dict[str, Dict[str, Any]]) -> None:
    for bucket in bucket_map.values():
        if not isinstance(bucket, dict):
            continue
        bucket["cq_avg"] = 0.0
        bucket["bq_avg"] = 0.0


def _reset_cell_quality_fields(cells: Dict[Any, Any]) -> None:
    for ck in cells.values():
        try:
            ck.avg_cq_cell = 0.0
            if hasattr(ck, "avg_bq_cell"):
                ck.avg_bq_cell = 0.0
        except Exception:
            continue


def _reset_quality_formula_dependent_fields(mem) -> None:
    _reset_cell_quality_fields(mem.cells)
    _reset_bucket_quality_fields(mem.band_learning_stm)
    _reset_bucket_quality_fields(mem.band_learning_ltm)
    for ns in mem._provider_namespaces.values():
        cells = ns.get("cells")
        if isinstance(cells, dict):
            _reset_cell_quality_fields(cells)
        store = ns.get("band_learning_store")
        if store is not None:
            _reset_bucket_quality_fields(getattr(store, "stm", {}))
            _reset_bucket_quality_fields(getattr(store, "ltm", {}))


def _maybe_migrate_quality_formula_epoch(mem, loaded_epoch: str) -> bool:
    current_epoch = str(QUALITY_FORMULA_EPOCH or "")
    previous_epoch = str(loaded_epoch or "")
    mem._quality_formula_epoch = current_epoch
    if previous_epoch == current_epoch:
        return False
    _reset_quality_formula_dependent_fields(mem)
    mem._quality_formula_reset_applied = True
    log.info(
        "Quality formula epoch %s → %s: reset persisted CQ-derived memory fields",
        previous_epoch or "<none>",
        current_epoch,
    )
    return True


@dataclass(slots=True)
class PreparedMemoryCheckpoint:
    """Memory serialization prepared for the Intelligence owner to commit."""

    reason: str
    previous_applied_count: int
    payloads: dict[str, Any]
    receipt: dict[str, Any]

    @property
    def deferred(self) -> bool:
        return str(self.receipt.get("status") or "") == "deferred"


def prepare_memory_checkpoint(
    mem: Any,
    *,
    checkpoint_reason: str,
) -> PreparedMemoryCheckpoint:
    """Serialize Memory without selecting or committing a checkpoint owner."""

    why = str(checkpoint_reason or "manual")
    previous_applied_count = int(mem._journal_applied_count)
    receipt: dict[str, Any] = {
        "schema": "MemoryCheckpointReceipt.v2",
        "owner": "arch4.brain.memory_persistence",
        "status": "preparing",
        "saved": False,
        "reason": why,
        "started_at": float(time.time()),
    }
    scope_admission = memory_checkpoint_scope_admission(mem)
    receipt["scope_admission"] = scope_admission.as_dict()
    if scope_admission.deferred:
        receipt.update(
            {
                "status": "deferred",
                "deferred": True,
                "defer_reason": scope_admission.reason,
                "finished_at": float(time.time()),
            }
        )
        return PreparedMemoryCheckpoint(
            reason=why,
            previous_applied_count=previous_applied_count,
            payloads={},
            receipt=receipt,
        )
    if not scope_admission.admitted:
        raise ValueError(
            "memory_checkpoint_scope_invalid:" + str(scope_admission.reason)
        )
    checkpoint_scope = dict(scope_admission.evidence_scope)
    mem._capture_current_namespace()
    mem._journal_applied_count = int(mem._journal_event_count)
    data = {
        "version": ARCH4_VERSION,
        "evidence_scope": checkpoint_scope,
        "quality_formula_epoch": str(
            getattr(mem, "_quality_formula_epoch", QUALITY_FORMULA_EPOCH)
            or QUALITY_FORMULA_EPOCH
        ),
        "action_history": [record_to_dict(row) for row in mem.action_history[-4000:]],
        "evidence_quarantine": list(
            getattr(mem, "_evidence_quarantine", [])[-256:]
        ),
        "bands": {str(key): band_to_dict(value) for key, value in mem.bands.items()},
        "cells": {str(key): cell_to_dict(value) for key, value in mem.cells.items()},
        "baselines": {key: value.to_dict() for key, value in mem.baselines.items()},
        **mem._band_learning_store.to_dict(),
        **mem._band_confidence_store.to_dict(),
        "calibration_contexts": mem.calibration_contexts,
        "calibration_aliases": mem.calibration_aliases,
        "current_calibration_marker": mem.current_calibration_marker,
        "current_calibration_signature": mem.current_calibration_signature,
        "current_context_seen_actions": int(mem.current_context_seen_actions),
        "band_anchor_stability": {
            str(key): value for key, value in mem.band_anchor_stability.items()
        },
        "anchor_stability_projection_keys": sorted(
            str(value)
            for value in getattr(mem, "_anchor_stability_projection_keys", set())
            if str(value or "")
        )[-1024:],
        "providers": mem._serialize_provider_namespaces(),
        "provider_context": mem._current_provider_plmn,
        "congestion_hourly": {
            context: {str(hour): slot for hour, slot in hours.items()}
            for context, hours in mem._congestion_hourly.items()
        },
        "dwell_tracking": {
            str(key): dict(value) for key, value in mem._dwell_tracking.items()
        },
        "congestion_precursors": list(
            mem._congestion_precursors[-mem._congestion_precursors_max :]
        ),
        "delivered_load_speed_tests": {
            str(key): normalize_dlst(value)
            for key, value in mem._delivered_load_speed_tests.items()
        },
        "reward_ledger": reward_ledger_snapshot(mem),
        "reward_trajectory_feedback": reward_trajectory_snapshot(mem),
        "reward_projection_outbox": reward_projection_outbox_snapshot(mem),
        "reward_projection_effect_keys": reward_projection_effect_keys_snapshot(mem),
        "reward_evidence_quarantine": reward_evidence_quarantine_snapshot(mem),
    }
    return PreparedMemoryCheckpoint(
        reason=why,
        previous_applied_count=previous_applied_count,
        payloads={
            "memory.json": data,
            "stm_snapshot.json": mem._build_stm_snapshot(),
            "ltm_snapshot.json": mem._build_ltm_snapshot(),
            "memory_meta.json": mem._build_memory_meta(),
        },
        receipt=receipt,
    )


def complete_memory_checkpoint(
    mem: Any,
    prepared: PreparedMemoryCheckpoint,
    *,
    selection: IntelligenceCheckpointSelection,
) -> dict[str, Any]:
    if not isinstance(prepared, PreparedMemoryCheckpoint):
        raise TypeError("prepared_memory_checkpoint_required")
    if not isinstance(selection, IntelligenceCheckpointSelection):
        raise TypeError("intelligence_checkpoint_selection_required")
    mem._intelligence_checkpoint_selection = selection
    if int(getattr(mem, "_load_skipped_action_records", 0) or 0) > 0:
        mem._load_skipped_action_records = 0
    if int(getattr(mem, "_load_skipped_provider_namespaces", 0) or 0) > 0:
        mem._load_skipped_provider_namespaces = 0
    mem._dirty = False
    mem._dirty_event_count = 0
    mem._last_snapshot_ts = float(time.time())
    mem._checkpoint_count += 1
    mem._last_checkpoint_reason = prepared.reason
    mem._checkpoint_reason_counts[prepared.reason] = int(
        mem._checkpoint_reason_counts.get(prepared.reason, 0) or 0
    ) + 1
    mem._checkpoint_failed = False
    mem._checkpoint_failed_reason = ""
    mem._checkpoint_failed_ts = 0.0
    receipt = dict(prepared.receipt)
    receipt.update(
        {
            "status": "committed",
            "saved": True,
            "authoritative": True,
            "generation": selection.generation,
            "checkpoint_count": int(mem._checkpoint_count),
            "journal_applied_count": int(mem._journal_applied_count),
            "finished_at": float(time.time()),
        }
    )
    try:
        maybe_auto_compact_journals(mem)
    except Exception as exc:
        log.error("Post-checkpoint operational journal compaction failed: %s", exc)
        receipt["maintenance_status"] = "degraded"
        receipt["maintenance_error"] = f"{type(exc).__name__}: {exc}"
    return receipt


def fail_memory_checkpoint(
    mem: Any,
    prepared: PreparedMemoryCheckpoint | None,
    *,
    error: BaseException,
) -> dict[str, Any]:
    if prepared is not None:
        mem._journal_applied_count = prepared.previous_applied_count
        receipt = dict(prepared.receipt)
    else:
        receipt = {
            "schema": "MemoryCheckpointReceipt.v2",
            "owner": "arch4.brain.memory_persistence",
            "started_at": float(time.time()),
        }
    mem._checkpoint_failed = True
    mem._checkpoint_failed_reason = str(error)
    mem._checkpoint_failed_ts = float(time.time())
    receipt.update(
        {
            "status": "failed",
            "saved": False,
            "error": f"{type(error).__name__}: {error}",
            "finished_at": float(time.time()),
        }
    )
    return receipt


def load_memory(mem) -> None:
    """Load memory snapshot, then replay operational journal as needed."""
    recover_operational_compaction(mem)
    mem._journal_event_count = mem._event_journal.count()
    mem._journal_applied_count = mem._journal_event_count
    mem._journal_replayed_count = 0
    mem._journal_replay_error_count = 0
    mem._quality_formula_epoch = str(QUALITY_FORMULA_EPOCH or "")
    mem._quality_formula_reset_applied = False
    selection = getattr(mem, "_intelligence_checkpoint_selection", None)
    if selection is None and checkpoint_manifest_exists(mem.memory_dir):
        selection = select_checkpoint(mem.memory_dir)
        mem._intelligence_checkpoint_selection = selection
    manifest_present = isinstance(selection, IntelligenceCheckpointSelection)
    selection_error = str(
        getattr(mem, "_intelligence_checkpoint_error", "") or ""
    )
    if not manifest_present and selection_error:
        log.error("No valid Intelligence checkpoint remains after quarantine; starting fresh")
        replayed = replay_journal_events(mem, 0)
        if replayed:
            mem._mark_dirty(events=replayed)
        return
    if not manifest_present:
        if mem._snapshot_store.exists():
            raise RuntimeError(
                "fixed_memory_restart_reader_deleted:"
                "use_intelligence_checkpoint_store"
            )
        log.info("No memory file found, starting fresh")
        replayed = replay_journal_events(mem, 0)
        if replayed:
            log.info("Recovered %d memory journal events without snapshot", replayed)
            mem._mark_dirty(events=replayed)
        return
    try:
        raw = selection.artifact("memory.json")
        meta = selection.artifact("memory_meta.json")
        load_receipt = dict(selection.receipt)
        load_receipt["artifact"] = "memory.json"
        mem._checkpoint_load_receipt = load_receipt
        if bool(selection.recovered_previous):
            log.warning(
                "Current Intelligence checkpoint invalid; recovered previous-good generation %s",
                selection.generation,
            )
        data = raw if isinstance(raw, dict) else {}
        meta = meta if isinstance(meta, dict) else {}
        mem._journal_applied_count = snapshot_applied_journal_count(
            mem,
            mem._journal_event_count,
            meta=meta,
        )
        mem._last_snapshot_ts = float(meta.get("saved_ts", time.time()) or time.time())

        _load_skipped_records = 0
        for d in data.get("action_history", []):
            try:
                mem._append_action_history_record(dict_to_record(d))
            except Exception as e:
                _load_skipped_records += 1
                log.warning("Skipped bad action_history record: %s", e)
        if _load_skipped_records > 0:
            mem._load_skipped_action_records = _load_skipped_records
            mem._mark_dirty(events=_load_skipped_records)
            log.warning(
                "Memory load skipped %d corrupted action_history records",
                _load_skipped_records,
            )
        # Old checkpoint objective_lifecycle entries are skipped. They are now
        # unified into action_history via record_lifecycle_event().

        snapshot_scope_error = memory_snapshot_scope_error(
            data.get("evidence_scope") if isinstance(data.get("evidence_scope"), dict) else {}
        )
        if snapshot_scope_error:
            quarantine_unscoped_snapshot(mem, data, snapshot_scope_error)
            # Action rows were individually validated above.  Every aggregate
            # below is unscoped/foreign and therefore cannot enter active STM,
            # LTM, historical baselines, capacity, or delayed-reward state.
            data = {
                "version": data.get("version", ARCH4_VERSION),
                "quality_formula_epoch": data.get("quality_formula_epoch", ""),
                "evidence_quarantine": list(data.get("evidence_quarantine", []) or []),
            }
        else:
            mem._current_memory_evidence_scope = dict(data.get("evidence_scope") or {})

        main_ns = mem._deserialize_provider_namespace(data)
        mem.bands = main_ns["bands"]
        mem.cells = main_ns["cells"]
        mem.baselines = main_ns["baselines"]
        mem._band_learning_store = main_ns["band_learning_store"]
        mem.calibration_contexts = main_ns["calibration_contexts"]
        mem.calibration_aliases = main_ns["calibration_aliases"]
        mem.current_calibration_marker = main_ns["current_calibration_marker"]
        mem.current_calibration_signature = main_ns["current_calibration_signature"]
        mem.current_context_seen_actions = int(main_ns["current_context_seen_actions"] or 0)
        mem.band_anchor_stability = main_ns["band_anchor_stability"]
        mem._band_confidence_store = BandConfidenceStore.from_dict(data)
        compact_calibration_alias_graph(mem)
        mem.current_calibration_marker = resolve_marker_alias(mem, mem.current_calibration_marker)

        version = data.get("version", "v1")
        if version != ARCH4_VERSION:
            log.info("Memory version %s → %s (migrating)", version, ARCH4_VERSION)
        loaded_quality_epoch = str(data.get("quality_formula_epoch", "") or "")

        core_keys = {
            "action_history", "objective_lifecycle", "bands", "cells", "baselines",
            "band_learning_stm", "band_learning_ltm", "band_confidence_ltm",
            "calibration_contexts", "calibration_aliases", "current_calibration_marker",
            "current_calibration_signature", "current_context_seen_actions",
            "band_blacklist", "band_blacklist_records", "band_anchor_stability",
            "anchor_stability_projection_keys",
            "providers", "provider_context", "version", "quality_formula_epoch",
            "evidence_scope", "evidence_quarantine",
            "congestion_hourly",
            "dwell_tracking",
            "congestion_precursors",
            "delivered_load_speed_tests",
            "reward_ledger",
            "reward_trajectory_feedback",
            "reward_projection_outbox",
            "reward_projection_effect_keys",
            "reward_evidence_quarantine",
        }
        persisted_quarantine = [
            dict(row)
            for row in data.get("evidence_quarantine", [])[-256:]
            if isinstance(row, dict)
        ]
        mem._reward_evidence_quarantine = [
            dict(row)
            for row in data.get("reward_evidence_quarantine", [])[-512:]
            if isinstance(row, dict)
        ]
        mem._evidence_quarantine = (
            persisted_quarantine
            + list(getattr(mem, "_evidence_quarantine", []) or [])
        )[-256:]
        mem._anchor_stability_projection_keys = {
            str(value)
            for value in list(data.get("anchor_stability_projection_keys") or [])[-1024:]
            if str(value or "")
        }

        # ── Restore temporal congestion profile ──
        raw_ch = data.get("congestion_hourly")
        if isinstance(raw_ch, dict):
            restored: Dict[str, Dict[int, Dict[str, float]]] = {}
            for ctx, hours in raw_ch.items():
                if isinstance(hours, dict):
                    restored[str(ctx)] = {
                        int(h): {
                            "ema": float(slot.get("ema", 0.0) or 0.0),
                            "samples": int(slot.get("samples", 0) or 0),
                        }
                        for h, slot in hours.items()
                        if isinstance(slot, dict)
                    }
            mem._congestion_hourly = restored

        # ── Restore dwell tracking ──
        raw_dw = data.get("dwell_tracking")
        if isinstance(raw_dw, dict):
            for b, entry in raw_dw.items():
                if isinstance(entry, dict):
                    mem._dwell_tracking[int(b)] = {
                        "total_dwell_s": float(entry.get("total_dwell_s", 0.0) or 0.0),
                        "switch_count": int(entry.get("switch_count", 0) or 0),
                        "short_dwell_count": int(entry.get("short_dwell_count", 0) or 0),
                        "lower_controller_episode_count": int(
                            entry.get("lower_controller_episode_count", 0) or 0
                        ),
                        "guc_command_episode_count": int(
                            entry.get("guc_command_episode_count", 0) or 0
                        ),
                        "attribution_unavailable_episode_count": int(
                            entry.get("attribution_unavailable_episode_count", 0) or 0
                        ),
                    }

        # ── Restore congestion precursors ──
        raw_cp = data.get("congestion_precursors")
        if isinstance(raw_cp, list):
            mem._congestion_precursors = [
                p for p in raw_cp
                if isinstance(p, dict) and "ctx" in p and "fingerprint" in p
            ][-mem._congestion_precursors_max:]

        # ── Restore delivered-load-speed-test memory ──
        raw_dlst = data.get("delivered_load_speed_tests")
        if isinstance(raw_dlst, dict):
            mem._delivered_load_speed_tests = {
                str(k): normalize_dlst(v)
                for k, v in raw_dlst.items()
            }

        restore_reward_ledger_snapshot(
            mem,
            terminal_rows=(
                data.get("reward_ledger")
                if isinstance(data.get("reward_ledger"), dict)
                else {}
            ),
            trajectory_rows=(
                data.get("reward_trajectory_feedback")
                if isinstance(data.get("reward_trajectory_feedback"), dict)
                else {}
            ),
            projection_rows=(
                data.get("reward_projection_outbox")
                if isinstance(data.get("reward_projection_outbox"), dict)
                else {}
            ),
            projection_effect_keys=data.get("reward_projection_effect_keys")
            if isinstance(data.get("reward_projection_effect_keys"), list)
            else [],
        )

        mem._capture_current_namespace()
        _provider_load_errors = 0
        for plmn, ns_d in data.get("providers", {}).items():
            try:
                mem._provider_namespaces[plmn] = mem._deserialize_provider_namespace(ns_d)
            except Exception as e:
                _provider_load_errors += 1
                log.warning("Skipped corrupted provider namespace %s: %s", plmn, e)
        if _provider_load_errors > 0:
            mem._load_skipped_provider_namespaces = _provider_load_errors
            mem._mark_dirty(events=_provider_load_errors)
            log.warning(
                "Memory load skipped %d corrupted provider namespaces",
                _provider_load_errors,
            )
        migrated_quality_epoch = _maybe_migrate_quality_formula_epoch(mem, loaded_quality_epoch)
        mem.set_provider_context(data.get("provider_context", "") or "")
        if not isinstance(selection, IntelligenceCheckpointSelection):
            load_calibration_context_state(mem)
        replayed = replay_journal_events(mem, mem._journal_applied_count)
        if replayed:
            log.info("Replayed %d memory journal events after snapshot", replayed)
            mem._mark_dirty(events=replayed)
        elif migrated_quality_epoch:
            mem._dirty = True
            mem._dirty_event_count = max(1, int(getattr(mem, "_dirty_event_count", 0) or 0))

        log.info(
            "Loaded memory: %d actions, %d bands, %d cells, %d baselines",
            len(mem.action_history), len(mem.bands), len(mem.cells), len(mem.baselines),
        )
    except Exception as e:
        log.error("Memory load failed: %s — starting fresh", e)
        # Mark the failure on the memory object so the engine can surface it
        # as a GUI-visible warning (operator may not watch logs).
        mem._load_failed = True
        mem._load_failed_reason = str(e)


def needs_checkpoint(mem, *, now: Optional[float] = None, force: bool = False) -> bool:
    return checkpoint_due_reason(mem, now=now, force=force) is not None


def checkpoint_due_reason(
    mem,
    *,
    now: Optional[float] = None,
    force: bool = False,
) -> Optional[str]:
    if force:
        return "force"
    if not mem._dirty:
        return None
    ts = time.time() if now is None else float(now)
    if mem._dirty_event_count >= int(mem._checkpoint_max_dirty_events):
        return "dirty_event_threshold"
    if (ts - float(mem._last_snapshot_ts)) >= float(mem._checkpoint_interval_s):
        return "interval_elapsed"
    return None


def read_memory_meta(mem) -> Dict[str, Any]:
    selection = getattr(mem, "_intelligence_checkpoint_selection", None)
    if isinstance(selection, IntelligenceCheckpointSelection):
        raw = selection.artifact("memory_meta.json")
    else:
        raw = mem._meta_snapshot_store.read() if mem._meta_snapshot_store.exists() else None
    return raw if isinstance(raw, dict) else {}


def get_runtime_status(mem) -> Dict[str, Any]:
    calibration_db_health = calibration_db_write_health()
    io_stats = {
        "memory_snapshot": mem._snapshot_store.stats(),
        "stm_snapshot": mem._stm_snapshot_store.stats(),
        "ltm_snapshot": mem._ltm_snapshot_store.stats(),
        "meta_snapshot": mem._meta_snapshot_store.stats(),
        "event_journal": mem._event_journal.stats(),
        "calibration_store": mem._calibration_store.get_runtime_stats(),
        "calibration_db_write_health": calibration_db_health,
    }
    return {
        "dirty": bool(mem._dirty),
        "dirty_event_count": int(mem._dirty_event_count),
        "event_journal_count": int(mem._journal_event_count),
        "event_journal_applied_count": int(mem._journal_applied_count),
        "event_journal_replayed_count": int(mem._journal_replayed_count),
        "event_journal_replay_error_count": int(getattr(mem, "_journal_replay_error_count", 0) or 0),
        "checkpoint_interval_s": float(mem._checkpoint_interval_s),
        "checkpoint_max_dirty_events": int(mem._checkpoint_max_dirty_events),
        "checkpoint_count": int(mem._checkpoint_count),
        "last_checkpoint_reason": str(mem._last_checkpoint_reason or ""),
        "checkpoint_reason_counts": dict(mem._checkpoint_reason_counts or {}),
        "last_snapshot_ts": float(mem._last_snapshot_ts),
        "auto_compaction_enabled": bool(mem._auto_compaction_enabled),
        "operational_journal_auto_compact_at": int(mem._operational_journal_auto_compact_at),
        "operational_journal_auto_keep_last_applied": int(mem._operational_journal_auto_keep_last_applied),
        "cm_samples_auto_compact_at": int(mem._cm_samples_auto_compact_at),
        "cm_samples_auto_keep_last": int(mem._cm_samples_auto_keep_last),
        "last_operational_compaction": dict(mem._last_operational_compaction or {}),
        "last_cm_compaction": dict(mem._last_cm_compaction or {}),
        "io": io_stats,
        "calibration_db_write_health": calibration_db_health,
        # Load/checkpoint failure tracking for health monitoring
        "load_failed": bool(getattr(mem, "_load_failed", False)),
        "load_failed_reason": str(getattr(mem, "_load_failed_reason", "") or ""),
        "load_skipped_action_records": int(getattr(mem, "_load_skipped_action_records", 0) or 0),
        "load_skipped_provider_namespaces": int(getattr(mem, "_load_skipped_provider_namespaces", 0) or 0),
        "checkpoint_failed": bool(getattr(mem, "_checkpoint_failed", False)),
        "checkpoint_failed_reason": str(getattr(mem, "_checkpoint_failed_reason", "") or ""),
        "checkpoint_failed_ts": float(getattr(mem, "_checkpoint_failed_ts", 0.0) or 0.0),
        "checkpoint_load": dict(getattr(mem, "_checkpoint_load_receipt", {}) or {}),
    }


def get_persistence_health(mem) -> Dict[str, Any]:
    """Compact persistence health summary for status payload and GUI.

    Returns a small dict suitable for embedding in extra["persistence"].
    GUI can show this as a single status line.
    """
    load_failed = bool(getattr(mem, "_load_failed", False))
    checkpoint_failed = bool(getattr(mem, "_checkpoint_failed", False))
    skipped_records = int(getattr(mem, "_load_skipped_action_records", 0) or 0)
    skipped_providers = int(getattr(mem, "_load_skipped_provider_namespaces", 0) or 0)
    checkpoint_count = int(getattr(mem, "_checkpoint_count", 0) or 0)

    # Determine overall health status
    if load_failed:
        status = "degraded"
        status_text = "Memory load failed — running fresh"
    elif checkpoint_failed:
        status = "degraded"
        reason = str(getattr(mem, "_checkpoint_failed_reason", "") or "unknown")
        # Truncate long reasons for compact display
        if len(reason) > 40:
            reason = reason[:37] + "..."
        status_text = f"Checkpoint failed: {reason}"
    elif skipped_records > 0 or skipped_providers > 0:
        status = "partial"
        parts = []
        if skipped_records > 0:
            parts.append(f"{skipped_records} records")
        if skipped_providers > 0:
            parts.append(f"{skipped_providers} providers")
        status_text = f"Skipped corrupted: {', '.join(parts)}"
    else:
        status = "healthy"
        status_text = f"OK ({checkpoint_count} checkpoints)"

    return {
        "status": status,
        "status_text": status_text,
        "load_failed": load_failed,
        "checkpoint_failed": checkpoint_failed,
        "checkpoint_count": checkpoint_count,
        "skipped_records": skipped_records,
        "skipped_providers": skipped_providers,
    }


def snapshot_applied_journal_count(
    mem,
    journal_count: int,
    *,
    meta: Optional[Dict[str, Any]] = None,
) -> int:
    meta = dict(meta or read_memory_meta(mem))
    if "event_journal_applied_count" not in meta:
        return int(journal_count)
    try:
        applied = int(meta.get("event_journal_applied_count", journal_count) or 0)
    except Exception:
        return int(journal_count)
    return max(0, min(int(journal_count), applied))


def _journal_bytes(events: list[Dict[str, Any]]) -> bytes:
    return "".join(
        json.dumps(dict(event), ensure_ascii=True) + "\n"
        for event in events
    ).encode("utf-8")


def _journal_file_digest(path: Path) -> tuple[str, int]:
    data = path.read_bytes() if path.exists() else b""
    count = 0
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            decoded = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(decoded, dict):
            count += 1
    return hashlib.sha256(data).hexdigest(), count


def _compaction_transaction_path(mem) -> Path:
    return Path(mem.memory_dir) / OPERATIONAL_COMPACTION_TRANSACTION_FILE


def _commit_compaction_meta(mem, meta: Dict[str, Any], *, reason: str) -> None:
    # The compactor cannot select restart truth. Only the Intelligence
    # persistence coordinator may publish a generation; this file is a mirror.
    mem._meta_snapshot_store.write(meta, indent=2, sort_keys=True)


def recover_operational_compaction(mem) -> None:
    """Finish or discard one interrupted journal-compaction transaction."""

    transaction_path = _compaction_transaction_path(mem)
    if not transaction_path.exists():
        return
    try:
        raw = json.loads(transaction_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"operational compaction transaction unreadable:{type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(raw, dict) or str(raw.get("schema", "") or "") != (
        OPERATIONAL_COMPACTION_TRANSACTION_SCHEMA
    ):
        raise RuntimeError("operational compaction transaction schema invalid")

    actual_sha, actual_count = _journal_file_digest(mem._event_journal.path)
    after_matches = (
        actual_sha == str(raw.get("after_sha256", "") or "")
        and actual_count == int(raw.get("after_count", -1))
    )
    before_matches = (
        actual_sha == str(raw.get("before_sha256", "") or "")
        and actual_count == int(raw.get("before_count", -1))
    )
    if after_matches:
        meta = raw.get("meta_after")
        if not isinstance(meta, dict):
            raise RuntimeError("operational compaction transaction missing meta_after")
        _commit_compaction_meta(
            mem,
            dict(meta),
            reason="recover_operational_journal_compaction",
        )
    elif not before_matches:
        raise RuntimeError(
            "operational journal differs from both pre- and post-compaction evidence"
        )
    transaction_path.unlink()
    fsync_parent_dir(transaction_path, logger=log)


def compact_operational_journal(mem, *, keep_last_applied: int = 500) -> Dict[str, int]:
    events = mem._event_journal.read_all()
    total_before = len(events)
    if total_before <= 0:
        return {"before": 0, "after": 0, "applied_before": 0, "applied_after": 0, "dropped": 0}

    meta = read_memory_meta(mem)
    applied_before = snapshot_applied_journal_count(mem, total_before, meta=meta)
    keep_last = max(0, int(keep_last_applied or 0))
    applied_prefix = events[:applied_before]
    unapplied_tail = events[applied_before:]
    if unapplied_tail:
        raise RuntimeError(
            "operational journal compaction requires an Intelligence checkpoint first; "
            "unapplied valid-event tail is present"
        )

    if keep_last > 0:
        older = applied_prefix[:-keep_last]
        recent = applied_prefix[-keep_last:]
    else:
        older = applied_prefix
        recent = []
    retained_prefix = [ev for ev in older if should_keep_compacted_operational_event(ev)]
    retained_prefix.extend(recent)

    new_events = retained_prefix + unapplied_tail
    applied_after = len(retained_prefix)
    meta["event_journal"] = mem._event_journal.filename
    meta["event_journal_count"] = int(len(new_events))
    meta["event_journal_applied_count"] = int(applied_after)
    meta["last_compacted_ts"] = float(time.time())
    meta["last_compacted_dropped"] = max(0, total_before - len(new_events))

    before_sha, before_count = _journal_file_digest(mem._event_journal.path)
    after_bytes = _journal_bytes(new_events)
    transaction_path = _compaction_transaction_path(mem)
    atomic_write_json(
        transaction_path,
        {
            "schema": OPERATIONAL_COMPACTION_TRANSACTION_SCHEMA,
            "owner": "arch4.brain.memory_persistence",
            "prepared_at": float(time.time()),
            "before_sha256": before_sha,
            "before_count": int(before_count),
            "after_sha256": hashlib.sha256(after_bytes).hexdigest(),
            "after_count": int(len(new_events)),
            "meta_after": dict(meta),
        },
        indent=2,
        sort_keys=True,
    )
    mem._event_journal.rewrite(new_events)
    mem._journal_event_count = len(new_events)
    mem._journal_applied_count = applied_after
    _commit_compaction_meta(
        mem,
        meta,
        reason="operational_journal_compaction",
    )
    transaction_path.unlink()
    fsync_parent_dir(transaction_path, logger=log)

    return {
        "before": total_before,
        "after": len(new_events),
        "applied_before": applied_before,
        "applied_after": applied_after,
        "dropped": max(0, total_before - len(new_events)),
    }


def maybe_auto_compact_journals(mem) -> None:
    if not bool(mem._auto_compaction_enabled):
        return
    try:
        if int(mem._journal_event_count) >= int(mem._operational_journal_auto_compact_at):
            stats = compact_operational_journal(
                mem,
                keep_last_applied=int(mem._operational_journal_auto_keep_last_applied),
            )
            mem._last_operational_compaction = dict(stats)
            if int(stats.get("dropped", 0) or 0) > 0:
                log.info(
                    "Auto-compacted operational journal: before=%d after=%d dropped=%d",
                    int(stats.get("before", 0) or 0),
                    int(stats.get("after", 0) or 0),
                    int(stats.get("dropped", 0) or 0),
                )
    except Exception as e:
        log.warning("Automatic operational journal compaction failed: %s", e)
    # CalibrationAuthority is the sole CM sample journal writer/compactor.
    # Memory still owns its operational journal, but must not mutate the
    # calibration evidence journal during checkpoint.


def should_keep_compacted_operational_event(event: Dict[str, Any]) -> bool:
    if not isinstance(event, dict):
        return True
    kind = str(event.get("kind", "") or "")
    if kind in {
        TERMINAL_EVENT_KIND,
        PROJECTION_RECEIPT_EVENT_KIND,
        MILESTONE_PROJECTION_RECEIPT_EVENT_KIND,
        "action_proof_feedback",
        "staged_roi_projection_state",
    }:
        return True
    record = event.get("record")
    if not isinstance(record, dict):
        return True
    if kind in {"action_record", "staged_roi_action_record", "action_learning_commit", "action_proof_close"}:
        success = bool(record.get("success", False))
        hard_fail = bool(record.get("hard_fail", False))
        outcome = str(record.get("outcome_label", "") or "").strip().lower()
        incident_class = str(record.get("incident_class", "") or "").strip().lower()
        improvement = float(record.get("improvement", 0.0) or 0.0)
        commit_status = str(record.get("learning_commit_status", "") or "").strip().lower()
        if kind in {"action_learning_commit", "action_proof_close"} or commit_status == "committed":
            return True
        return (
            (not success)
            or hard_fail
            or outcome == "bad"
            or incident_class not in {"", "healthy"}
            or improvement >= 10.0
            or improvement <= -5.0
        )
    if kind == "objective_lifecycle":
        status = str(record.get("status", "") or "").strip().lower()
        reason = str(record.get("reason", "") or "").strip().lower()
        if any(token in status for token in ("retry", "fail", "cooldown", "blocked")):
            return True
        return any(token in reason for token in ("fail", "error", "no_effect", "emergency"))
    if kind == "delivered_load_speed_test":
        return True
    if kind == "lifecycle_event":
        status = str(record.get("status", "") or "").strip().lower()
        reason = str(record.get("reason", "") or "").strip().lower()
        if any(token in status for token in ("retry", "fail", "cooldown", "blocked")):
            return True
        return any(token in reason for token in ("fail", "error", "no_effect", "emergency"))
    if kind == "band_attempt":
        outcome = str(record.get("outcome", "") or "").strip().lower()
        appeared = bool(record.get("appeared_in_ca", False))
        weak_unusable = bool(record.get("weak_unusable", False))
        return weak_unusable or (not appeared) or any(
            token in outcome for token in ("absent", "unusable", "fail", "fallback")
        )
    if kind == "anchor_stability":
        return not bool(record.get("stuck", False))
    if kind == "blacklist_band":
        return True
    return True


def replay_journal_events(mem, start_index: int) -> int:
    if int(start_index or 0) >= int(mem._journal_event_count):
        return 0
    replayed = 0
    errors = 0
    original_provider = str(mem._current_provider_plmn or "")
    for idx, event in enumerate(mem._event_journal.iter_from(start_index), start=int(start_index or 0)):
        try:
            replayed += apply_journal_event(mem, event)
        except Exception as e:
            errors += 1
            kind = str(event.get("kind", "") or "") if isinstance(event, dict) else ""
            log.warning(
                "Memory journal replay skipped event idx=%d kind=%s: %s",
                idx,
                kind or "<unknown>",
                e,
            )
    if original_provider != mem._current_provider_plmn:
        mem.set_provider_context(original_provider)
    mem._sync_current_context_seen_actions()
    mem._journal_replayed_count = replayed
    mem._journal_replay_error_count = errors
    mem._journal_applied_count = mem._journal_event_count
    return replayed


def apply_journal_event(mem, event: Dict[str, Any]) -> int:
    if not isinstance(event, dict):
        return 0
    kind = str(event.get("kind", "") or "")
    record = event.get("record")
    if kind == TERMINAL_EVENT_KIND and isinstance(record, dict):
        try:
            apply_terminal_reward_entry(mem, record)
        except (TypeError, ValueError) as exc:
            quarantine_reward_evidence(
                mem,
                kind="terminal_reward",
                reason=str(exc),
                row=record,
                source="operational_journal",
            )
        return 1
    if kind == PROJECTION_RECEIPT_EVENT_KIND and isinstance(record, dict):
        try:
            apply_reward_projection_receipt(mem, record)
        except (TypeError, ValueError) as exc:
            quarantine_reward_evidence(
                mem,
                kind="reward_projection_receipt",
                reason=str(exc),
                row=record,
                source="operational_journal",
            )
        return 1
    if (
        kind == MILESTONE_PROJECTION_RECEIPT_EVENT_KIND
        and isinstance(record, dict)
    ):
        try:
            apply_trajectory_projection_receipt(mem, record)
        except (TypeError, ValueError) as exc:
            quarantine_reward_evidence(
                mem,
                kind="trajectory_projection_receipt",
                reason=str(exc),
                row=record,
                source="operational_journal",
            )
        return 1
    if kind == "action_proof_feedback" and isinstance(record, dict):
        rec = dict_to_record(record)
        mem._upsert_action_history_record(rec)
        trajectory = event.get("trajectory_feedback")
        if isinstance(trajectory, dict):
            try:
                apply_nonterminal_trajectory_entry(mem, trajectory)
            except (TypeError, ValueError) as exc:
                quarantine_reward_evidence(
                    mem,
                    kind="trajectory_milestone",
                    reason=str(exc),
                    row=trajectory,
                    source="operational_journal",
                )
        return 1
    if kind == "action_record" and isinstance(record, dict):
        rec = dict_to_record(record)
        mem._append_action_history_record(rec)
        return 1
    if kind == "staged_roi_action_record" and isinstance(record, dict):
        rec = dict_to_record(record)
        mem._upsert_action_history_record(rec)
        return 1
    if kind == "staged_roi_projection_state" and isinstance(record, dict):
        rec = dict_to_record(record)
        mem._upsert_action_history_record(rec)
        return 1
    if kind == "action_learning_commit" and isinstance(record, dict):
        quarantine_reward_evidence(
            mem,
            kind="legacy_immediate_action_learning",
            reason="generic_action_learning_replay_forbidden",
            row=record,
            source="operational_journal",
        )
        return 1
    if kind == "action_proof_close" and isinstance(record, dict):
        rec = dict_to_record(record)
        mem._upsert_action_history_record(rec)
        return 1
    if kind == "delivered_load_speed_test" and isinstance(record, dict):
        return 1 if apply_delivered_load_speed_test_record(mem, record) else 0
    if kind == "objective_lifecycle" and isinstance(record, dict):
        apply_lifecycle_journal_record(mem, record)
        return 1
    if kind == "lifecycle_event" and isinstance(record, dict):
        apply_lifecycle_journal_record(mem, record)
        return 1
    if kind == "band_attempt" and isinstance(record, dict):
        apply_band_attempt_record(mem, record, emit_logs=False)
        return 1
    if kind == "anchor_stability" and isinstance(record, dict):
        apply_anchor_stability_record(mem, record, emit_logs=False)
        return 1
    if kind == "blacklist_band" and isinstance(record, dict):
        return 1 if apply_blacklist_band_record(mem, record, emit_logs=False) else 0
    return 0
