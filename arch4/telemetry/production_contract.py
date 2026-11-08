"""Production-minimal telemetry signal contract.

The full ``telemetry_history_*.jsonl`` row remains the comparison/debug lane.
This module owns the compact production lane used to prove the minimum
fourteen-day runtime evidence set before any full-history cutover.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from arch4.evidence_scope import validated_evidence_scope
from arch4.telemetry.history_contract import telemetry_formula_vector


PRODUCTION_TELEMETRY_BASENAME: Final[str] = "telemetry_production"
PRODUCTION_TELEMETRY_SCHEMA_VERSION: Final[str] = "arch_production_signal_v1"
PRODUCTION_TELEMETRY_SIGNAL_VERSION: Final[int] = 1
LOG_MODE_ENV: Final[str] = "ARCH_LOG_MODE"
LOG_MODES: Final[tuple[str, ...]] = ("compare", "production", "debug", "trace")
DEFAULT_LOG_MODE: Final[str] = "production"


PRODUCTION_SIGNAL_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "S001": ("record_type", "ts", "iso", "tick"),
    "S002": ("sample_ts", "sample_fresh", "sample_sequence", "sample_age_s", "sample_source"),
    "S003": ("engine_tick_ts", "engine_tick_iso"),
    "S004": (
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
    ),
    "S005": (
        "modem_live_io_quiet_active",
        "modem_live_io_quiet_remaining_s",
        "live_register_quiet_hold",
        "live_register_hold_reason",
    ),
    "S006": (
        "session_scope_key",
        "session_seq",
        "backend",
        "backend_family",
        "resolved_profile_id",
        "profile_id",
        "profile_provenance",
        "identity_state",
        "match_quality",
    ),
    "S007": ("model", "firmware", "software_version", "webui", "serial", "imei", "imsi", "access_type"),
    "S008": ("provider_name", "provider_plmn"),
    "S009": ("radio_connected", "radio_state_text", "rrc", "network_type", "network_type_ex"),
    "S010": ("primary_band", "serving_band", "serving_band_signature"),
    "S011": ("pci", "earfcn", "earfcn_ul", "enb_id", "cell_id", "tac"),
    "S012": (
        "configured_band_pool",
        "configured_band_pool_signature",
        "configured_band_pool_source",
        "configured_ca_pool_display",
    ),
    "S013": (
        "ca_signature",
        "ca_component_band_set",
        "ca_component_band_signature",
        "ca_component_count",
        "ca_composition_signature",
        "ca_source",
        "ca_state_text",
    ),
    "S014": ("ca_active", "cart", "cast", "ca_stat_pct", "ca_stat_scope_key", "cart_activity_per_hour"),
    "S015": (
        "cart_ld",
        "cart_ld_valid",
        "cast_ld",
        "cast_ld_valid",
        "cast_ld_pct",
        "ca_stat_ld_pct",
        "ca_stat_ld_valid",
        "cart_ld_activity",
        "ca_ld_change_per_hour",
    ),
    "S016": ("sinr_db", "rsrp_dbm", "rsrq_db", "rsrq_source", "rssi_dbm"),
    "S017": ("dl_bandwidth_mhz", "ul_bandwidth_mhz", "dl_freq_mhz", "ul_freq_mhz"),
    "S018": (
        "mcs_dl0",
        "mcs_dl1",
        "mcs_ul",
        "cqi0",
        "cqi1",
        "cqi_source",
        "qam_dl0",
        "qam_dl1",
        "qam_ul",
        "ri",
        "ri_valid",
        "ri_source",
        "trans_mode",
        "tx_power",
    ),
    "S019": (
        "dl_mbps",
        "ul_mbps",
        "has_traffic",
        "traffic_current_download_rate_bps",
        "traffic_current_upload_rate_bps",
        "traffic_rate_source",
    ),
    "S020": (
        "traffic_day_download_bytes",
        "traffic_day_upload_bytes",
        "traffic_day_used_bytes",
        "traffic_month_download_bytes",
        "traffic_month_upload_bytes",
        "traffic_total_download_bytes",
        "traffic_total_upload_bytes",
        "traffic_session_download_bytes",
        "traffic_session_upload_bytes",
        "traffic_accounting_source",
        "traffic_accounting_status",
        "traffic_volume_poll_interval_s",
        "traffic_volume_last_sample_ts",
        "traffic_volume_next_sample_ts",
        "traffic_volume_age_s",
    ),
    "S021": (
        "traffic_quota_start_day",
        "traffic_quota_limit_label",
        "traffic_quota_left_bytes",
        "traffic_quota_limit_bytes",
        "traffic_quota_month_threshold_pct",
        "traffic_quota_day_threshold_pct",
        "volume_used_today_gb",
        "volume_allowance_gb",
        "volume_remaining_today_gb",
    ),
    "S022": ("rtt_ms", "rtt_sliding_avg", "rtt_baseline", "rtt_quiet_delta", "rtt_quiet_held", "rtt_elevated"),
    "S023": (
        "rtt_delta",
        "rtt_delta_valid",
        "rtt_delta_display_ms",
        "rtt_delta_display_valid",
        "rtt_delta_display_held",
        "rtt_delta_display_live_ts",
        "rtt_delta_block_reason",
    ),
    "S024": (
        "rtt_active_traffic_class",
        "rtt_active_load_mbps",
        "rtt_low_load_latency_valid",
        "rtt_low_load_latency_reason",
        "rtt_low_load_latency_mbps",
        "rtt_active_jitter_mad_ms",
        "rtt_active_jitter_step_p95_ms",
        "rtt_active_jitter_samples",
        "rtt_active_jitter_valid",
    ),
    "S025": (
        "rtt_dl_jitter_bucket",
        "rtt_dl_jitter_trimmed_ms",
        "rtt_dl_jitter_baseline_ms",
        "rtt_dl_jitter_delta_ms",
        "rtt_dl_jitter_pct",
        "rtt_dl_jitter_valid",
        "rtt_dl_jitter_display_valid",
    ),
    "S026": (
        "cq_cell",
        "cq_cell_valid",
        "cq_cell_source",
        "cq_cell_display",
        "cq_cell_display_valid",
        "cq_cell_display_held",
        "cq_display_pct",
        "bq_cell",
        "bq_cell_valid",
        "bq_cell_source",
    ),
    "S027": ("cqi_mcs_gap_smoothed", "cqi_mcs_gap_valid", "cqi_mcs_delta_pct"),
    "S028": (
        "congestion_pct",
        "congestion_pct_valid",
        "congestion_display_pct",
        "congestion_display_valid",
        "congestion_supported",
        "congestion_support_reason",
        "congestion_bucket",
        "congestion_state_bucket",
        "congestion_state_avg_pct",
        "congestion_kind_hint",
        "congestion_source",
        "congestion_trend_display",
    ),
    "S029": (
        "congestion_raw_pct",
        "congestion_qam_gap",
        "congestion_slope",
        "congestion_rising",
        "congestion_flags_valid",
        "congestion_bad_now",
        "congestion_pressure_high",
        "congestion_pressure_moderate",
        "congestion_fast_mode",
        "congestion_hold_mode",
        "congestion_trusted_rtt",
        "congestion_rtt_base_pct",
        "congestion_trusted_rsrq",
        "congestion_rsrq_available",
        "congestion_rsrq_contributes",
        "congestion_rsrq_reason",
        "congestion_rsrq_pct",
        "congestion_rsrq_gap_db",
        "congestion_rsrq_gap_threshold_db",
        "congestion_trusted_active_jitter",
        "congestion_active_jitter_pct",
    ),
    "S030": (
        "rsrq_baseline",
        "rsrq_baseline_valid",
        "rsrq_gap",
        "rsrq_gap_valid",
        "rsrq_delta_display_db",
        "rsrq_delta_display_valid",
        "rsrq_delta_display_held",
        "rsrq_delta_display_live_ts",
        "rsrq_baseline_display_db",
        "rsrq_baseline_display_valid",
        "rsrq_baseline_display_held",
        "rsrq_baseline_display_live_ts",
    ),
    "S031": (
        "delivered_capacity_valid",
        "delivered_capacity_ratio",
        "delivered_capacity_best_dl_mbps",
        "delivered_capacity_total_bw_mhz",
        "delivered_capacity_ceiling_mbps",
        "delivered_capacity_sample_count",
        "delivered_capacity_source",
    ),
    "S032": (
        "dl_bottleneck_state",
        "dl_bottleneck_reason",
        "dl_bottleneck_proof_level",
        "dl_bottleneck_owner",
        "dl_bottleneck_schema_version",
    ),
    "S033": ("ri_stability_pct", "ri_activity_per_hour", "ri_stability_valid"),
    "S034": (
        "state",
        "action_lever",
        "action_reason",
        "action_text",
        "hour",
        "day_type",
        "drift_reason",
        "confidence_penalty",
    ),
    "S035": (
        "decision_trace_id",
        "decision_trace_source",
        "decision_trace_final",
        "decision_trace_verdict",
        "decision_trace_gate",
        "decision_trace_reason",
    ),
    "S036": (
        "runtime_clock_owner",
        "runtime_clock_source",
        "runtime_clock_clock_ms",
        "runtime_clock_max_clock_ms",
        "runtime_clock_sequence",
        "runtime_clock_tick",
        "runtime_clock_observed_ts",
        "runtime_clock_lane_count",
        "runtime_clock_over_budget_total",
        "runtime_clock_stale_job_total",
        "runtime_clock_max_lateness_s",
        "runtime_clock_error_count",
        "runtime_clock_top_over_budget_jobs",
        "runtime_clock_top_over_budget_lanes",
    ),
    "S037": (
        "publication_sequence",
        "publication_sequence_gap",
        "publication_missed_total",
        "publication_published",
        "publication_reason",
        "publication_duration_ms",
        "publication_last_error",
    ),
    "S038": ("rat_mode", "rat_lte_present", "rat_nr_present", "rat_owner"),
    "S039": (
        "nsa_active",
        "sa_active",
        "nr_band",
        "nr_pci",
        "nr_arfcn",
        "nr_cell_id",
        "nr_rsrp_dbm",
        "nr_rsrq_db",
        "nr_sinr_db",
        "nr_bandwidth_mhz",
        "nr_scs_khz",
        "nr_mode",
        "nr_state_text",
        "nr_secondary_active",
        "nr_transition_state",
        "nr_transition_dirty",
        "nr_identity_completeness",
        "nsa_snapshot",
        "nr_capacity_evidence",
    ),
    "S040": (
        "selected_band_set",
        "selected_band_set_signature",
        "active_band_set",
        "active_band_set_signature",
    ),
    "S041": ("mcs_collapse",),
    "S042": (
        "self_learning_strategy_outcome",
        "self_learning_strategy_reason",
        "self_learning_strategy_required_next_step",
        "smart_loop_decision_kind",
        "smart_loop_decision_semantic",
        "smart_loop_selected_action",
        "smart_loop_target_band",
        "smart_loop_reason_code",
        "smart_loop_score",
        "smart_loop_confidence",
        "smart_loop_evidence_count",
        "smart_loop_candidate_total",
        "smart_loop_candidate_operational",
        "smart_loop_candidate_actionable",
        "smart_loop_candidate_advisory",
        "smart_loop_candidate_blocked",
        "smart_loop_decision_allowed",
        "smart_loop_action_blocker",
        "smart_loop_action_block_reason",
        "brain_learning_effect_closure_status",
        "brain_learning_effect_closure_reason",
        "brain_learning_effect_selected_penalized",
        "brain_learning_effect_bad_proofs",
        "brain_learning_effect_good_proofs",
        "decision_quality_bucket",
        "decision_quality_reason",
        "decision_quality_should_have_acted",
    ),
}

PRODUCTION_SIGNAL_IDS: Final[tuple[str, ...]] = tuple(PRODUCTION_SIGNAL_FIELDS)

_EMPTY_VALUES: Final[tuple[Any, ...]] = (None, "", [], {})


def normalize_log_mode(value: Any) -> str:
    """Return the declared log mode, defaulting to production-minimal mode."""
    mode = str(value or DEFAULT_LOG_MODE).strip().lower().replace("-", "_")
    if mode in {"minimal", "prod"}:
        return "production"
    if mode not in LOG_MODES:
        return DEFAULT_LOG_MODE
    return mode


def _selected(row: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in fields
        if key in row and row.get(key) not in _EMPTY_VALUES
    }


def _runtime_clock_compact(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("runtime_clock")
    if not isinstance(value, Mapping):
        return _selected(row, PRODUCTION_SIGNAL_FIELDS["S036"])
    out: dict[str, Any] = {}
    for key in ("owner", "source", "clock_ms", "max_clock_ms", "sequence", "tick", "observed_ts"):
        item = value.get(key)
        if item not in _EMPTY_VALUES:
            out[f"runtime_clock_{key}"] = item
    lanes = value.get("lanes")
    if isinstance(lanes, Mapping) and lanes:
        lane_values = [lane for lane in lanes.values() if isinstance(lane, Mapping)]
        out["runtime_clock_lane_count"] = len(lane_values)
        out["runtime_clock_over_budget_total"] = int(
            sum(int(lane.get("over_budget_count", 0) or 0) for lane in lane_values)
        )
        out["runtime_clock_stale_job_total"] = int(
            sum(int(lane.get("stale_job_count", 0) or 0) for lane in lane_values)
        )
        out["runtime_clock_max_lateness_s"] = max(
            [float(lane.get("max_lateness_s", 0.0) or 0.0) for lane in lane_values] or [0.0]
        )
        out["runtime_clock_error_count"] = int(
            sum(1 for lane in lane_values if str(lane.get("last_error") or "").strip())
        )
        out["runtime_clock_top_over_budget_lanes"] = _runtime_clock_top_rows(
            lanes,
            name_key="lane",
            limit=3,
        )
    jobs = value.get("jobs")
    if isinstance(jobs, Mapping) and jobs:
        out["runtime_clock_top_over_budget_jobs"] = _runtime_clock_top_rows(
            jobs,
            name_key="job",
            limit=5,
        )
    return out


def _runtime_clock_top_rows(rows: Mapping[str, Any], *, name_key: str, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, row in rows.items():
        if not isinstance(row, Mapping):
            continue
        over_count = int(row.get("over_budget_count", 0) or 0)
        stale_count = int(row.get("stale_job_count", 0) or 0)
        last_duration_ms = float(row.get("last_run_duration_ms", 0.0) or 0.0)
        if over_count <= 0 and stale_count <= 0 and last_duration_ms <= float(row.get("budget_ms", 0.0) or 0.0):
            continue
        out.append(
            {
                name_key: str(name),
                "over_budget_count": over_count,
                "stale_job_count": stale_count,
                "run_count": int(row.get("run_count", 0) or 0),
                "last_run_duration_ms": round(last_duration_ms, 3),
                "budget_ms": round(float(row.get("budget_ms", 0.0) or 0.0), 3),
                "last_backpressure_reason": str(row.get("last_backpressure_reason", "") or ""),
            }
        )
    out.sort(
        key=lambda item: (
            int(item.get("over_budget_count", 0) or 0),
            int(item.get("stale_job_count", 0) or 0),
            float(item.get("last_run_duration_ms", 0.0) or 0.0),
        ),
        reverse=True,
    )
    return out[: max(0, int(limit or 0))]


def _modem_sample_publication_compact(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("modem_sample_publication_status")
    if not isinstance(value, Mapping):
        return _selected(row, PRODUCTION_SIGNAL_FIELDS["S037"])
    field_map = {
        "sequence": "publication_sequence",
        "sequence_gap": "publication_sequence_gap",
        "missed_total": "publication_missed_total",
        "published": "publication_published",
        "reason": "publication_reason",
        "duration_ms": "publication_duration_ms",
        "last_error": "publication_last_error",
    }
    return {
        target: value.get(source)
        for source, target in field_map.items()
        if value.get(source) not in _EMPTY_VALUES
    }


def _rat_compact(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("rat_snapshot")
    if not isinstance(value, Mapping):
        return _selected(row, PRODUCTION_SIGNAL_FIELDS["S038"])
    out: dict[str, Any] = {}
    for key in ("mode", "lte_present", "nr_present", "owner", "schema_version"):
        item = value.get(key)
        if item not in _EMPTY_VALUES:
            out[f"rat_{key}"] = item
    return out


def _smartness_compact(row: Mapping[str, Any]) -> dict[str, Any]:
    strategy = row.get("self_learning_strategy")
    if not isinstance(strategy, Mapping):
        return _selected(row, PRODUCTION_SIGNAL_FIELDS["S042"])
    out: dict[str, Any] = {}
    out["self_learning_strategy_outcome"] = strategy.get("outcome")
    out["self_learning_strategy_reason"] = strategy.get("reason") or strategy.get("reason_code")
    out["self_learning_strategy_required_next_step"] = strategy.get("required_next_step")
    decision = strategy.get("bandit_decision")
    if isinstance(decision, Mapping):
        out["smart_loop_decision_kind"] = decision.get("decision_kind")
        out["smart_loop_decision_semantic"] = decision.get("decision_semantic")
        out["smart_loop_selected_action"] = decision.get("selected_action")
        out["smart_loop_target_band"] = decision.get("target_band")
        out["smart_loop_reason_code"] = decision.get("reason_code")
        out["smart_loop_score"] = decision.get("score")
        out["smart_loop_confidence"] = decision.get("confidence")
        out["smart_loop_evidence_count"] = decision.get("evidence_count")
    counts = strategy.get("candidate_counts")
    if isinstance(counts, Mapping):
        out["smart_loop_candidate_total"] = counts.get("total")
        out["smart_loop_candidate_operational"] = counts.get("operational")
        out["smart_loop_candidate_actionable"] = counts.get("actionable")
        out["smart_loop_candidate_advisory"] = counts.get("advisory")
        out["smart_loop_candidate_blocked"] = counts.get("blocked")
    availability = strategy.get("action_availability")
    if isinstance(availability, Mapping):
        out["smart_loop_decision_allowed"] = availability.get("decision_allowed")
        out["smart_loop_action_blocker"] = availability.get("blocker")
        out["smart_loop_action_block_reason"] = availability.get("reason")
    learning_effect = row.get("brain_learning_effect")
    if isinstance(learning_effect, Mapping):
        out["brain_learning_effect_closure_status"] = learning_effect.get("smart_loop_closure_status")
        out["brain_learning_effect_closure_reason"] = learning_effect.get("smart_loop_closure_reason_code")
        out["brain_learning_effect_selected_penalized"] = learning_effect.get("selected_candidate_penalized")
        out["brain_learning_effect_bad_proofs"] = learning_effect.get("action_proof_bad_count")
        out["brain_learning_effect_good_proofs"] = learning_effect.get("action_proof_good_count")
    decision_quality = row.get("decision_quality")
    if isinstance(decision_quality, Mapping):
        out["decision_quality_bucket"] = decision_quality.get("bucket")
        out["decision_quality_reason"] = decision_quality.get("decision_quality_reason")
        out["decision_quality_should_have_acted"] = decision_quality.get("should_have_acted")
    return {key: value for key, value in out.items() if value not in _EMPTY_VALUES}


def _signal_payload(signal_id: str, row: Mapping[str, Any]) -> dict[str, Any]:
    if signal_id == "S036":
        return _runtime_clock_compact(row)
    if signal_id == "S037":
        return _modem_sample_publication_compact(row)
    if signal_id == "S038":
        return _rat_compact(row)
    if signal_id == "S042":
        return _smartness_compact(row)
    return _selected(row, PRODUCTION_SIGNAL_FIELDS[signal_id])


def build_production_telemetry_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build one compact, numbered production row from the full fact row."""
    try:
        scope = validated_evidence_scope(
            row.get("evidence_scope")
            if isinstance(row.get("evidence_scope"), Mapping)
            else {}
        )
    except (TypeError, ValueError) as exc:
        quarantine = row.get("evidence_quarantine")
        if not isinstance(quarantine, Mapping):
            quarantine = {
                "schema": "EvidenceQuarantine.v1",
                "owner": "production_telemetry_contract",
                "reason": str(exc),
            }
        return {
            "schema_version": PRODUCTION_TELEMETRY_SCHEMA_VERSION,
            "signal_version": PRODUCTION_TELEMETRY_SIGNAL_VERSION,
            "record_type": "evidence_quarantine",
            "ts": row.get("ts"),
            "iso": row.get("iso", ""),
            "tick": row.get("tick", 0),
            "evidence_quarantine": dict(quarantine),
            "signals": {},
        }
    record_type = str(row.get("record_type") or "tick")
    epochs = dict(scope.formula_vector.epochs)
    if (
        record_type == "tick"
        and scope.formula_vector != telemetry_formula_vector()
    ) or (
        record_type == "action_regrade"
        and "crystal_ball" not in epochs
    ):
        return {
            "schema_version": PRODUCTION_TELEMETRY_SCHEMA_VERSION,
            "signal_version": PRODUCTION_TELEMETRY_SIGNAL_VERSION,
            "record_type": "evidence_quarantine",
            "ts": row.get("ts"),
            "iso": row.get("iso", ""),
            "tick": row.get("tick", 0),
            "evidence_quarantine": {
                "schema": "EvidenceQuarantine.v1",
                "owner": "production_telemetry_contract",
                "reason": "production_formula_vector_mismatch",
            },
            "signals": {},
        }
    signals = {
        signal_id: _signal_payload(signal_id, row)
        for signal_id in PRODUCTION_SIGNAL_IDS
    }
    return {
        "schema_version": PRODUCTION_TELEMETRY_SCHEMA_VERSION,
        "signal_version": PRODUCTION_TELEMETRY_SIGNAL_VERSION,
        "record_type": record_type,
        "ts": row.get("ts"),
        "iso": row.get("iso", ""),
        "tick": row.get("tick", 0),
        "evidence_scope": scope.as_dict(),
        "signals": signals,
    }


