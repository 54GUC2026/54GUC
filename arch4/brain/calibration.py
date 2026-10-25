"""
arch4/brain/calibration.py

ARCH6v2 calibration lane (advisory).
Computes expected-vs-current signal gaps from learned baselines and health
status, then exports a normalized calibration gap score.
"""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

from dataclasses import dataclass
from collections import deque
from typing import Dict, Any, List, Optional
import time

from arch4.interfaces import Arch4Config
from arch4.brain.memory import BaselineStats
from arch4.config_views import build_calibration_engine_config
from arch4.brain.congestion_signals import congestion_rtt_level, quiet_rtt_elevated


@dataclass
class CalibrationStatus:
    enabled: bool = True
    baseline_ready: bool = False
    sample_count: int = 0
    expected_dl_mbps: float = 0.0
    expected_rtt_ms: float = 0.0
    expected_sinr_db: float = 0.0
    throughput_gap_ratio: float = 0.0
    rtt_gap_ratio: float = 0.0
    sinr_gap_db: float = 0.0
    gap_score: float = 0.0
    gap_score_smooth: float = 0.0
    gap_score_volatility: float = 0.0
    gap_score_set_threshold: float = 0.0
    gap_score_clear_threshold: float = 0.0
    set_evidence_ratio: float = 0.0
    clear_evidence_ratio: float = 0.0
    needs_calibration: bool = False
    health_ok: bool = True
    ca_persistence_ratio: float = 0.0
    network_load_biased: bool = False
    network_load_summary: str = ""
    network_load_reason: str = ""
    # NR state bias: set when nsa_active changed mid-window (attach or detach).
    # Throughput/SINR deltas in this tick are caused by NR state change, not
    # by antenna position or LTE RF — gap score for this tick is suppressed.
    nr_state_biased: bool = False
    nr_state_summary: str = ""
    nr_state_reason: str = ""
    offpeak_window: bool = False
    state_name: str = ""
    fault_state_active: bool = False
    baseline_drift_detected: bool = False
    baseline_drift_summary: str = ""
    baseline_drift_reason: str = ""
    recalibration_recommended: bool = False
    threshold_adjust_hint_db: float = 0.0
    ts: float = 0.0


