from arch4.controller_frames import DerivedSignalFrames
"""
arch4/brain/memory.py
═══════════════════════════════════════════════════════════════
ARCH4 v1 — Learning Memory

Replaces T1-T5 time zones with signal-condition-based learning.
The brain remembers:
  - Action → result pairs tagged by signal conditions
  - Band discovery results (what works, what doesn't)
  - Per-cell performance history
  - Learned baselines for "what's normal under these conditions"

Memory persists to disk (JSON) across brain restarts.
═══════════════════════════════════════════════════════════════
"""

from arch4.controller_frames import CellularIntelligenceGeneration

import time
import logging
import threading
from pathlib import Path
from typing import Callable, Optional, List, Dict, Any

from arch4.interfaces import (
    ActionRecord, BandKnowledge, CellKnowledge,
    LeverType, Arch4Config,
    ARCH_RUNTIME_MEMORY_DIR,
    ARCH_QUALITY_FORMULA_EPOCH,
)
from arch4.customer_contracts.settings import IntelligencePreferences
from arch4.config_views import build_memory_learning_config
from arch4.memory_store import CalibrationStore, JsonSnapshotStore, JsonlEventJournal
from arch4.brain.memory_baselines import BaselineStats
from arch4.brain.memory_action_store import ActionHistoryStore
from arch4.brain.memory_namespaces import (
    bind_namespace,
    capture_current_namespace,
    deserialize_provider_namespace,
    new_provider_namespace,
    serialize_provider_calibration_namespaces,
    serialize_provider_ltm_namespaces,
    serialize_provider_namespaces,
    serialize_provider_stm_namespaces,
    set_provider_context as namespace_set_provider_context,
)
from arch4.brain.memory_snapshots import (
    build_ltm_snapshot,
    build_memory_meta,
    build_stm_snapshot,
)
from arch4.brain.memory_persistence import (
    apply_journal_event,
    compact_operational_journal,
    get_runtime_status,
    load_memory,
    maybe_auto_compact_journals,
    QUALITY_FORMULA_EPOCH,
    read_memory_meta,
    replay_journal_events,
    snapshot_applied_journal_count,
)
from arch4.brain.band_learning import BandConfidenceStore, BandLearningStore
from arch4.brain.memory_calibration_context_owner import (
    blend_signature as calibration_context_blend_signature,
    calibration_similarity as calibration_context_similarity,
    compact_calibration_alias_graph as calibration_context_compact_alias_graph,
    context_seed as calibration_context_seed,
    maybe_reconcile_mature_contexts as calibration_context_maybe_reconcile_mature_contexts,
    merge_marker_learning as calibration_context_merge_marker_learning,
    resolve_marker_alias as calibration_context_resolve_marker_alias,
    resolve_or_create_provisional_marker as calibration_context_resolve_or_create_marker,
    set_calibration_context as calibration_context_set_calibration_context,
    sync_current_context_seen_actions as calibration_context_sync_seen_actions,
)
from arch4.brain.memory_calibration_store import (
    init_calibration_db,
    prune_calibration_db,
)

log = logging.getLogger("arch4.brain.memory")

# Number of permanent failures before blacklisting a band at this location
BLACKLIST_THRESHOLD = 5
MEMORY_CHECKPOINT_INTERVAL_S = 300.0
MEMORY_CHECKPOINT_MAX_DIRTY_EVENTS = 100
MEMORY_JOURNAL_AUTO_COMPACT_AT = 5000
MEMORY_JOURNAL_AUTO_KEEP_LAST_APPLIED = 1000
CM_SAMPLES_AUTO_COMPACT_AT = 10000
CM_SAMPLES_AUTO_KEEP_LAST = 2000
CM_SAMPLES_AUTO_GAP_THRESHOLD = 0.4


