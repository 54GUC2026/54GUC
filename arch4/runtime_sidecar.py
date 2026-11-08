"""
Redundant atomic JSON sidecar helpers used by runtime health/status files.

These files are operational truth surfaces. They should not depend on the
larger telemetry/event logger staying healthy, and readers should never see
half-written JSON. Critical truth files also keep a shadow copy plus a bounded
30-minute ring so readers can recover from transient missing/corrupt primaries.

Runtime Health Sidecar Schema
=============================

runtime_health.json owns runtime health. NGUI page truth is an output-only
diagnostic log and is never consumed here.

Common fields (present in both):
  - pid: int — Process ID of the writer
  - instance_id: str — Runtime instance identifier (env var)
  - phase: str — Raw runtime phase name
  - ts: float — Unix timestamp of last update
  - iso: str — ISO formatted timestamp
  - last_progress_ts: float — Last real progress milestone timestamp
  - last_error: str — Last error message (empty if ok)
  - observe_only: bool — Whether in observe-only mode

Unified summary fields (added by summarize_runtime_health_status):
  - status_phase: str — Normalized phase: starting, running, ready,
                        connection_lost, warning, error, shutdown, unknown
  - raw_phase: str — Original phase value before normalization
  - cycle_in_progress: bool — True if currently processing a runtime cycle
  - has_completed_cycle: bool — True if at least one cycle completed

Runner-specific fields (runtime_health.json only):
  - modem_backend: str — Backend name from config
  - check_only: bool — Whether in --check mode
  - cycle_count: int — Current cycle number
  - consecutive_cycle_errors: int — Error counter for restarts
  - last_cycle_started_ts: float — When current/last cycle started
  - last_cycle_completed_ts: float — When last cycle finished

Phase Mapping
-------------
Runner phases → status_phase:
  init, startup_begin, startup_ok → starting
  cycle_ok, cycle_recoverable_fault → running
  cycle_started → running (if has_completed_cycle) else starting
  check_complete → ready
  connection_lost → connection_lost
  fatal_cycle, fatal_runner, fatal_startup → error
  shutdown → shutdown

"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from arch4.atomic_io import atomic_write_text
from arch4.brain.decision_receipt import (
    compact_brain_current_state_payload,
    promote_compact_decision_receipt,
)
from arch4.interfaces import (
    ARCH_CURRENT_STATE_FILENAME,
    ARCH_RUNTIME_HEALTH_FILENAME,
    ARCH_SIGNAL_STATE_FILENAME,
    canonical_state_name,
)
from arch4.runtime_support_tier import (
    SUPPORT_AUTHORITY_FIELDS,
    TIER_AUTOMATION_ELIGIBLE,
    TIER_MONITOR_SAFE,
    TIER_OBSERVED,
    support_tier_write_allowed,
    support_tier_from_payload,
)
from arch4.canonical_delta_display import build_delta_display_surface
from arch4.canonical_identity_display import build_canonical_identity_display
from arch4.portable_state_import import beta_soak_status
from arch4.smart_loop.strategy_register import (
    strategy_authority_projection,
    strategy_candidate_projection,
    strategy_tsar_plan_projection,
)
from arch4.runtime_operator_contract import (
    OPERATOR_FIELD_SPECS,
    public_field_owner_contract,
)
from arch4.runtime_registers import (
    REGISTER_CA,
    REGISTER_HEALTH,
    REGISTER_IDENTITY,
    REGISTER_OPERATIONAL_STATUS,
    REGISTER_LATENCY,
    REGISTER_QUALITY,
    REGISTER_RADIO,
    REGISTER_RUNTIME_CLOCK,
    REGISTER_TRAFFIC,
    RADIO_IDENTITY_KEYS,
    runtime_register_public_field_projection,
    runtime_sample_is_stale,
    runtime_sample_stale_after_s,
)
from arch4.runtime_identity_card import resolve_resolved_identity_card
from arch4.runtime_sidecar_identity import (
    build_expected_sidecar_identity as build_expected_sidecar_identity,
    extract_sidecar_identity as extract_sidecar_identity,
    sidecar_identity_matches,
)
from arch4.runtime_sidecar_health import (
    JSON_SIDECAR_LAST_TEXT_BY_PATH,
    clear_json_sidecar_write_failure,
    format_runtime_iso_utc,
    json_sidecar_failure_key,
    json_sidecar_write_health as json_sidecar_write_health,
    last_json_sidecar_write_failure as last_json_sidecar_write_failure,
    record_json_sidecar_primary_success,
    record_json_sidecar_unchanged_skip,
    record_json_sidecar_write_attempt,
    record_json_sidecar_write_failure,
)


log = logging.getLogger("arch4.runtime_sidecar")


CRITICAL_TRUTH_FILENAMES = {
    ARCH_RUNTIME_HEALTH_FILENAME,
    ARCH_CURRENT_STATE_FILENAME,
    "capabilities.json",
}
TRUTH_RING_FILENAMES = {
    ARCH_RUNTIME_HEALTH_FILENAME,
    ARCH_CURRENT_STATE_FILENAME,
    "capabilities.json",
}
CURRENT_STATE_SOURCE_FILENAMES = {
    ARCH_RUNTIME_HEALTH_FILENAME,
    "capabilities.json",
    ARCH_SIGNAL_STATE_FILENAME,
}

_LIVE_RUNTIME_REGISTER_NAMES = frozenset(
    {
        REGISTER_RADIO,
        REGISTER_TRAFFIC,
        REGISTER_LATENCY,
        REGISTER_QUALITY,
        REGISTER_CA,
    }
)
CURRENT_STATE_TRAFFIC_INT_FIELDS = (
    "traffic_day_download_bytes",
    "traffic_day_upload_bytes",
    "traffic_day_used_bytes",
    "traffic_month_download_bytes",
    "traffic_month_upload_bytes",
    "traffic_total_download_bytes",
    "traffic_total_upload_bytes",
    "traffic_session_download_bytes",
    "traffic_session_upload_bytes",
    "traffic_device_download_counter_bytes",
    "traffic_device_upload_counter_bytes",
    "traffic_quota_start_day",
    "traffic_quota_limit_bytes",
)
CURRENT_STATE_TRAFFIC_FLOAT_FIELDS = (
    "volume_used_today_gb",
    "volume_remaining_today_gb",
    "volume_allowance_gb",
    "traffic_session_duration_s",
    "traffic_volume_poll_interval_s",
    "traffic_volume_last_sample_ts",
    "traffic_volume_next_sample_ts",
    "traffic_volume_age_s",
    "traffic_quota_month_threshold_pct",
    "traffic_quota_day_threshold_pct",
)
CURRENT_STATE_TRAFFIC_TEXT_FIELDS = (
    "traffic_accounting_source",
    "traffic_accounting_status",
    "traffic_accounting_month_key",
    "traffic_accounting_day_key",
    "traffic_quota_limit_label",
)
TRUTH_SHADOW_DIRNAME = ".truth_shadow"
TRUTH_RING_DIRNAME = ".truth_ring"
TRUTH_RING_SLOTS = 30

STALE_OPERATOR_PRESERVED_FIELDS = frozenset(
    {
        "state",
        "provider_name",
        "provider_plmn",
        "serving_band",
        "serving_band_signature",
        "primary_band",
        "configured_band_pool",
        "configured_band_pool_signature",
        "configured_band_pool_source",
        "configured_ca_pool_display",
        "pci",
        "cell_id",
        "enb_id",
        "earfcn_dl",
        "earfcn",
        "tac",
        "congestion_supported",
        "congestion_support_reason",
    }
)

STALE_OPERATOR_VALID_FALSE_FIELDS = frozenset(
    {
        "radio_connected",
        "ca_active",
        "nsa_active",
        "sa_active",
        "cart_ld_valid",
        "cart_ld_display_valid",
        "cast_ld_valid",
        "ca_realtime_ld_valid",
        "ca_stat_ld_valid",
        "ri_valid",
        "ri_stability_valid",
        "rtt_delta_valid",
        "rtt_quiet_held",
        "rtt_delta_display_valid",
        "rtt_delta_display_held",
        "rtt_dl_jitter_valid",
        "rsrq_baseline_valid",
        "rsrq_baseline_display_valid",
        "rsrq_baseline_display_held",
        "rsrq_gap_valid",
        "rsrq_delta_display_valid",
        "rsrq_delta_display_held",
        "cq_cell_valid",
        "bq_cell_valid",
        "congestion_pct_valid",
        "congestion_display_valid",
        "congestion_flags_valid",
        "congestion_bad_now",
        "congestion_pressure_high",
        "congestion_pressure_moderate",
        "cqi_mcs_gap_valid",
        "scheduler_zero_authoritative",
        "scheduler_stream1_available",
    }
)

STALE_OPERATOR_TEXT_OVERRIDES = {
    "radio_state_text": "Waiting for fresh modem data",
    "ca_state_text": "Waiting for fresh modem data",
    "network_type": "",
    "network_type_ex": "",
    "ca_source": "stale_sample",
    "ri_source": "stale_sample",
    "cq_cell_source": "stale_sample",
    "bq_cell_source": "stale_sample",
    "rtt_delta_block_reason": "live_register_stale",
    "congestion_internal_bucket": "stale",
    "congestion_bucket": "stale",
    "congestion_state_bucket": "stale",
    "congestion_source": "stale_sample",
    "congestion_fast_mode": "",
    "congestion_hold_mode": "",
    "congestion_rsrq_reason": "live_register_stale",
    "congestion_trend_display": "",
    "congestion_kind_hint": "",
}

_INTERNAL_DISPLAY_VALUE_KEYS = frozenset(
    {
        "cq_cell_display",
        "cq_cell_display_valid",
        "cq_cell_display_held",
        "scheduler_ri_valid",
        "scheduler_ri_source",
    }
)


def _internal_display_value_key(key: Any) -> bool:
    text = str(key or "")
    return text in _INTERNAL_DISPLAY_VALUE_KEYS or (
        text.startswith("scheduler_") and text.endswith("_display")
    )


def _strip_internal_display_values(name: str, register: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(register)
    value = out.get("value")
    if not isinstance(value, Mapping):
        return out
    cleaned = {
        key: item
        for key, item in value.items()
        if not _internal_display_value_key(key)
    }
    if len(cleaned) != len(value):
        out["value"] = cleaned
    return out

OPERATOR_EXPLICIT_EMPTY_FIELDS = frozenset(
    {
        "congestion_support_reason",
    }
)


def _resolve_identity_card(*candidates: Any) -> dict[str, Any]:
    return resolve_resolved_identity_card(*candidates)


def _compact_policy_overlay_surface(payload: Any) -> dict[str, Any]:
    """Keep current_state policy overlay useful without duplicating full policy maps."""
    if not isinstance(payload, dict) or not payload:
        return {}
    out: dict[str, Any] = {}
    for key in (
        "active",
        "overlay_id",
        "version",
        "path",
        "digest",
        "applied_ts",
        "changed_keys",
        "category_counts",
        "operator_messages",
        "last_error",
    ):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            out[key] = value

    precedence = payload.get("precedence") if isinstance(payload.get("precedence"), dict) else {}
    if precedence:
        field_sources = precedence.get("field_sources") if isinstance(precedence.get("field_sources"), dict) else {}
        source_counts: dict[str, int] = {}
        for row in field_sources.values():
            if not isinstance(row, dict):
                continue
            source = str(row.get("source", "") or "unknown")
            source_counts[source] = int(source_counts.get(source, 0)) + 1
        compact_precedence = {
            "model": str(precedence.get("model", "") or ""),
            "runtime_influence": str(precedence.get("runtime_influence", "") or ""),
            "overlay_changed_keys": list(precedence.get("overlay_changed_keys", []) or []),
            "operator_messages": dict(precedence.get("operator_messages", {}) or {}),
            "field_source_counts": source_counts,
            "field_sources_omitted": True,
        }
        out["precedence"] = compact_precedence

    history = payload.get("change_history") if isinstance(payload.get("change_history"), list) else []
    if history:
        compact_history = []
        for raw in history[-5:]:
            if not isinstance(raw, dict):
                continue
            compact_history.append({
                key: raw.get(key)
                for key in (
                    "kind",
                    "ts",
                    "overlay_id",
                    "version",
                    "digest",
                    "changed_keys",
                    "operator_message_keys",
                    "error",
                )
                if raw.get(key) not in (None, "", [], {})
            })
        if compact_history:
            out["change_history"] = compact_history

    if "precedence" in out or "change_history" in out:
        out["sidecar_compaction"] = {
            "surface": "current_state",
            "detail_owner": "current_state.policy_overlay",
        }
    return out


def _current_state_owner_contract() -> dict[str, Any]:
    """Publish the ownership map for the assembled current-state surface.

    `current_state.json` is an export envelope. It may assemble owner surfaces,
    but it must not become a hidden second implementation of those surfaces.
    This contract is intentionally machine-readable so tests and diagnostics can
    catch new duplicate-truth fields as they are added.
    """

    return {
        "contract_version": 1,
        "surface": ARCH_CURRENT_STATE_FILENAME,
        "role": "export_envelope",
        "assembly_role": "assembled_owner_surface",
        "rule": "one_public_field_one_owner",
        "allows_fallback_truth_chains": False,
        "owners": {
            "runtime": {
                "writer": ARCH_RUNTIME_HEALTH_FILENAME,
                "owns": [
                    "runtime_mode",
                    "modem_backend",
                ],
            },
            "capabilities": {
                "writer": "capabilities.json",
                "owns": [
                    "capability_contract",
                    "runtime_identity",
                    "runtime_identity_display",
                    "resolved_identity_card",
                    "support_state",
                    "support_tier",
                    "support_tier_label",
                    "write_authority",
                    "write_block_reason",
                ],
            },
            "signals": {
                "writer": ARCH_SIGNAL_STATE_FILENAME,
                "owns": ["signals"],
            },
            "runtime_clock": {
                "writer": "core_runtime_snapshot.runtime_clock_register",
                "owns": [
                    "runtime_clock",
                    "runtime_clock_internal_service_status",
                ],
            },
            "engine": {
                "writer": ARCH_CURRENT_STATE_FILENAME,
                "owns": [
                    "operator",
                    "learning",
                    "calibration_state",
                    "calibration_authority",
                    "calibration_workflow",
                    "calibration_surface",
                    "calibration_learning",
                    "learning_calibration_handoff",
                    "brain_vectors",
                    "brain_bouncer",
                    "brain_referee",
                    "policy_overlay",
                    "bouncer_intent",
                    "bouncer_outcome",
                    "referee_outcome",
                ],
            },
        },
        "assembled_fields": {
            "freshness": "source_file_metadata",
            "authority": "writer_manifest",
            "precedence": "field_source_manifest",
            "explainability": "projection_from_owner_surfaces",
            "provider": "compatibility_alias_for_provider_name",
            "plmn": "compatibility_alias_for_provider_plmn",
        },
        "public_field_ownership": public_field_owner_contract(),
    }


_CURRENT_STATE_EXPLICIT_ENGINE_DIAGNOSTIC_FIELDS = frozenset(
    {
        "runtime_clock",
        "runtime_clock_internal_service_status",
        "runtime_sidecar_write_queue",
        "json_sidecar_write_health",
    }
)


def _diagnostic_observed_ts(value: Any) -> float:
    if not isinstance(value, Mapping):
        return 0.0
    for key in ("observed_ts", "last_run_ts", "last_finish_ts", "last_start_ts", "ts"):
        try:
            ts = float(value.get(key) or 0.0)
        except Exception:
            ts = 0.0
        if ts > 0.0:
            return ts
    newest = 0.0
    jobs = value.get("jobs")
    if isinstance(jobs, Mapping):
        for row in jobs.values():
            if not isinstance(row, Mapping):
                continue
            for key in ("last_finish_ts", "last_start_ts", "last_due_ts"):
                try:
                    newest = max(newest, float(row.get(key) or 0.0))
                except Exception:
                    continue
    lanes = value.get("lanes")
    if isinstance(lanes, Mapping):
        for row in lanes.values():
            if not isinstance(row, Mapping):
                continue
            for key in ("last_finish_ts", "last_start_ts"):
                try:
                    newest = max(newest, float(row.get(key) or 0.0))
                except Exception:
                    continue
    return newest


def _diagnostic_is_fresh(value: Any, *, now: float, max_age_s: float = 2.5) -> bool:
    ts = _diagnostic_observed_ts(value)
    return ts > 0.0 and max(0.0, float(now) - ts) <= max(0.1, float(max_age_s))


def _strip_non_owner_current_state_diagnostics(payload: dict[str, Any], *, now: float) -> dict[str, Any]:
    """Remove diagnostic fields unless a fresh owner-stamped export supplied them."""

    if not isinstance(payload, dict):
        return {}
    out = dict(payload)
    for key in _CURRENT_STATE_EXPLICIT_ENGINE_DIAGNOSTIC_FIELDS:
        value = out.get(key)
        if not _diagnostic_is_fresh(value, now=now):
            out.pop(key, None)
    return out


def _backend_family_surface(
    *,
    modem_backend: Any = "",
    backend_family: Any = "",
) -> dict[str, str]:
    """Project backend family identity without owning modem behavior."""
    backend = str(modem_backend or "").strip().lower()
    family = str(backend_family or "").strip().lower()
    try:
        from arch4.operator_profiles import family_label, family_support_text, infer_backend_family

        if backend and not family:
            family = str(infer_backend_family(backend) or "").strip().lower()
        label = str(family_label(family) or "") if family else ""
        support = str(family_support_text(family) or "") if family else ""
    except Exception:
        label = family
        support = ""
    out: dict[str, str] = {}
    if family:
        out["backend_family"] = family
    if label:
        out["backend_family_label"] = label
    if support:
        out["backend_family_support_text"] = support
    return out


TRUTH_RING_SLOT_SECONDS = 60
RUNTIME_SURFACE_MAX_AGE_S = 8.0
CURRENT_STATE_TELEMETRY_INPUT_MAX_AGE_S = 10.0
_TERMINAL_RUNTIME_PHASES = {"shutdown", "stale_session", "fatal_runner"}


def _runtime_path_key(path: Path | str) -> str:
    try:
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        return str(target)
    except Exception:
        return str(path)


def _dedupe_targets(targets: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for target in targets:
        key = _runtime_path_key(target)
        if key in seen:
            continue
        seen.add(key)
        out.append(target)
    return out


def _is_critical_truth_path(path: Path) -> bool:
    return path.name in CRITICAL_TRUTH_FILENAMES


def _uses_truth_ring(path: Path) -> bool:
    return path.name in TRUTH_RING_FILENAMES


def _is_current_state_source_path(path: Path) -> bool:
    return path.name in CURRENT_STATE_SOURCE_FILENAMES


def _brain_decision_receipt_from_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    brain = payload.get("brain") if isinstance(payload.get("brain"), Mapping) else {}
    trace_id = str(payload.get("decision_trace_id", "") or "")
    if trace_id:
        return compact_brain_current_state_payload(
            {
                **(dict(brain) if isinstance(brain, Mapping) else {}),
                "decision_trace": {
                    "decision_trace_id": trace_id,
                    "source": str(payload.get("decision_trace_source", "") or ""),
                    "final": str(payload.get("decision_trace_final", "") or ""),
                    "tsar_verdict": str(payload.get("decision_trace_verdict", "") or ""),
                    "primary_blocker_gate": str(payload.get("decision_trace_gate", "") or ""),
                    "primary_blocker_reason": str(payload.get("decision_trace_reason", "") or ""),
                },
            }
        )
    return compact_brain_current_state_payload(brain)


def _brain_vectors_with_decision_owner(
    brain_vectors: Mapping[str, Any],
    brain_decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Project Brain vector diagnostics with decision fields owned by receipt."""

    vectors = dict(brain_vectors) if isinstance(brain_vectors, Mapping) else {}
    if not vectors:
        return {}
    trace = brain_decision.get("decision_trace") if isinstance(brain_decision.get("decision_trace"), Mapping) else {}
    tsar = vectors.get("brain_tsar") if isinstance(vectors.get("brain_tsar"), Mapping) else {}
    if not trace and not tsar:
        return vectors
    current_tsar = dict(tsar)
    current_tsar["decision_verdict"] = str(trace.get("tsar_verdict") or "")
    current_tsar["decision_reason"] = str(trace.get("primary_blocker_reason") or "")
    current_tsar["decision_summary"] = str(trace.get("tsar_summary") or "")
    current_tsar["decision_trace_id"] = str(trace.get("decision_trace_id") or "")
    current_tsar["decision_trace_source"] = str(trace.get("source") or "")
    vectors["brain_tsar"] = current_tsar
    return vectors