class CalibrationEngine:
    def __init__(self, cfg: Arch4Config):
        self.cfg = cfg
        self.calib_engine_cfg = build_calibration_engine_config(cfg)
        self._st = CalibrationStatus()
        self._events: List[Dict[str, Any]] = []
        self._enabled: bool = True
        self._ca_window = deque(maxlen=40)
        self._gap_window = deque(
            maxlen=max(3, int(getattr(cfg, "calibration_score_window_ticks", 12) or 12))
        )
        self._drift_window = deque(
            maxlen=max(4, int(getattr(cfg, "calibration_baseline_drift_window_ticks", 24) or 24))
        )
        self._set_streak = 0
        self._clear_streak = 0

    @staticmethod
    def _window_mean(values: List[float]) -> float:
        if not values:
            return 0.0
        return float(sum(values)) / float(len(values))

    @classmethod
    def _window_pstdev(cls, values: List[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = cls._window_mean(values)
        return (sum((float(v) - mean) ** 2 for v in values) / float(len(values))) ** 0.5

    @staticmethod
    def _hour_in_window(hour: int, start_h: int, end_h: int) -> bool:
        hour_i = int(hour) % 24
        start_i = int(start_h) % 24
        end_i = int(end_h) % 24
        if start_i == end_i:
            return True
        if start_i < end_i:
            return start_i <= hour_i < end_i
        return hour_i >= start_i or hour_i < end_i

    def start(self, now: float | None = None) -> Dict[str, Any]:
        now = float(now or time.time())
        self._enabled = True
        self._st.enabled = True
        self._st.ts = now
        self._gap_window.clear()
        self._drift_window.clear()
        self._set_streak = 0
        self._clear_streak = 0
        self._events.append(
            {
                "kind": "calibration_control",
                "command": "start",
                "enabled": True,
                "ts": now,
            }
        )
        return self.get_status()

    def stop(self, now: float | None = None) -> Dict[str, Any]:
        now = float(now or time.time())
        self._enabled = False
        self._st.enabled = False
        self._st.needs_calibration = False
        self._st.ts = now
        self._gap_window.clear()
        self._drift_window.clear()
        self._set_streak = 0
        self._clear_streak = 0
        self._events.append(
            {
                "kind": "calibration_control",
                "command": "stop",
                "enabled": False,
                "ts": now,
            }
        )
        return self.get_status()

    def restart(self, now: float | None = None) -> Dict[str, Any]:
        now = float(now or time.time())
        self._enabled = True
        self._st = CalibrationStatus(enabled=True, ts=now)
        self._ca_window.clear()
        self._gap_window.clear()
        self._drift_window.clear()
        self._set_streak = 0
        self._clear_streak = 0
        self._events.append(
            {
                "kind": "calibration_control",
                "command": "restart",
                "enabled": True,
                "ts": now,
            }
        )
        return self.get_status()

    def refresh_thresholds(self) -> None:
        """Rebuild config view after calibration profile update."""
        self.calib_engine_cfg = build_calibration_engine_config(self.cfg)

    def _reset_scoring_state(self) -> None:
        """Zero all scoring/evidence fields and clear windows."""
        st = self._st
        st.throughput_gap_ratio = 0.0
        st.rtt_gap_ratio = 0.0
        st.sinr_gap_db = 0.0
        st.gap_score = 0.0
        st.gap_score_smooth = 0.0
        st.gap_score_volatility = 0.0
        st.gap_score_set_threshold = 0.0
        st.gap_score_clear_threshold = 0.0
        st.set_evidence_ratio = 0.0
        st.clear_evidence_ratio = 0.0
        st.needs_calibration = False
        st.network_load_biased = False
        st.network_load_summary = ""
        st.network_load_reason = ""
        st.nr_state_biased = False
        st.nr_state_summary = ""
        st.nr_state_reason = ""
        st.baseline_drift_detected = False
        st.baseline_drift_summary = ""
        st.baseline_drift_reason = ""
        st.recalibration_recommended = False
        st.threshold_adjust_hint_db = 0.0
        self._drift_window.clear()
        self._set_streak = 0
        self._clear_streak = 0

    def _update_gap_scores(
        self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames,
        rsrq_gap: float, rsrq_gap_valid: bool, rtt_level: str,
    ) -> None:
        """Compute throughput/RTT/SINR gap ratios and weighted gap_score."""
        st = self._st
        if st.expected_dl_mbps > 0.0 and modem.traffic.has_traffic:
            st.throughput_gap_ratio = max(
                0.0, (st.expected_dl_mbps - modem.traffic.dl_mbps) / st.expected_dl_mbps
            )
        else:
            st.throughput_gap_ratio = 0.0

        if st.expected_rtt_ms > 0.0:
            st.rtt_gap_ratio = max(
                0.0, (computed.latency.rtt_sliding_avg - st.expected_rtt_ms) / max(1.0, st.expected_rtt_ms)
            )
        else:
            st.rtt_gap_ratio = 0.0

        st.sinr_gap_db = modem.cellular.sinr_db - st.expected_sinr_db if st.expected_sinr_db else 0.0
        sinr_penalty = max(0.0, -st.sinr_gap_db / 10.0)
        health_penalty = 0.25 if not st.health_ok else 0.0
        likely_loaded_window = bool(
            rsrq_gap_valid
            and rsrq_gap >= 3.0
            and rtt_level in {"busy", "alarm"}
        )
        throughput_weight = 0.28 if likely_loaded_window else 0.48
        rtt_weight = 0.42 if likely_loaded_window else 0.30
        sinr_weight = 0.30 if likely_loaded_window else 0.22

        st.gap_score = (
            throughput_weight * st.throughput_gap_ratio
            + rtt_weight * st.rtt_gap_ratio
            + sinr_weight * sinr_penalty
            + health_penalty
        )

    def _update_set_clear_evidence(
        self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames,
        rsrq_gap_valid: bool, before: bool, now: float,
    ) -> None:
        """Score smoothing, NR bias suppression, and set/clear evidence tracking."""
        st = self._st

        set_threshold = max(0.0, float(getattr(self.cfg, "calibration_gap_threshold", 0.40) or 0.40))
        clear_threshold = float(
            getattr(self.cfg, "calibration_gap_clear_threshold", (set_threshold * 0.80)) or (set_threshold * 0.80)
        )
        clear_threshold = max(0.0, min(set_threshold, clear_threshold))

        nr_just_dropped = bool(getattr(computed.quality, 'nr_just_dropped', False))
        nr_recovering = bool(getattr(computed.quality, 'nr_recovering', False))
        nsa_active = bool(modem.cellular.nsa_active)
        st.nr_state_biased = nr_just_dropped or (nr_recovering and not nsa_active)
        if st.nr_state_biased:
            st.nr_state_summary = (
                "NR state changed this tick — throughput/SINR delta is NR-driven, not LTE RF."
            )
            st.nr_state_reason = (
                "nr_just_dropped" if nr_just_dropped else "nr_recovering"
            )
        else:
            st.nr_state_summary = ""
            st.nr_state_reason = ""

        if st.health_ok and not st.nr_state_biased:
            self._gap_window.append(float(st.gap_score))
        gap_values = [float(v) for v in self._gap_window]
        st.gap_score_smooth = self._window_mean(gap_values)
        st.gap_score_volatility = self._window_pstdev(gap_values)
        volatility_margin = min(
            max(0.0, float(getattr(self.cfg, "calibration_volatility_margin_cap", 0.12) or 0.12)),
            st.gap_score_volatility
            * max(0.0, float(getattr(self.cfg, "calibration_volatility_margin_gain", 0.35) or 0.35)),
        )
        st.gap_score_set_threshold = set_threshold + volatility_margin
        st.gap_score_clear_threshold = min(
            st.gap_score_set_threshold,
            clear_threshold + (volatility_margin * 0.5),
        )
        set_confirm_ticks = max(
            1, int(getattr(self.cfg, "calibration_set_confirm_ticks", 2) or 2)
        )
        clear_confirm_ticks = max(
            1, int(getattr(self.cfg, "calibration_clear_confirm_ticks", 3) or 3)
        )
        set_evidence_ratio = max(
            0.0, min(1.0, float(getattr(self.cfg, "calibration_set_evidence_ratio", 0.55) or 0.55))
        )
        clear_evidence_ratio = max(
            0.0, min(1.0, float(getattr(self.cfg, "calibration_clear_evidence_ratio", 0.65) or 0.65))
        )
        recent_set_values = gap_values[-set_confirm_ticks:]
        recent_clear_values = gap_values[-clear_confirm_ticks:]
        st.set_evidence_ratio = float(
            sum(1 for v in recent_set_values if v >= st.gap_score_set_threshold)
        ) / float(max(1, len(recent_set_values)))
        st.clear_evidence_ratio = float(
            sum(1 for v in recent_clear_values if v <= st.gap_score_clear_threshold)
        ) / float(max(1, len(recent_clear_values)))

        if st.baseline_ready:
            if before:
                clear_ready = bool(
                    self._window_mean(recent_clear_values) <= st.gap_score_clear_threshold
                    and st.clear_evidence_ratio >= clear_evidence_ratio
                )
                if clear_ready:
                    self._clear_streak += 1
                else:
                    self._clear_streak = 0
                self._set_streak = 0
                st.needs_calibration = self._clear_streak < clear_confirm_ticks
            else:
                set_ready = bool(
                    (
                        st.gap_score >= st.gap_score_set_threshold
                        or self._window_mean(recent_set_values) >= set_threshold
                    )
                    and st.set_evidence_ratio >= set_evidence_ratio
                )
                if set_ready:
                    self._set_streak += 1
                else:
                    self._set_streak = 0
                self._clear_streak = 0
                st.needs_calibration = self._set_streak >= set_confirm_ticks
        else:
            self._set_streak = 0
            self._clear_streak = 0
            st.needs_calibration = False

    def _update_load_bias(
        self, modem: CellularIntelligenceGeneration,
        rsrq_gap: float, rsrq_gap_valid: bool,
    ) -> None:
        """Detect when gap appears network-load-driven rather than RF-position-driven."""
        st = self._st
        power_ok = float(getattr(modem.cellular, 'rsrp_dbm', 0.0) or 0.0) >= float(
            getattr(self.cfg, "calibration_load_bias_rsrp_floor_dbm", -92.0) or -92.0
        )
        quality_poor = (
            float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0)
            <= float(getattr(self.cfg, "calibration_load_bias_sinr_ceiling_db", 6.0) or 6.0)
            or float(getattr(modem.cellular, 'rsrq_db', 0.0) or 0.0)
            <= float(getattr(self.cfg, "calibration_load_bias_rsrq_ceiling_db", -11.0) or -11.0)
        )
        load_gap_high = bool(rsrq_gap_valid and rsrq_gap >= 4.0)
        throughput_gap_big = st.throughput_gap_ratio >= float(
            getattr(self.cfg, "calibration_load_bias_throughput_gap_ratio", 0.35) or 0.35
        )
        rtt_gap_big = st.rtt_gap_ratio >= float(
            getattr(self.cfg, "calibration_load_bias_rtt_gap_ratio", 0.35) or 0.35
        )
        st.network_load_biased = bool(
            st.baseline_ready
            and bool(getattr(modem.cellular, 'radio_connected', False))
            and power_ok
            and (quality_poor or load_gap_high)
            and (throughput_gap_big or rtt_gap_big)
        )
        if st.network_load_biased:
            st.network_load_summary = (
                "Current drift looks load- or interference-biased, not purely antenna-position driven."
            )
            reasons: List[str] = []
            if throughput_gap_big:
                reasons.append("DL below baseline")
            if rtt_gap_big:
                reasons.append("RTT above baseline")
            if float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0) <= float(
                getattr(self.cfg, "calibration_load_bias_sinr_ceiling_db", 6.0) or 6.0
            ):
                reasons.append("SINR poor")
            if float(getattr(modem.cellular, 'rsrq_db', 0.0) or 0.0) <= float(
                getattr(self.cfg, "calibration_load_bias_rsrq_ceiling_db", -11.0) or -11.0
            ):
                reasons.append("RSRQ poor")
            if load_gap_high:
                reasons.append("RSRQ load gap high")
            st.network_load_reason = ", ".join(reasons[:4])
        else:
            st.network_load_summary = ""
            st.network_load_reason = ""

    def _update_baseline_drift(
        self, modem: CellularIntelligenceGeneration,
        rsrq_gap: float, rsrq_gap_valid: bool,
        before_drift: bool, now: float,
    ) -> None:
        """Detect sustained signal drift below learned baseline."""
        st = self._st
        drift_window_len = max(
            4, int(getattr(self.cfg, "calibration_baseline_drift_window_ticks", 24) or 24)
        )
        if self._drift_window.maxlen != drift_window_len:
            self._drift_window = deque(self._drift_window, maxlen=drift_window_len)
        drift_sinr_gap_db = max(
            0.0, float(getattr(self.cfg, "calibration_baseline_drift_sinr_gap_db", 2.0) or 2.0)
        )
        allowed_load_gap_db = max(
            0.0, float(getattr(self.cfg, "calibration_baseline_drift_rsrq_gap_db", 2.5) or 2.5)
        )
        drift_gap_score_min = max(
            0.0, float(getattr(self.cfg, "calibration_baseline_drift_gap_score_min", 0.18) or 0.18)
        )
        drift_hold_ratio = max(
            0.0, min(1.0, float(getattr(self.cfg, "calibration_baseline_drift_hold_ratio", 0.70) or 0.70))
        )
        drift_gain = max(
            0.0, float(getattr(self.cfg, "calibration_baseline_drift_threshold_hint_gain", 0.5) or 0.5)
        )
        drift_cap_db = max(
            0.0, float(getattr(self.cfg, "calibration_baseline_drift_threshold_hint_cap_db", 3.0) or 3.0)
        )
        clean_window = (not rsrq_gap_valid) or rsrq_gap <= allowed_load_gap_db
        sustained_signal_drop = (-st.sinr_gap_db) >= drift_sinr_gap_db
        drift_sample = bool(
            st.baseline_ready
            and bool(getattr(modem.cellular, 'radio_connected', False))
            and st.health_ok
            and not st.fault_state_active
            and not st.network_load_biased
            and st.gap_score >= drift_gap_score_min
            and sustained_signal_drop
            and clean_window
        )
        self._drift_window.append(1 if drift_sample else 0)
        drift_ratio = (
            float(sum(self._drift_window)) / float(max(1, len(self._drift_window)))
            if self._drift_window
            else 0.0
        )
        drift_ready = bool(
            len(self._drift_window) >= max(4, int(drift_window_len * 0.5))
            and drift_ratio >= drift_hold_ratio
        )
        st.baseline_drift_detected = drift_ready
        st.recalibration_recommended = drift_ready
        if drift_ready:
            drift_db = max(0.0, (-st.sinr_gap_db) - drift_sinr_gap_db)
            st.threshold_adjust_hint_db = min(drift_cap_db, drift_db * drift_gain)
            st.baseline_drift_reason = (
                f"SINR is {(-st.sinr_gap_db):.1f} dB below baseline with low load bias evidence "
                f"(hold ratio {drift_ratio:.2f})."
            )
            st.baseline_drift_summary = (
                "RF baseline appears worse than the learned norm. "
                "Recalibration is recommended when conditions are quiet."
            )
        else:
            st.baseline_drift_reason = ""
            st.baseline_drift_summary = ""
            st.threshold_adjust_hint_db = 0.0

        if drift_ready:
            st.needs_calibration = True

        if drift_ready and not before_drift:
            self._events.append(
                {
                    "kind": "calibration_drift_advisory",
                    "baseline_drift_detected": True,
                    "recalibration_recommended": True,
                    "threshold_adjust_hint_db": round(st.threshold_adjust_hint_db, 3),
                    "baseline_drift_reason": st.baseline_drift_reason,
                    "sinr_gap_db": round(st.sinr_gap_db, 3),
                    "gap_score": round(st.gap_score, 4),
                    "network_load_biased": bool(st.network_load_biased),
                    "nr_state_biased": bool(st.nr_state_biased),
                    "state_name": st.state_name,
                    "ts": now,
                }
            )

    def update(
        self,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        baseline: Optional[BaselineStats],
        health_status: Optional[Dict[str, Any]],
        state_name: str = "",
        now: float | None = None,
    ) -> Dict[str, Any]:
        now = float(now or time.time())
        st = self._st
        before = bool(st.needs_calibration)
        before_drift = bool(st.baseline_drift_detected)
        st.enabled = bool(self._enabled)

        st.ts = now
        st.sample_count = int(getattr(baseline, "sample_count", 0) or 0)
        st.baseline_ready = st.sample_count >= self.calib_engine_cfg.calibration_min_samples
        st.expected_dl_mbps = float(getattr(baseline, "avg_throughput_mbps", 0.0) or 0.0)
        st.expected_rtt_ms = float(getattr(baseline, "avg_rtt_ms", 0.0) or 0.0)
        st.expected_sinr_db = float(getattr(baseline, "avg_sinr", 0.0) or 0.0)
        st.health_ok = bool((health_status or {}).get("overall_ok", True))
        st.state_name = str(state_name or "")
        st.fault_state_active = bool(st.state_name.startswith("E_") or st.state_name == "BAD_RF")

        # offpeak_window: measured quietness is primary truth
        time_offpeak = self._hour_in_window(
            time.localtime(now).tm_hour,
            int(getattr(self.cfg, "calibration_offpeak_start_hour", 0) or 0),
            int(getattr(self.cfg, "calibration_offpeak_end_hour", 6) or 6),
        )
        rsrq_gap = float(getattr(computed.quality, 'rsrq_gap', 999.0) or 999.0)
        rsrq_gap_valid = bool(getattr(computed.quality, 'rsrq_gap_valid', False))
        rtt_level = str(getattr(computed.latency, 'rtt_pressure_level', "normal") or "normal")
        # Use quiet-window RTT for calibration gating — self-traffic RTT
        # shouldn't prevent calibration from collecting data.
        rtt_level_quiet = congestion_rtt_level(computed, baseline_level=rtt_level)
        rtt_warning_ms = float(getattr(self.cfg, "rtt_warning_ms", 100.0) or 100.0)
        rtt_elevated_quiet = quiet_rtt_elevated(computed, rtt_warning_ms)
        rsrq_quiet = (
            rsrq_gap_valid
            and rsrq_gap < 2.0
            and rtt_level_quiet in {"normal", "probe_missing"}
            and not rtt_elevated_quiet
        )
        time_quiet_fallback = (
            time_offpeak
            and rtt_level_quiet in {"normal", "probe_missing"}
            and not rtt_elevated_quiet
            and (not rsrq_gap_valid or rsrq_gap < 3.0)
        )
        st.offpeak_window = bool(rsrq_quiet or time_quiet_fallback)

        if bool(getattr(modem.cellular, 'radio_connected', False)):
            self._ca_window.append(1 if bool(getattr(modem.cellular, 'ca_active', False)) else 0)
        elif self._ca_window:
            self._ca_window.append(0)
        connected_samples = max(0, len(self._ca_window))
        st.ca_persistence_ratio = (
            float(sum(self._ca_window)) / float(connected_samples)
            if connected_samples > 0
            else (1.0 if bool(getattr(modem.cellular, 'ca_active', False)) else 0.0)
        )

        if not st.enabled:
            self._reset_scoring_state()
            return self.get_status()

        self._update_gap_scores(modem, computed, rsrq_gap, rsrq_gap_valid, rtt_level)

        if st.fault_state_active:
            self._gap_window.clear()
            self._set_streak = 0
            self._clear_streak = 0
            st.gap_score = 0.0
            st.gap_score_smooth = 0.0
            st.gap_score_volatility = 0.0
            st.gap_score_set_threshold = 0.0
            st.gap_score_clear_threshold = 0.0
            st.set_evidence_ratio = 0.0
            st.clear_evidence_ratio = 0.0
            st.needs_calibration = False
            st.network_load_biased = False
            st.network_load_summary = ""
            st.network_load_reason = ""
            st.nr_state_biased = False
            st.nr_state_summary = ""
            st.nr_state_reason = ""
            st.baseline_drift_detected = False
            st.baseline_drift_summary = ""
            st.baseline_drift_reason = ""
            st.recalibration_recommended = False
            st.threshold_adjust_hint_db = 0.0
            self._drift_window.clear()
            if before:
                self._events.append(
                    {
                        "kind": "calibration_state_change",
                        "needs_calibration": False,
                        "gap_score": 0.0,
                        "gap_score_smooth": 0.0,
                        "gap_score_volatility": 0.0,
                        "gap_score_set_threshold": 0.0,
                        "gap_score_clear_threshold": 0.0,
                        "set_evidence_ratio": 0.0,
                        "clear_evidence_ratio": 0.0,
                        "throughput_gap_ratio": round(st.throughput_gap_ratio, 4),
                        "rtt_gap_ratio": round(st.rtt_gap_ratio, 4),
                        "sinr_gap_db": round(st.sinr_gap_db, 3),
                        "sample_count": st.sample_count,
                        "health_ok": st.health_ok,
                        "ca_persistence_ratio": round(st.ca_persistence_ratio, 4),
                        "network_load_biased": False,
                        "nr_state_biased": False,
                        "state_name": st.state_name,
                        "ts": now,
                    }
                )
            return self.get_status()

        self._update_set_clear_evidence(modem, computed, rsrq_gap_valid, before, now)
        self._update_load_bias(modem, rsrq_gap, rsrq_gap_valid)
        self._update_baseline_drift(modem, rsrq_gap, rsrq_gap_valid, before_drift, now)

        if st.needs_calibration != before:
            self._events.append(
                {
                    "kind": "calibration_state_change",
                    "needs_calibration": st.needs_calibration,
                    "gap_score": round(st.gap_score, 4),
                    "gap_score_smooth": round(st.gap_score_smooth, 4),
                    "gap_score_volatility": round(st.gap_score_volatility, 4),
                    "gap_score_set_threshold": round(st.gap_score_set_threshold, 4),
                    "gap_score_clear_threshold": round(st.gap_score_clear_threshold, 4),
                    "set_evidence_ratio": round(st.set_evidence_ratio, 4),
                    "clear_evidence_ratio": round(st.clear_evidence_ratio, 4),
                    "throughput_gap_ratio": round(st.throughput_gap_ratio, 4),
                    "rtt_gap_ratio": round(st.rtt_gap_ratio, 4),
                    "sinr_gap_db": round(st.sinr_gap_db, 3),
                    "sample_count": st.sample_count,
                    "health_ok": st.health_ok,
                    "ca_persistence_ratio": round(st.ca_persistence_ratio, 4),
                    "network_load_biased": bool(st.network_load_biased),
                    "nr_state_biased": bool(st.nr_state_biased),
                    "ts": now,
                }
            )
        return self.get_status()

    def get_status(self) -> Dict[str, Any]:
        st = self._st
        return {
            "enabled": bool(st.enabled),
            "baseline_ready": bool(st.baseline_ready),
            "sample_count": int(st.sample_count),
            "expected_dl_mbps": float(st.expected_dl_mbps),
            "expected_rtt_ms": float(st.expected_rtt_ms),
            "expected_sinr_db": float(st.expected_sinr_db),
            "throughput_gap_ratio": float(st.throughput_gap_ratio),
            "rtt_gap_ratio": float(st.rtt_gap_ratio),
            "sinr_gap_db": float(st.sinr_gap_db),
            "gap_score": float(st.gap_score),
            "gap_score_smooth": float(st.gap_score_smooth),
            "gap_score_volatility": float(st.gap_score_volatility),
            "gap_score_set_threshold": float(st.gap_score_set_threshold),
            "gap_score_clear_threshold": float(st.gap_score_clear_threshold),
            "set_evidence_ratio": float(st.set_evidence_ratio),
            "clear_evidence_ratio": float(st.clear_evidence_ratio),
            "needs_calibration": bool(st.needs_calibration),
            "health_ok": bool(st.health_ok),
            "ca_persistence_ratio": float(st.ca_persistence_ratio),
            "network_load_biased": bool(st.network_load_biased),
            "network_load_summary": str(st.network_load_summary),
            "network_load_reason": str(st.network_load_reason),
            "nr_state_biased": bool(st.nr_state_biased),
            "nr_state_summary": str(st.nr_state_summary or ""),
            "nr_state_reason": str(st.nr_state_reason or ""),
            "offpeak_window": bool(st.offpeak_window),
            "state_name": str(st.state_name or ""),
            "fault_state_active": bool(st.fault_state_active),
            "baseline_drift_detected": bool(st.baseline_drift_detected),
            "baseline_drift_summary": str(st.baseline_drift_summary or ""),
            "baseline_drift_reason": str(st.baseline_drift_reason or ""),
            "recalibration_recommended": bool(st.recalibration_recommended),
            "threshold_adjust_hint_db": float(st.threshold_adjust_hint_db),
            "ts": float(st.ts),
        }

    def drain_runtime_events(self) -> List[Dict[str, Any]]:
        out = list(self._events)
        self._events.clear()
        return out