class Memory:
    """
    Persistent learning memory for the ARCH4 brain.
    
    Three subsystems:
      1. Action history — what we did, under what conditions, what happened
      2. Band knowledge — which bands work, which don't, CA combinations
      3. Baseline tracker — what's "normal" performance under current conditions
    """

    def __init__(
        self,
        config: Arch4Config,
        memory_dir: str = "",
        *,
        intelligence_checkpoint_selection: Any = None,
        intelligence_checkpoint_error: str = "",
        intelligence_preferences: IntelligencePreferences | None = None,
    ):
        self.cfg = config
        self.learning_cfg = build_memory_learning_config(config)
        self._quality_formula_epoch = str(QUALITY_FORMULA_EPOCH or ARCH_QUALITY_FORMULA_EPOCH)
        self._quality_formula_reset_applied = False
        requested_dir = str(memory_dir or "").strip()
        if requested_dir:
            selected_dir = Path(requested_dir)
        else:
            selected_dir = Path(f"./{ARCH_RUNTIME_MEMORY_DIR}")
        self.memory_dir = selected_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._snapshot_store = JsonSnapshotStore(self.memory_dir, "memory.json")
        self._stm_snapshot_store = JsonSnapshotStore(self.memory_dir, "stm_snapshot.json")
        self._ltm_snapshot_store = JsonSnapshotStore(self.memory_dir, "ltm_snapshot.json")
        self._meta_snapshot_store = JsonSnapshotStore(self.memory_dir, "memory_meta.json")
        # One Intelligence-selected generation is injected before learning
        # readers run. Fixed snapshots are output projections only.
        self._intelligence_checkpoint_selection = intelligence_checkpoint_selection
        self._intelligence_checkpoint_error = str(
            intelligence_checkpoint_error or ""
        )
        self._event_journal = JsonlEventJournal(self.memory_dir, "memory_events.jsonl")
        self._calibration_store = CalibrationStore(self.memory_dir)
        self._journal_event_count: int = 0
        self._journal_applied_count: int = 0
        self._journal_replayed_count: int = 0
        self._journal_replay_error_count: int = 0
        self._checkpoint_interval_s: float = MEMORY_CHECKPOINT_INTERVAL_S
        self._checkpoint_max_dirty_events: int = MEMORY_CHECKPOINT_MAX_DIRTY_EVENTS
        self._dirty: bool = False
        self._dirty_event_count: int = 0
        self._last_snapshot_ts: float = time.time()
        self._auto_compaction_enabled: bool = True
        self._operational_journal_auto_compact_at: int = MEMORY_JOURNAL_AUTO_COMPACT_AT
        self._operational_journal_auto_keep_last_applied: int = MEMORY_JOURNAL_AUTO_KEEP_LAST_APPLIED
        self._cm_samples_auto_compact_at: int = CM_SAMPLES_AUTO_COMPACT_AT
        self._cm_samples_auto_keep_last: int = CM_SAMPLES_AUTO_KEEP_LAST
        self._cm_samples_auto_gap_threshold: float = CM_SAMPLES_AUTO_GAP_THRESHOLD
        self._last_operational_compaction: Dict[str, int] = {}
        self._last_cm_compaction: Dict[str, int] = {}
        self._checkpoint_count: int = 0
        self._last_checkpoint_reason: str = ""
        self._checkpoint_reason_counts: Dict[str, int] = {}
        # Memory load failure tracking — set by load_memory() on corruption/parse error.
        # Engine checks this on startup and emits a gui-visible event.
        self._load_failed: bool = False
        self._load_failed_reason: str = ""
        # Intelligence checkpoint failure tracking — set by its coordinator.
        self._checkpoint_failed: bool = False
        self._checkpoint_failed_reason: str = ""
        self._checkpoint_failed_ts: float = 0.0
        # Load skipped record tracking — counts corrupted records that were skipped.
        self._load_skipped_action_records: int = 0
        self._load_skipped_provider_namespaces: int = 0
        self._evidence_quarantine: List[Dict[str, Any]] = []
        self._current_action_evidence_scope: Dict[str, Any] = {}
        self._current_memory_evidence_scope: Dict[str, Any] = {}
        self._db_lock = threading.Lock()
        self._calibration_db_path = self.memory_dir / "calibration.db"

        # Action history (bounded ring buffer — unified with objective lifecycle)
        self._action_history_store = ActionHistoryStore(max_history=4000)

        # Band knowledge
        self.bands: Dict[int, BandKnowledge] = {}

        # Cell knowledge
        self.cells: Dict[int, CellKnowledge] = {}  # keyed by PCI

        # ── Temporal congestion profile ──────────────────────────────────────
        # Per cell context (band|cell_id|earfcn), per hour-of-day (0-23):
        #   EMA of congestion_pct observed during that hour.
        # After a few days, this tells the brain "this cell is typically 70%
        # congested between 19:00-22:00" — enabling preemptive band switches.
        # Shape: {context_key: {hour_int: {"ema": float, "samples": int}}}
        # EMA alpha = 0.000385 at 2s tick rate → half-life ≈ 1 hour of ticks.
        # Since each hour-slot only receives ticks during its hour, each day's
        # data retains ~50% weight from the previous day — giving a genuine
        # multi-day weighted average that fades over ~1 week.
        self._congestion_hourly: Dict[str, Dict[int, Dict[str, float]]] = {}
        self._congestion_hourly_alpha: float = 0.000385

        # Intelligence-owned operator prior, injected once by the Intelligence
        # composition boundary.  Memory never reads a product settings file.
        preferences = intelligence_preferences or IntelligencePreferences()
        if type(preferences) is not IntelligencePreferences:
            raise TypeError("Memory requires typed IntelligencePreferences")
        self._band_priority: Dict[int, float] = dict(preferences.band_priority)

        # ── LUC7: Band dwell time tracking ───────────────────────────────
        # Track how long the brain stays on each band before being forced to
        # move. Short dwells after voluntary switches = bad decision.
        # Long dwells = good decision (stable choice).
        # Shape: {band_int: {"total_dwell_s": float, "switch_count": int,
        #                     "short_dwell_count": int}}
        # A "short dwell" is < 10 minutes. Updated on every band change.
        self._dwell_tracking: Dict[int, Dict[str, float]] = {}
        self._dwell_current_band: int = 0
        self._dwell_current_band_since: float = 0.0
        self._dwell_current_episode_id: str = ""
        self._dwell_current_selection_source: str = "attribution_unavailable"
        self._dwell_short_threshold_s: float = 600.0  # 10 minutes

        # ── LUC7: Congestion precursor pattern store ─────────────────────
        # When congestion is confirmed, the engine captures a signal fingerprint
        # from ~5 minutes before the event. Over time, this builds a library of
        # "what the cell looked like just before congestion hit."
        # When current signals match a stored precursor, the brain gets an
        # early warning even without a temporal forecast.
        # Shape: list of {"ctx": str, "hour": int, "fingerprint": dict, "ts": float}
        # Bounded to last 50 precursors to prevent unbounded growth.
        self._congestion_precursors: List[Dict[str, Any]] = []
        self._congestion_precursors_max: int = 50

        # FIX-2: Band blacklist — locally dead bands at this location
        self._band_learning_store = BandLearningStore()

        # Band anchor stability — how reliably does the modem hold a requested
        # anchor after priority rotation? Keyed by band number.
        # Shape: {band: {"attempts": int, "stuck": int}}
        # stuck_rate = stuck / attempts → 0.0 = always drifts, 1.0 = always holds
        self.band_anchor_stability: Dict[int, Dict[str, int]] = {}
        self._anchor_stability_projection_keys: set[str] = set()

        # Baseline tracker — rolling stats by signal condition bucket
        self.baselines: Dict[str, BaselineStats] = {}
        # Band-action learning buckets.
        # Key format: hHH:B1+B3 or hHH:B3_PRIMARY
        self._band_confidence_store = BandConfidenceStore()
        # Cross-session band confidence ranking (Tier 2, Item 10).
        # Accumulates evidence across days: how often each band config was chosen,
        # how often it produced good outcomes, with decay for old evidence.
        # Key: band_signature (e.g. "B1+B3") → {score, chosen_count, good_count,
        #      avg_sinr, avg_rtt, last_updated_ts, decay_factor}
        # Calibration contexts (C1/C2/...) with similarity aliasing.
        self.calibration_contexts: Dict[str, Dict[str, Any]] = {}
        self.calibration_aliases: Dict[str, str] = {}
        self.current_calibration_marker: str = "C0"
        self.current_calibration_signature: Dict[str, Any] = {}
        self.current_context_seen_actions: int = 0
        self._delivered_load_speed_tests: Dict[str, Dict[str, Any]] = {}

        # Provider-scoped namespaces (A1/MTS/Yettel) for learned state
        self._provider_namespaces: Dict[str, Dict[str, Any]] = {}
        self._current_provider_plmn: str = ""
        # Canonical delayed-learning admission state. RewardLedger owns the
        # contents; Memory owns its durable journal/snapshot container.
        self._reward_ledger: Dict[str, Dict[str, Any]] = {}
        self._reward_trajectory_feedback: Dict[str, Dict[str, Any]] = {}
        self._reward_projection_outbox: Dict[str, Dict[str, Any]] = {}
        self._reward_projection_effect_keys: set[str] = set()
        self._reward_evidence_quarantine: List[Dict[str, Any]] = []
        self._capture_current_namespace()

        # Load from disk
        self._load()
        self._init_calibration_db()

    def refresh_thresholds(self) -> None:
        """Refresh grouped learning/calibration config after calibration profile switch."""
        self.learning_cfg = build_memory_learning_config(self.cfg)

    @property
    def action_history(self) -> List[ActionRecord]:
        self._ensure_action_history_store()
        return self._action_history_store.records

    @property
    def band_priority(self) -> Dict[int, float]:
        """Return a copy of the injected Intelligence operator prior."""

        return dict(self._band_priority)

    @action_history.setter
    def action_history(self, value: List[ActionRecord]) -> None:
        self._ensure_action_history_store()
        self._action_history_store.set_records(value or [])

    @property
    def max_history(self) -> int:
        self._ensure_action_history_store()
        return self._action_history_store.max_history

    @max_history.setter
    def max_history(self, value: int) -> None:
        self._ensure_action_history_store()
        self._action_history_store.resize(value)

    def _ensure_action_history_store(self) -> None:
        if not hasattr(self, "_action_history_store"):
            self._action_history_store = ActionHistoryStore(max_history=4000)

    @property
    def band_learning_stm(self) -> Dict[str, Dict[str, Any]]:
        return self._band_learning_store.stm

    @property
    def objective_lifecycle(self) -> List[Dict[str, Any]]:
        raise RuntimeError(
            "illegal_memory_objective_lifecycle_view: use "
            "arch4.brain.memory_objective_lifecycle_owner.get_recent_objective_lifecycle"
        )

    @band_learning_stm.setter
    def band_learning_stm(self, value: Dict[str, Dict[str, Any]]) -> None:
        self._band_learning_store.stm = {
            str(k): dict(v) for k, v in dict(value or {}).items()
        }

    @property
    def band_learning_ltm(self) -> Dict[str, Dict[str, Any]]:
        return self._band_learning_store.ltm

    @band_learning_ltm.setter
    def band_learning_ltm(self, value: Dict[str, Dict[str, Any]]) -> None:
        self._band_learning_store.ltm = {
            str(k): dict(v) for k, v in dict(value or {}).items()
        }

    @property
    def band_blacklist(self) -> set:
        self._band_learning_store.prune_blacklist()
        return self._band_learning_store.blacklist

    @band_blacklist.setter
    def band_blacklist(self, value: set | list | tuple) -> None:
        values = {
            int(v) for v in (value or []) if str(v).strip().isdigit() and int(v) > 0
        }
        self._band_learning_store.replace_imported_blacklist(values)

    @property
    def band_confidence_ltm(self) -> Dict[str, Dict[str, Any]]:
        return self._band_confidence_store.confidence_ltm

    @band_confidence_ltm.setter
    def band_confidence_ltm(self, value: Dict[str, Dict[str, Any]]) -> None:
        self._band_confidence_store.confidence_ltm = {
            str(k): dict(v) for k, v in dict(value or {}).items()
        }

    # ─── Action recording ───────────────────────────────────

    def record_action(self, *args: Any, **kwargs: Any) -> ActionRecord:
        raise RuntimeError(
            "illegal_memory_action_history_adapter:use "
            "arch4.brain.memory_action_history_owner.record_action_history"
        )

    def commit_classified_action_learning(
        self,
        action_record: ActionRecord,
        *,
        modem_before: Optional[CellularIntelligenceGeneration] = None,
        modem_after: Optional[CellularIntelligenceGeneration] = None,
        computed_after: Optional[DerivedSignalFrames] = None,
        append_journal: bool = True,
        band_probe_classifier: Optional[Callable[..., tuple[str, bool]]] = None,
    ) -> Dict[str, Any]:
        raise RuntimeError(
            "illegal_immediate_action_learning_owner_deleted:"
            "strategic_learning_requires_terminal_reward_projection"
        )

    def _record_classified_band_attempts(
        self,
        action_record: ActionRecord,
        *,
        modem_before: Optional[CellularIntelligenceGeneration],
        modem_after: Optional[CellularIntelligenceGeneration],
        computed_after: Optional[DerivedSignalFrames],
        classifier: Optional[Callable[..., tuple[str, bool]]],
    ) -> None:
        raise RuntimeError(
            "illegal_classified_band_attempt_owner_deleted:"
            "operational_attempts_require_the_named_operational_owner"
        )

    def _upsert_action_history_record(self, record: ActionRecord) -> bool:
        if not self._action_record_scope_valid(record):
            return False
        self._action_history_store.upsert(record)
        return True

    def record_staged_roi_action_record(self, record: ActionRecord) -> Dict[str, Any]:
        raise RuntimeError(
            "illegal_memory_proof_mutation: use "
            "arch4.brain.proof_memory_store.record_staged_roi_action_record"
        )

    def close_action_proof_record(self, record: ActionRecord, *, ts: float) -> Dict[str, Any]:
        raise RuntimeError(
            "illegal_memory_proof_mutation: use "
            "arch4.brain.proof_memory_store.close_action_proof_record"
        )

    def get_similar_actions(self,
                            lever: LeverType,
                            sinr_range: tuple = (-999, 999),
                            ca_sig: Optional[str] = None,
                            limit: int = 10) -> List[ActionRecord]:
        raise RuntimeError(
            "illegal_memory_action_history_query_adapter:use "
            "arch4.brain.memory_action_history_owner.get_similar_actions"
        )

    def record_objective_lifecycle(
        self,
        *,
        stage: str,
        status: str,
        objective_id: str = "",
        lever: str = "",
        intent: str = "",
        reason: str = "",
        decision_trace_id: str = "",
        until_ts: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
        provider_plmn: str = "",
        provider_name: str = "",
    ) -> Dict[str, Any]:
        raise RuntimeError(
            "illegal_memory_objective_lifecycle_adapter:"
            "use arch4.brain.memory_objective_lifecycle_owner.record_objective_lifecycle"
        )

    def get_recent_objective_lifecycle(self, limit: int = 200) -> List[Dict[str, Any]]:
        raise RuntimeError(
            "illegal_memory_objective_lifecycle_adapter:"
            "use arch4.brain.memory_objective_lifecycle_owner.get_recent_objective_lifecycle"
        )

    def record_lifecycle_event(
        self,
        *,
        stage: str,
        status: str,
        objective_id: str = "",
        lever: str = "",
        intent: str = "",
        reason: str = "",
        decision_trace_id: str = "",
        until_ts: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
        provider_plmn: str = "",
        provider_name: str = "",
    ) -> ActionRecord:
        raise RuntimeError(
            "illegal_memory_objective_lifecycle_adapter:"
            "use arch4.brain.memory_objective_lifecycle_owner.record_lifecycle_event"
        )

    def _append_journal_event_strict(self, event: Dict[str, Any]) -> None:
        """Append one durable event and propagate failure to admission owners."""

        self._event_journal.append(event)
        self._journal_event_count += 1

    def _append_journal_event(self, event: Dict[str, Any]) -> bool:
        try:
            self._append_journal_event_strict(event)
            return True
        except Exception as e:
            log.error("Memory journal append failed: %s", e)
            return False

    def store_learning_gate_allowed(
        self,
        learning_gate: Dict[str, Any],
        *,
        surface: str,
        now: Optional[float] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        raise RuntimeError(
            "illegal_memory_learning_gate_journal_adapter:use "
            "arch4.brain.memory_learning_gate_journal_owner.store_learning_gate_allowed"
        )

    def _mark_dirty(self, *, events: int = 0) -> None:
        self._dirty = True
        self._dirty_event_count += max(0, int(events or 0))

    # ─── Delivered-load-speed-test memory ─────────────────────────────

    def record_delivered_load_speed_test(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        *,
        provider_plmn: str = "",
        now: Optional[float] = None,
        append_journal: bool = True,
    ) -> Dict[str, Any]:
        raise RuntimeError(
            "illegal_memory_delivered_load_adapter:use "
            "arch4.brain.memory_delivered_load_owner.record_delivered_load_speed_test"
        )

    def needs_checkpoint(self, *, now: Optional[float] = None, force: bool = False) -> bool:
        raise RuntimeError(
            "illegal_memory_persistence_adapter:use "
            "arch4.brain.memory_persistence.needs_checkpoint"
        )

    def checkpoint_due_reason(
        self,
        *,
        now: Optional[float] = None,
        force: bool = False,
    ) -> Optional[str]:
        raise RuntimeError(
            "illegal_memory_persistence_adapter:use "
            "arch4.brain.memory_persistence.checkpoint_due_reason"
        )

    # ─── Band knowledge ─────────────────────────────────────

    def record_band_attempt(self, band: int, appeared_in_ca: bool,
                            sinr: float = 0.0, ca_sig: str = "",
                            throughput: float = 0.0,
                            outcome: str = "",
                            is_primary: bool = False,
                            weak_unusable: bool = False) -> None:
        raise RuntimeError(
            "illegal_memory_band_attempt_mutation: use "
            "arch4.brain.memory_action_learning_store.record_band_attempt_to_memory_store"
        )

    def should_try_band(self, band: int) -> bool:
        raise RuntimeError(
            "illegal_memory_band_learning_query_adapter:"
            "use arch4.brain.memory_band_learning_query_store.should_try_band"
        )

    def is_probe_exhausted(
        self,
        hour: int,
        bands: list,
        *,
        now: float | None = None,
    ) -> bool:
        raise RuntimeError(
            "illegal_memory_band_learning_query_adapter:"
            "use arch4.brain.memory_band_learning_query_store.is_probe_exhausted"
        )

    def band_learning_evidence(self, *, hour: int, bands: list) -> Dict[str, Any]:
        raise RuntimeError(
            "illegal_memory_band_learning_query_adapter:"
            "use arch4.brain.memory_band_learning_query_store.band_learning_evidence"
        )

    def band_learning_memory_summary(self) -> Dict[str, Any]:
        raise RuntimeError(
            "illegal_memory_band_learning_query_adapter:"
            "use arch4.brain.memory_band_learning_query_store.band_learning_memory_summary"
        )

    def _band_suppression_context_key(self) -> str:
        raise RuntimeError(
            "illegal_memory_band_operational_adapter:"
            "use arch4.brain.memory_band_operational_history_owner.band_suppression_context_key"
        )

    def is_band_blacklisted(
        self,
        band: int,
        *,
        context_key: str | None = None,
        now: float | None = None,
    ) -> bool:
        raise RuntimeError(
            "illegal_memory_band_operational_adapter:"
            "use arch4.brain.memory_band_operational_history_owner.is_band_blacklisted"
        )

    def blacklist_band(
        self,
        band: int,
        *,
        ttl_s: float | None = None,
        context_key: str = "*",
        reason: str = "manual_band_blacklist",
        now: float | None = None,
    ) -> None:
        raise RuntimeError(
            "illegal_memory_band_operational_adapter:"
            "use arch4.brain.memory_band_operational_history_owner.blacklist_band"
        )

    def check_and_blacklist(self, band: int) -> None:
        raise RuntimeError(
            "illegal_memory_band_operational_adapter:"
            "use arch4.brain.memory_band_operational_history_owner.check_and_blacklist"
        )

    def record_anchor_stability(self, band: int, stuck: bool) -> None:
        raise RuntimeError(
            "illegal_memory_band_operational_adapter:"
            "use arch4.brain.memory_band_operational_history_owner.record_anchor_stability"
        )

    def get_best_known_bands(self, top_n: int = 4) -> List[int]:
        raise RuntimeError(
            "illegal_memory_band_learning_query_adapter:"
            "use arch4.brain.memory_band_learning_query_store.best_known_bands"
        )

    def get_known_good_bandset(self) -> Optional[List[int]]:
        raise RuntimeError(
            "illegal_memory_band_learning_query_adapter:"
            "use arch4.brain.memory_band_learning_query_store.known_good_bandset"
        )

    def get_ranked_bandsets_for_hour(
        self,
        hour: int,
        candidate_sets: List[List[int]],
        *,
        min_attempts: float = 2.0,
        rtt_pressure_level: str = "normal",
        rtt_delta: float = 0.0,
        rtt_delta_valid: bool = False,
        rsrq_gap: float = 0.0,
        rsrq_gap_valid: bool = False,
    ) -> List[tuple]:
        raise RuntimeError(
            "illegal_memory_band_learning_query_adapter:"
            "use arch4.brain.memory_band_learning_query_store.ranked_bandsets_for_hour"
        )

    def get_best_bandset_for_hour(
        self,
        hour: int,
        candidate_sets: List[List[int]],
        *,
        min_attempts: float = 2.0,
        min_score: float = 0.55,
        rtt_pressure_level: str = "normal",
        rtt_delta: float = 0.0,
        rtt_delta_valid: bool = False,
        rsrq_gap: float = 0.0,
        rsrq_gap_valid: bool = False,
    ) -> Optional[List[int]]:
        raise RuntimeError(
            "illegal_memory_band_learning_query_adapter:"
            "use arch4.brain.memory_band_learning_query_store.best_bandset_for_hour"
        )

    # ─── Cell knowledge ─────────────────────────────────────

    def update_cell(
        self,
        modem: CellularIntelligenceGeneration,
        computed: Optional[DerivedSignalFrames] = None,
        *,
        learning_gate: Dict[str, Any],
    ) -> None:
        raise RuntimeError(
            "illegal_memory_cell_knowledge_adapter:"
            "use arch4.brain.memory_cell_knowledge_owner.update_cell"
        )

    def update_congestion_profile(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        *,
        learning_gate: Dict[str, Any],
    ) -> None:
        raise RuntimeError(
            "illegal_memory_congestion_profile_adapter:use "
            "arch4.brain.memory_congestion_profile_owner.update_congestion_profile"
        )

    def get_congestion_forecast(
        self, modem: CellularIntelligenceGeneration, *, hours_ahead: int = 0,
    ) -> Optional[float]:
        raise RuntimeError(
            "illegal_memory_congestion_profile_adapter:use "
            "arch4.brain.memory_congestion_profile_owner.get_congestion_forecast"
        )

    def get_congestion_profile_summary(
        self, modem: CellularIntelligenceGeneration,
    ) -> Dict[int, float]:
        raise RuntimeError(
            "illegal_memory_congestion_profile_adapter:use "
            "arch4.brain.memory_congestion_profile_owner.get_congestion_profile_summary"
        )

    def get_least_congested_band_for_hour(
        self,
        hour: int,
        candidate_bands: List[int],
        cell_id: int = 0,
        earfcn: int = 0,
    ) -> Optional[tuple]:
        raise RuntimeError(
            "illegal_memory_congestion_profile_adapter:use "
            "arch4.brain.memory_congestion_profile_owner.get_least_congested_band_for_hour"
        )


    def get_band_dwell_stats(self, band: int) -> Optional[Dict[str, float]]:
        raise RuntimeError(
            "illegal_memory_cell_knowledge_adapter:"
            "use arch4.brain.memory_cell_knowledge_owner.get_band_dwell_stats"
        )

    def record_congestion_precursor(
        self,
        modem: CellularIntelligenceGeneration,
        fingerprint: Dict[str, Any],
    ) -> None:
        raise RuntimeError(
            "illegal_memory_congestion_profile_adapter:use "
            "arch4.brain.memory_congestion_profile_owner.record_congestion_precursor"
        )

    def match_congestion_precursor(
        self,
        modem: CellularIntelligenceGeneration,
        current_signals: Dict[str, float],
        *,
        tolerance: float = 0.35,
    ) -> Optional[float]:
        raise RuntimeError(
            "illegal_memory_congestion_profile_adapter:use "
            "arch4.brain.memory_congestion_profile_owner.match_congestion_precursor"
        )


    # ─── Baseline tracking (replaces time zones) ────────────

    def update_baseline(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        *,
        state_name: str = "",
        learning_gate: Dict[str, Any],
    ) -> None:
        raise RuntimeError(
            "illegal_memory_baseline_adapter:"
            "use arch4.brain.memory_baseline_owner.update_baseline"
        )

    def get_baseline(self, modem: CellularIntelligenceGeneration, computed: Optional[DerivedSignalFrames] = None) -> Optional['BaselineStats']:
        raise RuntimeError(
            "illegal_memory_baseline_adapter:"
            "use arch4.brain.memory_baseline_owner.get_baseline"
        )

    # ── Cross-session band confidence ranking (Tier 2, Item 10) ──────────

    def update_band_confidence(
        self,
        band_signature: str,
        *,
        outcome: str,           # "good" | "neutral" | "bad"
        sinr_db: float = 0.0,
        rtt_ms: float = 0.0,
        dlst: Optional[Dict[str, Any]] = None,
        now: float | None = None,
    ) -> None:
        raise RuntimeError(
            "illegal_memory_band_confidence_adapter:"
            "use arch4.brain.memory_band_confidence_owner.update_band_confidence"
        )

    def get_ranked_band_configs(
        self,
        *,
        min_observations: int = 3,
        limit: int = 10,
    ) -> list[dict]:
        raise RuntimeError(
            "illegal_memory_band_confidence_adapter:"
            "use arch4.brain.memory_band_confidence_owner.get_ranked_band_configs"
        )

    def is_below_baseline(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames) -> bool:
        raise RuntimeError(
            "illegal_memory_baseline_adapter:"
            "use arch4.brain.memory_baseline_owner.is_below_baseline"
        )

    def seed_baselines_from_historical(self, historical_hours: Dict) -> None:
        raise RuntimeError(
            "illegal_memory_baseline_adapter:"
            "use arch4.brain.memory_baseline_owner.seed_baselines_from_historical"
        )

    # ─── Persistence ────────────────────────────────────────

    def _save(
        self,
        extras: Optional[Dict[str, Any]] = None,
        *,
        checkpoint_reason: str = "manual",
    ) -> None:
        """Retired direct checkpoint surface."""

        del extras, checkpoint_reason
        raise RuntimeError(
            "memory_direct_save_owner_deleted:"
            "use_intelligence_persistence_coordinator"
        )

    def _load(self) -> None:
        """Load memory from disk."""
        load_memory(self)

    def _read_memory_meta(self) -> Dict[str, Any]:
        return read_memory_meta(self)

    def get_runtime_status(self) -> Dict[str, Any]:
        return get_runtime_status(self)

    def _snapshot_applied_journal_count(
        self,
        journal_count: int,
        *,
        meta: Optional[Dict[str, Any]] = None,
    ) -> int:
        return snapshot_applied_journal_count(self, journal_count, meta=meta)

    def compact_operational_journal(self, *, keep_last_applied: int = 500) -> Dict[str, int]:
        return compact_operational_journal(self, keep_last_applied=keep_last_applied)

    def _maybe_auto_compact_journals(self) -> None:
        maybe_auto_compact_journals(self)

    @staticmethod
    def _should_keep_compacted_operational_event(event: Dict[str, Any]) -> bool:
        from arch4.brain.memory_persistence import should_keep_compacted_operational_event
        return should_keep_compacted_operational_event(event)

    def _quarantine_evidence_row(
        self,
        *,
        kind: str,
        reason: str,
        row: Dict[str, Any],
    ) -> None:
        self._evidence_quarantine.append(
            {
                "schema": "EvidenceQuarantine.v1",
                "owner": "arch4.brain.memory",
                "kind": str(kind or "unknown"),
                "reason": str(reason or "evidence_scope_invalid"),
                "quarantined_at": time.time(),
                "row": dict(row or {}),
            }
        )
        if len(self._evidence_quarantine) > 256:
            self._evidence_quarantine = self._evidence_quarantine[-256:]

    def _append_action_history_record(self, record: ActionRecord) -> bool:
        if not self._action_record_scope_valid(record):
            return False
        self._action_history_store.append(record)
        return True

    def _action_record_scope_valid(self, record: ActionRecord) -> bool:
        from arch4.evidence_scope import EvidenceScope

        try:
            EvidenceScope.from_mapping(
                dict(getattr(record, "baseline_evidence_scope", {}) or {})
            )
            EvidenceScope.from_mapping(
                dict(getattr(record, "evidence_scope", {}) or {})
            )
        except (TypeError, ValueError) as exc:
            from arch4.brain.memory_io import record_to_dict

            self._quarantine_evidence_row(
                kind="action_record",
                reason=str(exc),
                row=record_to_dict(record),
            )
            return False
        return True

    def _replay_journal_events(self, start_index: int) -> int:
        return replay_journal_events(self, start_index)

    def _apply_journal_event(self, event: Dict[str, Any]) -> int:
        return apply_journal_event(self, event)

    def _sync_current_context_seen_actions(self) -> None:
        calibration_context_sync_seen_actions(self)

    # ─── Calibration DB persistence (delegated to memory_calibration_store.py) ─

    def _init_calibration_db(self) -> None:
        init_calibration_db(
            self._calibration_db_path, self._db_lock,
            enabled=bool(self.learning_cfg.calibration_db_enabled),
        )

    def _prune_calibration_db(self, now: Optional[float] = None) -> None:
        prune_calibration_db(
            self._calibration_db_path, self._db_lock,
            enabled=bool(self.learning_cfg.calibration_db_enabled),
            retention_days=int(self.learning_cfg.calibration_db_retention_days),
            now=now,
        )

    def record_calibration_sample(
        self,
        modem,
        computed,
        calibration_status,
        state_name: str = "",
        now: Optional[float] = None,
        *,
        learning_gate: Dict[str, Any],
    ) -> None:
        raise RuntimeError(
            "illegal_memory_calibration_sample_adapter:use "
            "CalibrationAuthority.consume_calibration_observation"
        )

    def reassign_calibration_marker_history(self, src_marker: str, dst_marker: str) -> None:
        raise RuntimeError(
            "illegal_memory_calibration_history_adapter:use "
            "arch4.brain.memory_calibration_history_owner.reassign_calibration_marker_history"
        )

    def get_calibration_hourly_ranges(
        self,
        marker: str,
        lookback_hours: int = 24,
        now: Optional[float] = None,
    ) -> list:
        raise RuntimeError(
            "illegal_memory_calibration_history_adapter:use "
            "arch4.brain.memory_calibration_history_owner.get_calibration_hourly_ranges"
        )

    def set_provider_context(self, provider_plmn: str) -> None:
        namespace_set_provider_context(self, provider_plmn)

    def current_provider_context(self) -> str:
        return self._current_provider_plmn

    @staticmethod
    def _new_provider_namespace() -> Dict[str, Any]:
        return new_provider_namespace()

    def _capture_current_namespace(self) -> None:
        capture_current_namespace(self)

    def _bind_namespace(self, ns: Dict[str, Any]) -> None:
        bind_namespace(self, ns)

    def _serialize_provider_namespaces(self) -> Dict[str, dict]:
        return serialize_provider_namespaces(self)

    def _serialize_provider_stm_namespaces(self) -> Dict[str, dict]:
        return serialize_provider_stm_namespaces(self)

    def _serialize_provider_ltm_namespaces(self) -> Dict[str, dict]:
        return serialize_provider_ltm_namespaces(self)

    def _serialize_provider_calibration_namespaces(self) -> Dict[str, Dict[str, Any]]:
        return serialize_provider_calibration_namespaces(self)

    def _build_stm_snapshot(self) -> Dict[str, Any]:
        return build_stm_snapshot(self)

    def _build_ltm_snapshot(self) -> Dict[str, Any]:
        return build_ltm_snapshot(self)

    def _build_memory_meta(self) -> Dict[str, Any]:
        return build_memory_meta(self)

    def _deserialize_provider_namespace(self, ns_d: dict) -> Dict[str, Any]:
        return deserialize_provider_namespace(self, ns_d)

    def _resolve_marker_alias(self, marker: str) -> str:
        return calibration_context_resolve_marker_alias(self, marker)

    def _compact_calibration_alias_graph(self) -> None:
        calibration_context_compact_alias_graph(self)

    def set_calibration_context(
        self,
        calibration_status: Optional[Dict[str, Any]],
        modem_meta: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Resolve active calibration context marker (C1/C2/...).

        Policy:
        - Keep current marker only when the active context seed is unchanged.
        - If context seed changed, create a NEW provisional marker immediately.
        - Only after configured delay (default 24h) compare provisional marker
          against other markers; if similar, merge/delete provisional marker.
        """
        return calibration_context_set_calibration_context(
            self,
            calibration_status,
            modem_meta=modem_meta,
            now=now,
        )

    def _resolve_or_create_provisional_marker(
        self,
        sig: Dict[str, Any],
        now: float,
    ) -> tuple[str, float, bool]:
        return calibration_context_resolve_or_create_marker(self, sig, now)

    def _maybe_reconcile_mature_contexts(self, now: float) -> List[tuple[str, str, float]]:
        return calibration_context_maybe_reconcile_mature_contexts(self, now)

    @staticmethod
    def _blend_signature(old: Dict[str, Any], new: Dict[str, Any], alpha: float = 0.2) -> Dict[str, Any]:
        return calibration_context_blend_signature(old, new, alpha=alpha)

    @staticmethod
    def _context_seed(sig: Dict[str, Any]) -> str:
        return calibration_context_seed(sig)

    def _merge_marker_learning(self, old_marker: str, new_marker: str) -> None:
        calibration_context_merge_marker_learning(self, old_marker, new_marker)

    @staticmethod
    def _calibration_similarity(a: Dict[str, Any], b: Dict[str, Any]) -> float:
        return calibration_context_similarity(a, b)

    def should_block_band_action(
        self,
        hour: int,
        bands: List[int],
        now: Optional[float] = None,
        current_sinr: Optional[float] = None,
    ) -> tuple[bool, str]:
        raise RuntimeError(
            "illegal_memory_band_learning_query_adapter:"
            "use arch4.brain.memory_band_learning_query_store.should_block_band_action"
        )

    def should_hold_known_outage_hour(
        self,
        *,
        hour: int,
        now: Optional[float] = None,
        current_sinr: Optional[float] = None,
    ) -> tuple[bool, str]:
        raise RuntimeError(
            "illegal_memory_band_learning_query_adapter:"
            "use arch4.brain.memory_band_learning_query_store.should_hold_known_outage_hour"
        )
