"""
Grouped config views for runtime consumers.

These views let modules depend on smaller concern-specific contracts instead
of the full flat schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from arch4.interfaces import (
    Arch4Config,
    DEFAULT_CONGESTION_RSRQ_GAP_THRESHOLD_DB,
    DEFAULT_MODEM_STARTUP_READ_TIMEOUT_S,
    DEPLOYED_BANDS,
)

def _float_cfg(cfg: Arch4Config, field: str, default: float) -> float:
    value = getattr(cfg, field, None)
    if value is None:
        return float(default)
    return float(value)

@dataclass(frozen=True)
class SignalContractConfig:
    sinr_scale: float
    rsrp_scale: float
    rsrq_scale: float
    rssi_scale: float
    contract_warn: bool


@dataclass(frozen=True)
class SignalProcessingConfig:
    mcs_idle_floor: int
    rtt_ema_alpha: float
    rtt_sliding_window: int
    rtt_warning_ms: float
    rtt_cliff_ms: float
    rsrq_window_ticks: int
    rsrq_degrading_slope: float
    v5_cq_window_samples: int
    v5_cq_min_samples: int
    v5_cq_hold_last_min_samples: int
    v5_cq_coherence_min: float
    v5_cq_mcs_positive_ratio_min: float
    v5_cq_weight_cqi: float
    v5_cq_weight_mod: float
    v5_cq_weight_ca: float
    v5_cq_weight_ri: float
    v5_cq_ri_bonus: float
    v5_cq_weight_rtt_penalty: float
    v5_cq_rtt_ref_ms: float
    v5_cq_rtt_span_ms: float
    v7_qam_integration_enabled: bool
    v7_qam_confidence_min: float
    v5_rtt_congestion_window: int
    v5_rtt_recent_window: int
    congestion_qualifier_gap_high: float
    congestion_qualifier_gap_mod: float
    congestion_qualifier_rtt_high_ms: float
    congestion_qualifier_rtt_mod_ms: float
    congestion_rtt_floor_ms: float
    congestion_rtt_ceiling_ms: float
    congestion_rsrq_gap_threshold_db: float
    congestion_rsrq_ceiling_db: float
    congestion_mcs_floor_ratio: float


@dataclass(frozen=True)
class HealthCheckConfig:
    ping_rtt_max_ms: float
    ping_fail_threshold: int
    ping_recover_threshold: int
    hold_down_s: float
    multi_target_enabled: bool
    multi_min_ok_targets: int


@dataclass(frozen=True)
class TelemetryConfig:
    rollup_enabled: bool


@dataclass(frozen=True)
class ControllerPostureConfig:
    emergency_radio_down_persist_s: float
    emergency_sinr_floor: float
    emergency_observe_s: float
    emergency_sinr_persist_s: float
    emergency_congestion_persist_s: float
    rtt_warning_ms: float
    rtt_cliff_ms: float
    rtt_warning_clear_ms: float
    congestion_rf_ok_sinr_db: float
    congestion_rf_ok_rsrp_dbm: float
    congestion_self_load_dl_mbps: float
    congestion_suspect_entry_ticks: int
    congestion_suspect_recurrence_hits: int
    congestion_suspect_recurrence_window_s: float
    degraded_entry_ticks: int
    degraded_recovery_ticks: int
    degraded_idle_exit_ticks: int
    degraded_timeout_s: float
    congestion_confirm_cooldown_s: float
    congestion_confirm_probe_ticks: int
    congestion_confirm_bad_ratio: float
    congestion_suspect_clear_ticks: int
    congestion_confirm_clear_ticks: int
    sinr_feasibility_gate_db: float
    emergency_recovery_stable_s: float
    emergency_reentry_guard_s: float
    emergency_timeout_s: float
    degraded_reentry_guard_s: float
    soft_emergency_hold_s: float
    v5_low_rf_sinr_gate_db: float
    v5_cq_confidence_min_for_state: float
    v5_cq_bad_threshold: float
    v5_cq_good_threshold: float
    congestion_qualifier_rtt_high_ms: float
    congestion_qualifier_rtt_mod_ms: float
    congestion_qualifier_window_ticks: int
    congestion_qualifier_persist_hits: int
    congestion_qualifier_clear_ticks: int
    congestion_qualifier_rsrq_gap_db: float


@dataclass(frozen=True)
class DecisionStateConfig:
    v5_good_dl_hold_enabled: bool
    v5_good_dl_hold_mbps: float
    rtt_warning_ms: float
    sinr_feasibility_gate_db: float
    hilink_priority_rotate_verify_s: float
    congested_check_interval_s: float
    congestion_mitigation_cooldown_s: float
    preemptive_switch_cooldown_s: float
    emergency_step_wait_s: float


@dataclass(frozen=True)
class DecisionPipelineConfig:
    min_action_interval_s: float
    soft_congestion_enabled: bool
    v5_low_rf_sinr_gate_db: float
    congestion_rf_ok_sinr_db: float
    congestion_rf_ok_rsrp_dbm: float
    soft_congestion_min_dl_mbps: float
    v5_cq_confidence_min_for_state: float
    congestion_qualifier_rtt_high_ms: float
    congestion_qualifier_rtt_mod_ms: float
    soft_congestion_entry_ticks: int
    soft_congestion_clear_ticks: int
    objective_feedback_no_effect_block_s: float
    ca_lost_window_s: float
    sinr_feasibility_gate_db: float
    ca_lost_on_threshold: float
    ca_lost_off_threshold: float
    global_band_churn_window_s: float
    global_band_churn_emergency_max_actions: int
    global_band_churn_max_actions: int
    global_band_churn_emergency_cooldown_s: float
    global_band_churn_cooldown_s: float
    ca_lost_eval_guard_s: float
    restore_ca_retry_after_s: float
    restore_ca_repeat_block_s: float
    explore_repeat_block_s: float
    band_action_fail_limit: int
    band_action_cooldown_s: float
    cell_lock_retry_after_s: float
    restart_retry_after_s: float
    escape_probe_enabled: bool
    escape_probe_require_idle: bool
    escape_probe_suppression_window_s: float
    escape_probe_suppression_min_count: int
    escape_probe_bypassable_share_min: float
    escape_probe_quality_confidence_max: float
    escape_probe_posture_modifier_max: float
    escape_probe_global_interval_s: float
    escape_probe_context_interval_s: float
    escape_probe_rf_cliff_sinr_min_db: float
    escape_probe_rf_cliff_sinr_ticks: int


@dataclass(frozen=True)
class IncidentAssessmentConfig:
    emergency_sinr_floor: float
    sinr_feasibility_gate_db: float
    soft_congestion_lq_threshold: float
    rtt_warning_ms: float
    modem_backend: str


@dataclass(frozen=True)
class BouncerConfig:
    rtt_warning_ms: float
    rtt_cliff_ms: float
    stable_cq_threshold: float
    stable_sinr_db: float


@dataclass(frozen=True)
class HistoricalConfig:
    hourly_pattern_min_samples: int
    degradation_window_rtt_ms: float
    degradation_window_throughput_mbps: float
    degradation_window_severe_rtt_ms: float


@dataclass(frozen=True)
class LearningAuthorityConfig:
    emergency_sinr_floor: float
    v5_low_rf_sinr_gate_db: float


@dataclass(frozen=True)
class MemoryLearningConfig:
    band_never_works_threshold: int
    band_retry_interval_s: float
    calibration_db_enabled: bool
    calibration_db_retention_days: int
    emergency_sinr_floor: float
    rtt_cliff_ms: float
    night_probe_trigger_bad_rf_sinr_db: float
    sinr_feasibility_gate_db: float
    learning_ltm_decay: float
    learning_stm_window_s: float
    learning_marker_warmup_attempts: int
    learning_context_compare_delay_s: float
    learning_context_similarity_threshold: float
    learning_enabled: bool
    learning_retry_min_elapsed_s: float
    learning_retry_sinr_improvement_db: float
    learning_stm_hard_fail_block_count: int
    learning_stm_hard_fail_block_s: float
    learning_stm_min_attempts: int
    learning_stm_bad_rate_threshold: float
    learning_ltm_min_attempts: int
    learning_ltm_bad_rate_threshold: float
    probe_exhaustion_min_attempts: float
    probe_exhaustion_score_floor: float
    probe_exhaustion_recency_s: float


@dataclass(frozen=True)
class HuaweiConnectionConfig:
    host: str
    user: str
    password: str
    ping_interface: str
    expected_model: str
    expected_serial: str
    expected_firmware: str
    expected_imei: str


@dataclass(frozen=True)
class HuaweiRuntimeConfig:
    netmode_submit_timeout_s: float
    netmode_continue_on_read_timeout_for_verification: bool
    sequential_step_wait_s: float
    pband_wait_s: float


@dataclass(frozen=True)
class ZteConnectionConfig:
    host: str
    user: str
    password: str
    ping_interface: str
    expected_model: str
    expected_serial: str
    expected_firmware: str
    expected_imei: str


MF296RConnectionConfig = ZteConnectionConfig


@dataclass(frozen=True)
class MikroTikConnectionConfig:
    host: str
    user: str
    password: str
    port: int
    api_timeout_s: float


@dataclass(frozen=True)
class MikroTikRuntimeConfig:
    rf_monitor_interval_s: float
    qcainfo_interval_s: float
    qeng_mcs_interval_s: float
    qnwcfg_mcs_interval_s: float
    serving_cell_extra_interval_s: float
    neighbor_scan_interval_s: float
    wifi_status_interval_s: float
    at_live_polling_enabled: bool
    runtime_capability_probe_enabled: bool
    at_read_delay_s: float
    at_write_delay_s: float
    at_bus_guard_s: float
    at_bus_guard_max_s: float
    at_bus_guard_step_s: float
    adaptive_bus_guard: bool
    at_latency_warn_ms: float
    post_write_blackout_s: float
    action_min_session_uptime_s: float
    capability_probe_interval_s: float


@dataclass(frozen=True)
class StartupDisplayConfig:
    modem_backend: str
    host_display: str
    connect_error_host: str
    deployed_bands: list[int]


@dataclass(frozen=True)
class BackendConnectionConfig:
    backend: str
    host: str
    user: str
    password: str
    ping_interface: str
    transport: str
    expected_model: str
    expected_serial: str
    expected_firmware: str
    expected_imei: str


@dataclass(frozen=True)
class OpenWrtConnectionConfig:
    host: str
    user: str
    password: str
    modem_id: str
    ping_interface: str
    expected_model: str
    expected_serial: str
    expected_firmware: str
    expected_imei: str


@dataclass(frozen=True)
class ZyxelConnectionConfig:
    host: str
    user: str
    password: str
    ping_interface: str
    expected_model: str
    expected_serial: str
    expected_firmware: str
    expected_imei: str


@dataclass(frozen=True)
class ModelConnectionConfig:
    profile_name: str
    profile_path: str


def build_signal_contract_config(cfg: Arch4Config) -> SignalContractConfig:
    return SignalContractConfig(
        sinr_scale=float(getattr(cfg, "signal_sinr_scale", 1.0) or 1.0),
        rsrp_scale=float(getattr(cfg, "signal_rsrp_scale", 1.0) or 1.0),
        rsrq_scale=float(getattr(cfg, "signal_rsrq_scale", 1.0) or 1.0),
        rssi_scale=float(getattr(cfg, "signal_rssi_scale", 1.0) or 1.0),
        contract_warn=bool(getattr(cfg, "signal_contract_warn", True)),
    )


def build_signal_processing_config(cfg: Arch4Config) -> SignalProcessingConfig:
    return SignalProcessingConfig(
        mcs_idle_floor=int(getattr(cfg, "mcs_idle_floor", 5) or 5),
        rtt_ema_alpha=float(getattr(cfg, "rtt_ema_alpha", 0.20) or 0.20),
        rtt_sliding_window=max(1, int(getattr(cfg, "rtt_sliding_window", 13) or 13)),
        rtt_warning_ms=float(getattr(cfg, "rtt_warning_ms", 150.0) or 150.0),
        rtt_cliff_ms=float(getattr(cfg, "rtt_cliff_ms", 400.0) or 400.0),
        rsrq_window_ticks=max(1, int(getattr(cfg, "rsrq_window_ticks", 30) or 30)),
        rsrq_degrading_slope=float(getattr(cfg, "rsrq_degrading_slope", -0.05) or -0.05),
        v5_cq_window_samples=max(3, int(getattr(cfg, "v5_cq_window_samples", 15) or 15)),
        v5_cq_min_samples=max(1, int(getattr(cfg, "v5_cq_min_samples", 6) or 6)),
        v5_cq_hold_last_min_samples=max(1, int(getattr(cfg, "v5_cq_hold_last_min_samples", 2) or 2)),
        v5_cq_coherence_min=float(getattr(cfg, "v5_cq_coherence_min", 0.55) or 0.55),
        v5_cq_mcs_positive_ratio_min=float(getattr(cfg, "v5_cq_mcs_positive_ratio_min", 0.50) or 0.50),
        v5_cq_weight_cqi=float(getattr(cfg, "v5_cq_weight_cqi", 0.30) or 0.30),
        v5_cq_weight_mod=float(getattr(cfg, "v5_cq_weight_mod", 0.30) or 0.30),
        v5_cq_weight_ca=float(getattr(cfg, "v5_cq_weight_ca", 0.30) or 0.30),
        v5_cq_weight_ri=float(getattr(cfg, "v5_cq_weight_ri", 0.10) or 0.10),
        v5_cq_ri_bonus=float(getattr(cfg, "v5_cq_ri_bonus", 0.10) or 0.10),
        v5_cq_weight_rtt_penalty=float(getattr(cfg, "v5_cq_weight_rtt_penalty", 0.10) or 0.10),
        v5_cq_rtt_ref_ms=float(getattr(cfg, "v5_cq_rtt_ref_ms", 60.0) or 60.0),
        v5_cq_rtt_span_ms=max(1.0, float(getattr(cfg, "v5_cq_rtt_span_ms", 120.0) or 120.0)),
        v7_qam_integration_enabled=bool(getattr(cfg, "v7_qam_integration_enabled", True)),
        v7_qam_confidence_min=float(getattr(cfg, "v7_qam_confidence_min", 0.55) or 0.55),
        v5_rtt_congestion_window=max(3, int(getattr(cfg, "v5_rtt_congestion_window", 20) or 20)),
        v5_rtt_recent_window=max(1, int(getattr(cfg, "v5_rtt_recent_window", 3) or 3)),
        congestion_qualifier_gap_high=float(getattr(cfg, "congestion_qualifier_gap_high", 0.50) or 0.50),
        congestion_qualifier_gap_mod=float(getattr(cfg, "congestion_qualifier_gap_mod", 0.25) or 0.25),
        congestion_qualifier_rtt_high_ms=float(getattr(cfg, "congestion_qualifier_rtt_high_ms", 95.0) or 95.0),
        congestion_qualifier_rtt_mod_ms=float(getattr(cfg, "congestion_qualifier_rtt_mod_ms", 75.0) or 75.0),
        congestion_rtt_floor_ms=max(0.0, float(getattr(cfg, "congestion_rtt_floor_ms", 5.0) or 5.0)),
        congestion_rtt_ceiling_ms=max(1.0, float(getattr(cfg, "congestion_rtt_ceiling_ms", 150.0) or 150.0)),
        congestion_rsrq_gap_threshold_db=max(
            0.0,
            _float_cfg(
                cfg,
                "congestion_rsrq_gap_threshold_db",
                DEFAULT_CONGESTION_RSRQ_GAP_THRESHOLD_DB,
            ),
        ),
        congestion_rsrq_ceiling_db=max(0.1, float(getattr(cfg, "congestion_rsrq_ceiling_db", 6.0) or 6.0)),
        congestion_mcs_floor_ratio=max(0.0, min(1.0, float(getattr(cfg, "congestion_mcs_floor_ratio", 0.05) or 0.05))),
    )


def build_health_check_config(cfg: Arch4Config) -> HealthCheckConfig:
    return HealthCheckConfig(
        ping_rtt_max_ms=float(getattr(cfg, "health_ping_rtt_max_ms", 800.0) or 800.0),
        ping_fail_threshold=max(1, int(getattr(cfg, "health_ping_fail_threshold", 3) or 3)),
        ping_recover_threshold=max(1, int(getattr(cfg, "health_ping_recover_threshold", 1) or 1)),
        hold_down_s=max(0.0, float(getattr(cfg, "health_hold_down_s", 180.0) or 180.0)),
        multi_target_enabled=bool(getattr(cfg, "health_multi_target_enabled", False)),
        multi_min_ok_targets=max(1, int(getattr(cfg, "health_multi_min_ok_targets", 2) or 2)),
    )


def build_telemetry_config(cfg: Arch4Config) -> TelemetryConfig:
    return TelemetryConfig(
        rollup_enabled=bool(getattr(cfg, "telemetry_rollup_enabled", True)),
    )


def build_controller_posture_config(cfg: Arch4Config) -> ControllerPostureConfig:
    return ControllerPostureConfig(
        emergency_radio_down_persist_s=float(getattr(cfg, "emergency_radio_down_persist_s", 10.0) or 10.0),
        emergency_sinr_floor=_float_cfg(cfg, "emergency_sinr_floor", 3.0),
        emergency_observe_s=float(getattr(cfg, "emergency_observe_s", 8.0) or 8.0),
        emergency_sinr_persist_s=float(getattr(cfg, "emergency_sinr_persist_s", 20.0) or 20.0),
        emergency_congestion_persist_s=float(getattr(cfg, "emergency_congestion_persist_s", 300.0) or 300.0),
        rtt_warning_ms=float(getattr(cfg, "rtt_warning_ms", 150.0) or 150.0),
        rtt_cliff_ms=float(getattr(cfg, "rtt_cliff_ms", 400.0) or 400.0),
        rtt_warning_clear_ms=float(getattr(cfg, "rtt_warning_clear_ms", 120.0) or 120.0),
        congestion_rf_ok_sinr_db=float(getattr(cfg, "congestion_rf_ok_sinr_db", 6.0) or 6.0),
        congestion_rf_ok_rsrp_dbm=float(getattr(cfg, "congestion_rf_ok_rsrp_dbm", -90.0) or -90.0),
        congestion_self_load_dl_mbps=float(getattr(cfg, "congestion_self_load_dl_mbps", 10.0) or 10.0),
        congestion_suspect_entry_ticks=max(1, int(getattr(cfg, "congestion_suspect_entry_ticks", 3) or 3)),
        congestion_suspect_recurrence_hits=max(1, int(getattr(cfg, "congestion_suspect_recurrence_hits", 3) or 3)),
        congestion_suspect_recurrence_window_s=float(
            getattr(cfg, "congestion_suspect_recurrence_window_s", 300.0) or 300.0
        ),
        degraded_entry_ticks=max(1, int(getattr(cfg, "degraded_entry_ticks", 10) or 10)),
        degraded_recovery_ticks=max(1, int(getattr(cfg, "degraded_recovery_ticks", 15) or 15)),
        degraded_idle_exit_ticks=max(1, int(getattr(cfg, "degraded_idle_exit_ticks", 15) or 15)),
        degraded_timeout_s=float(getattr(cfg, "degraded_timeout_s", 300.0) or 300.0),
        congestion_confirm_cooldown_s=float(getattr(cfg, "congestion_confirm_cooldown_s", 120.0) or 120.0),
        congestion_confirm_probe_ticks=max(1, int(getattr(cfg, "congestion_confirm_probe_ticks", 4) or 4)),
        congestion_confirm_bad_ratio=float(getattr(cfg, "congestion_confirm_bad_ratio", 0.6) or 0.6),
        congestion_suspect_clear_ticks=max(1, int(getattr(cfg, "congestion_suspect_clear_ticks", 3) or 3)),
        congestion_confirm_clear_ticks=max(1, int(getattr(cfg, "congestion_confirm_clear_ticks", 5) or 5)),
        sinr_feasibility_gate_db=float(getattr(cfg, "sinr_feasibility_gate_db", 10.0) or 10.0),
        emergency_recovery_stable_s=float(getattr(cfg, "emergency_recovery_stable_s", 20.0) or 20.0),
        emergency_reentry_guard_s=float(getattr(cfg, "emergency_reentry_guard_s", 120.0) or 120.0),
        emergency_timeout_s=float(getattr(cfg, "emergency_timeout_s", 120.0) or 120.0),
        degraded_reentry_guard_s=float(getattr(cfg, "degraded_reentry_guard_s", 30.0) or 30.0),
        soft_emergency_hold_s=float(getattr(cfg, "soft_emergency_hold_s", 900.0) or 900.0),
        v5_low_rf_sinr_gate_db=float(getattr(cfg, "v5_low_rf_sinr_gate_db", 4.0) or 4.0),
        v5_cq_confidence_min_for_state=float(getattr(cfg, "v5_cq_confidence_min_for_state", 0.45) or 0.45),
        v5_cq_bad_threshold=float(getattr(cfg, "v5_cq_bad_threshold", 0.42) or 0.42),
        v5_cq_good_threshold=float(getattr(cfg, "v5_cq_good_threshold", 0.55) or 0.55),
        congestion_qualifier_rtt_high_ms=float(getattr(cfg, "congestion_qualifier_rtt_high_ms", 95.0) or 95.0),
        congestion_qualifier_rtt_mod_ms=float(getattr(cfg, "congestion_qualifier_rtt_mod_ms", 75.0) or 75.0),
        congestion_qualifier_window_ticks=max(
            1, int(getattr(cfg, "congestion_qualifier_window_ticks", 10) or 10)
        ),
        congestion_qualifier_persist_hits=max(
            1, int(getattr(cfg, "congestion_qualifier_persist_hits", 8) or 8)
        ),
        congestion_qualifier_clear_ticks=max(
            1, int(getattr(cfg, "congestion_qualifier_clear_ticks", 3) or 3)
        ),
        congestion_qualifier_rsrq_gap_db=float(
            getattr(cfg, "congestion_qualifier_rsrq_gap_db", 4.0) or 4.0
        ),
    )


def build_decision_state_config(cfg: Arch4Config) -> DecisionStateConfig:
    return DecisionStateConfig(
        v5_good_dl_hold_enabled=bool(getattr(cfg, "v5_good_dl_hold_enabled", True)),
        v5_good_dl_hold_mbps=float(getattr(cfg, "v5_good_dl_hold_mbps", 60.0) or 60.0),
        rtt_warning_ms=float(getattr(cfg, "rtt_warning_ms", 150.0) or 150.0),
        sinr_feasibility_gate_db=float(getattr(cfg, "sinr_feasibility_gate_db", 10.0) or 10.0),
        hilink_priority_rotate_verify_s=float(
            getattr(cfg, "hilink_priority_rotate_verify_s", 600.0) or 600.0
        ),
        congested_check_interval_s=float(getattr(cfg, "congested_check_interval_s", 150.0) or 150.0),
        congestion_mitigation_cooldown_s=_float_cfg(cfg, "congestion_mitigation_cooldown_s", 1800.0),
        preemptive_switch_cooldown_s=_float_cfg(cfg, "preemptive_switch_cooldown_s", 1800.0),
        emergency_step_wait_s=float(getattr(cfg, "emergency_step_wait_s", 20.0) or 20.0),
    )


def build_decision_pipeline_config(cfg: Arch4Config) -> DecisionPipelineConfig:
    return DecisionPipelineConfig(
        min_action_interval_s=_float_cfg(cfg, "min_action_interval_s", 30.0),
        soft_congestion_enabled=bool(getattr(cfg, "soft_congestion_enabled", True)),
        v5_low_rf_sinr_gate_db=float(getattr(cfg, "v5_low_rf_sinr_gate_db", 4.0) or 4.0),
        congestion_rf_ok_sinr_db=float(getattr(cfg, "congestion_rf_ok_sinr_db", 6.0) or 6.0),
        congestion_rf_ok_rsrp_dbm=float(getattr(cfg, "congestion_rf_ok_rsrp_dbm", -90.0) or -90.0),
        soft_congestion_min_dl_mbps=float(getattr(cfg, "soft_congestion_min_dl_mbps", 0.8) or 0.8),
        v5_cq_confidence_min_for_state=float(
            getattr(cfg, "v5_cq_confidence_min_for_state", 0.45) or 0.45
        ),
        congestion_qualifier_rtt_high_ms=float(
            getattr(cfg, "congestion_qualifier_rtt_high_ms", 95.0) or 95.0
        ),
        congestion_qualifier_rtt_mod_ms=float(
            getattr(cfg, "congestion_qualifier_rtt_mod_ms", 75.0) or 75.0
        ),
        soft_congestion_entry_ticks=max(1, int(getattr(cfg, "soft_congestion_entry_ticks", 5) or 5)),
        soft_congestion_clear_ticks=max(1, int(getattr(cfg, "soft_congestion_clear_ticks", 3) or 3)),
        objective_feedback_no_effect_block_s=float(
            getattr(cfg, "objective_feedback_no_effect_block_s", 900.0) or 900.0
        ),
        ca_lost_window_s=_float_cfg(cfg, "ca_lost_window_s", 20.0),
        sinr_feasibility_gate_db=float(getattr(cfg, "sinr_feasibility_gate_db", 10.0) or 10.0),
        ca_lost_on_threshold=float(getattr(cfg, "ca_lost_on_threshold", 0.1) or 0.1),
        ca_lost_off_threshold=float(getattr(cfg, "ca_lost_off_threshold", 0.5) or 0.5),
        global_band_churn_window_s=_float_cfg(cfg, "global_band_churn_window_s", 600.0),
        global_band_churn_emergency_max_actions=max(
            1, int(getattr(cfg, "global_band_churn_emergency_max_actions", 3) or 3)
        ),
        global_band_churn_max_actions=max(1, int(getattr(cfg, "global_band_churn_max_actions", 6) or 6)),
        global_band_churn_emergency_cooldown_s=_float_cfg(
            cfg, "global_band_churn_emergency_cooldown_s", 3600.0
        ),
        global_band_churn_cooldown_s=_float_cfg(cfg, "global_band_churn_cooldown_s", 1800.0),
        ca_lost_eval_guard_s=_float_cfg(cfg, "ca_lost_eval_guard_s", 30.0),
        restore_ca_retry_after_s=_float_cfg(cfg, "restore_ca_retry_after_s", 1800.0),
        restore_ca_repeat_block_s=_float_cfg(cfg, "restore_ca_repeat_block_s", 3600.0),
        explore_repeat_block_s=_float_cfg(cfg, "explore_repeat_block_s", 3600.0),
        band_action_fail_limit=max(1, int(getattr(cfg, "band_action_fail_limit", 3) or 3)),
        band_action_cooldown_s=_float_cfg(cfg, "band_action_cooldown_s", 3600.0),
        cell_lock_retry_after_s=_float_cfg(cfg, "cell_lock_retry_after_s", 900.0),
        restart_retry_after_s=_float_cfg(cfg, "restart_retry_after_s", 1800.0),
        escape_probe_enabled=bool(getattr(cfg, "escape_probe_enabled", True)),
        escape_probe_require_idle=bool(getattr(cfg, "escape_probe_require_idle", True)),
        escape_probe_suppression_window_s=float(
            getattr(cfg, "escape_probe_suppression_window_s", 7200.0) or 7200.0
        ),
        escape_probe_suppression_min_count=max(
            1, int(getattr(cfg, "escape_probe_suppression_min_count", 50) or 50)
        ),
        escape_probe_bypassable_share_min=max(
            0.0,
            min(1.0, float(getattr(cfg, "escape_probe_bypassable_share_min", 0.60) or 0.60)),
        ),
        escape_probe_quality_confidence_max=max(
            0.0,
            min(1.0, float(getattr(cfg, "escape_probe_quality_confidence_max", 0.50) or 0.50)),
        ),
        escape_probe_posture_modifier_max=max(
            0.0,
            float(getattr(cfg, "escape_probe_posture_modifier_max", 0.70) or 0.70),
        ),
        escape_probe_global_interval_s=max(
            0.0, float(getattr(cfg, "escape_probe_global_interval_s", 1800.0) or 1800.0)
        ),
        escape_probe_context_interval_s=max(
            0.0, float(getattr(cfg, "escape_probe_context_interval_s", 5400.0) or 5400.0)
        ),
        escape_probe_rf_cliff_sinr_min_db=float(
            getattr(cfg, "escape_probe_rf_cliff_sinr_min_db", 3.0) or 3.0
        ),
        escape_probe_rf_cliff_sinr_ticks=max(
            1, int(getattr(cfg, "escape_probe_rf_cliff_sinr_ticks", 2) or 2)
        ),
    )


def build_incident_assessment_config(cfg: Arch4Config) -> IncidentAssessmentConfig:
    return IncidentAssessmentConfig(
        emergency_sinr_floor=_float_cfg(cfg, "emergency_sinr_floor", 3.0),
        sinr_feasibility_gate_db=float(getattr(cfg, "sinr_feasibility_gate_db", 10.0) or 10.0),
        soft_congestion_lq_threshold=float(getattr(cfg, "soft_congestion_lq_threshold", 0.2) or 0.2),
        rtt_warning_ms=float(getattr(cfg, "rtt_warning_ms", 150.0) or 150.0),
        modem_backend=str(getattr(cfg, "modem_backend", "") or ""),
    )


def build_bouncer_config(cfg: Arch4Config) -> BouncerConfig:
    stable_cq = getattr(cfg, "bouncer_stable_cq_threshold", None)
    if stable_cq is None:
        stable_cq = getattr(cfg, "bouncer_stable_lq_threshold", 0.65)
    return BouncerConfig(
        rtt_warning_ms=float(getattr(cfg, "rtt_warning_ms", 150.0) or 150.0),
        rtt_cliff_ms=float(getattr(cfg, "rtt_cliff_ms", 400.0) or 400.0),
        stable_cq_threshold=float(stable_cq or 0.65),
        stable_sinr_db=float(getattr(cfg, "bouncer_stable_sinr_db", 8.0) or 8.0),
    )


def build_historical_config(cfg: Arch4Config) -> HistoricalConfig:
    return HistoricalConfig(
        hourly_pattern_min_samples=max(1, int(getattr(cfg, "hourly_pattern_min_samples", 10) or 10)),
        degradation_window_rtt_ms=float(
            getattr(cfg, "degradation_window_rtt_ms", 100.0) or 100.0
        ),
        degradation_window_throughput_mbps=float(
            getattr(cfg, "degradation_window_throughput_mbps", 5.0) or 5.0
        ),
        degradation_window_severe_rtt_ms=float(
            getattr(cfg, "degradation_window_severe_rtt_ms", 200.0) or 200.0
        ),
    )


def build_learning_authority_config(cfg: Arch4Config) -> LearningAuthorityConfig:
    return LearningAuthorityConfig(
        emergency_sinr_floor=_float_cfg(cfg, "emergency_sinr_floor", 3.0),
        v5_low_rf_sinr_gate_db=float(getattr(cfg, "v5_low_rf_sinr_gate_db", 4.0) or 4.0),
    )


def build_memory_learning_config(cfg: Arch4Config) -> MemoryLearningConfig:
    marker_warmup = getattr(cfg, "learning_marker_warmup_attempts", 2)
    return MemoryLearningConfig(
        band_never_works_threshold=max(1, int(getattr(cfg, "band_never_works_threshold", 5) or 5)),
        band_retry_interval_s=float(getattr(cfg, "band_retry_interval_s", 3600.0) or 3600.0),
        calibration_db_enabled=bool(getattr(cfg, "calibration_db_enabled", True)),
        calibration_db_retention_days=max(1, int(getattr(cfg, "calibration_db_retention_days", 120) or 120)),
        emergency_sinr_floor=_float_cfg(cfg, "emergency_sinr_floor", 3.0),
        rtt_cliff_ms=float(getattr(cfg, "rtt_cliff_ms", 400.0) or 400.0),
        night_probe_trigger_bad_rf_sinr_db=float(
            getattr(cfg, "night_probe_trigger_bad_rf_sinr_db", 5.0) or 5.0
        ),
        sinr_feasibility_gate_db=float(getattr(cfg, "sinr_feasibility_gate_db", 10.0) or 10.0),
        learning_ltm_decay=float(getattr(cfg, "learning_ltm_decay", 0.990) or 0.990),
        learning_stm_window_s=float(getattr(cfg, "learning_stm_window_s", 21600.0) or 21600.0),
        learning_marker_warmup_attempts=max(0, int(2 if marker_warmup is None else marker_warmup)),
        learning_context_compare_delay_s=float(
            getattr(cfg, "learning_context_compare_delay_s", 86400.0) or 86400.0
        ),
        learning_context_similarity_threshold=float(
            getattr(cfg, "learning_context_similarity_threshold", 0.92) or 0.92
        ),
        learning_enabled=bool(getattr(cfg, "learning_enabled", True)),
        learning_retry_min_elapsed_s=float(getattr(cfg, "learning_retry_min_elapsed_s", 1800.0) or 1800.0),
        learning_retry_sinr_improvement_db=float(
            getattr(cfg, "learning_retry_sinr_improvement_db", 4.0) or 4.0
        ),
        learning_stm_hard_fail_block_count=max(
            1, int(getattr(cfg, "learning_stm_hard_fail_block_count", 1) or 1)
        ),
        learning_stm_hard_fail_block_s=float(
            getattr(cfg, "learning_stm_hard_fail_block_s", 7200.0) or 7200.0
        ),
        learning_stm_min_attempts=max(1, int(getattr(cfg, "learning_stm_min_attempts", 2) or 2)),
        learning_stm_bad_rate_threshold=float(
            getattr(cfg, "learning_stm_bad_rate_threshold", 0.60) or 0.60
        ),
        learning_ltm_min_attempts=max(1, int(getattr(cfg, "learning_ltm_min_attempts", 4) or 4)),
        learning_ltm_bad_rate_threshold=float(
            getattr(cfg, "learning_ltm_bad_rate_threshold", 0.70) or 0.70
        ),
        probe_exhaustion_min_attempts=float(
            getattr(cfg, "probe_exhaustion_min_attempts", 5.0) or 5.0
        ),
        probe_exhaustion_score_floor=float(
            getattr(cfg, "probe_exhaustion_score_floor", 0.35) or 0.35
        ),
        probe_exhaustion_recency_s=float(
            getattr(cfg, "probe_exhaustion_recency_s", 7200.0) or 7200.0
        ),
    )


def build_huawei_connection_config(cfg: Arch4Config) -> HuaweiConnectionConfig:
    backend = str(getattr(cfg, "modem_backend", "") or "").strip().lower()
    conn = _build_backend_connection_config(cfg, backend_override=backend or "b818")
    return HuaweiConnectionConfig(
        host=conn.host,
        user=conn.user,
        password=conn.password,
        ping_interface=conn.ping_interface,
        expected_model=conn.expected_model,
        expected_serial=conn.expected_serial,
        expected_firmware=conn.expected_firmware,
        expected_imei=conn.expected_imei,
    )


def build_huawei_runtime_config(cfg: Arch4Config) -> HuaweiRuntimeConfig:
    hilink_pband_wait_s = float(getattr(cfg, "hilink_pband_wait_s", 5.0) or 5.0)
    return HuaweiRuntimeConfig(
        netmode_submit_timeout_s=float(getattr(cfg, "hilink_netmode_submit_timeout_s", 2.5) or 2.5),
        netmode_continue_on_read_timeout_for_verification=bool(
            getattr(
                cfg,
                "hilink_netmode_continue_on_read_timeout_for_verification",
                True,
            )
        ),
        sequential_step_wait_s=float(getattr(cfg, "hilink_sequential_step_wait_s", 20.0) or 20.0),
        pband_wait_s=hilink_pband_wait_s,
    )


def build_zte_connection_config(cfg: Arch4Config) -> ZteConnectionConfig:
    conn = _build_backend_connection_config(cfg, backend_override="mc888")
    return ZteConnectionConfig(
        host=conn.host,
        user=conn.user,
        password=conn.password,
        ping_interface=conn.ping_interface,
        expected_model=conn.expected_model,
        expected_serial=conn.expected_serial,
        expected_firmware=conn.expected_firmware,
        expected_imei=conn.expected_imei,
    )


def build_mikrotik_connection_config(cfg: Arch4Config) -> MikroTikConnectionConfig:
    conn = _build_backend_connection_config(cfg, backend_override="mikrotik")
    return MikroTikConnectionConfig(
        host=conn.host,
        user=conn.user,
        password=conn.password,
        port=int(getattr(cfg, "router_port", 8728) or 8728),
        api_timeout_s=float(getattr(cfg, "router_api_timeout_s", 30.0) or 30.0),
    )


def build_openwrt_connection_config(cfg: Arch4Config) -> OpenWrtConnectionConfig:
    conn = _build_backend_connection_config(cfg, backend_override="openwrt")
    return OpenWrtConnectionConfig(
        host=conn.host,
        user=conn.user,
        password=conn.password,
        modem_id=str(getattr(cfg, "openwrt_modem_id", "") or ""),
        ping_interface=conn.ping_interface,
        expected_model=conn.expected_model,
        expected_serial=conn.expected_serial,
        expected_firmware=conn.expected_firmware,
        expected_imei=conn.expected_imei,
    )


def build_zyxel_connection_config(cfg: Arch4Config) -> ZyxelConnectionConfig:
    conn = _build_backend_connection_config(cfg, backend_override="nr5101")
    return ZyxelConnectionConfig(
        host=conn.host or "192.168.1.1",
        user=conn.user or "admin",
        password=conn.password,
        ping_interface=conn.ping_interface,
        expected_model=conn.expected_model,
        expected_serial=conn.expected_serial,
        expected_firmware=conn.expected_firmware,
        expected_imei=conn.expected_imei,
    )


def build_model_connection_config(cfg: Arch4Config) -> ModelConnectionConfig:
    return ModelConnectionConfig(
        profile_name=str(getattr(cfg, "model_profile_name", "") or ""),
        profile_path=str(getattr(cfg, "model_profile_path", "") or ""),
    )


def build_mikrotik_runtime_config(cfg: Arch4Config) -> MikroTikRuntimeConfig:
    return MikroTikRuntimeConfig(
        rf_monitor_interval_s=float(getattr(cfg, "mikrotik_rf_monitor_interval_s", 3.0) or 3.0),
        qcainfo_interval_s=float(getattr(cfg, "mikrotik_qcainfo_interval_s", 8.0) or 8.0),
        qeng_mcs_interval_s=float(getattr(cfg, "mikrotik_qeng_mcs_interval_s", 15.0) or 15.0),
        qnwcfg_mcs_interval_s=float(getattr(cfg, "mikrotik_qnwcfg_mcs_interval_s", 6.0) or 6.0),
        serving_cell_extra_interval_s=float(
            getattr(cfg, "mikrotik_serving_cell_extra_interval_s", 10.0) or 10.0
        ),
        neighbor_scan_interval_s=float(getattr(cfg, "mikrotik_neighbor_scan_interval_s", 30.0) or 30.0),
        wifi_status_interval_s=float(getattr(cfg, "mikrotik_wifi_status_interval_s", 5.0) or 5.0),
        at_live_polling_enabled=bool(getattr(cfg, "mikrotik_at_live_polling_enabled", False)),
        runtime_capability_probe_enabled=bool(
            getattr(cfg, "mikrotik_runtime_capability_probe_enabled", False)
        ),
        at_read_delay_s=float(getattr(cfg, "mikrotik_at_read_delay_s", 1.0) or 1.0),
        at_write_delay_s=float(getattr(cfg, "mikrotik_at_write_delay_s", 2.5) or 2.5),
        at_bus_guard_s=float(getattr(cfg, "mikrotik_at_bus_guard_s", 0.3) or 0.3),
        at_bus_guard_max_s=float(getattr(cfg, "mikrotik_at_bus_guard_max_s", 0.8) or 0.8),
        at_bus_guard_step_s=float(getattr(cfg, "mikrotik_at_bus_guard_step_s", 0.1) or 0.1),
        adaptive_bus_guard=bool(getattr(cfg, "mikrotik_adaptive_bus_guard", True)),
        at_latency_warn_ms=float(getattr(cfg, "mikrotik_at_latency_warn_ms", 1500.0) or 1500.0),
        post_write_blackout_s=float(getattr(cfg, "mikrotik_post_write_blackout_s", 10.0) or 10.0),
        action_min_session_uptime_s=float(
            getattr(cfg, "mikrotik_action_min_session_uptime_s", 60.0) or 60.0
        ),
        capability_probe_interval_s=float(
            getattr(cfg, "mikrotik_capability_probe_interval_s", 300.0) or 300.0
        ),
    )


def _build_backend_connection_config(
    cfg: Arch4Config,
    *,
    backend_override: str = "",
) -> BackendConnectionConfig:
    from arch4.modem_live.profile_catalog import (
        modem_connection_config_fields,
        modem_connection_transport,
        modem_profile_selection,
    )
    from arch4.operator_profiles import infer_backend_family

    backend = str(backend_override or getattr(cfg, "modem_backend", "") or "")
    family = infer_backend_family(backend)
    host_field, user_field, pass_field, ping_field = modem_connection_config_fields(backend)
    host = ""
    user = ""
    resolver = getattr(cfg, "connection_defaults_for_backend", None)
    if callable(resolver):
        try:
            host, user = resolver(backend)
        except Exception:
            host, user = "", ""
    if not host:
        host_field = host_field or _FAMILY_HOST_FIELD.get(family, "router_host")
        host = str(getattr(cfg, host_field, "") or "")
    if not user:
        user_field = user_field or _FAMILY_USER_FIELD.get(family, "router_user")
        user = str(getattr(cfg, user_field, "") or "")
    secret_resolver = getattr(cfg, "connection_secret_for_backend", None)
    password = str(secret_resolver(backend) or "") if callable(secret_resolver) else ""
    if not password:
        pass_field = pass_field or _FAMILY_PASSWORD_FIELD.get(family, "router_password")
        password = str(getattr(cfg, pass_field, "") or "")
    ping_resolver = getattr(cfg, "connection_ping_interface_for_backend", None)
    ping_interface = str(ping_resolver(backend) or "") if callable(ping_resolver) else ""
    if not ping_interface:
        ping_field = ping_field or _FAMILY_PING_FIELD.get(family, "")
        ping_interface = str(getattr(cfg, ping_field, "") or "") if ping_field else ""
    transport_resolver = getattr(cfg, "connection_transport_for_backend", None)
    transport = str(transport_resolver(backend) or "unknown") if callable(transport_resolver) else "unknown"
    if transport == "unknown":
        transport = str(modem_connection_transport(backend) or _FAMILY_TRANSPORT.get(family, "unknown") or "unknown")
    guard_resolver = getattr(cfg, "target_guard_for_backend", None)
    guard = guard_resolver(backend) if callable(guard_resolver) else {}
    if not isinstance(guard, dict):
        guard = {}
    if not guard:
        guard = {
            "expected_model": str(getattr(cfg, "target_guard_model", "") or ""),
            "expected_serial": str(getattr(cfg, "target_guard_serial", "") or ""),
            "expected_firmware": str(getattr(cfg, "target_guard_firmware", "") or ""),
            "expected_imei": str(getattr(cfg, "target_guard_imei", "") or ""),
        }
    selected_id = str(getattr(cfg, "modem_profile_id", "") or "").strip()
    if selected_id:
        selected = modem_profile_selection(selected_id)
        requested_backend = str(getattr(cfg, "modem_backend", "") or "").strip().lower()
        if selected is None or selected.backend != requested_backend:
            raise ValueError("selected_profile_backend_mismatch")
        guard = dict(guard)
        required_guards = (("expected_model", selected.target_guard_model),
                           ("expected_firmware", selected.target_guard_firmware)) if selected.target_guard_firmware else ()
        for field, required in required_guards:
            if not required:
                continue
            entered = str(guard.get(field, "") or "").strip()
            if entered and entered.casefold() != required.casefold():
                raise ValueError(f"selected_profile_{field}_conflict")
            guard[field] = required
    return BackendConnectionConfig(
        backend=backend,
        host=str(host or ""),
        user=str(user or ""),
        password=password,
        ping_interface=ping_interface,
        transport=transport,
        expected_model=str(guard.get("expected_model", "") or ""),
        expected_serial=str(guard.get("expected_serial", "") or ""),
        expected_firmware=str(guard.get("expected_firmware", "") or ""),
        expected_imei=str(guard.get("expected_imei", "") or ""),
    )


def build_backend_connection_config(cfg: Arch4Config) -> BackendConnectionConfig:
    return _build_backend_connection_config(cfg)


def build_startup_display_config(cfg: Arch4Config) -> StartupDisplayConfig:
    backend = str(getattr(cfg, "modem_backend", "") or "")
    is_model = backend == "model"
    deployed = getattr(cfg, "deployed_bands", None)
    bands = sorted(int(b) for b in (deployed or sorted(DEPLOYED_BANDS)))
    model_profile = str(getattr(cfg, "model_profile_name", "") or "")
    model_path = str(getattr(cfg, "model_profile_path", "") or "")
    model_display = model_path or (f"profile:{model_profile}" if model_profile else "profile:default")
    backend_conn = build_backend_connection_config(cfg)
    host_display = model_display if is_model else backend_conn.host
    return StartupDisplayConfig(
        modem_backend=backend,
        host_display=host_display,
        connect_error_host=host_display,
        deployed_bands=bands,
    )


@dataclass(frozen=True)
class InputModuleConfig:
    modem_backend: str
    modem_profile_id: str
    target_guard_serial: str
    target_guard_imei: str
    rtt_probe_target: str
    rtt_probe_count: int
    actionable_rtt_probe_count: int
    actionable_modem_read_timeout_s: float
    actionable_rtt_max_age_s: float
    modem_live_register_enabled: bool
    modem_live_register_interval_s: float
    modem_live_register_stale_s: float
    modem_live_backend_io_parallel_safe: Optional[bool]
    modem_live_io_serial_required: Optional[bool]
    modem_live_startup_parallel_allowed: Optional[bool]
    modem_live_maintenance_holds_io_lock: Optional[bool]
    live_maintenance_interval_s: float
    neighbor_scan_interval_s: float
    health_multi_target_enabled: bool
    health_probe_interval_s: float
    health_probe_timeout_s: float
    health_ping_rtt_max_ms: float
    health_ping_targets: list
    health_dns_targets: list
    health_http_targets: list
    # Full startup acquisition wait; not the normal live-read deadline.
    modem_read_timeout_s: float = DEFAULT_MODEM_STARTUP_READ_TIMEOUT_S


@dataclass(frozen=True)
class DecisionPolicyConfig:
    night_training_start_hour: int
    night_training_end_hour: int
    emergency_action_window_s: float
    emergency_action_max_per_window: int
    emergency_action_global_cooldown_s: float
    emergency_action_cooldown_s: float


@dataclass(frozen=True)
class NightTrainingConfig:
    night_training_start_hour: int
    night_training_end_hour: int
    night_probe_dwell_s: float
    night_probe_cooldown_s: float
    night_probe_trigger_bad_rf_sinr_db: float
    night_probe_trigger_bad_rsrp_dbm: float
    night_probe_recheck_s: float
    night_probe_trigger_no_ca_s: float


@dataclass(frozen=True)
class CalibrationEngineConfig:
    calibration_min_samples: int


def build_input_module_config(cfg: Arch4Config) -> InputModuleConfig:
    _health_probe_interval_raw = getattr(cfg, "health_probe_interval_s", 30.0)
    _health_probe_interval_s = 30.0 if _health_probe_interval_raw is None else float(_health_probe_interval_raw)
    return InputModuleConfig(
        modem_backend=str(cfg.modem_backend),
        modem_profile_id=str(cfg.modem_profile_id or ""),
        target_guard_serial=str(cfg.target_guard_serial or ""),
        target_guard_imei=str(cfg.target_guard_imei or ""),
        rtt_probe_target=str(getattr(cfg, "rtt_probe_target", "8.8.8.8") or "8.8.8.8"),
        rtt_probe_count=max(1, int(getattr(cfg, "rtt_probe_count", 3) or 3)),
        actionable_rtt_probe_count=max(
            1, int(getattr(cfg, "actionable_rtt_probe_count", 1) or 1)
        ),
        actionable_modem_read_timeout_s=max(
            0.5, float(getattr(cfg, "actionable_modem_read_timeout_s", 2.5) or 2.5)
        ),
        actionable_rtt_max_age_s=max(
            1.0, float(getattr(cfg, "actionable_rtt_max_age_s", 2.5) or 2.5)
        ),
        modem_live_register_enabled=bool(getattr(cfg, "modem_live_register_enabled", True)),
        modem_live_register_interval_s=max(
            0.5, float(getattr(cfg, "modem_live_register_interval_s", 2.0) or 2.0)
        ),
        modem_live_register_stale_s=max(
            2.0, float(getattr(cfg, "modem_live_register_stale_s", 6.0) or 6.0)
        ),
        modem_live_backend_io_parallel_safe=getattr(cfg, "modem_live_backend_io_parallel_safe", None),
        modem_live_io_serial_required=getattr(cfg, "modem_live_io_serial_required", None),
        modem_live_startup_parallel_allowed=getattr(cfg, "modem_live_startup_parallel_allowed", None),
        modem_live_maintenance_holds_io_lock=getattr(cfg, "modem_live_maintenance_holds_io_lock", None),
        live_maintenance_interval_s=max(
            2.0, float(getattr(cfg, "live_maintenance_interval_s", 10.0) or 10.0)
        ),
        neighbor_scan_interval_s=max(
            5.0, float(getattr(cfg, "neighbor_scan_interval_s", 120.0) or 120.0)
        ),
        health_multi_target_enabled=bool(getattr(cfg, "health_multi_target_enabled", True)),
        health_probe_timeout_s=max(0.3, float(getattr(cfg, "health_probe_timeout_s", 2.0) or 2.0)),
        health_ping_rtt_max_ms=float(getattr(cfg, "health_ping_rtt_max_ms", 800.0) or 800.0),
        health_ping_targets=list(
            getattr(
                cfg,
                "health_ping_targets",
                ["1.1.1.1", "8.8.8.8", "9.9.9.9"],
            )
            or []
        ),
        health_dns_targets=list(getattr(cfg, "health_dns_targets", ["google.com"]) or []),
        health_http_targets=list(
            getattr(cfg, "health_http_targets",
                    ["https://connectivitycheck.gstatic.com/generate_204"]) or []
        ),
        health_probe_interval_s=max(0.0, _health_probe_interval_s),
        modem_read_timeout_s=max(
            2.0,
            float(getattr(cfg, "modem_read_timeout_s", DEFAULT_MODEM_STARTUP_READ_TIMEOUT_S)
                  or DEFAULT_MODEM_STARTUP_READ_TIMEOUT_S),
        ),
    )


def build_decision_policy_config(cfg: Arch4Config) -> DecisionPolicyConfig:
    return DecisionPolicyConfig(
        night_training_start_hour=int(getattr(cfg, "night_training_start_hour", 0) or 0),
        night_training_end_hour=int(getattr(cfg, "night_training_end_hour", 6) or 6),
        emergency_action_window_s=_float_cfg(cfg, "emergency_action_window_s", 900.0),
        emergency_action_max_per_window=max(1, int(getattr(cfg, "emergency_action_max_per_window", 3) or 3)),
        emergency_action_global_cooldown_s=_float_cfg(
            cfg, "emergency_action_global_cooldown_s", 3600.0
        ),
        emergency_action_cooldown_s=_float_cfg(cfg, "emergency_action_cooldown_s", 120.0),
    )


def build_night_training_config(cfg: Arch4Config) -> NightTrainingConfig:
    return NightTrainingConfig(
        night_training_start_hour=int(getattr(cfg, "night_training_start_hour", 0) or 0),
        night_training_end_hour=int(getattr(cfg, "night_training_end_hour", 6) or 6),
        night_probe_dwell_s=max(60.0, float(getattr(cfg, "night_probe_dwell_s", 900.0) or 900.0)),
        night_probe_cooldown_s=max(60.0, float(getattr(cfg, "night_probe_cooldown_s", 600.0) or 600.0)),
        night_probe_trigger_bad_rf_sinr_db=float(
            getattr(cfg, "night_probe_trigger_bad_rf_sinr_db", 5.0) or 5.0
        ),
        night_probe_trigger_bad_rsrp_dbm=float(
            getattr(cfg, "night_probe_trigger_bad_rsrp_dbm", -92.0) or -92.0
        ),
        night_probe_recheck_s=max(60.0, float(getattr(cfg, "night_probe_recheck_s", 1800.0) or 1800.0)),
        night_probe_trigger_no_ca_s=max(
            60.0, float(getattr(cfg, "night_probe_trigger_no_ca_s", 1800.0) or 1800.0)
        ),
    )


def build_calibration_engine_config(cfg: Arch4Config) -> CalibrationEngineConfig:
    return CalibrationEngineConfig(
        calibration_min_samples=max(1, int(getattr(cfg, "calibration_min_samples", 30) or 30)),
    )


# ── Neutral connection resolution ────────────────────────────────────────────
#
# These functions let GUI / launcher code route connection credentials
# through a single family-dispatched path instead of inline vendor checks.

_FAMILY_HOST_FIELD: dict[str, str] = {
    "huawei_hilink_like": "b818_host",
    "zte_goform_like": "zte_host",
    "zte_zwrt_ubus_like": "zte_host",
    "zyxel_webui_like": "zyxel_host",
    "openwrt_modemmanager_like": "openwrt_host",
    "routeros_at_like": "router_host",
    "modem_model_like": "router_host",
}

_FAMILY_USER_FIELD: dict[str, str] = {
    "huawei_hilink_like": "b818_user",
    "zte_goform_like": "zte_user",
    "zte_zwrt_ubus_like": "zte_user",
    "zyxel_webui_like": "zyxel_user",
    "openwrt_modemmanager_like": "openwrt_user",
    "routeros_at_like": "router_user",
    "modem_model_like": "router_user",
}

_FAMILY_PASSWORD_FIELD: dict[str, str] = {
    "huawei_hilink_like": "b818_password",
    "zte_goform_like": "zte_password",
    "zte_zwrt_ubus_like": "zte_password",
    "zyxel_webui_like": "zyxel_password",
    "openwrt_modemmanager_like": "openwrt_password",
    "routeros_at_like": "router_password",
    "modem_model_like": "router_password",
}

_FAMILY_PING_FIELD: dict[str, str] = {
    "huawei_hilink_like": "b818_ping_interface",
    "zte_goform_like": "zte_ping_interface",
    "zte_zwrt_ubus_like": "zte_ping_interface",
    "zyxel_webui_like": "zyxel_ping_interface",
    "openwrt_modemmanager_like": "openwrt_ping_interface",
    "routeros_at_like": "",
    "modem_model_like": "",
}

_FAMILY_TRANSPORT: dict[str, str] = {
    "huawei_hilink_like": "hilink_http_xml",
    "zte_goform_like": "zte_goform_http",
    "zte_zwrt_ubus_like": "zte_zwrt_ubus",
    "zyxel_webui_like": "zyxel_webui_http",
    "openwrt_modemmanager_like": "mmcli_json",
    "routeros_at_like": "routeros_api + at_chat",
    "modem_model_like": "model_backend",
}


def resolve_connection_host(cfg: Arch4Config, backend: str = "") -> str:
    """Return the resolved connection host for the given backend.

    Uses backend family to select the correct config field, eliminating
    vendor-name checks at call sites.
    """
    from arch4.modem_live.profile_catalog import modem_connection_config_fields
    from arch4.operator_profiles import infer_backend_family
    b = str(backend or getattr(cfg, "modem_backend", "") or "").strip().lower()
    family = infer_backend_family(b)
    field = modem_connection_config_fields(b)[0] or _FAMILY_HOST_FIELD.get(family, "router_host")
    return str(getattr(cfg, field, "") or "")


def resolve_connection_username(cfg: Arch4Config, backend: str = "") -> str:
    """Return the resolved connection username for the given backend."""
    from arch4.modem_live.profile_catalog import modem_connection_config_fields
    from arch4.operator_profiles import infer_backend_family
    b = str(backend or getattr(cfg, "modem_backend", "") or "").strip().lower()
    family = infer_backend_family(b)
    field = modem_connection_config_fields(b)[1] or _FAMILY_USER_FIELD.get(family, "router_user")
    return str(getattr(cfg, field, "") or "")


def resolve_connection_password(cfg: Arch4Config, backend: str = "") -> str:
    """Return the resolved connection password for the given backend."""
    from arch4.modem_live.profile_catalog import modem_connection_config_fields
    from arch4.operator_profiles import infer_backend_family
    b = str(backend or getattr(cfg, "modem_backend", "") or "").strip().lower()
    family = infer_backend_family(b)
    field = modem_connection_config_fields(b)[2] or _FAMILY_PASSWORD_FIELD.get(family, "router_password")
    return str(getattr(cfg, field, "") or "")


def apply_connection_credentials(
    cfg: Arch4Config,
    backend: str,
    host: str,
    username: str,
    password: str,
) -> None:
    """Route host/user/password into the correct vendor-specific config fields.

    This is the single place where GUI/launcher code maps neutral credentials
    to the backend-specific Arch4Config fields. The factory and config_views
    build_*_connection_config() functions then read these fields unchanged.
    """
    from arch4.modem_live.profile_catalog import modem_connection_config_fields
    from arch4.operator_profiles import infer_backend_family
    b = str(backend or "").strip().lower()
    family = infer_backend_family(b)
    host_field, user_field, pass_field, _ping_field = modem_connection_config_fields(b)
    host_field = host_field or _FAMILY_HOST_FIELD.get(family, "router_host")
    user_field = user_field or _FAMILY_USER_FIELD.get(family, "router_user")
    pass_field = pass_field or _FAMILY_PASSWORD_FIELD.get(family, "router_password")
    setattr(cfg, host_field, host)
    setattr(cfg, user_field, username)
    setattr(cfg, pass_field, password)
