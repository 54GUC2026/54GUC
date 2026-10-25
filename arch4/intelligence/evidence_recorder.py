"""
arch4/intelligence/evidence_recorder.py
═══════════════════════════════════════════════════════════════
ARCH4 — Telemetry and Event Logging

Seven outputs with explicit authority hierarchy:

  1. telemetry_production_*.jsonl  [PRODUCTION NUMBERED HISTORY]
     Per-day compact JSONL file. Owns numbered production evidence.

  2. telemetry_trace_matrix_*.jsonl  [PRODUCTION TRACE MATRIX]
     Named dimensional delta stream derived from the same fact rows as
     telemetry_production.

  3. events_*.jsonl  [STRUCTURED EVENT STREAM]
     JSONL decision/transition log. Authoritative for forensic event
     reconstruction and overnight analysis.

  4. raw_*.log  [FORENSIC / CRASH ONLY]
     Plain-text echo of high-value events. Forensic use only.
     Not a primary data surface — do not introduce CSV or JSONL
     consumers that fall back to this file.

  5. forensic_compact_*.jsonl  [COMPACT FORENSIC DERIVED STREAM]
     Delta-coded tick facts and event evidence. The codec lives in
     arch4.telemetry.forensic_codec; artifact discovery and file decoding live
     only in arch4.historical_artifact_reader. Never a live GUI/runtime data
     bus.

  6. telemetry_history_*.jsonl  [COMPARE / DEBUG HISTORY]
     Full legacy JSONL row. Written only in explicit compare/debug/trace modes.
     Production mode disables this lane after production/trace side-by-side
     validation passed.

Writes to timestamped files in output directory.
═══════════════════════════════════════════════════════════════
"""

from arch4.controller_frames import (
    AcceptedControllerGeneration,
    CellularIntelligenceGeneration,
    DerivedSignalFrames,
    project_controller_read_model,
)

import copy
import gzip
import json
import os
import shutil
import time
import logging
import datetime as dt
from collections import deque
from pathlib import Path
from threading import RLock
from typing import Optional, IO, Any, Mapping, Dict

from arch4.interfaces import (
    SystemState,
    LeverAction, LeverResult,
    ARCH_RUNTIME_OUTPUT_DIR,
    ARCH_RUNTIME_TELEMETRY_HISTORY_BASENAME,
)
from arch4.controller_posture import ControllerPosture
from arch4.action_transaction import (
    action_transaction_key_from_action,
)
from arch4.brain.tick_contract import (
    DecisionContext,
    DecisionTrace,
    ExecutionOutcome,
)
from arch4.telemetry.rollup import HourlyRollup
from arch4.telemetry.history_contract import build_telemetry_history_fact_row
from arch4.telemetry.forensic_codec import (
    COMPACT_FORENSIC_FILE_PATTERN,
    COMPACT_FORENSIC_RETENTION_DAYS,
    CompactForensicStreamWriter,
)
from arch4.telemetry.production_contract import (
    LOG_MODE_ENV,
    PRODUCTION_TELEMETRY_BASENAME,
    build_production_telemetry_row,
    normalize_log_mode,
)
from arch4.telemetry.decision_context_contract import (
    build_decision_context_capture_snapshot,
)
from arch4.payload_budget import PayloadBudgetExceeded, retained_payload_bytes
from arch4.atomic_io import atomic_output_path
from arch4.historical_artifact_reader import FAMILY_EVENT, HistoricalArtifactReader
from arch4.telemetry.trace_matrix import (
    TRACE_MATRIX_BASENAME,
    build_trace_matrix_delta,
    public_trace_matrix_delta,
)
# LEGACY telemetry-history passthroughs retained for external tooling:
# build_telemetry_history_fact_row writes cqi_mcs_delta_pct and congestion_raw_pct.
from arch4.runtime_identity import build_runtime_session_scope, runtime_session_scope_key
from arch4.operator_profiles import derive_runtime_sidecar_metadata
from arch4.canonical_delta_display import build_delta_display_surface
from arch4.modem_live.compatibility_identity import (
    DecodeCatalogReplica,
    IdentityContractError,
    validated_identity_publication_copy,
)
from arch4.modem_live.truth_matrix import (
    ModemLiveTruthMatrixError,
    ValidatedTruthMatrixReader,
)

log = logging.getLogger("arch4.telemetry")

_DECISION_CONTEXT_SNAPSHOT_MAX_BYTES = 256 * 1024
_DECISION_CONTEXT_RING_MAX_BYTES = 16 * 1024 * 1024
_DECISION_CONTEXT_RING_MAX_ITEMS = 4096

_SMARTNESS_OWNER_PAYLOADS: dict[str, tuple[str, str]] = {
    "self_learning_strategy": ("smart_loop.closed_loop.v1", "arch4.smart_loop.closed_loop"),
    "brain_learning_effect": ("brain_learning_effect.v1", "arch4.brain.learning_effect"),
    "decision_quality": ("brain_decision_quality_summary.v1", "arch4.brain.decision_quality_owner"),
}