def flatten_production_telemetry_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a flat named-field row from a production-numbered row.

    This is the only legal boundary adapter for consumers that need named
    fields from ``telemetry_production``.  It keeps S001-S042 ownership in this
    module instead of teaching GUI/support/report callers about numbered groups.
    Non-production rows are returned unchanged so explicit compare/debug tools
    can reuse the same import boundary.
    """

    if not isinstance(row, Mapping):
        return {}
    if row.get("schema_version") != PRODUCTION_TELEMETRY_SCHEMA_VERSION or not isinstance(row.get("signals"), Mapping):
        return dict(row)
    signals = row.get("signals")
    out: dict[str, Any] = {
        "record_type": str(row.get("record_type") or "tick"),
        "ts": row.get("ts"),
        "iso": row.get("iso", ""),
        "tick": row.get("tick", 0),
        "evidence_scope": dict(row.get("evidence_scope") or {}),
    }
    for signal_id in PRODUCTION_SIGNAL_IDS:
        payload = signals.get(signal_id) if isinstance(signals, Mapping) else None
        if not isinstance(payload, Mapping):
            continue
        for field in PRODUCTION_SIGNAL_FIELDS.get(signal_id, ()):
            if field in payload and payload.get(field) not in _EMPTY_VALUES:
                out[field] = payload.get(field)
    return out


def production_signal_field_owner(field_name: str) -> str:
    """Return the one production signal ID that owns a field, or empty string."""
    owners = [
        signal_id
        for signal_id, fields in PRODUCTION_SIGNAL_FIELDS.items()
        if field_name in fields
    ]
    if len(owners) == 1:
        return owners[0]
    return ""
