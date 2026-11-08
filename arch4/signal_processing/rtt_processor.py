"""
arch4/signal_processing/rtt_processor.py
═══════════════════════════════════════════════════════════════
RTT Signal Processor — single source of truth for all RTT-derived state.

Input:   rtt_ms (from ICMP ping, None if probe failed)
         Core-owned LatencyTrafficQualification
         modem context (band, pci, earfcn for per-cell baseline)

Outputs on ComputedSignals:
  .rtt_ms               raw ping value (passthrough)
  .rtt_ema              fast EMA tracker
  .rtt_sliding_avg      sliding window average (primary smoothed value)
  .rtt_baseline         THE learned idle floor (per-context, permanent ratchet)
  .rtt_delta            sliding_avg - baseline (0 if below baseline)
  .rtt_delta_valid      True only when baseline is learned (not default)
  .rtt_elevated         sliding_avg > warning threshold
  .rtt_alarm            sliding_avg > cliff threshold
  .rtt_probe_ok         True if this tick had a fresh RTT sample
  .rtt_pressure_level   "normal" | "busy" | "alarm" | "probe_missing"
  .rtt_congestion_avg   V5 congestion-window average
  .rtt_recent_median    V5 recent-window median
Baseline learning:
  - Per-context key: band|pci|earfcn
  - Learns during idle+calm ticks (traffic ≤100 kbps, sliding < warning)
  - Uses trimmed mean of samples in rolling window
  - Ratchet floor: once baseline reaches a value, it NEVER rises above it.
    Only a lower (better) RTT reading can lower it. This is permanent.
  - On fresh start with no history: uses config default (rtt_baseline_default)
  - Exact learned history may enter only through the Product Intelligence
    Matrix and can never replace a better live floor
  - No morning-window restriction — learns continuously

Traffic-aware delta:
  - Below 10 Mbps DL / 0.5 Mbps UL: delta = congestion-quiet RTT lane - idle_baseline
  - Above those thresholds: delta is suppressed entirely for congestion purposes
  - After traffic falls back below the release thresholds: a dedicated 12-tick quiet
    recovery lane filters the burst tail before fresh RTT is trusted again

Trust level: HIGH
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import math
from collections import deque
from typing import Any, Optional
from arch4.rtt_observation import RttObservation

from arch4.core_contracts.signals import LatencyTrafficQualification
from arch4.interfaces import ComputedSignals

log = logging.getLogger("arch4.signal_processing.rtt")


class RTTProcessor:
    def __init__(
        self,
        *,
        ema_alpha: float = 0.3,
        sliding_window_size: int = 13,
        baseline_min_samples: int = 10,
        baseline_max_samples: int = 60,
        baseline_trim_frac: float = 0.20,
        baseline_ema_alpha: float = 0.05,
        rtt_warning_ms: float = 100.0,
        rtt_cliff_ms: float = 300.0,
        congestion_window_size: int = 20,
        recent_window_size: int = 3,
        rtt_mod_ms: float = 75.0,
        default_baseline_ms: float = 55.0,
        quiet_recovery_total_ticks: int = 12,
        ul_quarantine_total_ticks: int = 4,
        lower_baseline_confirm_samples: int = 5,
    ):
        # ── Config ──
        self._ema_alpha = ema_alpha
        self._rtt_warning_ms = rtt_warning_ms
        self._rtt_cliff_ms = rtt_cliff_ms
        self._baseline_min_samples = baseline_min_samples
        self._baseline_max_samples = baseline_max_samples
        self._baseline_trim_frac = baseline_trim_frac
        self._baseline_ema_alpha = baseline_ema_alpha
        self._rtt_mod_ms = rtt_mod_ms
        self._default_baseline_ms = default_baseline_ms

        # ── State ──
        self._ema: float = 40.0
        self._history: deque = deque(maxlen=sliding_window_size)
        self._initialized: bool = False
        self._last_rtt: float = 0.0
        self._last_probe: RttObservation | None = None
        self._active_context_key = ""
        self._last_delta_valid = False
        self._last_delta_reason = "probe_missing"

        # ── Per-context baseline (ONE baseline system) ──
        # Key: "band|pci|earfcn"
        self._baselines: dict[str, float] = {}
        self._baseline_samples: dict[str, list[float]] = {}
        self._baseline_counts: dict[str, int] = {}
        # Ratchet floor: permanent low-water mark per cell.
        # Stores the lowest (best) RTT observed. Baseline can never go above this.
        self._floor: dict[str, float] = {}

        # ── V5 congestion + recent lanes ──
        self._cong_hist: deque = deque(maxlen=congestion_window_size)
        self._recent_hist: deque = deque(maxlen=recent_window_size)
        self._cong_last: float = 0.0

        # ── Congestion-only quiet RTT lane ──
        # This lane is independent from the generic RTT sliding window.
        # During self-traffic we freeze it completely. When quiet returns, a
        # 12-tick recovery window gradually flushes the traffic tail.
        self._quiet_history: deque = deque(maxlen=sliding_window_size)
        self._quiet_last: float = 0.0
        self._quiet_busy: bool = False
        self._quiet_busy_mode: str = ""
        self._quiet_recovery_window: deque = deque(maxlen=max(1, int(quiet_recovery_total_ticks)))
        self._quiet_recovery_ticks: int = 0
        self._quiet_recovery_total_ticks: int = max(1, int(quiet_recovery_total_ticks))
        self._ul_quarantine_total_ticks: int = max(1, int(ul_quarantine_total_ticks))
        self._lower_baseline_confirm_samples: int = max(
            1,
            int(lower_baseline_confirm_samples),
        )
        self._lower_baseline_candidates: dict[str, list[float]] = {}
        self._quiet_recovery_mode: str = ""
        self._quiet_reacquire_active: bool = False
        self._quiet_reacquire_ceiling_margin_ms: float = 25.0
        self._quiet_reacquire_ceiling_factor: float = 1.6

        self._traffic_enter_ticks_required: int = 3
        self._traffic_release_ticks_required: int = 3
        self._traffic_over_ticks: int = 0
        self._traffic_under_ticks: int = 0
        self._traffic_instant_busy_active: bool = False

    def update_thresholds(self, *, rtt_warning_ms: float, rtt_cliff_ms: float) -> None:
        """Update thresholds from calibration profile."""
        self._rtt_warning_ms = rtt_warning_ms
        self._rtt_cliff_ms = rtt_cliff_ms

    @staticmethod
    def context_key(band: int, cell_id: int, earfcn: int) -> str:
        return f"{band}|{cell_id}|{earfcn}"

    def _sliding_avg(self) -> float:
        if not self._history:
            return 0.0
        return sum(self._history) / len(self._history)

    def _learn_baseline(self, ctx_key: str, rtt_ms: float) -> None:
        """Add a qualifying idle sample to the per-context baseline.
        Result is clamped to never exceed the ratchet floor."""
        samples = self._baseline_samples.setdefault(ctx_key, [])
        samples.append(rtt_ms)
        if len(samples) > self._baseline_max_samples:
            samples.pop(0)
        self._baseline_counts[ctx_key] = len(samples)

        if len(samples) >= self._baseline_min_samples:
            sorted_s = sorted(samples)
            trim = max(1, int(len(sorted_s) * self._baseline_trim_frac))
            trimmed = sorted_s[trim:-trim] if trim < len(sorted_s) // 2 else sorted_s
            computed = sum(trimmed) / len(trimmed)
            # Enforce floor — baseline can never rise above the ratchet value
            floor = self._floor.get(ctx_key)
            if floor is not None and computed > floor:
                computed = floor
            self._baselines[ctx_key] = computed

    def _ratchet_baseline(self, ctx_key: str, rtt_ms: float) -> None:
        """Snap baseline down permanently when live is below it.
        Sets a permanent floor — baseline can never rise above this value."""
        self._baselines[ctx_key] = rtt_ms
        self._floor[ctx_key] = rtt_ms
        # Reset samples to the new level so trimmed mean stays consistent
        count = max(self._baseline_min_samples,
                    self._baseline_counts.get(ctx_key, self._baseline_min_samples))
        self._baseline_samples[ctx_key] = [rtt_ms] * count
        self._baseline_counts[ctx_key] = count
        self._lower_baseline_candidates.pop(ctx_key, None)

    def _clear_lower_baseline_candidates(self, ctx_key: str) -> None:
        self._lower_baseline_candidates.pop(ctx_key, None)

    def _confirm_lower_live_baseline(
        self,
        *,
        ctx_key: str,
        rtt_ms: float,
        baseline: float,
        sliding: float,
        traffic: LatencyTrafficQualification,
    ) -> float | None:
        """Return a confirmed lower baseline from bounded fresh RTT evidence.

        The quiet congestion lane may legitimately smooth old samples, but the
        active baseline cannot remain above a better current RTT for a long
        time.  A short confirmed run of qualified fresh samples proves that the
        old floor is stale without waiting for the full quiet window to drain.
        """

        if (
            not traffic.valid
            or not traffic.rtt_baseline_idle
            or sliding >= self._rtt_warning_ms
            or rtt_ms <= 0.0
            or baseline <= 0.0
            or rtt_ms >= baseline
        ):
            self._clear_lower_baseline_candidates(ctx_key)
            return None

        candidates = self._lower_baseline_candidates.setdefault(ctx_key, [])
        candidates.append(float(rtt_ms))
        if len(candidates) > self._lower_baseline_confirm_samples:
            del candidates[: len(candidates) - self._lower_baseline_confirm_samples]
        if len(candidates) < self._lower_baseline_confirm_samples:
            return None

        confirmed = self._median(candidates)
        if 0.0 < confirmed < baseline:
            self._ratchet_baseline(ctx_key, confirmed)
            return float(confirmed)
        return None

    def admit_learned_baseline(self, ctx_key: str, baseline_ms: float) -> bool:
        """Admit one exact-context learned floor without replacing better live truth."""
        try:
            baseline = float(baseline_ms)
        except (TypeError, ValueError):
            return False
        if not 0.0 < baseline < 10_000.0:
            return False
        current = self._floor.get(ctx_key, self._baselines.get(ctx_key))
        if current is not None and float(current) <= baseline:
            return False
        self._ratchet_baseline(ctx_key, baseline)
        return True

    @staticmethod
    def _median(values: list[float]) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        n = len(s)
        m = n // 2
        return float(s[m]) if n % 2 == 1 else float((s[m - 1] + s[m]) / 2.0)

    @staticmethod
    def _avg(values: list[float]) -> float:
        if not values:
            return 0.0
        return float(sum(values) / len(values))

    @staticmethod
    def _percentile(values: list[float], q: float) -> float:
        ordered = sorted(float(v) for v in values)
        if not ordered:
            return 0.0
        if len(ordered) == 1:
            return ordered[0]
        pos = max(0.0, min(1.0, q)) * (len(ordered) - 1)
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return ordered[lo]
        frac = pos - lo
        return ordered[lo] * (1.0 - frac) + ordered[hi] * frac

    def _trimmed_mean(self, values: list[float], trim_frac: float) -> float:
        ordered = sorted(float(v) for v in values if float(v) >= 0.0)
        if not ordered:
            return 0.0
        trim = int(len(ordered) * max(0.0, min(0.49, float(trim_frac))))
        if trim > 0 and (len(ordered) - (2 * trim)) >= 1:
            ordered = ordered[trim:-trim]
        return self._avg(ordered)

    def _quiet_avg(self) -> float:
        if not self._quiet_history:
            return 0.0
        return self._avg([float(v) for v in self._quiet_history])

    def _quiet_recovery_anchor(
        self,
        *,
        baseline: float,
        sliding: float,
        rtt_ms: float,
    ) -> float:
        anchor = float(self._quiet_last or self._quiet_avg() or 0.0)
        if anchor > 0.0:
            return anchor
        if sliding > 0.0:
            return float(sliding)
        if baseline > 0.0:
            return float(baseline)
        return float(rtt_ms)

    def _quiet_recovery_drop_count(self) -> int:
        ticks = max(0, int(self._quiet_recovery_ticks))
        if ticks <= 0:
            return 0
        if ticks <= 6:
            return 6
        return max(0, self._quiet_recovery_total_ticks - ticks)

    def _quiet_recovery_avg(self) -> float:
        values = [float(v) for v in self._quiet_recovery_window]
        if not values:
            return 0.0
        drop_count = min(self._quiet_recovery_drop_count(), max(0, len(values) - 1))
        keep_count = max(1, len(values) - drop_count)
        kept = sorted(values)[:keep_count]
        return self._avg(kept)

    def update(
        self,
        rtt_observation: RttObservation,
        traffic: LatencyTrafficQualification,
        ctx_key: str,
        cs: ComputedSignals,
    ) -> None:
        """Process one tick of RTT data."""
        if not isinstance(traffic, LatencyTrafficQualification):
            raise TypeError("rtt_processor_latency_traffic_contract_required")
        if not isinstance(rtt_observation, RttObservation):
            raise TypeError("rtt_processor_typed_observation_required")
        previous = self._last_probe
        if previous is not None and previous.namespace == rtt_observation.namespace and (
            rtt_observation.probe_sequence < previous.probe_sequence
        ):
            cs.rtt_delta_block_reason = "probe_out_of_order"
            return
        new_sample = rtt_observation.is_new_sample_after(previous)
        self._last_probe = rtt_observation
        if self._active_context_key and (
            self._active_context_key != ctx_key
            or (previous is not None and previous.namespace != rtt_observation.namespace)
        ):
            # Quiet windows describe one physical context, not its successor.
            self._history.clear()
            self._cong_hist.clear()
            self._recent_hist.clear()
            self._cong_last = 0.0
            self._quiet_history.clear()
            self._quiet_recovery_window.clear()
            self._quiet_last = 0.0
            self._quiet_recovery_ticks = 0
            self._quiet_recovery_mode = ""
            self._quiet_reacquire_active = False
            self._quiet_busy = False
            self._quiet_busy_mode = ""
            self._traffic_over_ticks = self._traffic_under_ticks = 0
            self._traffic_instant_busy_active = False
            self._initialized = False
            self._last_delta_valid = False
            self._last_delta_reason = "probe_not_current_context"
        self._active_context_key = ctx_key
        rtt_ms = rtt_observation.value_ms if rtt_observation.has_fresh_sample else None
        sliding = self._sliding_avg()
        if rtt_ms is None:
            cs.rtt_ms = self._last_rtt
            cs.rtt_ema = self._ema
            cs.rtt_sliding_avg = sliding
            cs.rtt_probe_ok = False
            cs.rtt_elevated = sliding > self._rtt_warning_ms
            cs.rtt_alarm = sliding > self._rtt_cliff_ms
            cs.rtt_pressure_level = "probe_missing"
        elif new_sample:
            self._last_rtt = rtt_ms
            cs.rtt_ms = rtt_ms
            cs.rtt_probe_ok = True

            if not self._initialized:
                self._ema = rtt_ms
                self._initialized = True

            # EMA — always updates
            self._ema += self._ema_alpha * (rtt_ms - self._ema)
            cs.rtt_ema = self._ema

            # Sliding window
            self._history.append(rtt_ms)
            sliding = self._sliding_avg()
            cs.rtt_sliding_avg = sliding

            # Baseline learning — idle AND calm only
            if traffic.valid and traffic.rtt_baseline_idle and sliding < self._rtt_warning_ms:
                self._learn_baseline(ctx_key, rtt_ms)

            # Elevated / alarm
            cs.rtt_elevated = sliding > self._rtt_warning_ms
            cs.rtt_alarm = sliding > self._rtt_cliff_ms
            if cs.rtt_alarm:
                cs.rtt_pressure_level = "alarm"
            elif cs.rtt_elevated:
                cs.rtt_pressure_level = "busy"
            else:
                cs.rtt_pressure_level = "normal"

        else:
            cs.rtt_ms = rtt_ms
            cs.rtt_probe_ok = True
            cs.rtt_ema = self._ema
            cs.rtt_sliding_avg = sliding
            cs.rtt_elevated = sliding > self._rtt_warning_ms
            cs.rtt_alarm = sliding > self._rtt_cliff_ms
            cs.rtt_pressure_level = "alarm" if cs.rtt_alarm else "busy" if cs.rtt_elevated else "normal"

        # ── V5 congestion + recent lanes ──
        if new_sample:
            self._cong_hist.append(rtt_ms)
            self._recent_hist.append(rtt_ms)

        if self._cong_hist:
            cong_avg = sum(self._cong_hist) / len(self._cong_hist)
            self._cong_last = cong_avg
        else:
            cong_avg = self._cong_last or sliding or 0.0

        cs.rtt_congestion_avg = float(cong_avg)

        recent_vals = [float(v) for v in self._recent_hist]
        cs.rtt_recent_median = self._median(recent_vals)

        # ── Delta: congestion quiet lane vs per-context baseline ──
        cs.rtt_delta_block_reason = ""
        baseline = self._baselines.get(ctx_key)
        sample_count = self._baseline_counts.get(ctx_key, 0)
        baseline_learned = sample_count >= self._baseline_min_samples
        # Use config default if no learned baseline exists yet
        if baseline is None:
            baseline = self._floor.get(ctx_key, self._default_baseline_ms)
            self._baselines[ctx_key] = baseline

        if not traffic.valid:
            self._clear_lower_baseline_candidates(ctx_key)
            cs.rtt_delta = 0.0
            cs.rtt_delta_valid = False
            cs.rtt_delta_block_reason = traffic.invalid_reason
            cs.rtt_baseline = float(baseline)
            cs.rtt_effective_baseline = float(baseline)
            self._last_delta_valid = False
            self._last_delta_reason = traffic.invalid_reason
            return

        if self._quiet_busy:
            if self._traffic_instant_busy_active:
                busy_now = traffic.rtt_busy_now
                if not busy_now:
                    self._traffic_instant_busy_active = False
                    self._traffic_under_ticks = 0
                    self._traffic_over_ticks = 0
            else:
                if traffic.rtt_release_safe:
                    self._traffic_under_ticks = min(
                        self._traffic_release_ticks_required,
                        self._traffic_under_ticks + 1,
                    )
                else:
                    self._traffic_under_ticks = 0
                if traffic.rtt_instant_busy:
                    self._traffic_over_ticks = self._traffic_enter_ticks_required
                busy_now = self._traffic_under_ticks < self._traffic_release_ticks_required
        else:
            if traffic.rtt_instant_busy:
                self._traffic_over_ticks = self._traffic_enter_ticks_required
                self._traffic_instant_busy_active = True
            elif traffic.rtt_busy_now:
                self._traffic_over_ticks = min(
                    self._traffic_enter_ticks_required,
                    self._traffic_over_ticks + 1,
                )
            else:
                self._traffic_over_ticks = 0
                self._traffic_instant_busy_active = False
            self._traffic_under_ticks = 0
            busy_now = self._traffic_over_ticks >= self._traffic_enter_ticks_required
        effective_baseline = baseline
        quiet_avg = float(self._quiet_last or self._quiet_avg() or 0.0)

        if busy_now:
            self._clear_lower_baseline_candidates(ctx_key)
            if not self._quiet_busy:
                self._quiet_busy = True
                self._quiet_busy_mode = traffic.rtt_busy_mode or "dl"
                self._quiet_recovery_window.clear()
                self._quiet_recovery_ticks = 0
                self._quiet_recovery_mode = ""
                self._quiet_reacquire_active = False
            elif traffic.rtt_busy_mode == "ul":
                self._quiet_busy_mode = "ul"
            cs.rtt_delta = 0.0
            cs.rtt_delta_valid = False
            cs.rtt_delta_block_reason = (
                "ul_contamination" if self._quiet_busy_mode == "ul" else "traffic_busy"
            )
        elif rtt_ms is None or rtt_ms <= 0.0:
            self._clear_lower_baseline_candidates(ctx_key)
            cs.rtt_delta = 0.0
            cs.rtt_delta_valid = False
            cs.rtt_delta_block_reason = "probe_missing"
        elif not new_sample:
            # No new ping is not failure, nor a new quiet/recovery sample.
            cs.rtt_delta_valid = self._last_delta_valid
            cs.rtt_delta = max(0.0, quiet_avg - effective_baseline) if self._last_delta_valid else 0.0
            cs.rtt_delta_block_reason = self._last_delta_reason
        else:
            if self._quiet_busy:
                self._quiet_recovery_ticks = 0
                self._quiet_recovery_mode = (
                    "ul_quarantine" if self._quiet_busy_mode == "ul" else "dl_hold"
                )
                if self._quiet_recovery_mode == "ul_quarantine":
                    self._quiet_recovery_window.clear()
                else:
                    anchor = self._quiet_recovery_anchor(
                        baseline=float(baseline),
                        sliding=float(sliding),
                        rtt_ms=float(rtt_ms),
                    )
                    self._quiet_recovery_window = deque(
                        [float(anchor)] * self._quiet_recovery_total_ticks,
                        maxlen=self._quiet_recovery_total_ticks,
                    )
                self._quiet_busy = False
                self._quiet_busy_mode = ""

            finished_ul_quarantine = False
            if self._quiet_recovery_mode == "ul_quarantine":
                quarantine_total = max(1, int(self._ul_quarantine_total_ticks))
                self._quiet_recovery_ticks = min(
                    quarantine_total,
                    self._quiet_recovery_ticks + 1,
                )
                quiet_avg = 0.0
                if self._quiet_recovery_ticks >= quarantine_total:
                    self._quiet_recovery_window.clear()
                    self._quiet_recovery_ticks = 0
                    self._quiet_history.clear()
                    self._quiet_last = float(baseline)
                    self._quiet_reacquire_active = True
                    finished_ul_quarantine = True
                    self._quiet_recovery_mode = ""
            elif self._quiet_recovery_window:
                self._quiet_recovery_window.append(float(rtt_ms))
                self._quiet_recovery_ticks = min(
                    self._quiet_recovery_total_ticks,
                    self._quiet_recovery_ticks + 1,
                )
                quiet_avg = self._quiet_recovery_avg()
                if self._quiet_recovery_ticks >= self._quiet_recovery_total_ticks:
                    self._quiet_history = deque(
                        [float(v) for v in self._quiet_recovery_window],
                        maxlen=self._quiet_history.maxlen,
                    )
                    self._quiet_recovery_window.clear()
                    self._quiet_recovery_ticks = 0
                    quiet_avg = self._quiet_avg()
                    self._quiet_recovery_mode = ""
            else:
                self._quiet_history.append(float(rtt_ms))
                quiet_avg = self._quiet_avg()

            if quiet_avg > 0.0 and self._quiet_recovery_mode != "ul_quarantine":
                self._quiet_last = float(quiet_avg)

            reacquire_ceiling = max(
                float(baseline) * self._quiet_reacquire_ceiling_factor,
                float(baseline) + self._quiet_reacquire_ceiling_margin_ms,
            )
            reacquire_ready = bool(
                self._quiet_reacquire_active
                and rtt_ms is not None
                and float(rtt_ms) <= reacquire_ceiling
            )
            confirmed_lower_baseline = self._confirm_lower_live_baseline(
                ctx_key=ctx_key,
                rtt_ms=float(rtt_ms),
                baseline=float(baseline),
                sliding=float(sliding),
                traffic=traffic,
            )
            if confirmed_lower_baseline is not None:
                baseline = confirmed_lower_baseline
                effective_baseline = baseline
                baseline_learned = True

            if self._quiet_recovery_mode == "ul_quarantine" or finished_ul_quarantine:
                cs.rtt_delta = 0.0
                cs.rtt_delta_valid = False
                cs.rtt_delta_block_reason = "ul_contamination"
            elif self._quiet_reacquire_active and not reacquire_ready:
                cs.rtt_delta = 0.0
                cs.rtt_delta_valid = False
                cs.rtt_delta_block_reason = "ul_contamination"
            elif baseline_learned and quiet_avg > 0.0:
                if self._quiet_reacquire_active:
                    self._quiet_history = deque(
                        [float(rtt_ms)],
                        maxlen=self._quiet_history.maxlen,
                    )
                    quiet_avg = self._quiet_avg()
                self._quiet_reacquire_active = False
                raw_delta = quiet_avg - effective_baseline
                cs.rtt_delta = max(0.0, raw_delta)
                cs.rtt_delta_valid = True
            else:
                cs.rtt_delta = 0.0
                cs.rtt_delta_valid = False

            if (
                not self._quiet_recovery_window
                and self._quiet_recovery_mode != "ul_quarantine"
                and quiet_avg > 0.0
                and quiet_avg < baseline
            ):
                self._ratchet_baseline(ctx_key, quiet_avg)
                baseline = quiet_avg
                effective_baseline = baseline

        cs.rtt_baseline = float(baseline)
        cs.rtt_effective_baseline = float(effective_baseline)
        self._last_delta_valid = cs.rtt_delta_valid
        self._last_delta_reason = cs.rtt_delta_block_reason

        if self._quiet_recovery_window:
            cs.rtt_congestion_avg = float(quiet_avg)
            cs.rtt_recent_median = float(quiet_avg)
        elif self._quiet_history:
            cs.rtt_congestion_avg = float(quiet_avg)
            cs.rtt_recent_median = float(quiet_avg)
        elif self._quiet_last > 0.0:
            cs.rtt_congestion_avg = float(self._quiet_last)
            cs.rtt_recent_median = float(self._quiet_last)

    def export_state(self) -> dict[str, Any]:
        return {
            "last_probe": self._last_probe.as_dict() if self._last_probe else None,
            "active_context_key": self._active_context_key,
            "last_delta_valid": self._last_delta_valid,
            "last_delta_reason": self._last_delta_reason,
            "ema": float(self._ema),
            "history": [float(v) for v in self._history],
            "initialized": bool(self._initialized),
            "last_rtt": float(self._last_rtt),
            "baselines": {k: float(v) for k, v in self._baselines.items()},
            "floor": {k: float(v) for k, v in self._floor.items()},
            "baseline_counts": dict(self._baseline_counts),
            "baseline_samples": {k: list(v[-self._baseline_max_samples:])
                                 for k, v in self._baseline_samples.items()},
            "cong_hist": [float(v) for v in self._cong_hist],
            "recent_hist": [float(v) for v in self._recent_hist],
            "cong_last": float(self._cong_last),
            "quiet_history": [float(v) for v in self._quiet_history],
            "quiet_last": float(self._quiet_last),
            "quiet_busy": bool(self._quiet_busy),
            "quiet_busy_mode": str(self._quiet_busy_mode),
            "quiet_recovery_window": [float(v) for v in self._quiet_recovery_window],
            "quiet_recovery_ticks": int(self._quiet_recovery_ticks),
            "quiet_recovery_mode": str(self._quiet_recovery_mode),
            "quiet_reacquire_active": bool(self._quiet_reacquire_active),
            "lower_baseline_candidates": {
                str(k): [float(v) for v in values[-self._lower_baseline_confirm_samples:]]
                for k, values in self._lower_baseline_candidates.items()
            },
            "traffic_over_ticks": int(self._traffic_over_ticks),
            "traffic_under_ticks": int(self._traffic_under_ticks),
            "traffic_instant_busy_active": bool(self._traffic_instant_busy_active),
        }

    def restore_state(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            raw_probe = payload.get("last_probe")
            self._last_probe = RttObservation.from_mapping(raw_probe) if raw_probe is not None else None
            self._active_context_key = str(payload.get("active_context_key", ""))
            self._last_delta_valid = payload.get("last_delta_valid") is True
            self._last_delta_reason = str(payload.get("last_delta_reason", "probe_missing"))
            self._ema = float(payload.get("ema", self._ema) or self._ema)
            self._history = deque(
                [float(v) for v in list(payload.get("history", []))[-self._history.maxlen:]],
                maxlen=self._history.maxlen,
            )
            self._initialized = bool(payload.get("initialized", self._initialized))
            self._last_rtt = float(payload.get("last_rtt", self._last_rtt) or self._last_rtt)
            raw_b = payload.get("baselines")
            if "baselines" in payload and isinstance(raw_b, dict):
                self._baselines = {k: float(v) for k, v in raw_b.items() if isinstance(v, (int, float))}
            raw_f = payload.get("floor")
            if "floor" in payload and isinstance(raw_f, dict):
                self._floor = {k: float(v) for k, v in raw_f.items() if isinstance(v, (int, float))}
            raw_c = payload.get("baseline_counts")
            if "baseline_counts" in payload and isinstance(raw_c, dict):
                self._baseline_counts = {k: int(v) for k, v in raw_c.items() if isinstance(v, (int, float))}
            raw_s = payload.get("baseline_samples")
            if "baseline_samples" in payload and isinstance(raw_s, dict):
                self._baseline_samples = {
                    k: [float(s) for s in v if isinstance(s, (int, float))]
                    for k, v in raw_s.items() if isinstance(v, list)
                }
            self._cong_hist = deque(
                [float(v) for v in list(payload.get("cong_hist", []))[-self._cong_hist.maxlen:]],
                maxlen=self._cong_hist.maxlen,
            )
            self._recent_hist = deque(
                [float(v) for v in list(payload.get("recent_hist", []))[-self._recent_hist.maxlen:]],
                maxlen=self._recent_hist.maxlen,
            )
            self._cong_last = float(payload.get("cong_last", self._cong_last) or self._cong_last)
            self._quiet_history = deque(
                [float(v) for v in list(payload.get("quiet_history", []))[-self._quiet_history.maxlen:]],
                maxlen=self._quiet_history.maxlen,
            )
            self._quiet_last = float(payload.get("quiet_last", self._quiet_last) or self._quiet_last)
            self._quiet_busy = bool(payload.get("quiet_busy", self._quiet_busy))
            self._quiet_busy_mode = str(payload.get("quiet_busy_mode", self._quiet_busy_mode) or "")
            self._quiet_recovery_window = deque(
                [float(v) for v in list(payload.get("quiet_recovery_window", []))[-self._quiet_recovery_window.maxlen:]],
                maxlen=self._quiet_recovery_window.maxlen,
            )
            self._quiet_recovery_ticks = int(payload.get("quiet_recovery_ticks", self._quiet_recovery_ticks) or self._quiet_recovery_ticks)
            self._quiet_recovery_mode = str(payload.get("quiet_recovery_mode", self._quiet_recovery_mode) or "")
            self._quiet_reacquire_active = bool(payload.get("quiet_reacquire_active", self._quiet_reacquire_active))
            raw_lower = payload.get("lower_baseline_candidates")
            if isinstance(raw_lower, dict):
                self._lower_baseline_candidates = {
                    str(k): [float(v) for v in list(values)[-self._lower_baseline_confirm_samples:] if isinstance(v, (int, float))]
                    for k, values in raw_lower.items()
                    if isinstance(values, list)
                }
            self._traffic_over_ticks = int(payload.get("traffic_over_ticks", self._traffic_over_ticks) or self._traffic_over_ticks)
            self._traffic_under_ticks = int(payload.get("traffic_under_ticks", self._traffic_under_ticks) or self._traffic_under_ticks)
            self._traffic_instant_busy_active = bool(payload.get("traffic_instant_busy_active", self._traffic_instant_busy_active))
            return True
        except Exception:
            return False