_RAW_ECHO_KINDS = frozenset({
    "startup",
    "effective_policy_snapshot",
    "runtime_session_scope_changed",
    "external_memory_auto_import",
    "state_transition",
    "health_stage_change",
    "radio_state_change",
    "cell_structural_change",
    "action",
    "action_issued",
    "action_completed",
    "action_suppressed",
    "action_learning_outcome",
    "action_grade",
    "action_regrade",
    "manual_action_submitted",
    "manual_action_executed",
    "manual_action_rejected",
    "manual_action_effect",
    "runtime_recovery_armed",
    "manual_radio_guard_armed",
    "mutating_action_verification",
    "cell_opt_suppressed",
    "exploration_suppressed",
    "emergency_resume",
    "emergency_clear",
    "calibration_profile_applied",
    "calibration_bootstrap_auto_started",
    "calibration_started",
    "calibration_state_change",
    "calibration_completed",
    "calibration_stopped",
    "calibration_candidate_rejected",
    "logger_degraded",
})
_RAW_ECHO_SKIP_KEYS = frozenset({
    "event",
    "schema_version",
    "ts",
    "iso",
    "details",
    "runtime_identity",
    "runtime_capabilities",
    "policy",
    "policy_snapshot",
})
_RAW_ECHO_VALUE_LIMIT = 180
_RAW_ECHO_LINE_LIMIT = 1600


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _optional_float(value: Any, *, valid: bool) -> Optional[float]:
    if not valid:
        return None
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _env_truthy(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _csv_optional(value: Any, *, valid: bool, precision: int = 1) -> str:
    parsed = _optional_float(value, valid=valid)
    if parsed is None:
        return ""
    return f"{parsed:.{precision}f}"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first_text(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _identity_publication_candidate(runtime: Mapping[str, Any]) -> Mapping[str, Any]:
    matrix = runtime.get("truth_matrix")
    if not isinstance(matrix, Mapping):
        return {}
    try:
        candidate = ValidatedTruthMatrixReader(matrix).line_values("identity")
    except (ModemLiveTruthMatrixError, TypeError, ValueError):
        return {}
    if all(
        isinstance(candidate.get(key), Mapping)
        for key in (
            "decode_catalog_snapshot",
            "access_type_contract",
            "modem_identity_key",
        )
    ):
        return candidate
    return {}


def _durable_identity_catalog_references(runtime: Mapping[str, Any]) -> dict[str, Any]:
    candidate = _identity_publication_candidate(runtime)
    if not candidate:
        return {"identity_decode_status": "unavailable"}
    try:
        validated = validated_identity_publication_copy(
            candidate,
            replica=DecodeCatalogReplica(),
        )
    except (IdentityContractError, KeyError, TypeError, ValueError) as exc:
        return {
            "identity_decode_status": "quarantined",
            "identity_decode_error": str(exc),
        }
    return {
        "identity_decode_status": "validated_modem_live_replica",
        "identity_schema": str(validated.get("schema") or ""),
        "identity_owner": str(validated.get("owner") or ""),
        "identity_code": str(validated.get("identity_code") or ""),
        "identity_status": str(validated.get("identity_status") or ""),
        "catalog_version": int(validated.get("catalog_version", 0) or 0),
        "catalog_checksum": str(validated.get("catalog_checksum") or ""),
        "profile_id": str(validated.get("profile_id") or ""),
        "access_type_id": str(validated.get("access_type_id") or ""),
        "access_type_fingerprint": str(validated.get("access_type_fingerprint") or ""),
        "combination_guard": str(validated.get("combination_guard") or ""),
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_identity_text(
    *sources: Any,
    keys: tuple[str, ...],
) -> str:
    for source in sources:
        if isinstance(source, Mapping):
            for key in keys:
                text = _text(source.get(key))
                if text:
                    return text
            continue
        for key in keys:
            text = _text(getattr(source, key, ""))
            if text:
                return text
    return ""


def _fill_provider_identity_from_canonical_surfaces(
    row: dict[str, Any],
    *,
    runtime: Mapping[str, Any],
    modem: CellularIntelligenceGeneration,
    session_metadata: Mapping[str, Any],
) -> None:
    canonical_status = _mapping(runtime.get("_canonical_status"))
    base_status = _mapping(runtime.get("_base_status"))
    runtime_identity = _mapping(runtime.get("runtime_identity"))
    resolved_identity_card = _mapping(runtime.get("resolved_identity_card"))
    capability_profile = _mapping(runtime.get("capability_profile"))
    provider_name = _first_identity_text(
        canonical_status,
        base_status,
        row,
        session_metadata,
        runtime_identity,
        resolved_identity_card,
        capability_profile,
        runtime,
        modem,
        keys=("provider_name", "provider", "operator_name", "operator"),
    )
    provider_plmn = _first_identity_text(
        canonical_status,
        base_status,
        row,
        session_metadata,
        runtime_identity,
        resolved_identity_card,
        capability_profile,
        runtime,
        modem,
        keys=("provider_plmn", "plmn", "plmn_id"),
    )
    if provider_name:
        row["provider_name"] = provider_name
        row["provider"] = provider_name
    if provider_plmn:
        row["provider_plmn"] = provider_plmn
        row["plmn"] = provider_plmn


class _HistoryFileGate:
    """Context manager: close today's JSONL for safe external reads.

    Acts as a semaphore on the latest telemetry_history file.  While
    held, the logger's writer side is blocked — no new rows can be
    appended.  On exit the file is reopened for append automatically.
    """

    def __init__(self, logger: "IntelligenceEvidenceRecorder") -> None:
        self._logger = logger

    def __enter__(self) -> "IntelligenceEvidenceRecorder":
        self._logger._pause_history_writes()
        return self._logger

    def __exit__(self, *exc: Any) -> None:
        self._logger._resume_history_writes()


class IntelligenceEvidenceRecorder:
    """Intelligence-only telemetry, grading, trace, and prediction evidence."""

    def __init__(self, output_dir: str = f"./{ARCH_RUNTIME_OUTPUT_DIR}", enable_rollup: bool = True):
        self.output_dir = Path(output_dir)
        self._include_interpretive_tick_fields = _env_truthy(
            os.environ.get("ARCH_INCLUDE_INTERPRETIVE_TICK_FIELDS")
        )
        self._log_mode = normalize_log_mode(os.environ.get(LOG_MODE_ENV))
        self._write_full_telemetry_history = self._log_mode in {"compare", "debug", "trace"}
        self._production_telemetry_enabled = True
        # ── Degraded mode tracking ────────────────────────────────────────────
        # Track write failures to emit health warnings and continue operation.
        # Logger continues attempting writes even after failures (transient fix).
        # Initialized early so _record_write_failure can be called during setup.
        self._write_failure_count: int = 0
        self._last_write_failure_ts: float = 0.0
        self._last_write_failure_reason: str = ""
        self._degraded_mode: bool = False
        self._event_payload_truncation_count: int = 0
        self._last_event_payload_truncation_ts: float = 0.0
        self._last_event_payload_truncation_kind: str = ""
        self._last_event_payload_original_bytes: int = 0
        self._telemetry_history_rows_written: int = 0
        self._production_telemetry_rows_written: int = 0
        self._trace_matrix_rows_written: int = 0
        self._compact_forensic_tick_rows_written: int = 0
        self._compact_forensic_event_rows_written: int = 0

        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._record_write_failure("output_dir mkdir", exc)
        # A crash/restart can create two logger instances inside one wall-clock
        # second.  The old second-resolution tag opened the same event path with
        # ``w`` and erased the first process's durable idempotency evidence.
        tag = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._event_path = self.output_dir / f"events_{tag}.jsonl"
        self._raw_path = self.output_dir / f"raw_{tag}.log"
        self._compact_forensic_writer: Optional[CompactForensicStreamWriter] = None

        self._event_file: Optional[IO] = None
        self._raw_file: Optional[IO] = None
        self._telemetry_history_day: str = ""
        self._telemetry_history_path: Optional[Path] = None
        self._telemetry_history_file: Optional[IO] = None
        self._production_telemetry_day: str = ""
        self._production_telemetry_path: Optional[Path] = None
        self._production_telemetry_file: Optional[IO] = None
        self._trace_matrix_day: str = ""
        self._trace_matrix_path: Optional[Path] = None
        self._trace_matrix_file: Optional[IO] = None
        self._trace_matrix_state: dict[str, Any] = {}
        self._history_paused: bool = False
        self._history_paused_rows_dropped: int = 0
        self._cycle_count: int = 0
        self._event_seq: int = 0
        # Raw log opt-out: set to False to suppress all raw_*.log writes.
        # Useful for portable/headless runs where the raw log is noise.
        # Defaults to True to preserve existing forensic behavior.
        self._raw_enabled: bool = True
        self._rollup: Optional[HourlyRollup] = (
            HourlyRollup(self.output_dir) if enable_rollup else None
        )
        self._config_generation_lock = RLock()
        self._config_generation_id = "bootstrap"
        self._config_fingerprint = ""
        self._decision_context_enabled: bool = False
        self._decision_context_start_hour: int = 0
        self._decision_context_end_hour: int = 6
        self._decision_context_pre_s: float = 180.0
        self._decision_context_post_s: float = 180.0
        self._decision_context_ring_s: float = 480.0
        self._decision_context_ring: deque[tuple[dict[str, Any], int]] = deque(
            maxlen=_DECISION_CONTEXT_RING_MAX_ITEMS
        )
        self._decision_context_ring_bytes: int = 0
        self._decision_context_ring_dropped: int = 0
        self._decision_context_snapshot_rejected: int = 0
        self._decision_context_last_rejection: str = ""
        self._decision_context_last_rejection_ts: float = 0.0
        self._decision_context_last_accepted_ts: float = 0.0
        self._decision_context_last_snapshot: Optional[dict[str, Any]] = None
        self._decision_context_active: Optional[dict[str, Any]] = None
        self._decision_context_seq: int = 0
        self._runtime_event_defaults: Dict[str, Any] = {}
        self._session_context_defaults: Dict[str, Any] = {}
        self._active_session_scope: Dict[str, str] = {}
        self._active_session_scope_key: str = ""
        self._session_seq: int = 0
        self._last_delta_display_surface: Dict[str, Any] = {}
        self._event_debounce_seen: Dict[str, float] = {}
        self._idempotent_event_keys: set[str] = set()
        self._idempotent_event_lock = RLock()
        self._open_files(tag)

    def _open_files(self, tag: str) -> None:
        """Open Intelligence evidence files.
        
        Fail-soft: if primary files cannot be opened, enter degraded mode
        but don't crash. Runner continues without logging.
        """
        try:
            self._event_file = open(self._event_path, "w", buffering=1)
        except Exception as exc:
            self._event_file = None
            self._record_write_failure("event file open", exc)

        try:
            self._raw_file = open(self._raw_path, "w", buffering=1)
            self._write_raw_session_header(tag)
        except Exception as exc:
            self._raw_file = None
            self._record_write_failure("raw file open", exc)

        try:
            self._compact_forensic_writer = CompactForensicStreamWriter(
                self.output_dir,
                tag=tag,
            )
        except Exception as exc:
            self._compact_forensic_writer = None
            self._record_write_failure("compact forensic open", exc)

        if self._degraded_mode:
            log.warning("Logger degraded: %s", self._last_write_failure_reason)
        else:
            log.info("Logging to: %s", self.output_dir)
            log.info("  Mode:   %s", self._log_mode)
            log.info("  Events: %s", self._event_path.name)
            log.info("  Raw:    %s", self._raw_path.name)
            log.info("  Prod:   %s_*.jsonl", PRODUCTION_TELEMETRY_BASENAME)
            log.info("  Compact:%s", COMPACT_FORENSIC_FILE_PATTERN)
        # Create the canonical telemetry history file immediately so monitor-mode
        # GUI can bind to the final source before the first live tick arrives.
        if self._write_full_telemetry_history:
            self._rotate_telemetry_history(time.time())
        if self._production_telemetry_enabled:
            self._rotate_production_telemetry(time.time())
        # Prune old output files on startup — keep last N days.
        self._cleanup_old_output_files()
        # Note: second call removed (was duplicate — no behavior change)

    def configure_runtime_event_defaults(self, **kwargs: Any) -> None:
        """Persist shared runtime metadata for events and sidecars."""
        defaults = {str(k): v for k, v in kwargs.items() if v not in (None, "")}
        self._runtime_event_defaults = derive_runtime_sidecar_metadata(defaults, defaults)

    def runtime_event_defaults(self) -> Dict[str, Any]:
        runtime_defaults = getattr(self, "_runtime_event_defaults", {}) or {}
        session_defaults = getattr(self, "_session_context_defaults", {}) or {}
        return dict(runtime_defaults) | dict(session_defaults)

    def _session_identity_metadata(
        self,
        modem: CellularIntelligenceGeneration,
        *,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> tuple[dict[str, Any], dict[str, str], str]:
        runtime = extra if isinstance(extra, Mapping) else {}
        identity = (
            runtime.get("runtime_identity")
            if isinstance(runtime.get("runtime_identity"), Mapping)
            else {}
        )
        resolved_card = (
            runtime.get("resolved_identity_card")
            if isinstance(runtime.get("resolved_identity_card"), Mapping)
            else {}
        )
        capability_profile = (
            runtime.get("capability_profile")
            if isinstance(runtime.get("capability_profile"), Mapping)
            else {}
        )
        scope = build_runtime_session_scope(identity)
        scope_key = str(runtime_session_scope_key(scope) or "")
        metadata: dict[str, Any] = {
            "session_scope_key": scope_key,
            "session_seq": self._session_seq or 1,
        }
        metadata.update(_durable_identity_catalog_references(runtime))
        for key in (
            "target",
            "backend",
            "backend_family",
            "model",
            "firmware",
            "provider_name",
            "provider_plmn",
            "serial",
            "imei",
            "imsi",
            "sim_id",
        ):
            value = _text(identity.get(key))
            if value not in (None, ""):
                metadata[key] = value
        resolved_profile_id = _first_text(
            identity.get("resolved_profile_id"),
            identity.get("profile_id"),
            resolved_card.get("profile_id"),
            capability_profile.get("profile_id"),
        )
        if resolved_profile_id:
            metadata["resolved_profile_id"] = resolved_profile_id
            metadata["profile_id"] = resolved_profile_id
        profile_provenance = _first_text(
            identity.get("resolved_profile_provenance"),
            resolved_card.get("profile_provenance"),
            resolved_card.get("provenance"),
            capability_profile.get("provenance"),
        )
        if profile_provenance:
            metadata["profile_provenance"] = profile_provenance
        for key in ("identity_state", "support_tier_label", "match_quality"):
            value = _first_text(identity.get(key), resolved_card.get(key))
            if value:
                metadata[key] = value
        return metadata, scope, scope_key

    def _update_session_context(
        self,
        modem: CellularIntelligenceGeneration,
        *,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        metadata, scope, scope_key = self._session_identity_metadata(modem, extra=extra)
        if not scope:
            return metadata
        if not self._active_session_scope:
            self._session_seq = 1
        elif scope_key and scope_key != self._active_session_scope_key:
            previous_scope = dict(self._active_session_scope)
            previous_scope_key = str(self._active_session_scope_key or "")
            self._session_seq += 1
            metadata["session_seq"] = self._session_seq
            self._session_context_defaults = dict(metadata)
            self._active_session_scope = dict(scope)
            self._active_session_scope_key = str(scope_key or "")
            self.log_event(
                "runtime_session_scope_changed",
                previous_session_scope=previous_scope,
                previous_session_scope_key=previous_scope_key,
                current_session_scope=dict(scope),
                current_session_scope_key=str(scope_key or ""),
                session_seq=self._session_seq,
            )
            return dict(self._session_context_defaults)
        metadata["session_seq"] = self._session_seq or 1
        self._session_context_defaults = dict(metadata)
        self._active_session_scope = dict(scope)
        self._active_session_scope_key = str(scope_key or "")
        return dict(self._session_context_defaults)

    # ── Old file cleanup ────────────────────────────────────────────────

    OUTPUT_FILE_RETENTION_DAYS: int = 14
    OUTPUT_RETENTION_POLICY: dict[str, dict[str, Any]] = {
        "telemetry_history": {
            "surface_class": "compare_debug_history",
            "patterns": [f"{ARCH_RUNTIME_TELEMETRY_HISTORY_BASENAME}_*.jsonl"],
            "retention_days": OUTPUT_FILE_RETENTION_DAYS,
            "compression": "eligible",
        },
        "telemetry_production": {
            "surface_class": "production_minimal_numbered_history",
            "patterns": [f"{PRODUCTION_TELEMETRY_BASENAME}_*.jsonl"],
            "retention_days": OUTPUT_FILE_RETENTION_DAYS,
            "compression": "eligible",
        },
        "telemetry_trace_matrix": {
            "surface_class": "production_trace_matrix_named_delta_history",
            "patterns": [f"{TRACE_MATRIX_BASENAME}_*.jsonl"],
            "retention_days": OUTPUT_FILE_RETENTION_DAYS,
            "compression": "eligible",
        },
        "events": {
            "surface_class": "structured_event_stream",
            "patterns": ["events_*.jsonl"],
            "retention_days": OUTPUT_FILE_RETENTION_DAYS,
            "compression": "eligible",
        },
        "raw_log": {
            "surface_class": "forensic_raw",
            "patterns": ["raw_*.log"],
            "retention_days": OUTPUT_FILE_RETENTION_DAYS,
            "compression": "lane_specific",
        },
        "compact_forensic": {
            "surface_class": "compact_forensic_derived_stream",
            "patterns": [COMPACT_FORENSIC_FILE_PATTERN],
            "retention_days": COMPACT_FORENSIC_RETENTION_DAYS,
            "compression": "eligible",
        },
    }
    EVENT_DEBOUNCE_WINDOW_S: float = 30.0
    EVENT_PAYLOAD_MAX_BYTES: int = 128 * 1024
    EVENT_PAYLOAD_FIELD_MAX_BYTES: int = 24 * 1024
    EVENT_PAYLOAD_PREVIEW_CHARS: int = 512
    EVENT_DEBOUNCE_WINDOWS_S: dict[str, float] = {
        "decision_trace": 60.0,
        "cell_structural_change": 300.0,
        "brain_posture_deferred": 60.0,
        "radio_stability_hold_deferred": 60.0,
        "action_availability_blocked": 60.0,
    }
    EVENT_DEBOUNCE_KINDS: frozenset[str] = frozenset({
        # High-volume runtime traces can backlog the ordered output lane on
        # slow filesystems. Keep the first copy of the same truth surface.
        "decision_trace",
        # CA signatures can flap B3/B3+CA repeatedly without useful operator
        # value; band/PCI changes still pass when their payload changes.
        "cell_structural_change",
        "brain_posture_deferred",
        "radio_stability_hold_deferred",
        "action_availability_blocked",
        # Per-tick suppression events — fire every tick during active hold/block
        # windows; debounce prevents log flooding during prolonged incidents.
        "exploration_suppressed",
        "explore_suppressed_empty_memory",
        "explore_suppressed_no_band_learning",
        "probe_untested_settled_skipped",
        "cell_opt_suppressed",
        "action_suppressed_bouncer_intent",
        "action_suppressed_unstable_lockout",
        "action_suppressed_mutating_inflight",
        # incident_recovery_hold fires every tick during DEGRADED/CONGESTED
        # incident hold (can produce 1000s of identical events in a 2-hour
        # incident window). Diagnostic value is in the first occurrence only.
        "incident_recovery_hold",
        # proposal_suppressed fires every tick when an intent is blocked by
        # objective feedback or an unsupported lever gate.
        "proposal_suppressed",
        # action_suppressed_probe_mode fires every tick an action is proposed
        # while probe_mode is active — long probe sessions produce many identical
        # events. First occurrence is sufficient for tester diagnostics.
        "action_suppressed_probe_mode",
        # action_suppressed_modem_unhealthy fires per tick while modem is
        # degraded and an action is queued. First + payload change is enough.
        "action_suppressed_modem_unhealthy",
        # action_suppressed_modem_read_missed fires per action attempt during
        # short missed-read windows; debounce keeps the log sparse.
        "action_suppressed_modem_read_missed",
        # Keep the confirmed-loss event sparse too.
        "action_suppressed_connection_lost",
        # RouterOS automatic radio-write guard can fire once per tick while
        # observe-only recovery keeps proposing the same blocked LTE write.
        "action_suppressed_routeros_auto_radio_write",
    })
    SUPPRESSION_DEBOUNCE_STABLE_KEYS: dict[str, tuple[str, ...]] = {
        "exploration_suppressed": (
            "gate",
            "lane",
            "incident_class",
            "recommended_next_step",
            "post_fault_class",
            "rf_cliff_reason",
            "escape_probe_reason",
        ),
        "cell_opt_suppressed": (
            "gate",
            "lane",
            "current_pci",
            "target_pci",
        ),
        "explore_suppressed_empty_memory": (
            "gate",
            "lane",
            "reason",
        ),
        "explore_suppressed_no_band_learning": (
            "gate",
            "lane",
            "reason",
        ),
        "probe_untested_settled_skipped": (
            "gate",
            "lane",
            "reason",
        ),
        "action_suppressed_bouncer_intent": (
            "lever",
            "intent",
            "intent_level",
            "reason",
        ),
        "action_suppressed_unstable_lockout": (
            "lever",
            "reason",
        ),
        "action_suppressed_mutating_inflight": (
            "lever",
            "reason",
        ),
        "action_suppressed_probe_mode": (
            "lever",
            "reason",
        ),
        "action_suppressed_modem_unhealthy": (
            "lever",
            "reason",
            "health_stage",
        ),
        "action_suppressed_modem_read_missed": (
            "lever",
            "reason",
        ),
        "action_suppressed_connection_lost": (
            "lever",
            "reason",
        ),
        "action_suppressed_routeros_auto_radio_write": (
            "lever",
            "backend_family",
            "reason",
        ),
        "incident_recovery_hold": (
            "state",
            "incident_class",
            "recommended_next_step",
            "next_step",
            "reason",
        ),
        "proposal_suppressed": (
            "lever",
            "intent",
            "reason",
        ),
    }

    def _cleanup_old_output_files(self) -> None:
        """Compress/delete telemetry files according to OUTPUT_RETENTION_POLICY.

        Covers all telemetry surfaces with explicit surface classes:
          telemetry_history_*.jsonl  [COMPARE / DEBUG]
          telemetry_production_*.jsonl [PRODUCTION MINIMAL]
          events_*.jsonl             [STRUCTURED EVENT STREAM]
          raw_*.log                  [FORENSIC]
          forensic_compact_*.jsonl   [COMPACT FORENSIC DERIVED STREAM]
          telemetry_trace_matrix_*.jsonl [PRODUCTION TRACE MATRIX]

        Called once at logger startup. Long-running sessions accumulate
        at most the per-surface retention window declared in
        OUTPUT_RETENTION_POLICY.
        """
        try:
            historical_reader = HistoricalArtifactReader(
                self.output_dir,
                retention_days=self.OUTPUT_FILE_RETENTION_DAYS,
            )
            self._historical_retention_status = historical_reader.enforce_retention()
            dirs_to_clean = list(historical_reader.roots()) or [self.output_dir]
            removed = 0
            for target_dir in dirs_to_clean:
                for spec in self.OUTPUT_RETENTION_POLICY.values():
                    retention_days = max(1, int(spec.get("retention_days", self.OUTPUT_FILE_RETENTION_DAYS) or self.OUTPUT_FILE_RETENTION_DAYS))
                    cutoff_ts = time.time() - retention_days * 86400
                    for pattern in spec.get("patterns", []):
                        candidates = list(target_dir.glob(str(pattern)))
                        candidates.extend(target_dir.glob(f"{pattern}.gz"))
                        for p in candidates:
                            try:
                                # Retention is based on actual file age, not on the
                                # timestamp embedded in the filename. Restarted or
                                # copied portable sessions may legitimately keep an
                                # older tag while still being a fresh file.
                                if p.stat().st_mtime < cutoff_ts:
                                    p.unlink()
                                    removed += 1
                                    continue
                                if (
                                    "eligible" in str(spec.get("compression") or "")
                                    and p.suffix != ".gz"
                                    and p.stat().st_mtime < time.time() - 86400
                                    and self._compress_closed_output_file(p)
                                ):
                                    removed += 1
                            except OSError:
                                continue
            if removed:
                log.info(
                    "Cleanup: removed %d output files under surface-class retention policy",
                    removed,
                )
        except Exception as exc:
            self._historical_retention_status = {
                "schema": "HistoricalArtifactRetention.v1",
                "owner": "arch4.historical_artifact_reader",
                "status": "degraded",
                "error": str(exc),
            }
            log.debug("_cleanup_old_output_files: suppressed %s", exc)

    @staticmethod
    def _compress_closed_output_file(path: Path) -> bool:
        gz_path = path.with_suffix(path.suffix + ".gz")
        if gz_path.exists():
            return False
        try:
            with atomic_output_path(gz_path, logger=log) as staged_gz:
                with path.open("rb") as src:
                    with gzip.open(staged_gz, "wb", compresslevel=6) as dst:
                        shutil.copyfileobj(src, dst)
            path.unlink()
            return True
        except Exception as exc:
            log.debug("compression skipped for %s: %s", path, exc)
            return False

    # ── Write failure tracking ─────────────────────────────────────────
    # Record write failures for health monitoring. Logger continues
    # attempting writes even after failures to handle transient issues.

    def _record_write_failure(self, reason: str, exc: Exception) -> None:
        """Record a write failure for health tracking.

        Does NOT re-raise — caller decides whether to continue or abort.
        Emits a warning the first time and on significant interval changes.
        """
        self._write_failure_count += 1
        self._last_write_failure_ts = time.time()
        self._last_write_failure_reason = f"{reason}: {type(exc).__name__}: {exc}"

        if not self._degraded_mode:
            self._degraded_mode = True
            log.warning(
                "Logger entering degraded mode: %s (%s)",
                reason, exc,
            )

    def status(self) -> Dict[str, Any]:
        """Return Intelligence evidence-writer status only."""
        compact_status: dict[str, Any] = {}
        if self._compact_forensic_writer is not None:
            try:
                compact_status = self._compact_forensic_writer.health_status()
            except Exception:
                compact_status = {}
        return {
            "degraded_mode": self._degraded_mode,
            "write_failure_count": self._write_failure_count,
            "last_write_failure_ts": self._last_write_failure_ts,
            "last_write_failure_reason": self._last_write_failure_reason,
            "cycle_count": self._cycle_count,
            "log_mode": self._log_mode,
            "production_telemetry_enabled": self._production_telemetry_enabled,
            "full_telemetry_history_enabled": self._write_full_telemetry_history,
            "telemetry_history_rows_written": self._telemetry_history_rows_written,
            "telemetry_history_paused_rows_dropped": self._history_paused_rows_dropped,
            "production_telemetry_rows_written": self._production_telemetry_rows_written,
            "trace_matrix_rows_written": self._trace_matrix_rows_written,
            "compact_forensic_tick_rows_written": self._compact_forensic_tick_rows_written,
            "compact_forensic_event_rows_written": self._compact_forensic_event_rows_written,
            "compact_forensic": compact_status,
            "historical_retention": dict(
                getattr(self, "_historical_retention_status", {}) or {}
            ),
            "event_payload_truncation_count": self._event_payload_truncation_count,
            "last_event_payload_truncation_ts": self._last_event_payload_truncation_ts,
            "last_event_payload_truncation_kind": self._last_event_payload_truncation_kind,
            "last_event_payload_original_bytes": self._last_event_payload_original_bytes,
            "decision_context_ring_items": len(self._decision_context_ring),
            "decision_context_ring_bytes": self._decision_context_ring_bytes,
            "decision_context_ring_max_items": _DECISION_CONTEXT_RING_MAX_ITEMS,
            "decision_context_ring_max_bytes": _DECISION_CONTEXT_RING_MAX_BYTES,
            "decision_context_ring_dropped": self._decision_context_ring_dropped,
            "decision_context_snapshot_rejected": self._decision_context_snapshot_rejected,
            "decision_context_last_rejection": self._decision_context_last_rejection,
            "decision_context_last_rejection_ts": self._decision_context_last_rejection_ts,
            "decision_context_last_accepted_ts": self._decision_context_last_accepted_ts,
            "decision_context_capture_active": self._decision_context_active is not None,
            "decision_context_capture_id": str(
                (self._decision_context_active or {}).get("capture_id", "")
            ),
        }

    def is_degraded(self) -> bool:
        """Return True if logger has experienced write failures."""
        return self._degraded_mode

    def is_raw_log_enabled(self) -> bool:
        """Return True if raw_*.log writes are enabled (default: True)."""
        return bool(self._raw_enabled)

    # ── History file gate ─────────────────────────────────────────────
    # Semaphore for today's JSONL.  While the gate is held for reading,
    # the writer side (``_write_telemetry_history_row``) is blocked.
    # While the writer has the file open, any reader must wait — in
    # practice the engine orchestrates this so reads and writes never
    # overlap.
    #
    # Usage:
    #     with logger.history_file_gate():
    #         # file handle is closed — safe to iterate today's JSONL
    #         crystal_ball.build_from_jsonl(output_dir)

    def history_file_gate(self):
        """Context manager: close today's JSONL for safe external reads.

        Flushes and closes the file handle on entry, reopens for append
        on exit. While inside the gate, ``_write_telemetry_history_row`` drops
        rows with a visible counter; it never retains a memory backlog.
        """
        return _HistoryFileGate(self)

    def _pause_history_writes(self) -> None:
        """Flush and close today's telemetry history file handle."""
        self._history_paused = True
        try:
            if self._telemetry_history_file is not None:
                self._telemetry_history_file.flush()
                self._telemetry_history_file.close()
                self._telemetry_history_file = None
        except Exception as exc:
            log.debug("_pause_history_writes: suppressed %s", exc)

    def _resume_history_writes(self) -> None:
        """Reopen today's telemetry history file for append."""
        self._history_paused = False
        # Force _rotate_telemetry_history to reopen on the next write
        # by clearing the cached day tag.
        self._telemetry_history_day = ""

    def _telemetry_history_name(self, ts: float) -> str:
        day = time.strftime("%Y%m%d", time.localtime(ts))
        return f"{ARCH_RUNTIME_TELEMETRY_HISTORY_BASENAME}_{day}.jsonl"

    def _production_telemetry_name(self, ts: float) -> str:
        day = time.strftime("%Y%m%d", time.localtime(ts))
        return f"{PRODUCTION_TELEMETRY_BASENAME}_{day}.jsonl"

    def _trace_matrix_name(self, ts: float) -> str:
        day = time.strftime("%Y%m%d", time.localtime(ts))
        return f"{TRACE_MATRIX_BASENAME}_{day}.jsonl"

    def _rotate_telemetry_history(self, ts: float) -> None:
        day = time.strftime("%Y%m%d", time.localtime(ts))
        if day == self._telemetry_history_day and self._telemetry_history_file is not None:
            return
        previous_day = self._telemetry_history_day
        previous_path = self._telemetry_history_path
        try:
            if self._telemetry_history_file is not None:
                self._telemetry_history_file.close()
        except Exception as exc:
            log.debug("_rotate_telemetry_history: suppressed %s", exc)
            pass
        if previous_day and previous_day != day and previous_path is not None:
            try:
                if previous_path.exists() and previous_path.stat().st_size == 0:
                    previous_path.unlink()
            except Exception as exc:
                log.debug("_rotate_telemetry_history cleanup suppressed %s", exc)
        name = self._telemetry_history_name(ts)
        self._telemetry_history_day = day
        self._telemetry_history_path = self.output_dir / name
        try:
            self._telemetry_history_file = open(self._telemetry_history_path, "a", buffering=1, encoding="utf-8")
        except Exception as exc:
            self._telemetry_history_file = None
            self._record_write_failure("telemetry history open", exc)

    def _rotate_production_telemetry(self, ts: float) -> None:
        day = time.strftime("%Y%m%d", time.localtime(ts))
        if day == self._production_telemetry_day and self._production_telemetry_file is not None:
            return
        previous_day = self._production_telemetry_day
        previous_path = self._production_telemetry_path
        try:
            if self._production_telemetry_file is not None:
                self._production_telemetry_file.close()
        except Exception as exc:
            log.debug("_rotate_production_telemetry: suppressed %s", exc)
        if previous_day and previous_day != day and previous_path is not None:
            try:
                if previous_path.exists() and previous_path.stat().st_size == 0:
                    previous_path.unlink()
            except Exception as exc:
                log.debug("_rotate_production_telemetry cleanup suppressed %s", exc)
        name = self._production_telemetry_name(ts)
        self._production_telemetry_day = day
        self._production_telemetry_path = self.output_dir / name
        try:
            self._production_telemetry_file = open(
                self._production_telemetry_path,
                "a",
                buffering=1,
                encoding="utf-8",
            )
        except Exception as exc:
            self._production_telemetry_file = None
            self._record_write_failure("production telemetry open", exc)

    def _rotate_trace_matrix(self, ts: float) -> None:
        day = time.strftime("%Y%m%d", time.localtime(ts))
        if day == self._trace_matrix_day and self._trace_matrix_file is not None:
            return
        previous_day = self._trace_matrix_day
        previous_path = self._trace_matrix_path
        try:
            if self._trace_matrix_file is not None:
                self._trace_matrix_file.close()
        except Exception as exc:
            log.debug("_rotate_trace_matrix: suppressed %s", exc)
        if previous_day and previous_day != day and previous_path is not None:
            try:
                if previous_path.exists() and previous_path.stat().st_size == 0:
                    previous_path.unlink()
            except Exception as exc:
                log.debug("_rotate_trace_matrix cleanup suppressed %s", exc)
        name = self._trace_matrix_name(ts)
        self._trace_matrix_day = day
        self._trace_matrix_path = self.output_dir / name
        self._trace_matrix_state = {}
        try:
            self._trace_matrix_file = open(
                self._trace_matrix_path,
                "a",
                buffering=1,
                encoding="utf-8",
            )
        except Exception as exc:
            self._trace_matrix_file = None
            self._record_write_failure("trace matrix open", exc)
    def _telemetry_history_row(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        posture: ControllerPosture,
        *,
        extra: Optional[dict[str, Any]] = None,
        decision_trace_obj: Optional[DecisionTrace] = None,
        decision_context_obj: Optional[DecisionContext] = None,
        tick_input_obj: AcceptedControllerGeneration,
        execution_outcome_obj: ExecutionOutcome,
    ) -> dict[str, Any]:
        if not isinstance(execution_outcome_obj, ExecutionOutcome):
            raise TypeError("telemetry_history_requires_execution_outcome")
        action_lever = execution_outcome_obj.action_lever
        action_reason = execution_outcome_obj.action_reason or "no_action"
        action_text = ""
        if action_lever.lower() != "none":
            action_text = f"{action_lever}: {action_reason}"
        runtime = dict(extra or {})
        if not isinstance(tick_input_obj, AcceptedControllerGeneration):
            raise RuntimeError(
                "telemetry_history_requires_accepted_controller_generation"
            )
        tick_input = tick_input_obj
        session_metadata, _scope, _scope_key = self._session_identity_metadata(
            modem,
            extra=runtime,
        )
        row = build_telemetry_history_fact_row(
            tick_input=tick_input,
            tick_count=self._cycle_count,
            state_name=str(posture.state.name),
            action_lever=action_lever,
            action_reason=action_reason,
            action_text=action_text,
            row_ts=time.time(),
        )
        brain = runtime.get("brain") if isinstance(runtime.get("brain"), dict) else {}
        row.update(
            {
                "decision_trace_id": str(
                    decision_trace_obj.decision_trace_id
                    if decision_trace_obj is not None
                    else brain.get("decision_trace_id", "") or ""
                ),
                "decision_trace_source": str(
                    decision_trace_obj.source
                    if decision_trace_obj is not None
                    else brain.get("decision_trace_source", "") or ""
                ),
                "decision_trace_final": str(
                    decision_trace_obj.final
                    if decision_trace_obj is not None
                    else brain.get("decision_trace_final", "") or ""
                ),
                "decision_trace_verdict": str(
                    decision_trace_obj.tsar_verdict
                    if decision_trace_obj is not None
                    else brain.get("decision_trace_verdict", "") or ""
                ),
                "decision_trace_gate": str(
                    decision_trace_obj.primary_blocker_gate
                    if decision_trace_obj is not None
                    else brain.get("decision_trace_gate", "") or ""
                ),
                "decision_trace_reason": str(
                    decision_trace_obj.primary_blocker_reason
                    if decision_trace_obj is not None
                    else brain.get("decision_trace_reason", "") or ""
                ),
            }
        )
        for diagnostic_key in (
            "modem_sample_publication_status",
            "runtime_clock",
            "runtime_clock_internal_service_status",
        ):
            diagnostic_value = runtime.get(diagnostic_key)
            if isinstance(diagnostic_value, dict) and diagnostic_value:
                row[diagnostic_key] = copy.deepcopy(diagnostic_value)
        self._promote_smartness_owner_payloads(row, runtime)
        if self._include_interpretive_tick_fields:
            incident = (
                runtime.get("incident_assessment")
                if isinstance(runtime.get("incident_assessment"), dict)
                else {}
            )
            root_cause = incident.get("root_cause") if isinstance(incident.get("root_cause"), dict) else {}
            calibration = runtime.get("calibration") if isinstance(runtime.get("calibration"), dict) else {}
            row.update(
                {
                    "warning_class": str(
                        decision_context_obj.warning_class
                        if decision_context_obj is not None
                        else getattr(posture, "warning_class", "") or ""
                    ),
                    "warning_source": str(
                        decision_context_obj.warning_source
                        if decision_context_obj is not None
                        else getattr(posture, "warning_source", "") or ""
                    ),
                    "warning_mechanism": str(
                        decision_context_obj.warning_mechanism
                        if decision_context_obj is not None
                        else getattr(posture, "warning_mechanism", "") or ""
                    ),
                    "warning_reason": str(
                        decision_context_obj.warning_reason
                        if decision_context_obj is not None
                        else getattr(posture, "warning_reason", "") or ""
                    ),
                    "incident_class": str(
                        decision_context_obj.incident_class
                        if decision_context_obj is not None
                        else incident.get("incident_class", "") or ""
                    ),
                    "incident_next_step": str(
                        decision_context_obj.incident_next_step
                        if decision_context_obj is not None
                        else incident.get("recommended_next_step", "") or ""
                    ),
                    "root_cause": {
                        "schema_version": str(root_cause.get("schema_version", "") or ""),
                        "owner": str(root_cause.get("owner", "") or ""),
                        "probable_cause": str(root_cause.get("probable_cause", "none") or "none"),
                        "cause_confidence": str(root_cause.get("cause_confidence", "") or ""),
                        "evidence_state": str(root_cause.get("evidence_state", "") or ""),
                        "missing_evidence": list(root_cause.get("missing_evidence", []) or [])
                        if isinstance(root_cause.get("missing_evidence"), list)
                        else [],
                        "rsrq_gap": _safe_float(root_cause.get("rsrq_gap"), 0.0),
                        "rsrq_gap_valid": bool(root_cause.get("rsrq_gap_valid", False)),
                    },
                    "calibration_network_load_biased": bool(calibration.get("network_load_biased", False)),
                    "calibration_offpeak_window": bool(calibration.get("offpeak_window", False)),
                    "calibration_baseline_drift_detected": bool(
                        calibration.get("baseline_drift_detected", False)
                    ),
                    "calibration_recalibration_recommended": bool(
                        calibration.get("recalibration_recommended", False)
                    ),
                    "calibration_threshold_adjust_hint_db": _safe_float(
                        calibration.get("threshold_adjust_hint_db"),
                        0.0,
                    ),
                    "calibration_baseline_drift_reason": str(
                        calibration.get("baseline_drift_reason", "") or ""
                    ),
                }
            )
        row.update(
            build_delta_display_surface(
                raw=row,
                previous=self._last_delta_display_surface,
                now_ts=float(row.get("ts") or time.time()),
                rsrq_supported=(str(row.get("rsrq_source") or "reported").strip().lower() != "unsupported"),
            )
        )
        self._last_delta_display_surface = {
            "rtt_delta_display_ms": row.get("rtt_delta_display_ms"),
            "rtt_delta_display_valid": row.get("rtt_delta_display_valid"),
            "rtt_delta_display_held": row.get("rtt_delta_display_held"),
            "rtt_delta_display_live_ts": row.get("rtt_delta_display_live_ts"),
            "rsrq_delta_display_db": row.get("rsrq_delta_display_db"),
            "rsrq_delta_display_valid": row.get("rsrq_delta_display_valid"),
            "rsrq_delta_display_held": row.get("rsrq_delta_display_held"),
            "rsrq_delta_display_live_ts": row.get("rsrq_delta_display_live_ts"),
            "rsrq_baseline_display_db": row.get("rsrq_baseline_display_db"),
            "rsrq_baseline_display_valid": row.get("rsrq_baseline_display_valid"),
            "rsrq_baseline_display_held": row.get("rsrq_baseline_display_held"),
            "rsrq_baseline_display_live_ts": row.get("rsrq_baseline_display_live_ts"),
        }
        row.update(session_metadata)
        _fill_provider_identity_from_canonical_surfaces(
            row,
            runtime=runtime,
            modem=modem,
            session_metadata=session_metadata,
        )
        return row

    @staticmethod
    def _promote_smartness_owner_payloads(row: dict[str, Any], runtime: Mapping[str, Any]) -> None:
        """Promote owner-stamped smartness surfaces into the tick fact row."""

        for key, (schema, owner) in _SMARTNESS_OWNER_PAYLOADS.items():
            payload = runtime.get(key)
            if not isinstance(payload, Mapping):
                continue
            if str(payload.get("schema") or "") != schema:
                continue
            if str(payload.get("owner") or "") != owner:
                continue
            row[key] = copy.deepcopy(dict(payload))

    def _write_telemetry_history_row(self, row: dict[str, Any]) -> None:
        if not self._write_full_telemetry_history:
            return
        if self._history_paused:
            self._history_paused_rows_dropped += 1
            return
        ts = float(row.get("ts") or time.time())
        self._rotate_telemetry_history(ts)
        payload = json.dumps(row)
        try:
            if self._telemetry_history_file is not None:
                self._telemetry_history_file.write(payload + "\n")
                self._telemetry_history_rows_written = int(getattr(self, "_telemetry_history_rows_written", 0) or 0) + 1
            else:
                self._record_write_failure("telemetry history file unavailable", RuntimeError("file handle unavailable"))
        except Exception as exc:
            self._record_write_failure("telemetry history write", exc)

    def _write_production_telemetry_row(self, row: Mapping[str, Any]) -> None:
        if not self._production_telemetry_enabled:
            return
        ts = float(row.get("ts") or time.time())
        self._rotate_production_telemetry(ts)
        try:
            payload = json.dumps(build_production_telemetry_row(row), separators=(",", ":"))
        except Exception as exc:
            self._record_write_failure("production telemetry encode", exc)
            return
        try:
            if self._production_telemetry_file is not None:
                self._production_telemetry_file.write(payload + "\n")
                self._production_telemetry_rows_written = int(getattr(self, "_production_telemetry_rows_written", 0) or 0) + 1
            else:
                self._record_write_failure(
                    "production telemetry file unavailable",
                    RuntimeError("file handle unavailable"),
                )
        except Exception as exc:
            self._record_write_failure("production telemetry write", exc)

    def _write_trace_matrix_row(self, row: Mapping[str, Any]) -> None:
        ts = float(row.get("ts") or time.time())
        self._rotate_trace_matrix(ts)
        try:
            delta = build_trace_matrix_delta(row, previous_dimensions=self._trace_matrix_state)
            self._trace_matrix_state = dict(delta.get("_state") or {})
            payload = json.dumps(public_trace_matrix_delta(delta), separators=(",", ":"))
        except Exception as exc:
            self._record_write_failure("trace matrix encode", exc)
            return
        try:
            if self._trace_matrix_file is not None:
                self._trace_matrix_file.write(payload + "\n")
                self._trace_matrix_rows_written = int(getattr(self, "_trace_matrix_rows_written", 0) or 0) + 1
            else:
                self._record_write_failure(
                    "trace matrix file unavailable",
                    RuntimeError("file handle unavailable"),
                )
        except Exception as exc:
            self._record_write_failure("trace matrix write", exc)

    @staticmethod
    def _compact_forensic_session_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
        keys = (
            "session_scope_key",
            "session_seq",
            "target",
            "backend",
            "backend_family",
            "model",
            "firmware",
            "software_version",
            "webui",
            "provider_name",
            "provider_plmn",
            "serial",
            "imei",
            "sim_id",
            "resolved_profile_id",
            "profile_id",
            "profile_provenance",
            "identity_state",
            "support_tier_label",
            "match_quality",
        )
        session = {
            key: row.get(key)
            for key in keys
            if row.get(key) not in (None, "", [], {})
        }
        if "session_seq" not in session:
            session["session_seq"] = 0
        return session

    def _write_compact_forensic_tick(self, row: Mapping[str, Any]) -> None:
        writer = self._compact_forensic_writer
        if writer is None:
            return
        try:
            session = self._compact_forensic_session_from_row(row)
            if session:
                writer.write_session(session)
            writer.write_tick(row)
            self._compact_forensic_tick_rows_written = int(getattr(self, "_compact_forensic_tick_rows_written", 0) or 0) + 1
        except Exception as exc:
            self._record_write_failure("compact forensic tick write", exc)

    def _write_compact_forensic_event(self, event: Mapping[str, Any]) -> None:
        writer = self._compact_forensic_writer
        if writer is None:
            return
        try:
            writer.write_event(event)
            self._compact_forensic_event_rows_written = int(getattr(self, "_compact_forensic_event_rows_written", 0) or 0) + 1
        except Exception as exc:
            self._record_write_failure("compact forensic event write", exc)

    def write_regrade_record(
        self,
        *,
        hour: int,
        day_type: str,
        band: int,
        cell_id: int = 0,
        drift_reason: str = "",
        confidence_penalty: int = 1,
        evidence_scope: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Write an action_regrade record through the telemetry owner lanes.

        Called by the engine when crystal ball drift detection shows that
        the forecast for a (day_type, hour, band) slot was wrong.  On cold
        rebuild the crystal ball reads these and reduces confidence on the
        affected slots so wrong predictions don't persist forever.

        Production always records compact numbered/trace evidence.  Full
        telemetry_history receives the same record only in explicit
        compare/debug/trace modes.
        """
        row = {
            "record_type": "action_regrade",
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "hour": int(hour),
            "day_type": str(day_type or ""),
            "primary_band": int(band),
            "cell_id": int(cell_id),
            "drift_reason": str(drift_reason or ""),
            "confidence_penalty": int(confidence_penalty),
            "evidence_scope": dict(evidence_scope or {}),
        }
        defaults = self.runtime_event_defaults()
        if defaults:
            row.update(defaults)
        self._write_telemetry_history_row(row)
        self._write_production_telemetry_row(row)
        self._write_trace_matrix_row(row)

    def _decision_context_snapshot_from_tick(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        posture: ControllerPosture,
        *,
        telemetry_row: Mapping[str, Any],
        extra: Optional[dict[str, Any]] = None,
        decision_trace_obj: Optional[DecisionTrace] = None,
        decision_context_obj: Optional[DecisionContext] = None,
        execution_outcome_obj: ExecutionOutcome,
    ) -> dict[str, Any]:
        if not isinstance(execution_outcome_obj, ExecutionOutcome):
            raise TypeError("decision_context_snapshot_requires_execution_outcome")
        runtime = dict(extra or {})
        brain = runtime.get("brain") if isinstance(runtime.get("brain"), dict) else {}
        health = runtime.get("health_checks") if isinstance(runtime.get("health_checks"), dict) else {}
        incident = runtime.get("incident_assessment") if isinstance(runtime.get("incident_assessment"), dict) else {}
        bouncer_intent = (
            runtime.get("bouncer_intent")
            if isinstance(runtime.get("bouncer_intent"), dict) else {}
        )
        bouncer_outcome = (
            runtime.get("bouncer_outcome")
            if isinstance(runtime.get("bouncer_outcome"), dict) else {}
        )
        return build_decision_context_capture_snapshot(
            telemetry_row,
            context_fields={
                "time_in_state_s": float(getattr(posture, "time_in_state_s", 0.0) or 0.0),
                "qm_dl0": int(telemetry_row.get("qm_dl0", 0) or 0),
                "qm_dl1": int(telemetry_row.get("qm_dl1", 0) or 0),
                "qm_ul": int(telemetry_row.get("qm_ul", 0) or 0),
                "tbs_dl0": int(runtime.get("tbs_dl0", 0) or 0),
                "tbs_dl1": int(runtime.get("tbs_dl1", 0) or 0),
                "tbs_ul": int(runtime.get("tbs_ul", 0) or 0),
                "tx_power": _safe_float(runtime.get("tx_power"), 0.0),
                "tx_power_text": str(runtime.get("tx_power") or ""),
                "rtt_pressure_level": str(getattr(computed.latency, 'rtt_pressure_level', "") or ""),
                "health_stage": str(
                    decision_context_obj.health_stage
                    if decision_context_obj is not None
                    else health.get("stage", brain.get("health_stage", "")) or ""
                ),
                "health_action_hint": str(
                    decision_context_obj.health_action_hint
                    if decision_context_obj is not None
                    else health.get("action_hint", brain.get("health_action_hint", "")) or ""
                ),
                "warning_class": str(
                    decision_context_obj.warning_class
                    if decision_context_obj is not None
                    else getattr(posture, "warning_class", "") or ""
                ),
                "warning_source": str(
                    decision_context_obj.warning_source
                    if decision_context_obj is not None
                    else getattr(posture, "warning_source", "") or ""
                ),
                "warning_mechanism": str(
                    decision_context_obj.warning_mechanism
                    if decision_context_obj is not None
                    else getattr(posture, "warning_mechanism", "") or ""
                ),
                "warning_reason": str(
                    decision_context_obj.warning_reason
                    if decision_context_obj is not None
                    else getattr(posture, "warning_reason", "") or ""
                ),
                "incident_class": str(
                    decision_context_obj.incident_class
                    if decision_context_obj is not None
                    else incident.get("incident_class", brain.get("incident_class", "")) or ""
                ),
                "incident_next_step": str(
                    decision_context_obj.incident_next_step
                    if decision_context_obj is not None
                    else incident.get(
                        "incident_next_step",
                        incident.get("recommended_next_step", brain.get("incident_next_step", "")),
                    ) or ""
                ),
                "decision_trace_id": str(
                    decision_trace_obj.decision_trace_id
                    if decision_trace_obj is not None
                    else brain.get("decision_trace_id", "") or ""
                ),
                "decision_trace_source": str(
                    decision_trace_obj.source
                    if decision_trace_obj is not None
                    else brain.get("decision_trace_source", "") or ""
                ),
                "decision_trace_final": str(
                    decision_trace_obj.final
                    if decision_trace_obj is not None
                    else brain.get("decision_trace_final", "") or ""
                ),
                "bouncer_intent": str(
                    execution_outcome_obj.bouncer_intent_code
                    if execution_outcome_obj is not None
                    else bouncer_intent.get("code", "") or ""
                ),
                "bouncer_outcome": str(
                    execution_outcome_obj.bouncer_outcome_code
                    if execution_outcome_obj is not None
                    else bouncer_outcome.get("code", "") or ""
                ),
                "action_suppressed": execution_outcome_obj.action_suppressed,
                "action_origin": execution_outcome_obj.action_origin,
                "operator_initiated": execution_outcome_obj.operator_initiated,
            },
            runtime=runtime,
        )

    @staticmethod
    def _format_action_outcome(outcome: Optional[ExecutionOutcome]) -> str:
        if outcome is None:
            return ""
        if outcome.action_suppressed:
            return "suppressed"
        success = outcome.action_success
        if success is None:
            return ""
        return "success" if success else "fail"

    def record_cycle(self,
                 modem: CellularIntelligenceGeneration,
                 computed: DerivedSignalFrames,
                 posture: ControllerPosture,
                 *,
                 extra: Optional[dict[str, Any]] = None,
                 decision_trace_obj: Optional[DecisionTrace] = None,
                 decision_context_obj: Optional[DecisionContext] = None,
                 tick_input_obj: AcceptedControllerGeneration,
                 execution_outcome_obj: ExecutionOutcome) -> None:
        """Log one brain tick.

        Writes production telemetry/trace evidence for each tick. Full
        telemetry_history rows are written only when explicit compare/debug
        modes are enabled; CSV is maintained for legacy consumers only.
        """
        self._cycle_count += 1
        if not isinstance(tick_input_obj, AcceptedControllerGeneration):
            raise RuntimeError(
                "intelligence_evidence_requires_accepted_controller_generation"
            )
        if not isinstance(execution_outcome_obj, ExecutionOutcome):
            raise TypeError("intelligence_evidence_requires_execution_outcome")
        tick_input = tick_input_obj
        tick_facts = project_controller_read_model(tick_input)
        self._update_session_context(modem, extra=extra)

        telemetry_row = self._telemetry_history_row(
            modem,
            computed,
            posture,
            extra=extra,
            decision_trace_obj=decision_trace_obj,
            decision_context_obj=decision_context_obj,
            tick_input_obj=tick_input,
            execution_outcome_obj=execution_outcome_obj,
        )
        self._write_telemetry_history_row(telemetry_row)
        self._write_production_telemetry_row(telemetry_row)
        self._write_trace_matrix_row(telemetry_row)
        self._write_compact_forensic_tick(telemetry_row)
        self.observe_decision_context(
            self._decision_context_snapshot_from_tick(
                modem,
                computed,
                posture,
                telemetry_row=telemetry_row,
                extra=extra,
                decision_trace_obj=decision_trace_obj,
                decision_context_obj=decision_context_obj,
                execution_outcome_obj=execution_outcome_obj,
            )
        )
        with self._config_generation_lock:
            if self._rollup:
                self._rollup.update(
                    modem,
                    computed,
                    posture,
                    action_present=(
                        execution_outcome_obj.action_lever.lower() != "none"
                    ),
                )

    def log_event(self, kind: str, **kwargs) -> None:
        """Log a structured event to events_*.jsonl.

        Every event carries fixed envelope fields:
          ts, iso         — timestamp (unix + ISO-8601)
          tick, seq       — brain tick counter + monotonic event sequence
          kind            — canonical event type discriminator [REQUIRED]
          event           — compat alias for kind [COMPAT ALIAS — use kind]
          schema_version  — 'arch_event_v2' for deserializers [REQUIRED]

        Payload fields (**kwargs) are event-kind-specific. See
        docs/event_jsonl_schema.md for the full field inventory.
        """
        payload = dict(kwargs or {})
        event_ts, event_iso = self._consume_event_timestamp(payload)
        verdict = payload.get("verdict") if isinstance(payload.get("verdict"), dict) else {}
        if verdict:
            payload.setdefault("level", str(verdict.get("level", "") or ""))
            payload.setdefault("code", str(verdict.get("code", "") or ""))
            payload.setdefault("message", str(verdict.get("message", "") or ""))
            if "details" not in payload and isinstance(verdict.get("details"), dict):
                payload["details"] = dict(verdict.get("details") or {})
        defaults = self.runtime_event_defaults()
        if defaults:
            payload = derive_runtime_sidecar_metadata(payload, defaults) | payload
        # Runtime defaults may carry their own sample timestamp. The event
        # envelope owns these fields, so keep producer/event time canonical.
        for envelope_key in ("ts", "iso", "tick", "seq", "kind", "event", "schema_version"):
            payload.pop(envelope_key, None)
        payload = self._enforce_event_payload_budget(kind, payload)
        if payload.get("payload_truncated") is True:
            self._event_payload_truncation_count += 1
            self._last_event_payload_truncation_ts = time.time()
            self._last_event_payload_truncation_kind = str(kind or "")
            try:
                self._last_event_payload_original_bytes = int(payload.get("payload_original_bytes", 0) or 0)
            except Exception:
                self._last_event_payload_original_bytes = 0
        now = event_ts if event_ts > 0.0 else time.time()
        iso = event_iso or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
        if self._should_debounce_event(kind, payload, now=now):
            return
        self._event_seq += 1
        event = {
            "ts": now,
            "iso": iso,
            "tick": self._cycle_count,
            "seq": self._event_seq,
            "kind": kind,                    # canonical discriminator
            "event": kind,                   # COMPAT ALIAS — use 'kind' in new consumers
            "schema_version": "arch_event_v2",  # schema version for deserializers
            **payload,
        }
        event_line = json.dumps(event) + "\n"
        self._write_event_line(event_line)
        self._write_compact_forensic_event(event)
        _RAW_ECHO_KINDS = {
            "startup",
            "effective_policy_snapshot",
            "runtime_session_scope_changed",
            "external_memory_auto_import",
            "state_transition",
            "health_stage_change",
            "radio_state_change",
            "cell_structural_change",
            "action",
            "action_suppressed",
            "action_learning_outcome",
            "action_grade",
            "manual_action_submitted",
            "manual_action_executed",
            "manual_action_rejected",
            "runtime_recovery_armed",
            "manual_radio_guard_armed",
            "mutating_action_verification",
            "exploration_suppressed",
            "emergency_resume",
            "emergency_clear",
            "calibration_started",
            "calibration_completed",
            "calibration_stopped",
            "logger_degraded",
        }
        if kind in _RAW_ECHO_KINDS:
            self.log_raw(self._raw_event_line(kind=kind, event=event, payload=payload))

    def log_event_once(self, idempotency_key: str, kind: str, **kwargs: Any) -> dict[str, Any]:
        """Durably write one event for a stable projection key.

        The event stream itself is the idempotency registry, so a restart can
        prove an earlier write completed without trusting volatile state.
        """

        key = str(idempotency_key or "").strip()
        if not key:
            return {"recorded": False, "reason": "idempotency_key_missing"}
        with self._idempotent_event_lock:
            if self._event_idempotency_key_exists(key):
                return {"recorded": True, "duplicate": True, "idempotency_key": key}
            self.log_event(kind, idempotency_key=key, **kwargs)
            if self._event_file is not None:
                try:
                    self._event_file.flush()
                    os.fsync(self._event_file.fileno())
                except Exception as exc:
                    self._record_write_failure("idempotent event fsync", exc)
            recorded = self._event_idempotency_key_exists(key, refresh=True)
            return {
                "recorded": bool(recorded),
                "duplicate": False,
                "idempotency_key": key,
                "reason": "recorded" if recorded else "event_write_not_durable",
            }

    def _event_idempotency_key_exists(self, key: str, *, refresh: bool = False) -> bool:
        if not refresh and key in self._idempotent_event_keys:
            return True
        reader = HistoricalArtifactReader(self.output_dir)
        for record in reader.iter_records(families=(FAMILY_EVENT,)):
            if str(record.payload.get("idempotency_key") or "") == key:
                self._idempotent_event_keys.add(key)
                return True
        return False

    def _write_event_line(self, event_line: str) -> None:
        """Write one structured event row, with a direct append recovery path."""
        if self._event_file:
            try:
                self._event_file.write(event_line)
                return
            except Exception as exc:
                self._record_write_failure("event write", exc)
        elif self._raw_file is not None or self._telemetry_history_file is not None:
            self._record_write_failure("event write", RuntimeError("event file handle unavailable"))
        else:
            return

        try:
            with self._event_path.open("a", encoding="utf-8") as handle:
                handle.write(event_line)
        except Exception as exc:
            self._record_write_failure("event recovery write", exc)

    @staticmethod
    def _consume_event_timestamp(payload: Dict[str, Any]) -> tuple[float, str]:
        event_ts = 0.0
        for key in ("event_ts", "producer_ts", "ts"):
            if key not in payload:
                continue
            value = payload.pop(key, None)
            try:
                event_ts = float(value)
            except Exception:
                event_ts = 0.0
            if event_ts > 0.0:
                break
        event_iso = ""
        for key in ("event_iso", "producer_iso", "iso"):
            if key not in payload:
                continue
            event_iso = str(payload.pop(key, "") or "").strip()
            if event_iso:
                break
        return event_ts, event_iso

    @classmethod
    def _json_size_bytes(cls, value: Any) -> int:
        try:
            return len(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))
        except Exception:
            return len(str(value).encode("utf-8", errors="replace"))

    @classmethod
    def _truncate_event_value(cls, value: Any, *, max_bytes: int) -> Any:
        value_type = type(value).__name__
        if isinstance(value, str):
            encoded = value.encode("utf-8", errors="replace")
            if len(encoded) <= max_bytes:
                return value
            preview = encoded[:max(0, max_bytes - 128)].decode("utf-8", errors="ignore")
            return {
                "_truncated": True,
                "type": "str",
                "original_bytes": len(encoded),
                "preview": preview[: cls.EVENT_PAYLOAD_PREVIEW_CHARS],
            }
        item_count = 0
        if isinstance(value, Mapping):
            item_count = len(value)
        elif isinstance(value, (list, tuple, set)):
            item_count = len(value)
        preview = ""
        try:
            preview = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        except Exception:
            preview = str(value)
        return {
            "_truncated": True,
            "type": value_type,
            "item_count": item_count,
            "original_bytes": cls._json_size_bytes(value),
            "preview": preview[: cls.EVENT_PAYLOAD_PREVIEW_CHARS],
        }

    @classmethod
    def _enforce_event_payload_budget(cls, kind: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        out = dict(payload or {})
        if not out:
            return out
        original_bytes = cls._json_size_bytes(out)
        if original_bytes <= cls.EVENT_PAYLOAD_MAX_BYTES:
            return out

        protected_keys = {
            "schema",
            "signature",
            "decision_trace_id",
            "snapshot_emit_reason",
            "suppressed_duplicate_count",
            "action_lever",
            "action_origin",
            "operator_initiated",
            "action_suppressed",
            "level",
            "code",
            "message",
        }
        truncated_fields: list[str] = []
        for key, value in list(out.items()):
            if key in protected_keys:
                continue
            if cls._json_size_bytes(value) > cls.EVENT_PAYLOAD_FIELD_MAX_BYTES:
                out[key] = cls._truncate_event_value(value, max_bytes=cls.EVENT_PAYLOAD_FIELD_MAX_BYTES)
                truncated_fields.append(str(key))

        while cls._json_size_bytes(out) > cls.EVENT_PAYLOAD_MAX_BYTES:
            candidates = [
                (cls._json_size_bytes(value), key)
                for key, value in out.items()
                if key not in protected_keys and key not in {"payload_truncated", "payload_truncated_fields"}
            ]
            if not candidates:
                break
            _, key = max(candidates)
            if isinstance(out.get(key), Mapping) and out[key].get("_truncated") is True:
                break
            out[key] = cls._truncate_event_value(out.get(key), max_bytes=cls.EVENT_PAYLOAD_FIELD_MAX_BYTES)
            if str(key) not in truncated_fields:
                truncated_fields.append(str(key))

        if truncated_fields:
            out["payload_truncated"] = True
            out["payload_truncation_reason"] = "event_payload_budget"
            out["payload_original_bytes"] = original_bytes
            out["payload_truncated_fields"] = sorted(truncated_fields)
        return out

    @staticmethod
    def _raw_echo_value(value: Any) -> str:
        if isinstance(value, (dict, list, tuple, set)):
            try:
                text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
            except Exception:
                text = str(value)
        else:
            text = str(value)
        text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
        if len(text) > _RAW_ECHO_VALUE_LIMIT:
            text = text[: _RAW_ECHO_VALUE_LIMIT - 3] + "..."
        return text

    def _raw_event_line(self, *, kind: str, event: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
        parts = [
            f"{event.get('iso', '')} [{kind}]",
            f"tick={event.get('tick', '')}",
            f"seq={event.get('seq', '')}",
        ]
        for key, value in payload.items():
            if key in _RAW_ECHO_SKIP_KEYS or value in (None, ""):
                continue
            parts.append(f"{key}={self._raw_echo_value(value)}")
            if len(" ".join(parts)) >= _RAW_ECHO_LINE_LIMIT:
                break
        line = " ".join(parts)
        if len(line) > _RAW_ECHO_LINE_LIMIT:
            return line[: _RAW_ECHO_LINE_LIMIT - 3] + "..."
        return line

    def _should_debounce_event(self, kind: str, payload: Mapping[str, Any], *, now: float) -> bool:
        if kind not in self.EVENT_DEBOUNCE_KINDS:
            return False
        try:
            fingerprint = json.dumps(
                self._event_debounce_fingerprint(kind, payload),
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except Exception:
            return False
        last_ts = float(self._event_debounce_seen.get(fingerprint, 0.0) or 0.0)
        window_s = float(self.EVENT_DEBOUNCE_WINDOWS_S.get(kind, self.EVENT_DEBOUNCE_WINDOW_S))
        if last_ts > 0.0 and (now - last_ts) < window_s:
            return True
        self._event_debounce_seen[fingerprint] = now
        return False

    @staticmethod
    def _event_debounce_fingerprint(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if kind == "decision_trace":
            return {
                "kind": kind,
                "source": payload.get("source", ""),
                "state": payload.get("state", ""),
                "proposed": payload.get("proposed", ""),
                "final": payload.get("final", ""),
                "tsar_verdict": payload.get("tsar_verdict", ""),
                "tsar_reason": payload.get("tsar_reason", ""),
                "primary_blocker_gate": payload.get("primary_blocker_gate", ""),
                "primary_blocker_reason": payload.get("primary_blocker_reason", ""),
            }
        if kind == "cell_structural_change":
            return {
                "kind": kind,
                "change_kind": payload.get("change_kind", ""),
                "band": payload.get("band", ""),
                "pci": payload.get("pci", ""),
                "earfcn": payload.get("earfcn", ""),
                "ca_sig": payload.get("ca_sig", ""),
                "ca_sig_before": payload.get("ca_sig_before", ""),
                "band_changed": payload.get("band_changed", ""),
                "pci_changed": payload.get("pci_changed", ""),
                "ca_changed": payload.get("ca_changed", ""),
            }
        if kind in {
            "brain_posture_deferred",
            "radio_stability_hold_deferred",
            "action_availability_blocked",
        }:
            return {
                "kind": kind,
                "source": payload.get("source", ""),
                "state": payload.get("state", ""),
                "lever": payload.get("lever", ""),
                "gate": payload.get("gate", ""),
                "reason": IntelligenceEvidenceRecorder._event_debounce_reason(
                    payload.get("reason")
                    or payload.get("message")
                    or payload.get("blocker")
                    or payload.get("suppressed_reason")
                    or ""
                ),
            }
        stable_keys = IntelligenceEvidenceRecorder.SUPPRESSION_DEBOUNCE_STABLE_KEYS.get(kind)
        if stable_keys:
            return {
                "kind": kind,
                **{key: payload.get(key, "") for key in stable_keys},
            }
        return {"kind": kind, "payload": dict(payload)}

    @staticmethod
    def _event_debounce_reason(value: Any) -> str:
        text = " ".join(str(value or "").split())
        if not text:
            return ""
        import re

        text = re.sub(r"\(\s*\d+(?:\.\d+)?s\s+left\s*\)", "(time-left)", text)
        text = re.sub(r"\b\d+(?:\.\d+)?s\s+remaining\b", "time-remaining", text)
        return text

    def log_state_transition(self, old: SystemState, new: SystemState,
                             reason: str = "") -> None:
        """Log a state transition event."""
        reason_txt = str(reason or "").strip()
        if not reason_txt:
            reason_txt = f"state transition {old.name}->{new.name}"
        self.log_event("state_transition",
                       old_state=old.name, new_state=new.name, reason=reason_txt)

    def configure_decision_context_capture(
        self,
        *,
        enabled: bool,
        sample_interval_s: float,
        start_hour: int,
        end_hour: int,
        pre_s: float,
        post_s: float,
        ring_s: float,
    ) -> None:
        self._decision_context_enabled = bool(enabled)
        self._decision_context_start_hour = int(start_hour)
        self._decision_context_end_hour = int(end_hour)
        self._decision_context_pre_s = max(30.0, float(pre_s or 0.0))
        self._decision_context_post_s = max(30.0, float(post_s or 0.0))
        min_ring = self._decision_context_pre_s + self._decision_context_post_s
        self._decision_context_ring_s = max(float(ring_s or 0.0), min_ring, max(60.0, float(sample_interval_s or 2.0) * 10.0))
        self._decision_context_ring.clear()
        self._decision_context_ring_bytes = 0
        self._decision_context_last_snapshot = None
        self._decision_context_active = None

    def observe_decision_context(self, snapshot: dict[str, Any]) -> None:
        if not self._decision_context_enabled or not isinstance(snapshot, dict):
            return
        try:
            now = float(snapshot.get("ts") or time.time())
        except Exception:
            now = time.time()
        retained_snapshot = dict(snapshot)
        try:
            retained_bytes = retained_payload_bytes(
                retained_snapshot,
                max_bytes=_DECISION_CONTEXT_SNAPSHOT_MAX_BYTES,
                label="decision context snapshot",
            )
        except PayloadBudgetExceeded as exc:
            self._decision_context_snapshot_rejected += 1
            self._decision_context_last_rejection = str(exc)
            self._decision_context_last_rejection_ts = float(now)
            if self._decision_context_snapshot_rejected == 1 or self._decision_context_snapshot_rejected % 100 == 0:
                log.warning("Decision-context snapshot rejected before retention: %s", exc)
                self.log_event(
                    "decision_context_snapshot_rejected",
                    reason=str(exc),
                    rejection_count=int(self._decision_context_snapshot_rejected),
                    max_snapshot_bytes=int(_DECISION_CONTEXT_SNAPSHOT_MAX_BYTES),
                )
            return
        self._decision_context_last_accepted_ts = float(now)
        while self._decision_context_ring and (
            len(self._decision_context_ring) >= _DECISION_CONTEXT_RING_MAX_ITEMS
            or self._decision_context_ring_bytes + retained_bytes > _DECISION_CONTEXT_RING_MAX_BYTES
        ):
            _dropped_snapshot, dropped_bytes = self._decision_context_ring.popleft()
            self._decision_context_ring_bytes = max(
                0,
                self._decision_context_ring_bytes - int(dropped_bytes),
            )
            self._decision_context_ring_dropped += 1
        self._decision_context_ring.append((retained_snapshot, retained_bytes))
        self._decision_context_ring_bytes += retained_bytes
        self._prune_decision_context_ring(now)

        previous = self._decision_context_last_snapshot or {}
        reasons = self._decision_context_reasons(previous, snapshot)
        in_window = self._decision_context_in_window(now)

        if self._decision_context_active is None and reasons and in_window:
            self._start_decision_context_capture(now=now, reasons=reasons)
            self._write_decision_context_trigger(snapshot=snapshot, reasons=reasons)
            self._write_decision_context_backfill(now=now)
            self.log_event(
                "decision_context_capture_started",
                capture_id=str(self._decision_context_active.get("capture_id", "")) if self._decision_context_active else "",
                capture_reasons=list(reasons),
            )
        elif self._decision_context_active is not None and reasons:
            self._decision_context_active["end_ts"] = max(
                float(self._decision_context_active.get("end_ts", now)),
                now + self._decision_context_post_s,
            )
            self._write_decision_context_trigger(snapshot=snapshot, reasons=reasons)

        if self._decision_context_active is not None:
            last_written_ts = float(self._decision_context_active.get("last_written_ts", 0.0))
            if now > last_written_ts:
                self._append_decision_context_rows([
                    self._decision_context_row("tick", snapshot=snapshot),
                ])
                self._decision_context_active["last_written_ts"] = now
            if now >= float(self._decision_context_active.get("end_ts", now)):
                self._decision_context_active = None

        self._decision_context_last_snapshot = dict(snapshot)

    @staticmethod
    def _hour_in_window(hour: int, start_hour: int, end_hour: int) -> bool:
        if start_hour == end_hour:
            return True
        if start_hour < end_hour:
            return start_hour <= hour < end_hour
        return hour >= start_hour or hour < end_hour

    def _decision_context_in_window(self, ts: float) -> bool:
        try:
            hour = time.localtime(ts).tm_hour
        except Exception:
            hour = time.localtime().tm_hour
        return self._hour_in_window(hour, self._decision_context_start_hour, self._decision_context_end_hour)

    def _prune_decision_context_ring(self, now: float) -> None:
        cutoff = now - self._decision_context_ring_s
        while (
            self._decision_context_ring
            and float(self._decision_context_ring[0][0].get("ts", 0.0) or 0.0) < cutoff
        ):
            _snapshot, retained_bytes = self._decision_context_ring.popleft()
            self._decision_context_ring_bytes = max(
                0,
                self._decision_context_ring_bytes - int(retained_bytes),
            )

    def _decision_context_reasons(
        self,
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> list[str]:
        reasons: list[str] = []
        if str(current.get("action_lever") or "none") != "none":
            if bool(current.get("action_suppressed")):
                reasons.append("action_suppressed")
            else:
                reasons.append("action")
        if previous:
            if str(current.get("state") or "") != str(previous.get("state") or ""):
                reasons.append("state_change")
            if str(current.get("incident_class") or "") != str(previous.get("incident_class") or ""):
                reasons.append("incident_change")
            if str(current.get("health_stage") or "") != str(previous.get("health_stage") or ""):
                reasons.append("health_stage_change")
            if str(current.get("radio_state_text") or "") != str(previous.get("radio_state_text") or ""):
                reasons.append("radio_state_change")
            if str(current.get("serving_band_signature") or "") != str(previous.get("serving_band_signature") or ""):
                reasons.append("serving_band_change")
            if str(current.get("band_set_signature") or "") != str(previous.get("band_set_signature") or ""):
                reasons.append("band_set_change")
        unique: list[str] = []
        for reason in reasons:
            if reason and reason not in unique:
                unique.append(reason)
        return unique

    def _decision_context_capture_name(self, now: float) -> str:
        self._decision_context_seq += 1
        return f"decision_context_{time.strftime('%Y%m%d_%H%M%S', time.localtime(now))}_{self._decision_context_seq:03d}.jsonl"

    def _decision_context_row(
        self,
        row_type: str,
        *,
        snapshot: Optional[dict[str, Any]] = None,
        reasons: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "record_type": row_type,
            "capture_id": str((self._decision_context_active or {}).get("capture_id", "")),
        }
        if snapshot is not None:
            row.update({k: v for k, v in snapshot.items() if k != "record_type"})
        if reasons:
            row["capture_reasons"] = list(reasons)
        return row

    def _start_decision_context_capture(self, *, now: float, reasons: list[str]) -> None:
        filename = self._decision_context_capture_name(now)
        capture_id = filename.rsplit(".", 1)[0]
        self._decision_context_active = {
            "capture_id": capture_id,
            "filename": filename,
            "end_ts": now + self._decision_context_post_s,
            "last_written_ts": 0.0,
        }
        self._append_decision_context_rows([
            {
                "record_type": "capture_meta",
                "capture_id": capture_id,
                "ts": now,
                "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
                "capture_reasons": list(reasons),
                "pre_s": self._decision_context_pre_s,
                "post_s": self._decision_context_post_s,
                "ring_s": self._decision_context_ring_s,
            }
        ])

    def _write_decision_context_trigger(self, *, snapshot: dict[str, Any], reasons: list[str]) -> None:
        self._append_decision_context_rows([
            self._decision_context_row("trigger", snapshot=snapshot, reasons=reasons),
        ])

    def _write_decision_context_backfill(self, *, now: float) -> None:
        if self._decision_context_active is None:
            return
        cutoff = now - self._decision_context_pre_s
        rows: list[dict[str, Any]] = []
        last_written = 0.0
        for snap, _retained_bytes in self._decision_context_ring:
            ts = float(snap.get("ts", 0.0) or 0.0)
            if ts < cutoff:
                continue
            rows.append(self._decision_context_row("tick", snapshot=snap))
            last_written = max(last_written, ts)
        if rows:
            self._append_decision_context_rows(rows)
            self._decision_context_active["last_written_ts"] = last_written

    def _append_decision_context_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows or self._decision_context_active is None:
            return
        path = self.output_dir / str(self._decision_context_active["filename"])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
        except Exception as exc:
            log.warning("Decision-context write failed for %s: %s", path, exc)

    def log_action(self, action: LeverAction, result: Optional[LeverResult],
                   suppressed: bool = False) -> None:
        """Log a lever action and its result (or suppression)."""
        action_key = action_transaction_key_from_action(action)
        correlation = (
            {
                "action_root_id": action_key.root_id,
                "command_id": action_key.command_id,
                "proof_id": action_key.proof_id,
                "action_transaction": action_key.as_dict(),
            }
            if action_key is not None
            else {}
        )
        if suppressed:
            self.log_event("action_suppressed",
                           lever=action.lever.name,
                           params=action.params,
                           reason=action.reason,
                           intent=action.intent,
                           objective_id=action.objective_id,
                           **correlation,
                           observe_only=True)
        else:
            details = dict(getattr(result, "details", {}) or {})
            self.log_event("action",
                           lever=action.lever.name,
                           params=action.params,
                           reason=action.reason,
                           intent=action.intent,
                           objective_id=action.objective_id,
                           **correlation,
                           success=result.success,
                           error=result.error,
                           radio_drop=result.radio_drop_detected,
                           details=details)

    def log_startup(self, constraints: dict) -> None:
        """Log startup checklist results."""
        self.log_event("startup", constraints=constraints)

    def _write_raw_session_header(self, tag: str) -> None:
        self.log_raw(
            " ".join(
                [
                    f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime())} [raw_session_start]",
                    f"tag={tag}",
                    f"pid={os.getpid()}",
                    f"output_dir={self.output_dir}",
                    "surface=forensic",
                ]
            )
        )

    def close(self) -> None:
        """Flush and close log files."""
        if self._event_file:
            self._event_file.close()
            self._event_file = None
        if self._raw_file:
            self._raw_file.close()
            self._raw_file = None
        if self._telemetry_history_file:
            self._telemetry_history_file.close()
            self._telemetry_history_file = None
        if self._production_telemetry_file:
            self._production_telemetry_file.close()
            self._production_telemetry_file = None
        if self._trace_matrix_file:
            self._trace_matrix_file.close()
            self._trace_matrix_file = None
        if self._compact_forensic_writer:
            self._compact_forensic_writer.close()
            self._compact_forensic_writer = None
        if self._rollup:
            self._rollup.close()
        log.info("Intelligence evidence closed. %d cycles recorded.", self._cycle_count)

    def set_raw_log_enabled(self, enabled: bool) -> None:
        """Enable or disable raw_*.log writes.

        When disabled, log_raw() is a no-op. Call with enabled=False for
        portable/headless/observe-only runs where raw log is redundant.
        Default is enabled=True (existing forensic behavior preserved).
        """
        self._raw_enabled = bool(enabled)

    def bind_config_generation(
        self,
        config: Any,
        *,
        generation_id: str,
        config_fingerprint: str,
    ) -> None:
        """Atomically apply the logger-owned part of a config generation."""

        enabled = bool(getattr(config, "telemetry_rollup_enabled", True))
        with self._config_generation_lock:
            if enabled and self._rollup is None:
                self._rollup = HourlyRollup(self.output_dir)
            elif not enabled and self._rollup is not None:
                self._rollup.close()
                self._rollup = None
            self._config_generation_id = str(generation_id or "")
            self._config_fingerprint = str(config_fingerprint or "")

    def log_raw(self, text: str) -> None:
        """Append raw text to the raw log file (forensic/crash use only).

        raw_*.log is a plain-text echo of selected high-value events and
        heartbeat summaries. It exists for post-mortem analysis when JSONL
        parsing is inconvenient (e.g. manual grep, crash inspection).

        This is NOT a primary data surface. Do not introduce consumers that
        depend on it as a canonical source. If raw_enabled=False, writes are
        silently dropped — callers do not need to guard this externally.
        """
        if not self._raw_enabled:
            return
        if self._raw_file:
            try:
                self._raw_file.write(text.rstrip() + "\n")
                self._raw_file.flush()
            except Exception as exc:
                self._record_write_failure("raw write", exc)

    @property
    def event_path(self) -> Path:
        return self._event_path


def intelligence_evidence_recorder_if_attached(
    owner: object,
) -> IntelligenceEvidenceRecorder | None:
    controller = getattr(owner, "controller", None)
    intelligence = getattr(controller, "intelligence", None)
    recorder = getattr(intelligence, "evidence", None)
    if recorder is None:
        return None
    if not isinstance(recorder, IntelligenceEvidenceRecorder):
        raise TypeError("intelligence_evidence_recorder_contract_invalid")
    return recorder


def require_intelligence_evidence_recorder(
    owner: object,
) -> IntelligenceEvidenceRecorder:
    recorder = intelligence_evidence_recorder_if_attached(owner)
    if recorder is None:
        raise RuntimeError("intelligence_evidence_recorder_not_attached")
    return recorder


__all__ = (
    "IntelligenceEvidenceRecorder",
    "intelligence_evidence_recorder_if_attached",
    "require_intelligence_evidence_recorder",
)
