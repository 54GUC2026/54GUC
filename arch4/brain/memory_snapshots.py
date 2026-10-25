"""
arch4/brain/memory_snapshots.py
───────────────────────────────────────────────────────────────────────────────
Snapshot payload builders for Memory.

These helpers shape the STM/LTM/meta snapshot payloads but do not perform any
I/O. The Memory facade still owns when snapshots are written.
───────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import time
from typing import Any, Dict

from arch4.brain.memory_io import band_to_dict, cell_to_dict, record_to_dict
from arch4.brain.memory_objective_lifecycle_owner import (
    get_recent_objective_lifecycle as memory_get_recent_objective_lifecycle,
)
from arch4.interfaces import ARCH4_VERSION
from arch4.brain.reward_ledger import (
    reward_ledger_snapshot,
    reward_projection_effect_keys_snapshot,
    reward_projection_outbox_snapshot,
    reward_trajectory_snapshot,
)


def build_stm_snapshot(mem) -> Dict[str, Any]:
    return {
        "version": ARCH4_VERSION,
        "evidence_scope": dict(
            getattr(mem, "_current_memory_evidence_scope", {}) or {}
        ),
        "action_history": [record_to_dict(r) for r in mem.action_history[-500:]],
        "objective_lifecycle": memory_get_recent_objective_lifecycle(mem, limit=4000),
        **mem._band_learning_store.to_stm_dict(),
        "delivered_load_speed_tests": {
            str(k): dict(v) for k, v in mem._delivered_load_speed_tests.items()
        },
        "current_calibration_marker": mem.current_calibration_marker,
        "current_calibration_signature": mem.current_calibration_signature,
        "current_context_seen_actions": int(mem.current_context_seen_actions),
        "providers": mem._serialize_provider_stm_namespaces(),
        "provider_context": mem._current_provider_plmn,
        "reward_trajectory_feedback": reward_trajectory_snapshot(mem),
        "reward_projection_outbox": reward_projection_outbox_snapshot(mem),
    }


def build_ltm_snapshot(mem) -> Dict[str, Any]:
    return {
        "version": ARCH4_VERSION,
        "evidence_scope": dict(
            getattr(mem, "_current_memory_evidence_scope", {}) or {}
        ),
        "bands": {str(b): band_to_dict(bk) for b, bk in mem.bands.items()},
        "cells": {str(p): cell_to_dict(ck) for p, ck in mem.cells.items()},
        "baselines": {k: v.to_dict() for k, v in mem.baselines.items()},
        **mem._band_learning_store.to_ltm_dict(),
        **mem._band_confidence_store.to_dict(),
        "delivered_load_speed_tests": {
            str(k): dict(v) for k, v in mem._delivered_load_speed_tests.items()
        },
        "band_anchor_stability": {
            str(b): s for b, s in mem.band_anchor_stability.items()
        },
        "providers": mem._serialize_provider_ltm_namespaces(),
        "provider_context": mem._current_provider_plmn,
        "reward_ledger": reward_ledger_snapshot(mem),
        "reward_projection_effect_keys": reward_projection_effect_keys_snapshot(mem),
    }


def build_memory_meta(mem) -> Dict[str, Any]:
    return {
        "version": ARCH4_VERSION,
        "evidence_scope": dict(
            getattr(mem, "_current_memory_evidence_scope", {}) or {}
        ),
        "snapshot_layout": "phase6_periodic_checkpoint",
        "snapshot_authoritative": "memory.json",
        "stm_snapshot": mem._stm_snapshot_store.filename,
        "ltm_snapshot": mem._ltm_snapshot_store.filename,
        "meta_snapshot": mem._meta_snapshot_store.filename,
        "event_journal": mem._event_journal.filename,
        "event_journal_count": int(mem._journal_event_count),
        "event_journal_applied_count": int(mem._journal_applied_count),
        "checkpoint_interval_s": float(mem._checkpoint_interval_s),
        "checkpoint_max_dirty_events": int(mem._checkpoint_max_dirty_events),
        "cm_meta_snapshot": mem._calibration_store.cm_meta_snapshot.filename,
        "cm_profiles_snapshot": mem._calibration_store.cm_profiles_snapshot.filename,
        "cm_samples_journal": mem._calibration_store.cm_samples_journal.filename,
        "provider_context": mem._current_provider_plmn,
        "current_calibration_marker": str(mem.current_calibration_marker or "C0"),
        "current_context_seen_actions": int(mem.current_context_seen_actions),
        "journal_replay_enabled": True,
        "saved_ts": float(time.time()),
    }