def _shadow_path(path: Path) -> Path:
    return path.parent / TRUTH_SHADOW_DIRNAME / path.name


def _ring_slot_path(path: Path, *, ts: float) -> Path:
    slot = int(max(0.0, float(ts)) // TRUTH_RING_SLOT_SECONDS) % TRUTH_RING_SLOTS
    return path.parent / TRUTH_RING_DIRNAME / path.name / f"{slot:02d}.json"


_ATOMIC_REPLACE_ATTEMPTS = 4
_ATOMIC_REPLACE_RETRY_BASE_S = 0.02
_SIDECAR_WRITE_ATTEMPTS = 3
_SIDECAR_WRITE_RETRY_BASE_S = 0.03
_JSON_SIDECAR_LAST_TEXT_BY_PATH = JSON_SIDECAR_LAST_TEXT_BY_PATH


def _write_text_atomically(path: Path, text: str) -> None:
    atomic_write_text(
        path,
        text,
        replace_attempts=_ATOMIC_REPLACE_ATTEMPTS,
        retry_base_s=_ATOMIC_REPLACE_RETRY_BASE_S,
        logger=log,
    )


def _json_sidecar_failure_key(path: Path) -> str:
    return json_sidecar_failure_key(path)


def _record_json_sidecar_write_failure(
    path: Path,
    exc: Exception,
    *,
    attempts: int,
    phase: str,
) -> None:
    record_json_sidecar_write_failure(path, exc, attempts=attempts, phase=phase)


def _clear_json_sidecar_write_failure(path: Path) -> None:
    clear_json_sidecar_write_failure(path)


def _record_json_sidecar_primary_success(path: Path) -> None:
    record_json_sidecar_primary_success(path)


def _can_skip_unchanged_json_sidecar(path: Path) -> bool:
    """Return True when content-cache skip is safe for this sidecar.

    Critical truth and current-state source files intentionally remain fresh on
    every write. Secondary exports may skip identical payload text because no
    reader should use their mtime as operational liveness.
    """
    return not (_is_critical_truth_path(path) or _is_current_state_source_path(path))


def _write_text_atomically_with_retries(path: Path, text: str, *, phase: str = "primary") -> None:
    attempts = max(1, int(_SIDECAR_WRITE_ATTEMPTS or 1))
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            _write_text_atomically(path, text)
            _clear_json_sidecar_write_failure(path)
            return
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts:
                _record_json_sidecar_write_failure(path, exc, attempts=attempt, phase=phase)
                raise
            time.sleep(float(_SIDECAR_WRITE_RETRY_BASE_S) * float(attempt))
    if last_exc is not None:
        _record_json_sidecar_write_failure(path, last_exc, attempts=attempts, phase=phase)
        raise last_exc


def _payload_ts(payload: Any) -> float:
    if isinstance(payload, dict):
        try:
            return float(payload.get("ts") or payload.get("last_progress_ts") or time.time())
        except Exception:
            return float(time.time())
    return float(time.time())


def _json_sidecar_boundary_payload(value: Any, *, _seen: set[int] | None = None) -> Any:
    """Return the only legal JSON payload shape for sidecar output.

    Sidecar files are a process boundary. Runtime objects may reach this
    boundary only through explicit value conversion; unknown arbitrary objects
    still fail so new implicit sidecar surfaces cannot be introduced silently.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)

    seen = _seen if _seen is not None else set()
    obj_id = id(value)
    if obj_id in seen:
        raise TypeError(f"cycle detected while serializing {type(value).__name__}")

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        seen.add(obj_id)
        try:
            return {
                str(field.name): _json_sidecar_boundary_payload(
                    getattr(value, field.name),
                    _seen=seen,
                )
                for field in dataclasses.fields(value)
            }
        finally:
            seen.discard(obj_id)

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        seen.add(obj_id)
        try:
            return _json_sidecar_boundary_payload(to_dict(), _seen=seen)
        finally:
            seen.discard(obj_id)

    if isinstance(value, Mapping):
        seen.add(obj_id)
        try:
            return {
                str(key): _json_sidecar_boundary_payload(item, _seen=seen)
                for key, item in value.items()
            }
        finally:
            seen.discard(obj_id)

    if isinstance(value, (list, tuple, set, frozenset)):
        seen.add(obj_id)
        try:
            return [_json_sidecar_boundary_payload(item, _seen=seen) for item in value]
        finally:
            seen.discard(obj_id)

    raise TypeError(f"Object of type {type(value).__name__} is not JSON sidecar serializable")


def _export_envelope_order(payload: Any) -> tuple[str, int] | None:
    if not isinstance(payload, dict):
        return None
    envelope = payload.get("export_envelope")
    if not isinstance(envelope, dict):
        return None
    writer_instance_id = str(envelope.get("writer_instance_id") or "").strip()
    if not writer_instance_id:
        return None
    try:
        enqueue_seq = int(envelope.get("enqueue_seq") or 0)
    except Exception:
        enqueue_seq = 0
    if enqueue_seq <= 0:
        return None
    return writer_instance_id, enqueue_seq


def _newest_same_writer_export_candidate(
    candidates: list[tuple[str, Path, Any]],
) -> tuple[str, Path, Any] | None:
    """Choose by export-envelope order only inside one writer instance.

    File mtime and payload ``ts`` can move backward across process restarts or
    clock adjustments. Queue ``enqueue_seq`` is meaningful only when the
    writer instance id matches, so this helper refuses to compare unrelated
    writers and lets the normal candidate precedence stand.
    """
    by_writer: dict[str, list[tuple[int, str, Path, Any]]] = {}
    for source, path, payload in candidates:
        order = _export_envelope_order(payload)
        if order is None:
            continue
        writer_instance_id, enqueue_seq = order
        by_writer.setdefault(writer_instance_id, []).append((enqueue_seq, source, path, payload))
    for rows in by_writer.values():
        if len(rows) < 2:
            continue
        enqueue_seq, source, path, payload = max(rows, key=lambda row: row[0])
        return source, path, payload
    return None


def _truth_payload_rejection(path: Path, payload: Any) -> str:
    if not (_is_current_state_source_path(path) or _is_critical_truth_path(path)):
        return ""
    if not isinstance(payload, dict):
        return f"{path.name}: refused non-object truth payload"
    if not payload:
        return f"{path.name}: refused empty truth payload"
    return ""


def _value_present(value: Any, *, reject_bool: bool = False) -> bool:
    if value in (None, "", [], {}):
        return False
    if isinstance(value, bool):
        return not reject_bool
    if isinstance(value, str) and value.strip().lower() in {
        "",
        "-",
        "--",
        "---",
        "n/a",
        "na",
        "none",
        "null",
        "unknown",
        "miss",
    }:
        return False
    return True


def _first_present(*values: Any, reject_bool: bool = False) -> Any:
    for value in values:
        if _value_present(value, reject_bool=reject_bool):
            return value
    return None


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def sidecar_last_ts(snapshot: Any) -> float:
    """Extract the freshest liveness/progress timestamp from a sidecar snapshot.

    Freshness accepts runtime progress, completed-sample, and publication
    timestamps. NGUI page truth is never a runtime freshness input.
    """
    if not isinstance(snapshot, dict):
        return 0.0
    newest = 0.0
    for key in ("last_progress_ts", "last_cycle_completed_ts", "last_cycle_ts", "ts"):
        try:
            value = float(snapshot.get(key) or 0.0)
        except Exception:
            value = 0.0
        if value > newest:
            newest = value
    return newest


def _first_source_present(*candidates: tuple[str, Any]) -> tuple[Any, str]:
    for source, value in candidates:
        if _value_present(value):
            return value, str(source or "")
    return "", ""


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return False


def _field_zero_is_missing(field: str, value: Any) -> bool:
    del field, value
    # Canonical capability/unsupported metadata is the source of truth for
    # display suppression. A numeric scheduler zero is a legitimate live value
    # once the lane is confirmed, so runtime-side scrubbing must not erase it.
    return False


def _congestion_authority_present(
    *,
    pct: Any,
    internal_bucket: Any,
    bucket: Any,
    state_avg_pct: Any,
    state_bucket: Any,
    source: Any,
    kind_hint: Any,
) -> bool:
    if pct not in (None, ""):
        return True
    if str(internal_bucket or "").strip():
        return True
    if str(bucket or "").strip():
        return True
    if str(state_bucket or "").strip():
        return True
    if str(source or "").strip():
        return True
    if str(kind_hint or "").strip():
        return True
    try:
        return float(state_avg_pct or 0.0) > 0.0
    except Exception:
        return False


def _coerce_congestion_pct_valid(
    explicit_value: Any,
    *,
    pct: Any,
    internal_bucket: Any,
    bucket: Any,
    state_avg_pct: Any,
    state_bucket: Any,
    source: Any,
    kind_hint: Any,
) -> bool:
    if explicit_value not in (None, ""):
        return _safe_bool(explicit_value)
    return _congestion_authority_present(
        pct=pct,
        internal_bucket=internal_bucket,
        bucket=bucket,
        state_avg_pct=state_avg_pct,
        state_bucket=state_bucket,
        source=source,
        kind_hint=kind_hint,
    )


def _canonical_state_value(value: Any) -> str:
    return canonical_state_name(value)


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _band_signature_from_value(value: Any) -> str:
    band = _safe_int(value)
    return f"B{band}" if band > 0 else ""


def _extract_learning_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    brain = payload.get("brain")
    if not isinstance(brain, dict):
        return {}
    learning = brain.get("learning")
    if not isinstance(learning, dict):
        return {}
    return dict(learning)


def _extract_dict_payload(payload: Any, key: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    value = payload.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _promote_operator_surface_from_runtime_registers(
    operator_surface: Mapping[str, Any],
    runtime_registers: Mapping[str, Any],
) -> dict[str, Any]:
    """Promote trusted public runtime-register facts into the operator surface."""

    out = dict(operator_surface or {})
    registers = runtime_registers if isinstance(runtime_registers, Mapping) else {}
    promoted, promoted_sources = runtime_register_public_field_projection(registers)
    if not promoted:
        return out
    sources = dict(out.get("sources")) if isinstance(out.get("sources"), Mapping) else {}
    for key, value in promoted.items():
        out[key] = value
        source = promoted_sources.get(key)
        if source:
            sources[key] = source
    out["sources"] = sources
    return out


_BRAIN_VECTOR_CURRENT_STATE_DICTS = (
    "opportunity_inventory",
    "counterfactual_opportunity_audit",
    "smart_loop_authority",
    "decision_quality",
    "brain_learning_effect",
)


def _project_brain_vector_current_state_surface(
    summary_status: Mapping[str, Any],
    brain_vectors: Mapping[str, Any],
    summary_runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project SmartCore-owned Brain facts to the current-state adapter surface.

    Current-state export is not a learning-loop owner.  It may copy owner
    surfaces that Brain/SmartCore already published, but it must not fabricate a
    missing candidate publication or infer final learning-loop state from
    candidate/referee fragments.
    """

    runtime = summary_runtime if isinstance(summary_runtime, Mapping) else {}
    runtime_vectors = (
        runtime.get("brain_vectors") if isinstance(runtime.get("brain_vectors"), Mapping) else {}
    )
    status = summary_status if isinstance(summary_status, Mapping) else {}
    vectors = brain_vectors if isinstance(brain_vectors, Mapping) else {}
    out: dict[str, Any] = {}

    def _mapping(value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, Mapping) else {}

    def _strategy_surface(value: Any) -> dict[str, Any]:
        row = _mapping(value)
        if row.get("schema") != "smart_loop.closed_loop.v1":
            return {}
        if row.get("owner") != "arch4.smart_loop.closed_loop":
            return {}
        if bool(row.get("register_fault", False)):
            return {}
        return row

    strategy = (
        _strategy_surface(runtime.get("self_learning_strategy"))
        or _strategy_surface(runtime_vectors.get("self_learning_strategy"))
        or _strategy_surface(status.get("self_learning_strategy"))
        or _strategy_surface(vectors.get("self_learning_strategy"))
    )
    tsar_plan = strategy_tsar_plan_projection(strategy) if strategy else {}
    publication = strategy_candidate_projection(strategy) if strategy else {}
    authority = strategy_authority_projection(strategy) if strategy else {}
    counterfactual_audit = (
        _mapping(runtime.get("counterfactual_opportunity_audit"))
        or _mapping(runtime_vectors.get("counterfactual_opportunity_audit"))
        or _mapping(status.get("counterfactual_opportunity_audit"))
        or _mapping(vectors.get("counterfactual_opportunity_audit"))
    )
    strategy_evidence = _mapping(strategy.get("evidence"))
    strategy_inputs = _mapping(strategy.get("inputs"))
    strategy_self_learning_input = _mapping(strategy_inputs.get("self_learning"))

    def _copy_scalar(target: str, *candidates: Any) -> None:
        if target in out:
            return
        for candidate in candidates:
            if candidate not in (None, ""):
                out[target] = candidate
                return

    _copy_scalar(
        "smart_loop_status",
        strategy_evidence.get("self_learning_status"),
        strategy_self_learning_input.get("status"),
    )
    _copy_scalar(
        "smart_loop_reason",
        strategy_evidence.get("self_learning_reason"),
        strategy_self_learning_input.get("reason"),
    )
    _copy_scalar(
        "smart_loop_required_next_step",
        strategy_self_learning_input.get("required_next_step"),
    )
    _copy_scalar(
        "smart_loop_operationalization_gap",
        strategy_evidence.get("candidate_operationalization_gap"),
    )
    _copy_scalar(
        "smart_loop_generation_gap",
        strategy_evidence.get("candidate_generation_gap"),
    )
    strategy_outcome = str(strategy.get("outcome", "") or "")
    strategy_reason = str(strategy.get("reason", "") or "")
    candidate_generation_gap = bool(strategy_evidence.get("candidate_generation_gap", False))
    counterfactual_gap = candidate_generation_gap or strategy_outcome == "candidate_missing_counterfactual"
    if strategy:
        out["counterfactual_status"] = (
            "candidate_generation_gap" if counterfactual_gap else "strategy_no_counterfactual_gap"
        )
        out["counterfactual_reason"] = strategy_reason
        out["counterfactual_should_have_candidate"] = bool(counterfactual_gap)
        out["counterfactual_candidate_generation_failed"] = bool(counterfactual_gap)

    for key in _BRAIN_VECTOR_CURRENT_STATE_DICTS:
        # Current-state receives owner-promoted top-level SmartCore surfaces
        # from engine finalization.  Nested brain_vectors are retained as a
        # diagnostic bundle, but they must not overwrite a fresher owner
        # surface after a governor/blocker clears.
        value = (
            runtime.get(key)
            if isinstance(runtime.get(key), Mapping)
            else runtime_vectors.get(key)
            if isinstance(runtime_vectors.get(key), Mapping)
            else status.get(key)
            if isinstance(status.get(key), Mapping)
            else vectors.get(key)
        )
        if isinstance(value, Mapping) and value:
            out[key] = dict(value)
    if strategy:
        out["self_learning_strategy"] = strategy
        out["self_learning_strategy_outcome"] = str(strategy.get("outcome", "") or "")
        out["self_learning_strategy_reason"] = str(strategy.get("reason", "") or "")
        out["self_learning_strategy_required_next_step"] = str(
            strategy.get("required_next_step", "") or ""
        )
        out["self_learning_loop_closed"] = bool(strategy.get("learning_loop_closed", False))
        out["self_learning_learning_signal"] = bool(strategy.get("learning_signal", False))
        out["self_learning_diagnostic_signal"] = bool(strategy.get("diagnostic_signal", False))
    if publication:
        out["smart_loop_candidate_projection"] = publication
    if authority:
        out["smart_loop_authority"] = authority
    if tsar_plan:
        out["smart_loop_tsar_plan"] = tsar_plan
    if counterfactual_audit:
        out["counterfactual_opportunity_audit"] = counterfactual_audit
    return out


def _normalize_modem_live_lane_from_sample_facts(status: Mapping[str, Any]) -> dict[str, Any]:
    """Tie Modem Live diagnostic lane mirrors to the current live sample facts.

    ``current_state.json`` is an export envelope. It may preserve Modem Live
    lane details, but the lane's sample identity must come from the current
    Modem Live sample fields in the same envelope. A copied diagnostic lane
    with an older sample_count is stale metadata and cannot be published next
    to a newer sample_sequence.
    """

    out = dict(status or {})
    sequence = _safe_int(out.get("sample_sequence"))
    if sequence <= 0:
        return out

    existing = out.get("modem_live_fast_lane")
    lane = dict(existing) if isinstance(existing, Mapping) else {}
    lane["owner"] = "modem_live"
    lane["lane"] = str(lane.get("lane") or "fast_modem_sample")
    lane["source"] = "modem_live"
    lane["sample_count"] = sequence
    lane["sample_count_source"] = "live_register_sequence"

    sample_ts = _safe_float(out.get("sample_ts"))
    if sample_ts > 0.0:
        for key in ("last_start_ts", "last_finish_ts", "last_success_ts"):
            lane[key] = max(_safe_float(lane.get(key)), sample_ts)

    interval_s = _safe_float(out.get("modem_sample_interval_s"))
    if interval_s > 0.0:
        lane["period_s"] = round(interval_s, 3)

    backend_fast = out.get("backend_fast_read")
    if isinstance(backend_fast, Mapping):
        duration_ms = _safe_float(backend_fast.get("duration_ms"))
        if duration_ms > 0.0:
            lane["work_ms"] = round(duration_ms, 3)

    access_scheduler = lane.get("access_scheduler")
    if isinstance(access_scheduler, Mapping):
        scheduler = dict(access_scheduler)
        scheduler["sample_count_source"] = "live_register_sequence"
        lanes = scheduler.get("lanes")
        if isinstance(lanes, Mapping):
            lanes_map = dict(lanes)
            fast_live = dict(lanes_map.get("fast_live") or {})
            fast_live["run_count"] = max(_safe_int(fast_live.get("run_count")), sequence)
            fast_live["sample_count_source"] = "live_register_sequence"
            lanes_map["fast_live"] = fast_live
            scheduler["lanes"] = lanes_map
        lane["access_scheduler"] = scheduler

    out["modem_live_fast_lane"] = lane

    runtime_registers = out.get("runtime_registers")
    if isinstance(runtime_registers, Mapping):
        registers = dict(runtime_registers)
        health = registers.get("health")
        if isinstance(health, Mapping):
            health_map = dict(health)
            value = health_map.get("value")
            value_map = dict(value) if isinstance(value, Mapping) else {}
            value_map["modem_live_fast_lane"] = dict(lane)
            health_map["value"] = value_map
            health_map["valid"] = True
            health_map["stale"] = False
            health_map["trust"] = str(health_map.get("trust") or "derived")
            health_map["source_kind"] = str(health_map.get("source_kind") or "derived")
            health_map["validity"] = "valid"
            registers["health"] = health_map
            out["runtime_registers"] = registers

    return out


_CURRENT_STATE_SIGNAL_SAMPLE_IDENTITY_FIELDS = (
    "engine_tick_ts",
    "sample_sequence",
    "sample_source",
    "modem_live_io_quiet_active",
    "modem_live_io_quiet_remaining_s",
    "live_register_quiet_hold",
    "live_register_hold_reason",
    "modem_sample_interval_s",
    "modem_sample_expected_interval_s",
    "modem_sample_slo_exceeded",
    "register_intake_interval_s",
    "register_intake_expected_interval_s",
    "register_intake_slo_exceeded",
    "register_intake_sequence_gap",
    "engine_intake_mode",
    "engine_intake_drained_samples",
    "engine_intake_missing_samples",
    "engine_intake_history_depth",
    "engine_intake_missing_total",
)


def _promote_current_state_sample_identity_from_signals(
    status: Mapping[str, Any],
    signals: Mapping[str, Any],
    *,
    now_ts: float,
) -> dict[str, Any]:
    """Use the fresh signal sidecar's owner-stamped modem sample edge.

    When a sidecar refresh is triggered without a direct current-state export,
    ``current_state.json`` must not keep reusing its own previous sample fields.
    The signal sidecar is the fresh sidecar in that path, and engine tick outbox
    stamps it with the Modem Live sample identity.
    """

    out = dict(status or {})
    if not isinstance(signals, Mapping) or not signals:
        return out
    signal_sample_ts = _safe_float(signals.get("sample_ts"))
    if signal_sample_ts <= 0.0:
        return out
    status_sample_ts = _safe_float(out.get("sample_ts"))
    if status_sample_ts > 0.0 and signal_sample_ts < status_sample_ts:
        return out

    out["sample_ts"] = signal_sample_ts
    age_s = max(0.0, float(now_ts) - signal_sample_ts)
    out["sample_age_s"] = round(age_s, 3)
    stale_after_s = _runtime_status_register_stale_after_s(out)
    signal_fresh = (
        _safe_bool(signals.get("sample_fresh"))
        if "sample_fresh" in signals
        else age_s <= stale_after_s
    )
    out["sample_fresh"] = bool(signal_fresh and age_s <= stale_after_s)
    for key in _CURRENT_STATE_SIGNAL_SAMPLE_IDENTITY_FIELDS:
        if key not in signals:
            continue
        value = signals.get(key)
        if value in (None, "", [], {}):
            continue
        out[key] = value
    return out


def _capability_runtime_identity(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    value = payload.get("runtime_identity")
    return dict(value) if isinstance(value, dict) else {}


def _capability_live_capture(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    value = payload.get("live_capture")
    return dict(value) if isinstance(value, dict) else {}


def _live_capture_rf_fresh(capabilities: dict[str, Any], status: dict[str, Any]) -> bool:
    live_capture = _capability_live_capture(capabilities)
    if not live_capture:
        return False
    status_ts = sidecar_last_ts(status)
    capabilities_ts = sidecar_last_ts(capabilities)
    return bool(
        capabilities_ts > 0.0
        and (status_ts <= 0.0 or capabilities_ts >= (status_ts - 2.0))
    )


def _runtime_status_base_for_freshness(status: Mapping[str, Any]) -> Mapping[str, Any]:
    canonical = status.get("_canonical_status")
    if isinstance(canonical, Mapping):
        return canonical
    base = status.get("_base_status")
    if isinstance(base, Mapping):
        return base
    return status


def _runtime_status_sample_stale(status: Mapping[str, Any], *, now_ts: float) -> bool:
    if not isinstance(status, Mapping) or not status:
        return False
    base = _runtime_status_base_for_freshness(status)
    modem_health = status.get("modem_health")
    modem_health_map = modem_health if isinstance(modem_health, Mapping) else {}
    explicit_sources = (status, base, modem_health_map)
    explicit_stale = any(
        _safe_bool(source.get(flag))
        for source in explicit_sources
        for flag in (
            "live_register_stale",
            "live_register_cached_stale",
            "register_cached_stale",
            "cached_stale",
        )
    )
    if explicit_stale:
        return True
    return runtime_sample_is_stale(status, base, modem_health_map, now_ts=now_ts)


def _runtime_status_register_stale_after_s(status: Mapping[str, Any]) -> float:
    base = _runtime_status_base_for_freshness(status)
    modem_health = status.get("modem_health")
    modem_health_map = modem_health if isinstance(modem_health, Mapping) else {}
    try:
        value = runtime_sample_stale_after_s(status, base, modem_health_map)
    except Exception:
        value = 6.0
    return max(1.0, _safe_float(value) or 6.0)


def _stale_runtime_register_copy(register: Mapping[str, Any], *, reason: str, age_s: float | None) -> dict[str, Any]:
    out = dict(register)
    out["valid"] = False
    out["stale"] = True
    out["trust"] = "stale"
    out["validity"] = "stale"
    if not str(out.get("source_kind") or "").strip():
        out["source_kind"] = "unknown"
    out["stale_reason"] = reason
    if age_s is not None:
        out["stale_age_s"] = round(max(0.0, age_s), 3)
    return out


def _runtime_register_source_allows_live_sample_refresh(register: Mapping[str, Any]) -> bool:
    """Return whether a grouped register may be refreshed by the enclosing sample.

    Stable radio/traffic/quality values are allowed to remain unchanged for many
    live samples. Presentation liveness therefore follows the enclosing
    modem sample, not only the last value-change timestamp. Previous
    current-state exports are not owner publications and must not be revived by
    a newer envelope.
    """

    source = str(register.get("source") or "").strip().lower()
    if source.startswith("previous_current_state.") or source.startswith("current_state."):
        return False
    source_kind = str(register.get("source_kind") or "").strip().lower()
    return source_kind in {"live", "cached", "derived"}


def _runtime_register_sample_refresh_ts(
    status: Mapping[str, Any],
    *,
    now_ts: float,
) -> float:
    """Return the fresh sample timestamp that can refresh grouped registers."""

    if not isinstance(status, Mapping) or not status:
        return 0.0
    if _runtime_status_sample_stale(status, now_ts=now_ts):
        return 0.0
    if not _safe_bool(status.get("sample_fresh")):
        return 0.0
    base = _runtime_status_base_for_freshness(status)
    for value in (
        status.get("sample_ts"),
        base.get("sample_ts") if isinstance(base, Mapping) else None,
        status.get("ts"),
        base.get("ts") if isinstance(base, Mapping) else None,
        status.get("timestamp"),
        base.get("timestamp") if isinstance(base, Mapping) else None,
    ):
        sample_ts = _safe_float(value)
        if sample_ts > 0.0:
            return sample_ts
    return 0.0


def _refreshed_runtime_register_copy(register: Mapping[str, Any], *, sample_ts: float) -> dict[str, Any]:
    out = dict(register)
    out["valid"] = True
    out["stale"] = False
    source_kind = str(out.get("source_kind") or "").strip().lower() or "derived"
    out["source_kind"] = source_kind
    if not str(out.get("trust") or "").strip().lower() or str(out.get("trust") or "").strip().lower() == "stale":
        out["trust"] = "live" if source_kind == "live" else "derived"
    out["validity"] = "valid"
    out["updated_at"] = sample_ts
    for key in ("stale_reason", "stale_age_s"):
        out.pop(key, None)
    return out


def _sanitize_current_state_runtime_registers(
    status: Mapping[str, Any],
    *,
    now_ts: float,
) -> dict[str, Any]:
    """Refuse old register groups, but keep stable live owner groups valid.

    ``updated_at`` is a value-change timestamp inside the register bank. The
    Presentation uses the live modem sample as the liveness edge,
    so unchanged RF/RTT/quality facts must not be blanked while fresh samples
    continue to arrive.
    """

    out = dict(status or {})
    runtime_registers = out.get("runtime_registers")
    if not isinstance(runtime_registers, Mapping) or not runtime_registers:
        return out

    stale_after_s = _runtime_status_register_stale_after_s(out)
    sample_refresh_ts = _runtime_register_sample_refresh_ts(out, now_ts=now_ts)
    registers = dict(runtime_registers)
    changed = False

    for name in _LIVE_RUNTIME_REGISTER_NAMES:
        register = registers.get(name)
        if not isinstance(register, Mapping):
            continue
        cleaned_register = _strip_internal_display_values(name, register)
        if cleaned_register != dict(register):
            registers[name] = cleaned_register
            register = cleaned_register
            changed = True
        value = register.get("value")
        has_value = isinstance(value, Mapping) and bool(value)
        dirty_or_transition = (
            _safe_bool(register.get("dirty"))
            or _safe_bool(register.get("transition"))
            or str(register.get("validity") or "").strip().lower() == "dirty"
        )
        if (
            sample_refresh_ts > 0.0
            and has_value
            and not dirty_or_transition
            and _runtime_register_source_allows_live_sample_refresh(register)
        ):
            if (
                _safe_bool(register.get("stale"))
                or not _safe_bool(register.get("valid"))
                or str(register.get("validity") or "").strip().lower() == "stale"
            ):
                registers[name] = _refreshed_runtime_register_copy(register, sample_ts=sample_refresh_ts)
                changed = True
                continue
            updated_at = _safe_float(register.get("updated_at"))
            if updated_at <= 0.0 or updated_at < sample_refresh_ts:
                refreshed = dict(register)
                refreshed["updated_at"] = sample_refresh_ts
                registers[name] = refreshed
                changed = True
            continue
        if _safe_bool(register.get("stale")) or not _safe_bool(register.get("valid")):
            continue

        updated_at = _safe_float(register.get("updated_at"))
        age_s: float | None = None
        reason = ""
        if updated_at > 0.0:
            age_s = max(0.0, now_ts - updated_at)
            if age_s > stale_after_s:
                reason = "runtime_register_age_exceeded"
        else:
            reason = "runtime_register_missing_updated_at"

        if reason:
            registers[name] = _stale_runtime_register_copy(register, reason=reason, age_s=age_s)
            changed = True

    if changed:
        out["runtime_registers"] = registers
    return out


def _current_state_sample_integrity_from_registers(
    status: Mapping[str, Any],
    runtime_registers: Mapping[str, Any],
    *,
    now_ts: float,
) -> dict[str, Any]:
    """Project current-state sample freshness from live owner registers.

    ``current_state.json`` is an export. Its top-level sample fields must not
    stay pinned to an older status summary when the live register fabric has a
    newer modem sample.
    """

    best_ts = 0.0
    best_sequence = 0
    best_source = ""
    stale_after_s = _runtime_status_register_stale_after_s(status)
    base = _runtime_status_base_for_freshness(status)
    explicit_sample_ts = _safe_float(status.get("sample_ts"))
    if explicit_sample_ts <= 0.0 and isinstance(base, Mapping):
        explicit_sample_ts = _safe_float(base.get("sample_ts"))

    for name in _LIVE_RUNTIME_REGISTER_NAMES:
        register = runtime_registers.get(name) if isinstance(runtime_registers, Mapping) else None
        if not isinstance(register, Mapping):
            continue
        value = register.get("value")
        if not isinstance(value, Mapping) or not value:
            continue
        if not _safe_bool(register.get("valid")) or _safe_bool(register.get("stale")):
            continue
        if not _runtime_register_source_allows_live_sample_refresh(register):
            continue
        updated_at = _safe_float(register.get("updated_at"))
        if updated_at <= 0.0:
            continue
        if (float(now_ts) - updated_at) > stale_after_s:
            continue
        sequence = int(_safe_float(register.get("sequence")))
        source = str(register.get("source") or "").strip()
        if updated_at > best_ts:
            best_ts = updated_at
            best_sequence = sequence
            best_source = source
        elif updated_at == best_ts and sequence > best_sequence:
            best_sequence = sequence
            if source:
                best_source = source

    if explicit_sample_ts > 0.0 and explicit_sample_ts >= best_ts:
        explicit_age_s = _safe_float(status.get("sample_age_s"))
        if explicit_age_s <= 0.0:
            explicit_age_s = max(0.0, float(now_ts) - explicit_sample_ts)
        explicit_sequence = int(_safe_float(status.get("sample_sequence")))
        explicit_source = str(status.get("sample_source") or "").strip()
        explicit_fresh = (
            _safe_bool(status.get("sample_fresh"))
            if "sample_fresh" in status
            else explicit_age_s <= stale_after_s
        )
        if explicit_fresh:
            return {
                "sample_ts": explicit_sample_ts,
                "sample_fresh": explicit_fresh,
                "sample_age_s": explicit_age_s,
                "sample_sequence": explicit_sequence if explicit_sequence > 0 else status.get("sample_sequence"),
                "sample_source": explicit_source or status.get("sample_source") or "runtime_registers",
            }

    if best_ts <= 0.0:
        best_ts = _runtime_register_sample_refresh_ts(status, now_ts=now_ts)
        best_sequence = int(_safe_float(status.get("sample_sequence")))
        best_source = str(status.get("sample_source") or "").strip()

    if best_ts <= 0.0:
        return {}

    age_s = max(0.0, float(now_ts) - best_ts)
    return {
        "sample_ts": best_ts,
        "sample_fresh": age_s <= stale_after_s,
        "sample_age_s": age_s,
        "sample_sequence": best_sequence if best_sequence > 0 else status.get("sample_sequence"),
        "sample_source": best_source or status.get("sample_source") or "runtime_registers",
    }


def _operator_stale_reason(status: Mapping[str, Any]) -> str:
    base = _runtime_status_base_for_freshness(status)
    modem_health = status.get("modem_health")
    modem_health_map = modem_health if isinstance(modem_health, Mapping) else {}
    fast_lane = status.get("modem_live_fast_lane")
    fast_lane_map = fast_lane if isinstance(fast_lane, Mapping) else {}
    register_read = fast_lane_map.get("register_read")
    register_read_map = register_read if isinstance(register_read, Mapping) else {}
    for source in (status, base, modem_health_map):
        for key in (
            "live_register_stale_cause",
            "stale_cause",
            "reason",
            "learning_block_reason",
        ):
            value = source.get(key)
            text = str(value or "").strip()
            if text and text.lower() not in {"ok", "fresh", "valid"}:
                return text
    for key in ("stale_cause", "reason"):
        text = str(register_read_map.get(key) or "").strip()
        if text and text.lower() not in {"ok", "fresh", "valid"}:
            return text
    return "sample_age_exceeded"


def _stale_operator_default(kind: str) -> Any:
    if kind == "list":
        return []
    if kind == "bool":
        return False
    if kind == "optional_bool":
        return None
    if kind in {"optional_float", "optional_int", "raw"}:
        return None
    if kind == "int":
        return 0
    if kind == "float":
        return 0.0
    return ""


def _apply_stale_operator_projection(surface: dict[str, Any], *, reason: str, age_s: Any) -> dict[str, Any]:
    """Project a stale modem sample without leaking held live facts as valid.

    Orientation/configuration fields stay visible. Live RF, CA, CQ, congestion,
    RTT, scheduler, traffic, and display-valid fields are invalid until Modem
    Live publishes a fresh sample again.
    """

    resolved = dict(surface)
    sources = dict(resolved.get("sources") if isinstance(resolved.get("sources"), dict) else {})
    for field, kind in OPERATOR_FIELD_SPECS:
        if field in STALE_OPERATOR_PRESERVED_FIELDS:
            continue
        resolved[field] = _stale_operator_default(kind)
        sources[field] = "stale_sample"

    for field in STALE_OPERATOR_VALID_FALSE_FIELDS:
        resolved[field] = False
        sources[field] = "stale_sample"
    for field, value in STALE_OPERATOR_TEXT_OVERRIDES.items():
        resolved[field] = value
        sources[field] = "stale_sample"

    resolved.update(
        {
            "operator_sample_stale": True,
            "operator_stale_reason": str(reason or "sample_age_exceeded"),
            "operator_stale_age_s": _safe_float(age_s) if age_s not in (None, "") else None,
            "live_register_stale": True,
            "modem_read_missed": True,
            "sample_fresh": False,
            "validity": "stale",
            "sources": sources,
        }
    )
    return resolved


def _build_runtime_surface_operator(
    *,
    base_operator: Any = None,
    base_source: str = "",
    prefer_runtime_state_when_current_missing: bool = False,
) -> dict[str, Any]:
    """Build the runtime operator surface at the single sidecar adapter boundary.

    ``base_operator`` is the only live operator field input. Capability
    ``live_capture`` remains support evidence and must not fill registered
    operator fields. Missing owner fields stay missing/empty; they are not
    rebuilt from runtime health, capability captures, or unrelated
    signal sidecars.
    """
    base = dict(base_operator) if isinstance(base_operator, dict) else {}
    base_sources = (
        base.get("sources")
        if isinstance(base.get("sources"), dict)
        else {}
    )
    identity_display = (
        base.get("radio_identity_display")
        if isinstance(base.get("radio_identity_display"), dict)
        else {}
    )
    identity_display_fields = set(RADIO_IDENTITY_KEYS) | {"primary_band", "band", "earfcn"}

    def _value_present(value: Any, *, reject_bool: bool = False) -> bool:
        if value in (None, "", [], {}):
            return False
        if isinstance(value, bool):
            return not reject_bool
        if isinstance(value, str) and value.strip().lower() in {
            "",
            "-",
            "--",
            "---",
            "n/a",
            "na",
            "none",
            "null",
            "unknown",
            "miss",
            "true",
            "false",
        }:
            return False
        return True

    def _pick_base(
        field: str,
        *,
        allow_empty: bool = False,
        reject_bool: bool = False,
    ) -> tuple[Any, str]:
        if allow_empty and field in base:
            return base.get(field), str(base_sources.get(field) or base_source or "")
        value = base.get(field)
        if field in identity_display_fields and _identity_field_missing(field, value):
            display_field = _radio_identity_display_field(field)
            display_value = identity_display.get(display_field)
            if _value_present(display_value, reject_bool=reject_bool):
                return display_value, "radio_identity_display"
        if _value_present(value, reject_bool=reject_bool):
            return value, str(base_sources.get(field) or base_source or "")
        if field == "earfcn":
            dl_value = base.get("earfcn_dl")
            if _value_present(dl_value, reject_bool=reject_bool):
                return dl_value, str(base_sources.get("earfcn_dl") or base_source or "")
        elif field == "earfcn_dl":
            legacy_value = base.get("earfcn")
            if _value_present(legacy_value, reject_bool=reject_bool):
                return legacy_value, str(base_sources.get("earfcn") or base_source or "")
        if field in identity_display_fields:
            display_field = _radio_identity_display_field(field)
            display_value = identity_display.get(display_field)
            if _value_present(display_value, reject_bool=reject_bool):
                return display_value, "radio_identity_display"
        return None, ""

    def _radio_identity_display_field(field: str) -> str:
        if field in {"primary_band", "band"}:
            return "serving_band"
        if field == "earfcn":
            return "earfcn_dl"
        return field

    def _identity_field_missing(field: str, value: Any) -> bool:
        if field in {"serving_band", "primary_band", "band", "cell_id", "pci", "earfcn_dl", "earfcn"}:
            try:
                return int(value or 0) <= 0
            except Exception:
                return True
        return not _value_present(value, reject_bool=True)

    def _pick_first(*fields: str, reject_bool: bool = False) -> tuple[Any, str]:
        for field in fields:
            value, source = _pick_base(field, reject_bool=reject_bool)
            if source:
                return value, source
        return None, ""

    base_state_raw, base_state_source = _pick_base("state")
    state_raw, state_source = base_state_raw, base_state_source
    state_value = _canonical_state_value(state_raw)

    provider_name_raw, provider_name_source = _pick_base("provider_name", reject_bool=True)
    provider_plmn_raw, provider_plmn_source = _pick_base("provider_plmn", reject_bool=True)

    resolved: dict[str, Any] = {
        "state": state_value,
        "provider_name": str(provider_name_raw or ""),
        "provider_plmn": str(provider_plmn_raw or ""),
    }
    sources: dict[str, str] = {
        "state": state_source,
        "provider_name": provider_name_source,
        "provider_plmn": provider_plmn_source,
    }

    for field, kind in OPERATOR_FIELD_SPECS:
        value, source = _pick_base(
            field,
            allow_empty=(kind == "raw" or field in OPERATOR_EXPLICIT_EMPTY_FIELDS),
        )
        if kind == "bool":
            resolved[field] = _safe_bool(value)
        elif kind == "optional_bool":
            resolved[field] = _safe_bool(value) if source else None
        elif kind == "str":
            resolved[field] = str(value or "")
        elif kind == "int":
            resolved[field] = _safe_int(value)
        elif kind == "float":
            resolved[field] = _safe_float(value)
        elif kind == "optional_float":
            resolved[field] = _safe_float(value) if value not in ("", None) else None
        elif kind == "optional_int":
            resolved[field] = _safe_int(value) if value not in ("", None) else None
        elif kind == "list":
            resolved[field] = list(value) if isinstance(value, list) else []
        else:
            resolved[field] = value
        sources[field] = source

    if not sources.get("ri_valid") and sources.get("ri"):
        resolved["ri_valid"] = _safe_int(resolved.get("ri")) > 0
        sources["ri_valid"] = "ri_value_present" if resolved["ri_valid"] else str(sources.get("ri") or "")
    if not resolved.get("ri_source") and sources.get("ri"):
        resolved["ri_source"] = str(sources.get("ri") or "")
    if not sources.get("serving_band_signature") or not _value_present(
        resolved.get("serving_band_signature"),
        reject_bool=True,
    ):
        band_sig_value, band_sig_source = _pick_base("serving_band_signature")
        resolved["serving_band_signature"] = str(band_sig_value or "")
        sources["serving_band_signature"] = band_sig_source

    ca_band_signature_source = str(sources.get("ca_band_signature") or "")
    if not sources.get("ca_band_signature") or not _value_present(
        resolved.get("ca_band_signature"),
        reject_bool=True,
    ):
        ca_band_signature_value, ca_band_signature_source = _pick_first("ca_band_signature", "ca_signature")
        resolved["ca_band_signature"] = str(ca_band_signature_value or "")
        sources["ca_band_signature"] = ca_band_signature_source
    else:
        ca_band_signature_source = str(sources.get("ca_band_signature") or "")
    if not resolved.get("ca_signature") and resolved.get("ca_band_signature"):
        resolved["ca_signature"] = resolved["ca_band_signature"]
        sources["ca_signature"] = ca_band_signature_source or "ca_band_signature"
    elif resolved.get("ca_signature") and not resolved.get("ca_band_signature"):
        resolved["ca_band_signature"] = resolved["ca_signature"]
        sources["ca_band_signature"] = sources.get("ca_signature") or "ca_signature"

    cart = _safe_int(resolved.get("cart"))
    cast = resolved.get("cast")
    cart_ld = _safe_int(resolved.get("cart_ld"))
    cart_ld_valid = _safe_bool(resolved.get("cart_ld_valid"))
    cart_ld_display_source = str(sources.get("cart_ld_display") or "")
    cart_ld_display_valid_source = str(sources.get("cart_ld_display_valid") or "")
    cart_ld_display = (
        _safe_int(resolved.get("cart_ld_display"))
        if cart_ld_display_source and _value_present(resolved.get("cart_ld_display"))
        else cart_ld
    )
    cart_ld_display_valid = (
        _safe_bool(resolved.get("cart_ld_display_valid"))
        if cart_ld_display_valid_source
        else cart_ld_valid
    )
    cast_ld_valid = _safe_bool(resolved.get("cast_ld_valid"))
    ca_stat_ld = resolved.get("ca_stat_ld_pct")
    ca_stat_ld_valid = _safe_bool(resolved.get("ca_stat_ld_valid"))
    cart_activity = resolved.get("cart_activity_per_hour")
    cart_ld_activity = resolved.get("cart_ld_activity")
    ri_stability_pct = resolved.get("ri_stability_pct")
    ri_activity_per_hour = resolved.get("ri_activity_per_hour")
    ri_stability_valid = _safe_bool(resolved.get("ri_stability_valid"))
    resolved["cart"] = cart
    resolved["cart_ld"] = cart_ld
    resolved["cart_ld_valid"] = cart_ld_valid
    resolved["cart_ld_display"] = cart_ld_display
    resolved["cart_ld_display_valid"] = cart_ld_display_valid
    if not cart_ld_display_source and sources.get("cart_ld"):
        sources["cart_ld_display"] = str(sources.get("cart_ld") or "")
    if not cart_ld_display_valid_source and sources.get("cart_ld_valid"):
        sources["cart_ld_display_valid"] = str(sources.get("cart_ld_valid") or "")
    resolved["cast_ld_valid"] = cast_ld_valid
    ca_active_source = str(sources.get("ca_active") or "")
    if ca_active_source:
        resolved["ca_active"] = _safe_bool(resolved.get("ca_active"))
    elif sources.get("cart"):
        resolved["ca_active"] = bool(cart)
        sources["ca_active"] = "ca_state_projection"
    else:
        resolved["ca_active"] = _safe_bool(resolved.get("ca_active"))
    resolved["ca_realtime_state"] = cart
    resolved["ca_stat_pct"] = cast
    resolved["ca_realtime_ld_state"] = cart_ld
    resolved["ca_realtime_ld_valid"] = cart_ld_valid
    resolved["ca_stat_ld_pct"] = ca_stat_ld
    resolved["ca_stat_ld_valid"] = ca_stat_ld_valid
    resolved["ca_change_per_hour"] = cart_activity
    resolved["ca_ld_change_per_hour"] = cart_ld_activity
    resolved["ri_stability_pct"] = ri_stability_pct
    resolved["ri_activity_per_hour"] = ri_activity_per_hour
    resolved["ri_stability_valid"] = ri_stability_valid
    for legacy_field in (
        "ca_realtime_state",
        "ca_stat_pct",
        "ca_realtime_ld_state",
        "ca_realtime_ld_valid",
        "ca_stat_ld_pct",
        "ca_stat_ld_valid",
        "ca_change_per_hour",
        "ca_ld_change_per_hour",
    ):
        sources[legacy_field] = "ca_state_projection"

    resolved["sources"] = sources
    return resolved


def _compact_payload_fields(payload: Any, *, fields: Iterable[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    for field in fields:
        if field in payload and payload.get(field) not in (None, ""):
            out[str(field)] = payload.get(field)
    return out


def _compact_runtime_payload(payload: Any) -> dict[str, Any]:
    data = _compact_payload_fields(
        payload,
        fields=(
            "ts",
            "iso",
            "phase",
            "runtime_mode",
            "state",
            "modem_backend",
            "backend_family",
            "write_authority",
            "write_block_reason",
            "observe_only",
            "cycle_count",
            "cycle_in_progress",
            "last_progress_ts",
            "last_cycle_started_ts",
            "last_cycle_completed_ts",
            "last_error",
            "instance_id",
            "profile_name",
        ),
    )
    if data:
        data.update(summarize_runtime_health_status(payload))
    return data


def _compact_capabilities_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = _compact_payload_fields(
        payload,
        fields=(
            "ts",
            "iso",
            "backend",
            "backend_family",
            "family",
            "model",
            "variant",
            "profile_name",
            "signal_contract",
            "traffic_visibility",
            "neighbor_visibility",
            "scheduler_source",
            "action_trust_posture",
            "provenance",
            "runtime_identity",
        ),
    )
    support_flags = {
        key: value
        for key, value in payload.items()
        if isinstance(key, str)
        and (
            key.startswith("supports_")
            or key in {"traffic_visibility", "neighbor_visibility"}
        )
        and value not in (None, "")
    }
    if support_flags:
        data.update(support_flags)
    live_capture = payload.get("live_capture")
    if isinstance(live_capture, dict) and live_capture:
        data["live_capture_summary"] = _compact_payload_fields(
            live_capture,
            fields=(
                "radio_connected",
                "radio_state_text",
                "serving_band",
                "serving_band_signature",
                "configured_band_pool",
                "configured_band_pool_signature",
                "configured_band_pool_source",
                "active_band_set",
                "ca_state_text",
                "pci",
                "sinr_db",
                "dl_mbps",
                "ul_mbps",
                "rtt_ms",
                "congestion_pct",
                "congestion_pct_valid",
                "congestion_display_pct",
                "congestion_display_valid",
                "congestion_bucket",
                "congestion_state_bucket",
            ),
        )
    resolved_identity_card = payload.get("resolved_identity_card")
    if isinstance(resolved_identity_card, dict) and resolved_identity_card:
        data["resolved_identity_card"] = dict(resolved_identity_card)
    return data


def _compact_signals_payload(payload: Any) -> dict[str, Any]:
    return _compact_payload_fields(
        payload,
        fields=(
            "ts",
            "iso",
            "cq_last_value",
            "rtt_last_ms",
            "rtt_delta_last_ms",
            "rsrq_gap_last_db",
            "congestion_pct_last",
            "congestion_bucket_last",
        ),
    )


def _support_surface_order(
    runtime_payload: Any,
) -> tuple[str, dict[str, Any], str, dict[str, Any], int, int]:
    runtime = dict(runtime_payload) if isinstance(runtime_payload, dict) else {}
    runtime_tier = support_tier_from_payload(runtime)
    return "runtime", runtime, "", {}, runtime_tier, 0


def _select_support_write_surface(
    primary_name: str,
    primary_payload: Mapping[str, Any] | None,
    secondary_name: str,
    secondary_payload: Mapping[str, Any] | None,
) -> tuple[Any, str, str, str]:
    primary = primary_payload if isinstance(primary_payload, Mapping) else {}
    if payload_authority := primary.get("write_authority"):
        return (
            payload_authority,
            primary_name,
            str(primary.get("write_block_reason") or ""),
            primary_name,
        )
    if "write_block_reason" in primary:
        return (
            primary.get("write_authority"),
            primary_name,
            str(primary.get("write_block_reason") or ""),
            primary_name,
        )
    return (
        "",
        "",
        "",
        "",
    )


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted(str(item) for item in value if str(item or "").strip())
    return []


def _confidence_label(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.50:
        return "medium"
    if score >= 0.25:
        return "low"
    return "none"


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _build_runtime_explainability_surface(
    *,
    runtime_identity: Mapping[str, Any],
    runtime_session_scope: Mapping[str, Any],
    resolved_identity_card: Mapping[str, Any],
    capability_contract: Mapping[str, Any],
    support_tier: Any,
    support_tier_label: Any,
    support_tier_detail: Mapping[str, Any],
    support_state: Any,
    write_authority: Any,
    write_block_reason: str,
    external_memory_prior: Mapping[str, Any],
    operational_status: Mapping[str, Any],
    policy_overlay: Mapping[str, Any],
    beta_soak: Mapping[str, Any],
    brain_bouncer: Mapping[str, Any],
    brain_referee: Mapping[str, Any],
    bouncer_intent: Mapping[str, Any],
    bouncer_outcome: Mapping[str, Any],
    referee_outcome: Mapping[str, Any],
) -> dict[str, Any]:
    tier = support_tier_from_payload({"support_tier": support_tier})
    identity_state = str(runtime_identity.get("identity_state") or "").strip()
    scope_tier = str(runtime_session_scope.get("scope_tier") or "").strip()
    profile_id = str(
        resolved_identity_card.get("profile_id")
        or capability_contract.get("profile_id")
        or runtime_identity.get("resolved_profile_id")
        or ""
    ).strip()
    canonical = (
        capability_contract.get("canonical_surface")
        if isinstance(capability_contract.get("canonical_surface"), Mapping)
        else {}
    )
    canonical_missing = _string_list(canonical.get("missing_groups") if isinstance(canonical, Mapping) else [])
    canonical_degraded = _string_list(canonical.get("degraded_groups") if isinstance(canonical, Mapping) else [])
    canonical_conflicts = _string_list(canonical.get("field_conflicts") if isinstance(canonical, Mapping) else [])
    canonical_complete = _safe_bool(canonical.get("complete")) if canonical else False
    operational_latest = (
        operational_status.get("latest")
        if isinstance(operational_status.get("latest"), Mapping)
        else {}
    )
    auto_import = (
        external_memory_prior.get("auto_import")
        if isinstance(external_memory_prior.get("auto_import"), Mapping)
        else {}
    )
    operational_detail = (
        operational_latest.get("detail")
        if isinstance(operational_latest.get("detail"), Mapping)
        else {}
    )
    operational_workflow = (
        operational_detail.get("workflow")
        if isinstance(operational_detail.get("workflow"), Mapping)
        else {}
    )
    bouncer_state = _first_text(brain_bouncer.get("state"), bouncer_outcome.get("state")).lower()
    bouncer_level = _first_text(brain_bouncer.get("level"), bouncer_intent.get("level")).lower()
    bouncer_code = _first_text(
        brain_bouncer.get("code"),
        brain_bouncer.get("trip_reason"),
        bouncer_intent.get("code"),
        bouncer_outcome.get("code"),
    )
    bouncer_reason = _first_text(
        brain_bouncer.get("trip_reason"),
        brain_bouncer.get("message"),
        bouncer_intent.get("message"),
        bouncer_outcome.get("message"),
        bouncer_code,
    )
    bouncer_reentry = _first_text(brain_bouncer.get("reentry_condition"))
    bouncer_tripped = _safe_bool(brain_bouncer.get("tripped")) or bouncer_state == "blocked"

    referee_state = _first_text(brain_referee.get("state")).lower()
    referee_grade = _first_text(brain_referee.get("grade")).lower()
    referee_summary = _first_text(
        brain_referee.get("summary"),
        referee_outcome.get("message"),
        referee_outcome.get("code"),
    )
    referee_decision_trace_id = _first_text(
        brain_referee.get("decision_trace_id"),
        bouncer_outcome.get("decision_trace_id"),
    )

    blockers: list[str] = []
    missing_evidence: list[str] = []
    if str(write_block_reason or "").strip():
        blockers.append(str(write_block_reason).strip())
    if canonical_conflicts:
        blockers.append("canonical_surface_field_conflict")
    if str(operational_latest.get("status") or "").strip().lower() in {"blocked", "rejected"}:
        blockers.append(f"operational_service_{operational_latest.get('status')}")
    if bouncer_tripped:
        blockers.append(f"bouncer:{bouncer_code or bouncer_reason or 'blocked'}")
    if referee_grade == "rejected":
        blockers.append(f"referee:{_first_text(brain_referee.get('verdict', {}).get('code') if isinstance(brain_referee.get('verdict'), Mapping) else '', referee_summary, 'rejected')}")

    if identity_state != "identified":
        missing_evidence.append("exact_runtime_identity")
    if not profile_id:
        missing_evidence.append("resolved_profile_id")
    if not scope_tier:
        missing_evidence.append("runtime_session_scope")
    if canonical_missing:
        missing_evidence.extend(f"canonical_missing:{name}" for name in canonical_missing)
    if canonical_degraded:
        missing_evidence.extend(f"canonical_degraded:{name}" for name in canonical_degraded)
    if int(tier or 0) < TIER_AUTOMATION_ELIGIBLE:
        missing_evidence.append("automation_eligible_runtime_surface")
    if not _safe_bool(external_memory_prior.get("present")):
        missing_evidence.append("external_ltm_prior")
    if referee_state == "pending":
        missing_evidence.append("referee_execution_outcome")

    score = 0.10
    if identity_state == "identified":
        score += 0.20
    elif identity_state:
        score += 0.08
    if profile_id:
        score += 0.08
    score += min(0.24, max(0, int(tier)) * 0.08)
    if canonical_complete:
        score += 0.20
    elif canonical:
        score += 0.08
    if _safe_bool(external_memory_prior.get("present")):
        score += 0.08
    if str(auto_import.get("status") or "").strip().lower() in {"imported", "skipped_existing_prior"}:
        score += 0.04
    if str(write_authority or "").strip().lower() == "full":
        score += 0.06
    if canonical_conflicts:
        score -= 0.30
    if bouncer_tripped:
        score -= 0.12
    if referee_grade == "rejected":
        score -= 0.16
    score -= min(0.18, 0.04 * len(canonical_missing))
    score -= min(0.12, 0.03 * len(canonical_degraded))
    score = round(max(0.0, min(1.0, score)), 3)

    if blockers:
        status = "blocked"
    elif int(tier or 0) >= TIER_AUTOMATION_ELIGIBLE and canonical_complete:
        status = "automation_eligible"
    elif support_tier_write_allowed(int(tier or 0)):
        status = "manual_action_safe"
    elif int(tier or 0) >= TIER_MONITOR_SAFE:
        status = "monitor_safe"
    elif int(tier or 0) >= TIER_OBSERVED:
        status = "observed"
    else:
        status = "unknown"

    if blockers:
        next_step = (
            bouncer_reentry
            if bouncer_tripped and bouncer_reentry
            else "Resolve the first blocker before trusting automation escalation."
        )
    elif missing_evidence:
        next_step = "Collect or import the missing evidence before promoting confidence."
    else:
        next_step = "Continue soak and monitor for drift."

    summary = (
        f"{status}: T{int(tier)} confidence {_confidence_label(score)}"
        if tier >= 0
        else f"{status}: confidence {_confidence_label(score)}"
    )

    return {
        "schema_version": "runtime_explainability_v1",
        "status": status,
        "summary": summary,
        "confidence": score,
        "confidence_label": _confidence_label(score),
        "evidence_strength": _confidence_label(score),
        "identity": {
            "identity_state": identity_state,
            "scope_tier": scope_tier,
            "profile_id": profile_id,
            "exact_identity": _safe_bool(resolved_identity_card.get("exact_identity", False)),
            "provisional": _safe_bool(resolved_identity_card.get("provisional", False)),
            "provisional_reason": str(resolved_identity_card.get("provisional_reason") or ""),
        },
        "support": {
            "state": str(support_state or ""),
            "tier": int(tier),
            "tier_label": str(support_tier_label or ""),
            "reason": str(support_tier_detail.get("reason") or ""),
            "write_authority": str(write_authority or ""),
            "write_block_reason": str(write_block_reason or ""),
        },
        "canonical_surface": {
            "complete": _safe_bool(canonical_complete),
            "missing_groups": canonical_missing,
            "degraded_groups": canonical_degraded,
            "field_conflicts": canonical_conflicts,
        },
        "external_memory": {
            "present": _safe_bool(external_memory_prior.get("present", False)),
            "runtime_influence": str(external_memory_prior.get("runtime_influence") or "none"),
            "fact_count": int(external_memory_prior.get("fact_count") or 0),
            "observation_count": int(external_memory_prior.get("observation_count") or 0),
            "auto_import_status": str(auto_import.get("status") or ""),
            "auto_import_authority": str(auto_import.get("authority") or ""),
        },
        "operational_service": {
            "status": str(operational_latest.get("status") or ""),
            "operation": str(operational_latest.get("operation") or ""),
            "service_owner": str(operational_latest.get("service_owner") or ""),
            "message": str(operational_latest.get("message") or ""),
            "workflow_status": str(operational_workflow.get("status") or ""),
        },
        "decision": {
            "decision_trace_id": referee_decision_trace_id,
            "bouncer": {
                "state": bouncer_state,
                "level": bouncer_level,
                "code": bouncer_code,
                "reason": bouncer_reason,
                "tripped": _safe_bool(bouncer_tripped),
                "reentry_condition": bouncer_reentry,
                "observe_monitor_fallback": _safe_bool(brain_bouncer.get("observe_monitor_fallback", False)),
            },
            "referee": {
                "state": referee_state,
                "grade": referee_grade,
                "summary": referee_summary,
                "outcome_confidence": str(brain_referee.get("outcome_confidence") or ""),
                "learning_update": str(brain_referee.get("learning_update") or ""),
                "prediction_feedback": str(brain_referee.get("prediction_feedback") or ""),
            },
        },
        "policy_overlay": {
            "active": _safe_bool(policy_overlay.get("active", False)),
            "overlay_id": str(policy_overlay.get("overlay_id") or ""),
            "last_error": str(policy_overlay.get("last_error") or ""),
        },
        "beta_soak": {
            "status": str(beta_soak.get("status") or ""),
            "ready": _safe_bool(beta_soak.get("ready", False)),
        },
        "blockers": blockers,
        "missing_evidence": sorted(set(missing_evidence)),
        "rollback_or_cooldown_reason": str(
                operational_workflow.get("rollback_reason")
                or operational_workflow.get("cooldown_reason")
            or support_tier_detail.get("reason")
            or write_block_reason
            or ""
        ),
        "next_step": next_step,
    }


def is_runtime_fresh(
    *,
    runtime_health_snapshot: Any = None,
    runtime_health_path: Path | None = None,
    expected_identity: Mapping[str, Any] | None = None,
    now: float = 0.0,
    max_age_s: float = 8.0,
    in_progress_max_age_s: float = 90.0,
) -> bool:
    """Single authoritative freshness check for runtime sidecars.

    Returns True if the runtime is considered alive based on:
    1. runtime_health.json timestamp within max_age_s
    2. cycle_in_progress with started_ts within in_progress_max_age_s

    current_state.json is an export projection and must not make a runtime
    online when runtime_health.json is stale or missing.
    """
    if now <= 0.0:
        now = time.time()

    # Terminal phases in runtime_health.json mean the runner has stopped or
    # been explicitly invalidated. These are never fresh regardless of ts.
    #   shutdown      — runner exited cleanly
    #   stale_session — a prior session stamped this before a new session
    #   fatal_runner  — runner crashed
    _TERMINAL_PHASES = {"shutdown", "stale_session", "fatal_runner"}
    if isinstance(runtime_health_snapshot, dict):
        _rh_phase = str(runtime_health_snapshot.get("phase", "") or "")
        if _rh_phase in _TERMINAL_PHASES:
            return False

    runtime_identity_ok = sidecar_identity_matches(
        runtime_health_snapshot,
        expected=expected_identity,
        source_path=runtime_health_path,
    )
    # 1. Runtime health sidecar timestamp
    runtime_ts = sidecar_last_ts(runtime_health_snapshot) if runtime_identity_ok else 0.0
    if runtime_ts > 0.0 and (now - runtime_ts) <= max_age_s:
        return True

    # 2. Cycle-in-progress grace (long cycles are not stale)
    rhs = runtime_health_snapshot if runtime_identity_ok and isinstance(runtime_health_snapshot, dict) else {}
    if rhs:
        phase = str(rhs.get("phase", "") or "")
        cycle_in_progress = _safe_bool(rhs.get("cycle_in_progress"))
        if cycle_in_progress or phase == "cycle_started":
            try:
                started_ts = float(
                    rhs.get("last_cycle_started_ts")
                    or rhs.get("last_progress_ts")
                    or rhs.get("ts")
                    or 0.0
                )
            except Exception:
                started_ts = 0.0
            if started_ts > 0.0 and (now - started_ts) <= in_progress_max_age_s:
                return True

    return False


def summarize_runtime_health_status(payload: Any) -> dict[str, Any]:
    """Summarize runtime health status into consistent fields.
    
    Maps raw runtime phases into a stable status phase:
    - starting: init, startup_begin, startup_ok
    - running: cycle_ok, modem_read_missed, cycle_recoverable_fault, cycle_started (after first cycle)
    - ready: check_complete
    - connection_lost: connection_lost
    - error: fatal_cycle, fatal_runner, fatal_startup
    - shutdown: shutdown
    """
    data = payload if isinstance(payload, dict) else {}
    raw_phase = str(data.get("phase", "") or "")
    last_cycle_completed_ts = _safe_float(data.get("last_cycle_completed_ts"))
    last_cycle_ts = _safe_float(data.get("last_cycle_ts"))
    has_seen_cycle = last_cycle_completed_ts > 0.0 or last_cycle_ts > 0.0

    # Runner phases
    if raw_phase in {"init", "startup_begin", "startup_ok"}:
        status_phase = "starting"
    elif raw_phase == "startup_blocked":
        status_phase = "startup_blocked"
    elif raw_phase == "reconnecting":
        status_phase = "reconnecting"
    elif raw_phase == "cycle_ok":
        status_phase = "running"
    elif raw_phase == "modem_read_missed":
        status_phase = "running"
    elif raw_phase == "cycle_recoverable_fault":
        status_phase = "running"
    elif raw_phase == "cycle_started":
        status_phase = "running" if has_seen_cycle else "starting"
    elif raw_phase == "check_complete":
        status_phase = "ready"
    elif raw_phase == "connection_lost":
        status_phase = "connection_lost"
    elif raw_phase in {"fatal_cycle", "fatal_runner", "fatal_startup"}:
        status_phase = "error"
    elif raw_phase == "shutdown":
        status_phase = "shutdown"
    else:
        status_phase = raw_phase or "unknown"

    return {
        "raw_phase": raw_phase or "unknown",
        "status_phase": status_phase,
        "cycle_in_progress": raw_phase == "cycle_started",
        "has_completed_cycle": has_seen_cycle,
    }



def write_json_sidecar(
    *,
    targets: Iterable[Path],
    payload: Any,
    indent: int | None = None,
    refresh_current_state: bool = False,
) -> bool:
    """Write JSON payload to sidecar files atomically.

    ``current_state.json`` has one writer: the accepted-generation tick export.
    The retired sidecar-reassembly option remains as a hard-failing argument so
    an old caller cannot silently reintroduce a mixed-generation truth path.

    Returns True if at least one write succeeded, False otherwise.
    Fail-soft: logs warning but does not raise on total failure.
    """
    if bool(refresh_current_state):
        raise RuntimeError("current_state_sidecar_reassembly_path_deleted")
    ts = _payload_ts(payload)
    primary_success = False
    last_error: str = ""
    paths = _dedupe_targets(targets)
    record_json_sidecar_write_attempt(len(paths))
    try:
        payload_for_write = _json_sidecar_boundary_payload(payload)
        text = json.dumps(payload_for_write, indent=indent)
    except Exception as exc:
        for path in paths:
            _record_json_sidecar_write_failure(path, exc, attempts=0, phase="serialize")
        log.warning("sidecar write refused unencodable payload: %s", exc)
        return False
    for path in paths:
        rejection = _truth_payload_rejection(path, payload_for_write)
        if rejection:
            last_error = rejection
            _record_json_sidecar_write_failure(path, ValueError(rejection), attempts=0, phase="validate")
            log.warning("sidecar write refused truth payload: %s", rejection)
            continue
        if _is_critical_truth_path(path):
            try:
                _write_text_atomically_with_retries(_shadow_path(path), text, phase="shadow")
            except Exception as exc:
                last_error = f"{path.name} shadow: {exc}"
                log.debug("write_json_sidecar: suppressed %s", exc)
                pass
        if _uses_truth_ring(path):
            try:
                _write_text_atomically_with_retries(_ring_slot_path(path, ts=ts), text, phase="ring")
            except Exception as exc:
                last_error = f"{path.name} ring: {exc}"
                log.debug("write_json_sidecar: suppressed %s", exc)
                pass
        try:
            cache_key = _json_sidecar_failure_key(path)
            if _can_skip_unchanged_json_sidecar(path) and _JSON_SIDECAR_LAST_TEXT_BY_PATH.get(cache_key) == text:
                record_json_sidecar_unchanged_skip()
                primary_success = True
                _clear_json_sidecar_write_failure(path)
                continue
            _write_text_atomically_with_retries(path, text, phase="primary")
            _JSON_SIDECAR_LAST_TEXT_BY_PATH[cache_key] = text
            _record_json_sidecar_primary_success(path)
            primary_success = True
        except Exception as exc:
            _JSON_SIDECAR_LAST_TEXT_BY_PATH.pop(_json_sidecar_failure_key(path), None)
            last_error = f"{path}: {exc}"
            log.debug("write_json_sidecar: suppressed %s", exc)
            pass
    if not primary_success:
        log.warning("sidecar write failed: %s", last_error or "no targets")
    return primary_success


def read_json_sidecar(
    path: Path,
    *,
    fallback: Optional[Any] = None,
) -> tuple[Optional[Any], str, Optional[Path]]:
    candidates: list[tuple[str, Path]] = [("primary", path)]
    if _is_critical_truth_path(path):
        candidates.append(("shadow", _shadow_path(path)))
    loaded_candidates: list[tuple[str, Path, Any]] = []
    for source, candidate in candidates:
        try:
            if not candidate.exists():
                continue
            with open(candidate, "r", encoding="utf-8", errors="ignore") as handle:
                loaded_candidates.append((source, candidate, json.load(handle)))
        except Exception:
            continue
    if loaded_candidates:
        envelope_choice = _newest_same_writer_export_candidate(loaded_candidates)
        if envelope_choice is not None:
            source, candidate, payload = envelope_choice
            return payload, source, candidate
        source, candidate, payload = loaded_candidates[0]
        return payload, source, candidate
    if _is_critical_truth_path(path):
        ring_dir = path.parent / TRUTH_RING_DIRNAME / path.name
        try:
            ring_paths = sorted(
                [p for p in ring_dir.glob("*.json") if p.is_file()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            ring_paths = []
        for candidate in ring_paths:
            try:
                with open(candidate, "r", encoding="utf-8", errors="ignore") as handle:
                    return json.load(handle), "ring", candidate
            except Exception:
                continue
    if fallback is not None:
        return fallback, "fallback", None
    return None, "missing", None


def repair_json_sidecar(
    path: Path,
    *,
    data: Optional[Any] = None,
    source: str = "",
) -> bool:
    if data is None:
        data, source, _ = read_json_sidecar(path)
    if data is None or str(source or "").lower() in {"", "primary", "missing"}:
        return False
    write_json_sidecar(
        targets=[path],
        payload=data,
        indent=2,
        refresh_current_state=False,
    )
    return True
