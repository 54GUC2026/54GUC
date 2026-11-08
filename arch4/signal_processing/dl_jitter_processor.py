"""
arch4/signal_processing/dl_jitter_processor.py
═══════════════════════════════════════════════════════════════
Dedicated DL jitter processor.

Owns the normalized downlink RTT-step jitter lane used by Core consumers:
  - no final congestion weighting authority
  - Core-owned 0-10 Mbps DL qualification
  - strict UL quiet gate

Inputs:
  - raw RTT samples
  - Core-owned LatencyTrafficQualification
  - per-cell context key

Outputs on ComputedSignals:
  - rtt_active_traffic_class
  - rtt_active_load_mbps
  - rtt_active_jitter_mad_ms
  - rtt_active_jitter_step_p95_ms
  - rtt_active_jitter_samples
  - rtt_active_jitter_valid
  - rtt_dl_jitter_bucket
  - rtt_dl_jitter_trimmed_ms
  - rtt_dl_jitter_baseline_ms
  - rtt_dl_jitter_delta_ms
  - rtt_dl_jitter_pct
  - rtt_dl_jitter_valid
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any, Optional

from arch4.core_contracts.signals import LatencyTrafficQualification
from arch4.interfaces import ComputedSignals
from arch4.rtt_observation import RttObservation


class DLJitterProcessor:
    DEFAULT_VISIBLE_JITTER_SCALE = 3.0

    def __init__(
        self,
        *,
        pct_scale_ms: float = 5.0,
        visible_scale: float = DEFAULT_VISIBLE_JITTER_SCALE,
        baseline_min_windows: int = 3,
        sample_max: int = 60,
        low_frac: float = 0.20,
        trim_frac: float = 0.10,
    ) -> None:
        self._pct_scale_ms = max(0.1, float(pct_scale_ms))
        self._baseline_min_windows = max(1, int(baseline_min_windows))
        self._sample_max = max(3, int(sample_max))
        self._low_frac = max(0.01, min(0.49, float(low_frac)))
        self._trim_frac = max(0.0, min(0.49, float(trim_frac)))
        self._visible_jitter_scale = max(0.1, min(50.0, float(visible_scale)))

        self._active_rtt_hist: deque[float] = deque(maxlen=6)
        self._active_step_hist: deque[float] = deque(maxlen=6)
        self._active_class: str = "unavailable"
        self._active_context_key: str = ""
        self._active_last_rtt: Optional[float] = None
        self._last_probe: RttObservation | None = None
        self._dl_jitter_baselines: dict[str, float] = {}
        self._dl_jitter_samples: dict[str, list[float]] = {}

    @staticmethod
    def _median(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(float(v) for v in values)
        n = len(ordered)
        mid = n // 2
        return float(ordered[mid]) if (n % 2) == 1 else float((ordered[mid - 1] + ordered[mid]) / 2.0)

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
        pos = max(0.0, min(1.0, float(q))) * (len(ordered) - 1)
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return ordered[lo]
        frac = pos - lo
        return ordered[lo] * (1.0 - frac) + ordered[hi] * frac

    def _trimmed_mean(self, values: list[float]) -> float:
        ordered = sorted(float(v) for v in values if float(v) >= 0.0)
        if not ordered:
            return 0.0
        trim = int(len(ordered) * self._trim_frac)
        if trim > 0 and (len(ordered) - (2 * trim)) >= 1:
            ordered = ordered[trim:-trim]
        return self._avg(ordered)

    def _learn_dl_jitter_baseline(self, key: str, observation_ms: float) -> tuple[float, bool]:
        samples = self._dl_jitter_samples.setdefault(key, [])
        samples.append(float(observation_ms))
        if len(samples) > self._sample_max:
            samples.pop(0)
        ordered = sorted(samples)
        low_keep = max(1, int(math.ceil(len(ordered) * self._low_frac)))
        candidate = self._avg(ordered[:low_keep])
        old = self._dl_jitter_baselines.get(key)
        if old is not None:
            baseline = min(float(old), candidate)
            self._dl_jitter_baselines[key] = float(baseline)
            return float(baseline), True
        if len(samples) < self._baseline_min_windows:
            return 0.0, False
        baseline = float(candidate)
        self._dl_jitter_baselines[key] = float(baseline)
        return float(baseline), True

    def update(
        self,
        *,
        rtt_observation: RttObservation,
        traffic: LatencyTrafficQualification,
        ctx_key: str,
        cs: ComputedSignals,
    ) -> None:
        if not isinstance(traffic, LatencyTrafficQualification):
            raise TypeError("dl_jitter_latency_traffic_contract_required")
        if not isinstance(rtt_observation, RttObservation):
            raise TypeError("dl_jitter_typed_observation_required")
        previous = self._last_probe
        if previous is not None and previous.namespace == rtt_observation.namespace and (
            rtt_observation.probe_sequence < previous.probe_sequence
        ):
            cs.rtt_dl_jitter_reason = "probe_out_of_order"
            return
        new_sample = rtt_observation.is_new_sample_after(previous)
        self._last_probe = rtt_observation
        traffic_class = "qualified" if traffic.jitter_valid else "unavailable"
        load_mbps = traffic.jitter_load_mbps
        cs.rtt_active_traffic_class = traffic_class
        cs.rtt_active_load_mbps = float(load_mbps)
        cs.rtt_dl_jitter_bucket = ""
        cs.rtt_dl_jitter_trimmed_ms = 0.0
        cs.rtt_dl_jitter_baseline_ms = 0.0
        cs.rtt_dl_jitter_delta_ms = 0.0
        cs.rtt_dl_jitter_pct = 0.0
        cs.rtt_dl_jitter_valid = False
        cs.rtt_dl_jitter_reason = ""

        context_key = str(ctx_key or "")
        if context_key != self._active_context_key or (
            previous is not None and previous.namespace != rtt_observation.namespace
        ):
            self._active_context_key = context_key
            self._active_rtt_hist.clear()
            self._active_step_hist.clear()
            self._active_last_rtt = None

        if not traffic.jitter_valid:
            self._active_class = "unavailable"
            self._active_last_rtt = None
            cs.rtt_active_jitter_mad_ms = 0.0
            cs.rtt_active_jitter_step_p95_ms = 0.0
            cs.rtt_active_jitter_samples = 0
            cs.rtt_active_jitter_valid = False
            cs.rtt_dl_jitter_reason = traffic.jitter_reason
            return

        if not rtt_observation.has_fresh_sample:
            # Keep qualified samples, but do not publish expired/failed probe
            # coverage as fresh jitter or connect a step across the gap.
            self._active_last_rtt = None
            cs.rtt_dl_jitter_reason = "probe_" + rtt_observation.freshness if rtt_observation.probe_status == "ok" else "probe_missing"
            return

        if traffic_class != self._active_class:
            self._active_class = traffic_class
            self._active_last_rtt = None

        if new_sample:
            current = rtt_observation.value_ms
            self._active_rtt_hist.append(current)
            if self._active_last_rtt is not None:
                self._active_step_hist.append(abs(current - self._active_last_rtt))
            self._active_last_rtt = current

        rtt_values = [float(v) for v in self._active_rtt_hist]
        step_values = [float(v) for v in self._active_step_hist]
        if rtt_values:
            median = self._median(rtt_values)
            deviations = [abs(v - median) for v in rtt_values]
            cs.rtt_active_jitter_mad_ms = float(self._median(deviations))
        else:
            cs.rtt_active_jitter_mad_ms = 0.0
        cs.rtt_active_jitter_step_p95_ms = float(self._percentile(step_values, 0.95))
        cs.rtt_active_jitter_samples = len(rtt_values)
        cs.rtt_active_jitter_valid = len(rtt_values) >= 3 and len(step_values) >= 2
        if not cs.rtt_active_jitter_valid:
            cs.rtt_dl_jitter_reason = "jitter_window_warming"
            return

        bucket = "0_10"
        trimmed_ms = float(self._trimmed_mean(step_values))
        baseline_key = f"{context_key}|dl_jitter|{bucket}"
        if new_sample:
            baseline_ms, baseline_ready = self._learn_dl_jitter_baseline(baseline_key, trimmed_ms)
        else:
            baseline_ms = self._dl_jitter_baselines.get(baseline_key, 0.0)
            baseline_ready = baseline_key in self._dl_jitter_baselines
        delta_ms = max(0.0, trimmed_ms - baseline_ms) if baseline_ready else trimmed_ms
        # Keep the millisecond lane raw, but amplify the normalized operator
        # coverage so short DL jitter episodes remain visible on graphs/logs.
        pct = max(0.0, min(100.0, (delta_ms / self._pct_scale_ms) * self._visible_jitter_scale))
        cs.rtt_dl_jitter_bucket = bucket
        cs.rtt_dl_jitter_trimmed_ms = trimmed_ms
        cs.rtt_dl_jitter_baseline_ms = baseline_ms if baseline_ready else 0.0
        cs.rtt_dl_jitter_delta_ms = delta_ms
        cs.rtt_dl_jitter_pct = pct
        cs.rtt_dl_jitter_valid = True
        cs.rtt_dl_jitter_reason = ""

    def update_formula(
        self,
        *,
        pct_scale_ms: float | None = None,
        visible_scale: float | None = None,
        trim_frac: float | None = None,
        low_frac: float | None = None,
    ) -> dict[str, Any]:
        """Update live formula knobs for normalized DL jitter output."""
        if pct_scale_ms is not None:
            self._pct_scale_ms = max(0.1, min(250.0, float(pct_scale_ms)))
        if visible_scale is not None:
            self._visible_jitter_scale = max(0.1, min(50.0, float(visible_scale)))
        if trim_frac is not None:
            self._trim_frac = max(0.0, min(0.49, float(trim_frac)))
        if low_frac is not None:
            self._low_frac = max(0.01, min(0.49, float(low_frac)))
        return self.formula_status()

    def formula_status(self) -> dict[str, Any]:
        return {
            "formula": "pct = clamp((max(0, trimmed_step_ms - baseline_ms) / pct_scale_ms) * visible_scale, 0, 100)",
            "pct_scale_ms": float(self._pct_scale_ms),
            "visible_scale": float(self._visible_jitter_scale),
            "trim_frac": float(self._trim_frac),
            "low_frac": float(self._low_frac),
            "baseline_min_windows": int(self._baseline_min_windows),
            "sample_max": int(self._sample_max),
            "owner": "DLJitterProcessor",
        }

    def export_state(self) -> dict[str, Any]:
        return {
            "last_probe": self._last_probe.as_dict() if self._last_probe else None,
            "active_context_key": self._active_context_key,
            "active_class": str(self._active_class),
            "active_last_rtt": None if self._active_last_rtt is None else float(self._active_last_rtt),
            "active_rtt_hist": [float(v) for v in self._active_rtt_hist],
            "active_step_hist": [float(v) for v in self._active_step_hist],
            "formula": self.formula_status(),
            "dl_jitter_baselines": {k: float(v) for k, v in self._dl_jitter_baselines.items()},
            "dl_jitter_samples": {
                k: [float(v) for v in values[-self._sample_max:]]
                for k, values in self._dl_jitter_samples.items()
            },
        }

    def restore_state(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            raw_probe = payload.get("last_probe")
            self._last_probe = RttObservation.from_mapping(raw_probe) if raw_probe is not None else None
            self._active_context_key = str(payload.get("active_context_key", ""))
            self._active_class = str(
                payload.get("active_class", self._active_class) or "unavailable"
            )
            raw_last = payload.get("active_last_rtt", self._active_last_rtt)
            self._active_last_rtt = None if raw_last in (None, "") else float(raw_last)
            self._active_rtt_hist = deque(
                [float(v) for v in list(payload.get("active_rtt_hist", []))[-self._active_rtt_hist.maxlen:]],
                maxlen=self._active_rtt_hist.maxlen,
            )
            self._active_step_hist = deque(
                [float(v) for v in list(payload.get("active_step_hist", []))[-self._active_step_hist.maxlen:]],
                maxlen=self._active_step_hist.maxlen,
            )
            formula = payload.get("formula")
            if isinstance(formula, dict):
                self.update_formula(
                    pct_scale_ms=formula.get("pct_scale_ms"),
                    visible_scale=formula.get("visible_scale"),
                    trim_frac=formula.get("trim_frac"),
                    low_frac=formula.get("low_frac"),
                )
            raw_jb = payload.get("dl_jitter_baselines", {})
            if isinstance(raw_jb, dict):
                self._dl_jitter_baselines = {k: float(v) for k, v in raw_jb.items() if isinstance(v, (int, float))}
            raw_js = payload.get("dl_jitter_samples", {})
            if isinstance(raw_js, dict):
                self._dl_jitter_samples = {
                    k: [float(s) for s in values if isinstance(s, (int, float))]
                    for k, values in raw_js.items()
                    if isinstance(values, list)
                }
            return True
        except Exception:
            return False

    def restore_checkpoint_rtt_state(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            raw_jb = payload.get("dl_jitter_baselines", {})
            raw_js = payload.get("dl_jitter_samples", {})
            if isinstance(raw_jb, dict):
                self._dl_jitter_baselines = {k: float(v) for k, v in raw_jb.items() if isinstance(v, (int, float))}
            if isinstance(raw_js, dict):
                self._dl_jitter_samples = {
                    k: [float(s) for s in values if isinstance(s, (int, float))]
                    for k, values in raw_js.items()
                    if isinstance(values, list)
                }
            return True
        except Exception:
            return False
