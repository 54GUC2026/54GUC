"""
arch4/signal_processing/congestion_combiner.py
═══════════════════════════════════════════════════════════════
Congestion Combiner — produces the fast combined congestion signal.

NOT a signal processor. This is a combiner that reads outputs
from RTTProcessor and RSRQProcessor (via ComputedSignals fields)
and produces the weighted congestion percentage + trend.

Inputs:
  Canonical path:
    CongestionInputContract from CongestionModule
  Test/replay boundary:
    callers explicitly construct CongestionInputContract from typed fields

Outputs on ComputedSignals:
  .congestion_pct              weighted average (0-100, clamped)
  .congestion_raw_pct          unclamped for logs
  .congestion_source           dominant contributor
  .congestion_rsrq_held        True when RSRQ contribution is held
  .congestion_internal_bucket  fast bucket before user-facing stabilization
  .congestion_slope            pct-points per minute (3-min window)
  .congestion_rising           True when slope > threshold

Weights: RTT 50%, RSRQ 50%
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from arch4.coerce import congestion_bucket_for_pct
from arch4.interfaces import ComputedSignals, DEFAULT_CONGESTION_RSRQ_GAP_THRESHOLD_DB

log = logging.getLogger("arch4.signal_processing.congestion")


@dataclass(frozen=True)
class RTTCongestionLane:
    ctx_key: str
    delta_ms: float
    valid: bool
    block_reason: str
    supported: bool = True
    jitter_excess_ms: float = 0.0
    jitter_valid: bool = False
    jitter_reason: str = ""


@dataclass(frozen=True)
class RSRQCongestionLane:
    ctx_key: str
    gap_db: float
    valid: bool
    baseline_db: float
    display_gap_db: float
    display_valid: bool
    display_held: bool
    supported: bool = True


@dataclass(frozen=True)
class CongestionInputContract:
    rtt: RTTCongestionLane
    rsrq: RSRQCongestionLane

    @classmethod
    def from_computed(
        cls,
        cs: ComputedSignals,
        *,
        rsrq_ctx_key: str,
        rtt_ctx_key: str | None = None,
    ) -> "CongestionInputContract":
        rtt_hold_ctx = str(rtt_ctx_key or rsrq_ctx_key or "")
        rsrq_ctx = str(rsrq_ctx_key or "")
        return cls(
            rtt=RTTCongestionLane(
                ctx_key=rtt_hold_ctx,
                delta_ms=max(0.0, float(getattr(cs, "rtt_delta", 0.0) or 0.0)),
                valid=bool(getattr(cs, "rtt_delta_valid", False)),
                block_reason=str(getattr(cs, "rtt_delta_block_reason", "") or "").strip().lower(),
                supported=True,
                jitter_excess_ms=max(
                    0.0,
                    float(getattr(cs, "rtt_dl_jitter_delta_ms", 0.0) or 0.0),
                ),
                jitter_valid=bool(getattr(cs, "rtt_dl_jitter_valid", False)),
                jitter_reason=str(
                    getattr(cs, "rtt_dl_jitter_reason", "") or ""
                ).strip().lower(),
            ),
            rsrq=RSRQCongestionLane(
                ctx_key=rsrq_ctx,
                gap_db=max(0.0, float(getattr(cs, "rsrq_gap", 0.0) or 0.0)),
                valid=bool(getattr(cs, "rsrq_gap_valid", False)),
                baseline_db=float(getattr(cs, "rsrq_baseline", 0.0) or 0.0),
                display_gap_db=max(0.0, float(getattr(cs, "rsrq_display_gap", 0.0) or 0.0)),
                display_valid=bool(getattr(cs, "rsrq_display_gap_valid", False)),
                display_held=bool(getattr(cs, "rsrq_display_gap_held", False)),
                supported=True,
            ),
        )


class CongestionCombiner:
    # Skip ticks after delta becomes valid (settling tail suppression).
    # RSRQ still settles here. RTT settling now belongs upstream in the
    # dedicated congestion quiet/recovery lane inside RTTProcessor.
    _RSRQ_SKIP_TICKS: int = 2
    _RTT_SKIP_TICKS: int = 0
    _WEIGHT_STEP_PCT: float = 5.0
    _RTT_JITTER_START_MS: float = 10.0
    _RTT_JITTER_FULL_MS: float = 30.0
    _RTT_JITTER_MAX_ADDITION_PCT: float = 40.0

    def __init__(
        self,
        *,
        rtt_weight: float = 50.0,
        rsrq_weight: float = 50.0,
        rtt_floor_ms: float = 5.0,
        rtt_ceiling_ms: float = 150.0,
        rsrq_gap_threshold_db: float = DEFAULT_CONGESTION_RSRQ_GAP_THRESHOLD_DB,
        rsrq_ceiling_db: float = 6.0,
        rsrq_hold_s: float = 30.0,
        rtt_hold_s: float | None = None,
        combined_hold_s: float = 30.0,
        trend_window_size: int = 90,
        trend_min_samples: int = 15,
        trend_rising_threshold: float = 3.0,
    ):
        self._rtt_weight = rtt_weight
        self._rsrq_weight = rsrq_weight
        self._rtt_floor = rtt_floor_ms
        self._rtt_ceiling = rtt_ceiling_ms
        self._rsrq_gap_threshold = rsrq_gap_threshold_db
        self._rsrq_ceiling = rsrq_ceiling_db
        self._rsrq_hold_s = rsrq_hold_s
        self._rtt_hold_s = float(rtt_hold_s if rtt_hold_s is not None else rsrq_hold_s)
        self._combined_hold_s = max(0.0, float(combined_hold_s))
        self._trend_rising_threshold = trend_rising_threshold
        self._trend_min_samples = trend_min_samples
        self._last_bucket: str = "normal"

        # ── RSRQ hold state ──
        self._last_rsrq_gap_db: float = 0.0
        self._last_rsrq_baseline_db: float = 0.0
        self._last_rsrq_pct: float = 0.0
        self._last_rsrq_valid_ts: float = 0.0
        self._last_rsrq_ctx: str = ""

        # ── RTT state ──
        self._quiet_rtt_delta: float = 0.0
        self._quiet_rtt_pct: float = 0.0
        self._quiet_rtt_base_pct: float = 0.0
        self._quiet_rtt_jitter_addition_pct: float = 0.0
        self._last_rtt_valid_ts: float = 0.0
        self._last_rtt_ctx: str = ""
        self._last_rtt_display_ms: float = 0.0
        self._last_rtt_display_live_ts: float = 0.0
        self._last_rtt_hold_refresh_ts: float = 0.0
        self._last_rtt_display_ctx: str = ""
        self._active_rtt_weight: float = float(rtt_weight)
        self._active_rsrq_weight: float = float(rsrq_weight)
        self._output_pct: float = 0.0
        self._output_valid: bool = False
        self._output_mode: str = "invalid"
        self._output_source: str = ""
        self._last_noninvalid_output_pct: float = 0.0
        self._last_noninvalid_output_source: str = ""
        self._last_noninvalid_output_valid: bool = False
        self._recover_from_invalid: bool = False
        self._ramp_recovery_active: bool = False
        self._combined_hold_started_ts: float = 0.0
        self._combined_hold_pct: float = 0.0

        # ── Trend ──
        self._trend_window: deque = deque(maxlen=trend_window_size)

        # ── Settling skip state ──
        # Track previous validity to detect invalid→valid transitions.
        # After transition, skip N ticks before trusting delta samples.
        # Initialize to True so first valid sample doesn't trigger skip.
        self._was_rsrq_valid: bool = True
        self._was_rtt_valid: bool = True
        self._rsrq_skip_counter: int = 0
        self._rtt_skip_counter: int = 0
        self._last_rsrq_display_live_ts: float = 0.0
        self._last_rsrq_hold_refresh_ts: float = 0.0
        self._last_rsrq_baseline_live_ts: float = 0.0

    def update_thresholds(
        self,
        *,
        rtt_floor_ms: float,
        rtt_ceiling_ms: float,
        rsrq_gap_threshold_db: float,
        rsrq_ceiling_db: float,
    ) -> None:
        self._rtt_floor = rtt_floor_ms
        self._rtt_ceiling = max(rtt_floor_ms + 0.001, rtt_ceiling_ms)
        self._rsrq_gap_threshold = rsrq_gap_threshold_db
        self._rsrq_ceiling = max(rsrq_gap_threshold_db + 0.001, rsrq_ceiling_db)

    def update_weights(self, *, rtt_weight: float, rsrq_weight: float) -> dict[str, float]:
        """Update runtime congestion lane weights.

        The weights are relative. Runtime normalizes invalid all-zero input
        back to the safe default so the combiner can never lose both lanes.
        """
        rtt = max(0.0, min(100.0, float(rtt_weight)))
        rsrq = max(0.0, min(100.0, float(rsrq_weight)))
        if (rtt + rsrq) <= 0.0:
            rtt = 50.0
            rsrq = 50.0
        self._rtt_weight = rtt
        self._rsrq_weight = rsrq
        self._active_rtt_weight = rtt
        self._active_rsrq_weight = rsrq
        return {
            "rtt_weight": float(self._rtt_weight),
            "rsrq_weight": float(self._rsrq_weight),
            "active_rtt_weight": float(self._active_rtt_weight),
            "active_rsrq_weight": float(self._active_rsrq_weight),
        }

    @staticmethod
    def _norm_pct(value: float, floor: float, ceiling: float) -> float:
        span = max(0.001, ceiling - floor)
        return max(0.0, ((value - floor) / span) * 100.0)

    @staticmethod
    def _step_toward(current: float, target: float, step: float) -> float:
        if current < target:
            return min(target, current + step)
        if current > target:
            return max(target, current - step)
        return current

    @staticmethod
    def _bucket_rtt_delta(delta_ms: float, quantum_ms: float = 5.0) -> float:
        try:
            delta = max(0.0, float(delta_ms or 0.0))
        except (TypeError, ValueError):
            delta = 0.0
        quantum = max(0.001, float(quantum_ms or 5.0))
        return float(math.ceil(delta / quantum) * quantum) if delta > 0.0 else quantum

    @classmethod
    def _rtt_pressure_with_jitter(
        cls,
        *,
        base_rtt_pct: float,
        jitter_excess_ms: float,
    ) -> tuple[float, float]:
        """Strengthen the RTT lane with bounded low-load variation evidence.

        Jitter is not a separately weighted congestion lane. It adds at most
        40 points to already-valid RTT pressure, linearly between 10 and 30 ms
        excess, and cannot create congestion on its own.
        """

        base = max(0.0, min(100.0, float(base_rtt_pct or 0.0)))
        jitter = max(0.0, float(jitter_excess_ms or 0.0))
        span = max(0.001, cls._RTT_JITTER_FULL_MS - cls._RTT_JITTER_START_MS)
        scale = max(
            0.0,
            min(1.0, (jitter - cls._RTT_JITTER_START_MS) / span),
        )
        addition = cls._RTT_JITTER_MAX_ADDITION_PCT * scale
        return min(100.0, base + addition), addition

    @staticmethod
    def _rsrq_hold_context_base(ctx_key: str) -> str:
        from arch4.signal_processing.context import rsrq_hold_context_base
        return rsrq_hold_context_base(ctx_key)

    @classmethod
    def _same_rsrq_hold_context(cls, left: str, right: str) -> bool:
        left_key = str(left or "")
        right_key = str(right or "")
        if not left_key or not right_key:
            return False
        if left_key == right_key:
            return True
        return cls._rsrq_hold_context_base(left_key) == cls._rsrq_hold_context_base(right_key)

    @staticmethod
    def _hold_active(*, refresh_ts: float, hold_s: float, now_ts: float) -> bool:
        return bool(refresh_ts > 0.0 and (now_ts - refresh_ts) <= float(hold_s))

    def _set_rtt_display_live(self, delta_ms: float, *, ctx_key: str, now_ts: float) -> None:
        self._last_rtt_display_ms = max(0.0, float(delta_ms or 0.0))
        self._last_rtt_display_live_ts = float(now_ts)
        self._last_rtt_hold_refresh_ts = float(now_ts)
        self._last_rtt_display_ctx = str(ctx_key or "")

    def _refresh_rtt_hold(self, *, now_ts: float) -> None:
        self._last_rtt_hold_refresh_ts = float(now_ts)

    def _rtt_display_hold_active(self, *, ctx_key: str, now_ts: float) -> bool:
        hold_ctx = str(self._last_rtt_display_ctx or self._last_rtt_ctx or "")
        refresh_ts = float(self._last_rtt_hold_refresh_ts or self._last_rtt_valid_ts or 0.0)
        return bool(
            hold_ctx == str(ctx_key or "")
            and self._hold_active(
                refresh_ts=refresh_ts,
                hold_s=self._rtt_hold_s,
                now_ts=now_ts,
            )
        )

    def _set_rsrq_display_live(self, gap_db: float, *, ctx_key: str, now_ts: float) -> None:
        self._last_rsrq_gap_db = max(0.0, float(gap_db or 0.0))
        self._last_rsrq_display_live_ts = float(now_ts)
        self._last_rsrq_hold_refresh_ts = float(now_ts)
        self._last_rsrq_ctx = str(ctx_key or "")

    def _refresh_rsrq_hold(self, *, now_ts: float) -> None:
        self._last_rsrq_hold_refresh_ts = float(now_ts)

    def _rsrq_display_hold_active(self, *, ctx_key: str, now_ts: float) -> bool:
        hold_ctx = str(self._last_rsrq_ctx or "")
        refresh_ts = float(self._last_rsrq_hold_refresh_ts or self._last_rsrq_valid_ts or 0.0)
        return bool(
            self._same_rsrq_hold_context(hold_ctx, ctx_key)
            and self._hold_active(
                refresh_ts=refresh_ts,
                hold_s=self._rsrq_hold_s,
                now_ts=now_ts,
            )
        )

    def _rtt_recovery_hold_allowed(self) -> bool:
        """Only preserve calm RTT through recovery.

        High RTT captured during own-traffic or burst phases is explicitly not
        trustworthy enough to replay after the recovery skip expires.
        """
        return bool(
            self._quiet_rtt_pct <= 0.0
            or self._quiet_rtt_delta <= self._rtt_floor
        )

    def _rebalance_active_weights(
        self,
        *,
        rtt_available: bool,
        rsrq_available: bool,
    ) -> None:
        total = max(
            0.0,
            float(self._rtt_weight)
            + float(self._rsrq_weight),
        )
        step = max(0.0, float(self._WEIGHT_STEP_PCT))
        raw_rtt = float(self._rtt_weight) if rtt_available else 0.0
        raw_rsrq = float(self._rsrq_weight) if rsrq_available else 0.0
        raw_total = max(0.0, raw_rtt + raw_rsrq)
        if raw_total > 0.0 and total > 0.0:
            target_scale = total / raw_total
            target_rtt = raw_rtt * target_scale
            target_rsrq = raw_rsrq * target_scale
        else:
            target_rtt = 0.0
            target_rsrq = 0.0

        self._active_rtt_weight = self._step_toward(self._active_rtt_weight, target_rtt, step)
        self._active_rsrq_weight = self._step_toward(self._active_rsrq_weight, target_rsrq, step)

    def _emit_output(self, cs: ComputedSignals, *, mode: str, target_pct: float, source: str) -> None:
        target = max(0.0, min(100.0, float(target_pct or 0.0)))
        mode = str(mode or "invalid").strip().lower()
        cs.congestion_fast_mode = mode

        if mode == "invalid":
            if self._output_valid:
                self._recover_from_invalid = True
            self._output_pct = 0.0
            self._output_valid = False
            self._output_mode = "invalid"
            self._output_source = ""
            self._ramp_recovery_active = False
            cs.congestion_raw_pct = 0.0
            cs.congestion_pct = 0.0
            cs.congestion_pct_valid = False
            cs.congestion_source = ""
            return

        # Fast congestion output should reflect the current justified target
        # immediately. Any calming belongs on the input side or the slower
        # operator-facing authority lane, not here.
        output = target
        self._recover_from_invalid = False
        self._ramp_recovery_active = False

        self._output_pct = float(output)
        self._output_valid = True
        self._output_mode = mode
        self._output_source = str(source or self._output_source or "")
        self._last_noninvalid_output_pct = float(output)
        self._last_noninvalid_output_source = str(self._output_source or "")
        self._last_noninvalid_output_valid = True
        cs.congestion_raw_pct = round(float(output), 1)
        cs.congestion_pct = round(float(output), 1)
        cs.congestion_pct_valid = True
        cs.congestion_source = str(self._output_source or "")

    def update(
        self,
        cs: ComputedSignals,
        ctx_key: str,
        *,
        lanes: CongestionInputContract,
    ) -> None:
        """Combine RTT and RSRQ deltas into congestion percentage.

        This combiner now obeys one strict contract:
          - it only consumes a canonical congestion input payload
          - it does not ingest raw modem RTT / RSRQ
          - it does not learn or rebuild baselines on its own
          - it does not silently switch between raw and processed sources

        Test and replay callers may use ``CongestionInputContract.from_computed``
        explicitly. Production receives the contract from CongestionModule.
        """
        now_ts = time.time()
        if not isinstance(lanes, CongestionInputContract):
            raise TypeError("congestion_input_contract_required")
        inputs = lanes
        rsrq_ctx_key = str(inputs.rsrq.ctx_key or ctx_key or "")
        rtt_hold_ctx_key = str(inputs.rtt.ctx_key or ctx_key or "")
        rtt_input_delta = float(inputs.rtt.delta_ms or 0.0)
        rtt_jitter_excess_ms = max(
            0.0,
            float(inputs.rtt.jitter_excess_ms or 0.0),
        )
        rtt_jitter_valid = bool(inputs.rtt.jitter_valid)
        rsrq_input_gap = float(inputs.rsrq.gap_db or 0.0)
        rsrq_input_baseline = float(inputs.rsrq.baseline_db or 0.0)
        rtt_block_reason = str(inputs.rtt.block_reason or "").strip().lower()
        rtt_hold_refreshable = rtt_block_reason in {"traffic_busy", "ul_contamination"}

        # ── Detect validity transitions ──
        rsrq_valid_now = bool(inputs.rsrq.valid)
        rtt_valid_now = bool(inputs.rtt.valid)
        rtt_ul_blocked = rtt_block_reason == "ul_contamination"
        # RSRQ: invalid→valid transition starts skip counter
        if rsrq_valid_now and not self._was_rsrq_valid:
            self._rsrq_skip_counter = self._RSRQ_SKIP_TICKS
        self._was_rsrq_valid = rsrq_valid_now

        # RTT: invalid→valid transition starts skip counter
        if rtt_valid_now and not self._was_rtt_valid:
            self._rtt_skip_counter = self._RTT_SKIP_TICKS
        self._was_rtt_valid = rtt_valid_now

        weighted: list[tuple[float, float, str]] = []
        cs.congestion_rsrq_held = False
        upstream_rsrq_display_gap = float(inputs.rsrq.display_gap_db or 0.0)
        upstream_rsrq_display_gap_valid = bool(inputs.rsrq.display_valid)
        upstream_rsrq_display_gap_held = bool(inputs.rsrq.display_held)
        cs.rsrq_display_gap = upstream_rsrq_display_gap
        cs.rsrq_display_gap_valid = upstream_rsrq_display_gap_valid
        cs.rsrq_display_gap_held = upstream_rsrq_display_gap_held
        cs.congestion_fast_mode = "invalid"
        cs.congestion_hold_mode = ""
        cs.congestion_trusted_rtt = False
        cs.congestion_rtt_base_pct = 0.0
        cs.congestion_trusted_rsrq = False
        cs.congestion_trusted_active_jitter = False
        cs.congestion_active_jitter_pct = 0.0
        cs.congestion_rsrq_available = False
        cs.congestion_rsrq_contributes = False
        cs.congestion_rsrq_reason = "not_valid"
        cs.congestion_rsrq_pct = 0.0
        cs.congestion_rsrq_gap_db = 0.0
        cs.congestion_rsrq_gap_threshold_db = float(self._rsrq_gap_threshold)
        cs.rtt_delta_display_ms = 0.0
        cs.rtt_delta_display_valid = False
        cs.rtt_delta_display_held = False
        cs.rtt_delta_display_live_ts = 0.0
        cs.rsrq_delta_display_db = 0.0
        cs.rsrq_delta_display_valid = False
        cs.rsrq_delta_display_held = False
        cs.rsrq_delta_display_live_ts = 0.0
        cs.rsrq_baseline_display_db = float(getattr(cs, "rsrq_baseline", 0.0) or 0.0)
        cs.rsrq_baseline_display_valid = bool(getattr(cs, "rsrq_baseline_valid", False))
        cs.rsrq_baseline_display_held = False
        cs.rsrq_baseline_display_live_ts = now_ts if cs.rsrq_baseline_display_valid else 0.0

        # ── RTT contribution ──
        rtt_weighted: tuple[float, float, str] | None = None
        rtt_live = False
        rtt_held = False
        rtt_display_held = False
        rtt_pct = 0.0
        rtt_delta = 0.0
        rtt_recovering = False
        if rtt_valid_now:
            if self._rtt_skip_counter > 0:
                self._rtt_skip_counter -= 1
                rtt_recovering = True
                rtt_held = bool(
                    self._rtt_recovery_hold_allowed()
                    and self._last_rtt_valid_ts > 0.0
                    and self._last_rtt_ctx == rtt_hold_ctx_key
                )
                if rtt_held:
                    rtt_pct = float(self._quiet_rtt_pct)
                    rtt_delta = float(self._quiet_rtt_delta)
                    rtt_display_held = True
                self._refresh_rtt_hold(now_ts=now_ts)
            else:
                rtt_live = True
                rtt_delta = max(0.0, float(rtt_input_delta))
                rtt_pct = self._norm_pct(
                    self._bucket_rtt_delta(rtt_delta),
                    self._rtt_floor,
                    self._rtt_ceiling,
                )
                base_rtt_pct = float(rtt_pct)
                jitter_addition_pct = 0.0
                if rtt_jitter_valid:
                    rtt_pct, jitter_addition_pct = self._rtt_pressure_with_jitter(
                        base_rtt_pct=rtt_pct,
                        jitter_excess_ms=rtt_jitter_excess_ms,
                    )
                cs.congestion_rtt_base_pct = float(base_rtt_pct)
                cs.congestion_active_jitter_pct = float(jitter_addition_pct)
                cs.congestion_trusted_active_jitter = bool(
                    rtt_jitter_valid and jitter_addition_pct > 0.0
                )
                self._quiet_rtt_delta = rtt_delta
                self._quiet_rtt_pct = rtt_pct
                self._quiet_rtt_base_pct = float(base_rtt_pct)
                self._quiet_rtt_jitter_addition_pct = float(
                    jitter_addition_pct
                )
                self._last_rtt_valid_ts = now_ts
                self._last_rtt_ctx = rtt_hold_ctx_key
                self._set_rtt_display_live(rtt_delta, ctx_key=rtt_hold_ctx_key, now_ts=now_ts)
                cs.congestion_trusted_rtt = True
        else:
            rtt_display_held = self._rtt_display_hold_active(ctx_key=rtt_hold_ctx_key, now_ts=now_ts)
            if rtt_display_held and rtt_hold_refreshable:
                self._refresh_rtt_hold(now_ts=now_ts)
                rtt_display_held = self._rtt_display_hold_active(ctx_key=rtt_hold_ctx_key, now_ts=now_ts)
            rtt_held = rtt_display_held
            if rtt_display_held:
                rtt_pct = float(self._quiet_rtt_pct)
                rtt_delta = float(self._last_rtt_display_ms or self._quiet_rtt_delta)
                cs.congestion_active_jitter_pct = float(
                    self._quiet_rtt_jitter_addition_pct
                )
                cs.congestion_rtt_base_pct = max(
                    0.0,
                    self._quiet_rtt_base_pct,
                )

        if rtt_held and not rtt_live:
            cs.congestion_active_jitter_pct = float(
                self._quiet_rtt_jitter_addition_pct
            )
            cs.congestion_rtt_base_pct = max(
                0.0,
                self._quiet_rtt_base_pct,
            )

        # ── RSRQ contribution ──
        rsrq_weighted: tuple[float, float, str] | None = None
        rsrq_live = False
        rsrq_held = False
        rsrq_pct = 0.0
        rsrq_gap = 0.0
        rsrq_reason = "not_valid"
        had_rsrq_history = bool(self._last_rsrq_valid_ts > 0.0 or self._last_rsrq_pct > 0.0)
        rsrq_recovering = False
        rsrq_ctx_matches = self._same_rsrq_hold_context(self._last_rsrq_ctx, rsrq_ctx_key)
        if rsrq_valid_now:
            cs.congestion_rsrq_available = True
            rsrq_gap = max(0.0, float(rsrq_input_gap))
            rsrq_pct = self._norm_pct(rsrq_gap, self._rsrq_gap_threshold, self._rsrq_ceiling)
            rsrq_contributes = bool(rsrq_pct > 0.0 or had_rsrq_history)
            rsrq_reason = "live_candidate" if rsrq_contributes else "below_threshold"
            if self._rsrq_skip_counter > 0:
                self._rsrq_skip_counter -= 1
                rsrq_recovering = True
                rsrq_reason = "recovery_hold" if rsrq_contributes else "below_threshold_recovery"
                rsrq_held = bool(
                    rsrq_ctx_matches
                    and self._last_rsrq_valid_ts > 0.0
                )
                if rsrq_held:
                    rsrq_pct = float(self._last_rsrq_pct)
                if rsrq_contributes and rsrq_ctx_matches and self._last_rsrq_valid_ts > 0.0:
                    # A valid same-context sample refreshes the hold window even
                    # while we are still suppressing live takeover during the
                    # recovery skip.
                    self._last_rsrq_valid_ts = now_ts
                    self._refresh_rsrq_hold(now_ts=now_ts)
            else:
                rsrq_live = rsrq_contributes
                if rsrq_contributes:
                    rsrq_reason = "trusted_live"
                    self._set_rsrq_display_live(rsrq_gap, ctx_key=rsrq_ctx_key, now_ts=now_ts)
                    self._last_rsrq_baseline_db = float(rsrq_input_baseline)
                    self._last_rsrq_baseline_live_ts = float(now_ts)
                    self._last_rsrq_pct = float(rsrq_pct)
                    self._last_rsrq_valid_ts = now_ts
        else:
            rsrq_held = bool(
                upstream_rsrq_display_gap_valid
                and upstream_rsrq_display_gap_held
                and self._same_rsrq_hold_context(self._last_rsrq_ctx, rsrq_ctx_key)
            )
            if not rsrq_held:
                rsrq_held = self._rsrq_display_hold_active(ctx_key=rsrq_ctx_key, now_ts=now_ts)
            if rsrq_held:
                held_gap = (
                    float(upstream_rsrq_display_gap)
                    if upstream_rsrq_display_gap_valid
                    else float(self._last_rsrq_gap_db)
                )
                rsrq_gap = max(0.0, held_gap)
                rsrq_pct = self._norm_pct(rsrq_gap, self._rsrq_gap_threshold, self._rsrq_ceiling)
                rsrq_reason = "trusted_hold"
                cs.congestion_rsrq_available = True

        # Cross-lane continuity: if one lane still keeps congestion trustworthy,
        # refresh the held lane instead of letting it expire mid-disturbance.
        if rtt_display_held and not rtt_live and (rsrq_live or rsrq_held):
            self._refresh_rtt_hold(now_ts=now_ts)
            rtt_display_held = self._rtt_display_hold_active(ctx_key=rtt_hold_ctx_key, now_ts=now_ts)
            rtt_held = rtt_display_held
            if rtt_display_held:
                rtt_pct = float(self._quiet_rtt_pct)
                rtt_delta = float(self._last_rtt_display_ms or self._quiet_rtt_delta)
        if rsrq_held and not rsrq_live and (rtt_live or rtt_held):
            self._refresh_rsrq_hold(now_ts=now_ts)
            rsrq_held = self._rsrq_display_hold_active(ctx_key=rsrq_ctx_key, now_ts=now_ts)
            if rsrq_held:
                rsrq_pct = float(self._last_rsrq_pct)
                rsrq_gap = float(self._last_rsrq_gap_db)
                rsrq_reason = "trusted_hold"

        # Capability controls weight ownership; one temporarily invalid sample
        # does not make a lane disappear.  This distinction is essential for
        # upload-contaminated RTT: its configured share remains reserved while
        # a structurally unavailable RSRQ lane may still transfer its share to
        # RTT for modems that cannot provide trustworthy RSRQ evidence.
        self._rebalance_active_weights(
            rtt_available=bool(inputs.rtt.supported),
            rsrq_available=bool(inputs.rsrq.supported),
        )

        if rtt_live or rtt_held:
            if self._active_rtt_weight > 0.0:
                rtt_weighted = (float(rtt_pct), self._active_rtt_weight, "rtt")
            cs.rtt_quiet_delta = float(rtt_delta)
            cs.rtt_quiet_held = bool((rtt_held or rtt_display_held) and not rtt_live)
        elif rtt_display_held:
            cs.rtt_quiet_delta = float(rtt_delta)
            cs.rtt_quiet_held = True
        else:
            cs.rtt_quiet_delta = 0.0
            cs.rtt_quiet_held = False

        if rsrq_live or rsrq_held:
            if self._active_rsrq_weight > 0.0:
                rsrq_weighted = (float(rsrq_pct), self._active_rsrq_weight, "rsrq")
            cs.congestion_rsrq_held = bool(rsrq_held and not rsrq_live)
            cs.congestion_rsrq_contributes = True
            cs.congestion_rsrq_reason = str(rsrq_reason or ("trusted_hold" if rsrq_held else "trusted_live"))
            cs.congestion_rsrq_pct = float(rsrq_pct)
            cs.congestion_rsrq_gap_db = float(rsrq_gap)
            cs.rsrq_display_gap = float(self._last_rsrq_gap_db if rsrq_held and not rsrq_live else max(0.0, float(rsrq_input_gap)))
            cs.rsrq_display_gap_valid = True
            cs.rsrq_display_gap_held = bool(rsrq_held and not rsrq_live)
            if rsrq_live:
                cs.congestion_trusted_rsrq = True
        else:
            cs.congestion_rsrq_reason = str(rsrq_reason or "not_valid")
            cs.congestion_rsrq_pct = float(rsrq_pct)
            cs.congestion_rsrq_gap_db = float(rsrq_gap)

        if rtt_live:
            cs.rtt_delta_display_ms = float(rtt_delta)
            cs.rtt_delta_display_valid = True
            cs.rtt_delta_display_held = False
            cs.rtt_delta_display_live_ts = float(now_ts)
        elif rtt_display_held:
            cs.rtt_delta_display_ms = float(self._last_rtt_display_ms or self._quiet_rtt_delta)
            cs.rtt_delta_display_valid = True
            cs.rtt_delta_display_held = True
            cs.rtt_delta_display_live_ts = float(self._last_rtt_display_live_ts or self._last_rtt_valid_ts)

        if rsrq_live:
            cs.rsrq_delta_display_db = float(rsrq_input_gap)
            cs.rsrq_delta_display_valid = True
            cs.rsrq_delta_display_held = False
            cs.rsrq_delta_display_live_ts = float(now_ts)
            cs.rsrq_baseline_display_db = float(rsrq_input_baseline)
            cs.rsrq_baseline_display_valid = bool(rsrq_input_baseline < 0.0)
            cs.rsrq_baseline_display_held = False
            cs.rsrq_baseline_display_live_ts = float(now_ts if rsrq_input_baseline < 0.0 else 0.0)
        elif rsrq_valid_now:
            cs.rsrq_delta_display_db = float(rsrq_gap)
            cs.rsrq_delta_display_valid = True
            cs.rsrq_delta_display_held = False
            cs.rsrq_delta_display_live_ts = float(now_ts)
            cs.rsrq_baseline_display_db = float(rsrq_input_baseline)
            cs.rsrq_baseline_display_valid = bool(rsrq_input_baseline < 0.0)
            cs.rsrq_baseline_display_held = False
            cs.rsrq_baseline_display_live_ts = float(now_ts if rsrq_input_baseline < 0.0 else 0.0)
        elif rsrq_held:
            cs.rsrq_delta_display_db = float(self._last_rsrq_gap_db)
            cs.rsrq_delta_display_valid = True
            cs.rsrq_delta_display_held = True
            cs.rsrq_delta_display_live_ts = float(self._last_rsrq_display_live_ts)
            cs.rsrq_baseline_display_db = float(self._last_rsrq_baseline_db)
            cs.rsrq_baseline_display_valid = bool(self._last_rsrq_baseline_db < 0.0)
            cs.rsrq_baseline_display_held = True
            cs.rsrq_baseline_display_live_ts = float(self._last_rsrq_baseline_live_ts)

        if rtt_weighted is not None:
            weighted.append(rtt_weighted)
        if rsrq_weighted is not None:
            weighted.append(rsrq_weighted)

        # ── Decide output mode ──
        has_live_input = bool(
            (rtt_valid_now and not rtt_recovering)
            or (rsrq_valid_now and not rsrq_recovering)
        )
        component_mode = "none"
        if has_live_input:
            component_mode = "live"
        elif weighted:
            component_mode = "signal_hold"

        if weighted:
            # Normalize by every structurally supported lane, not only lanes
            # that happened to qualify on this sample.  Otherwise transient
            # RTT contamination silently promotes RSRQ from its configured
            # share to 100% of congestion.
            total_w = max(
                0.0,
                float(self._active_rtt_weight)
                + float(self._active_rsrq_weight),
            )
            target_pct = sum(pct * w for pct, w, _ in weighted) / total_w if total_w > 0.0 else 0.0
            positive = [entry for entry in weighted if entry[0] > 0.0 and entry[1] > 0.0]
            dominant = max(positive, key=lambda x: x[0] * x[1]) if positive else None
            if component_mode == "live":
                self._combined_hold_started_ts = 0.0
                self._combined_hold_pct = 0.0
                self._emit_output(
                    cs,
                    mode=component_mode,
                    target_pct=target_pct,
                    source=dominant[2] if dominant else "",
                )
            elif component_mode == "signal_hold":
                if (
                    rtt_ul_blocked
                    and not (rsrq_live or rsrq_held)
                    and str(self._last_noninvalid_output_source or "") != "rsrq"
                ):
                    self._combined_hold_started_ts = 0.0
                    self._combined_hold_pct = 0.0
                    self._emit_output(cs, mode="invalid", target_pct=0.0, source="")
                else:
                    # If all live inputs are gone and only held signal state remains,
                    # freeze the last published fast congestion value instead of
                    # re-anchoring it from partial remembered components.
                    frozen_pct = float(self._output_pct) if self._output_valid else target_pct
                    self._emit_output(
                        cs,
                        mode=component_mode,
                        target_pct=frozen_pct,
                        source=self._output_source or (dominant[2] if dominant else ""),
                    )
            else:
                self._emit_output(cs, mode="invalid", target_pct=0.0, source="")
        elif has_live_input:
            self._combined_hold_started_ts = 0.0
            self._combined_hold_pct = 0.0
            self._emit_output(cs, mode="live", target_pct=0.0, source="")
        else:
            if (
                rtt_ul_blocked
                and not (rsrq_live or rsrq_held)
                and str(self._last_noninvalid_output_source or "") != "rsrq"
            ):
                # UL-contaminated RTT is not trustworthy congestion evidence at
                # all. Leave the fast lane invalid until a fresh clean RTT
                # sample is earned instead of publishing a fake valid 0%.
                self._combined_hold_started_ts = 0.0
                self._combined_hold_pct = 0.0
                self._emit_output(cs, mode="invalid", target_pct=0.0, source="")
            elif (rtt_recovering and not rtt_held) and not (rsrq_live or rsrq_held):
                # Clean DL recovery may publish a live zero once its upstream
                # quiet lane has already revalidated RTT.
                self._combined_hold_started_ts = 0.0
                self._combined_hold_pct = 0.0
                self._emit_output(cs, mode="live", target_pct=0.0, source="")
            elif (
                self._combined_hold_started_ts <= 0.0
                and self._last_noninvalid_output_valid
            ):
                self._combined_hold_started_ts = now_ts
                self._combined_hold_pct = float(self._last_noninvalid_output_pct)
                self._emit_output(
                    cs,
                    mode="combined_hold",
                    target_pct=self._combined_hold_pct,
                    source=self._last_noninvalid_output_source,
                )
            elif self._combined_hold_started_ts > 0.0 and (now_ts - self._combined_hold_started_ts) <= self._combined_hold_s:
                self._emit_output(
                    cs,
                    mode="combined_hold",
                    target_pct=self._combined_hold_pct,
                    source=self._output_source,
                )
            else:
                self._emit_output(cs, mode="invalid", target_pct=0.0, source="")

        hold_sources: list[str] = []
        if rtt_display_held and not rtt_live:
            hold_sources.append("rtt")
        if rsrq_held and not rsrq_live:
            hold_sources.append("rsrq")
        if cs.congestion_fast_mode == "combined_hold":
            cs.congestion_hold_mode = "combined"
        elif len(hold_sources) > 1:
            cs.congestion_hold_mode = "multi"
        elif hold_sources:
            cs.congestion_hold_mode = hold_sources[0]
        else:
            cs.congestion_hold_mode = ""

        if not bool(cs.congestion_pct_valid):
            self._last_bucket = "normal"
            self._trend_window.clear()
            cs.congestion_internal_bucket = ""
            cs.congestion_slope = 0.0
            cs.congestion_rising = False
            return

        self._last_bucket = congestion_bucket_for_pct(
            cs.congestion_pct,
            previous_bucket=self._last_bucket,
        )
        cs.congestion_internal_bucket = self._last_bucket

        # ── Trend (slope over window) ──
        self._trend_window.append((now_ts, cs.congestion_pct))
        if len(self._trend_window) >= self._trend_min_samples:
            samples = list(self._trend_window)
            t0 = samples[0][0]
            n = len(samples)
            t_vals = [s[0] - t0 for s in samples]
            c_vals = [s[1] for s in samples]
            t_mean = sum(t_vals) / n
            c_mean = sum(c_vals) / n
            num = sum((t - t_mean) * (c - c_mean) for t, c in zip(t_vals, c_vals))
            den = sum((t - t_mean) ** 2 for t in t_vals)
            slope_per_sec = num / den if den > 0.001 else 0.0
            slope_per_min = slope_per_sec * 60.0
            cs.congestion_slope = round(slope_per_min, 2)
            cs.congestion_rising = slope_per_min > self._trend_rising_threshold
        else:
            cs.congestion_slope = 0.0
            cs.congestion_rising = False

    def export_state(self) -> dict[str, Any]:
        return {
            "last_rsrq_gap_db": float(self._last_rsrq_gap_db),
            "last_rsrq_baseline_db": float(self._last_rsrq_baseline_db),
            "last_rsrq_pct": float(self._last_rsrq_pct),
            "last_rsrq_valid_ts": float(self._last_rsrq_valid_ts),
            "last_rsrq_ctx": str(self._last_rsrq_ctx),
            "quiet_rtt_delta": float(self._quiet_rtt_delta),
            "quiet_rtt_pct": float(self._quiet_rtt_pct),
            "quiet_rtt_base_pct": float(self._quiet_rtt_base_pct),
            "quiet_rtt_jitter_addition_pct": float(
                self._quiet_rtt_jitter_addition_pct
            ),
            "last_rtt_valid_ts": float(self._last_rtt_valid_ts),
            "last_rtt_ctx": str(self._last_rtt_ctx),
            "last_rtt_display_ms": float(self._last_rtt_display_ms),
            "last_rtt_display_live_ts": float(self._last_rtt_display_live_ts),
            "last_rtt_hold_refresh_ts": float(self._last_rtt_hold_refresh_ts),
            "last_rtt_display_ctx": str(self._last_rtt_display_ctx),
            "last_rsrq_display_live_ts": float(self._last_rsrq_display_live_ts),
            "last_rsrq_hold_refresh_ts": float(self._last_rsrq_hold_refresh_ts),
            "last_rsrq_baseline_live_ts": float(self._last_rsrq_baseline_live_ts),
            # Skip state
            "was_rsrq_valid": bool(self._was_rsrq_valid),
            "was_rtt_valid": bool(self._was_rtt_valid),
            "rsrq_skip_counter": int(self._rsrq_skip_counter),
            "rtt_skip_counter": int(self._rtt_skip_counter),
            "rtt_weight": float(self._rtt_weight),
            "rsrq_weight": float(self._rsrq_weight),
            "active_rtt_weight": float(self._active_rtt_weight),
            "active_rsrq_weight": float(self._active_rsrq_weight),
            "output_pct": float(self._output_pct),
            "output_valid": bool(self._output_valid),
            "output_mode": str(self._output_mode),
            "output_source": str(self._output_source),
            "last_noninvalid_output_pct": float(self._last_noninvalid_output_pct),
            "last_noninvalid_output_source": str(self._last_noninvalid_output_source),
            "last_noninvalid_output_valid": bool(self._last_noninvalid_output_valid),
            "ramp_recovery_active": bool(self._ramp_recovery_active),
            "combined_hold_started_ts": float(self._combined_hold_started_ts),
            "combined_hold_pct": float(self._combined_hold_pct),
        }

    def reset_runtime_state(self) -> None:
        self._last_rsrq_gap_db = 0.0
        self._last_rsrq_baseline_db = 0.0
        self._last_rsrq_pct = 0.0
        self._last_rsrq_valid_ts = 0.0
        self._last_rsrq_ctx = ""
        self._quiet_rtt_delta = 0.0
        self._quiet_rtt_pct = 0.0
        self._quiet_rtt_base_pct = 0.0
        self._quiet_rtt_jitter_addition_pct = 0.0
        self._last_rtt_valid_ts = 0.0
        self._last_rtt_ctx = ""
        self._last_rtt_display_ms = 0.0
        self._last_rtt_display_live_ts = 0.0
        self._last_rtt_hold_refresh_ts = 0.0
        self._last_rtt_display_ctx = ""
        self._last_rsrq_display_live_ts = 0.0
        self._last_rsrq_hold_refresh_ts = 0.0
        self._last_rsrq_baseline_live_ts = 0.0
        self._active_rtt_weight = float(self._rtt_weight)
        self._active_rsrq_weight = float(self._rsrq_weight)
        self._output_pct = 0.0
        self._output_valid = False
        self._output_mode = "invalid"
        self._output_source = ""
        self._last_noninvalid_output_pct = 0.0
        self._last_noninvalid_output_source = ""
        self._last_noninvalid_output_valid = False
        self._recover_from_invalid = False
        self._ramp_recovery_active = False
        self._combined_hold_started_ts = 0.0
        self._combined_hold_pct = 0.0
        self._trend_window.clear()
        self._last_bucket = "normal"
        self._rsrq_skip_counter = 0
        self._rtt_skip_counter = 0

    def restore_state(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        if (
            "quiet_rtt_jitter_confidence_pct" in payload
            and "quiet_rtt_jitter_addition_pct" not in payload
        ):
            # The retired percentage-of-headroom state has different math and
            # must not masquerade as the bounded-addition contract.
            return False
        try:
            self._last_rsrq_gap_db = float(payload.get("last_rsrq_gap_db", 0.0) or 0.0)
            self._last_rsrq_baseline_db = float(payload.get("last_rsrq_baseline_db", 0.0) or 0.0)
            self._last_rsrq_pct = float(payload.get("last_rsrq_pct", 0.0) or 0.0)
            self._last_rsrq_valid_ts = float(payload.get("last_rsrq_valid_ts", 0.0) or 0.0)
            self._last_rsrq_ctx = str(payload.get("last_rsrq_ctx", "") or "")
            self._quiet_rtt_delta = float(payload.get("quiet_rtt_delta", 0.0) or 0.0)
            self._quiet_rtt_pct = float(payload.get("quiet_rtt_pct", 0.0) or 0.0)
            self._quiet_rtt_base_pct = float(
                payload.get("quiet_rtt_base_pct", self._quiet_rtt_pct) or 0.0
            )
            self._quiet_rtt_jitter_addition_pct = float(
                payload.get("quiet_rtt_jitter_addition_pct", 0.0) or 0.0
            )
            self._last_rtt_valid_ts = float(payload.get("last_rtt_valid_ts", 0.0) or 0.0)
            self._last_rtt_ctx = str(payload.get("last_rtt_ctx", "") or "")
            self._last_rtt_display_ms = float(payload.get("last_rtt_display_ms", 0.0) or 0.0)
            self._last_rtt_display_live_ts = float(payload.get("last_rtt_display_live_ts", 0.0) or 0.0)
            self._last_rtt_hold_refresh_ts = float(payload.get("last_rtt_hold_refresh_ts", 0.0) or 0.0)
            self._last_rtt_display_ctx = str(payload.get("last_rtt_display_ctx", "") or "")
            self._last_rsrq_display_live_ts = float(payload.get("last_rsrq_display_live_ts", 0.0) or 0.0)
            self._last_rsrq_hold_refresh_ts = float(payload.get("last_rsrq_hold_refresh_ts", 0.0) or 0.0)
            self._last_rsrq_baseline_live_ts = float(payload.get("last_rsrq_baseline_live_ts", 0.0) or 0.0)
            # Skip state
            self._was_rsrq_valid = bool(payload.get("was_rsrq_valid", False))
            self._was_rtt_valid = bool(payload.get("was_rtt_valid", False))
            self._rsrq_skip_counter = int(payload.get("rsrq_skip_counter", 0) or 0)
            self._rtt_skip_counter = int(payload.get("rtt_skip_counter", 0) or 0)
            self._rtt_weight = float(payload.get("rtt_weight", self._rtt_weight) or self._rtt_weight)
            self._rsrq_weight = float(payload.get("rsrq_weight", self._rsrq_weight) or self._rsrq_weight)
            self._active_rtt_weight = float(payload.get("active_rtt_weight", self._rtt_weight) or self._rtt_weight)
            self._active_rsrq_weight = float(payload.get("active_rsrq_weight", self._rsrq_weight) or self._rsrq_weight)
            self._output_pct = float(payload.get("output_pct", 0.0) or 0.0)
            self._output_valid = bool(payload.get("output_valid", False))
            self._output_mode = str(payload.get("output_mode", "invalid") or "invalid")
            self._output_source = str(payload.get("output_source", "") or "")
            self._last_noninvalid_output_pct = float(payload.get("last_noninvalid_output_pct", 0.0) or 0.0)
            self._last_noninvalid_output_source = str(payload.get("last_noninvalid_output_source", "") or "")
            self._last_noninvalid_output_valid = bool(payload.get("last_noninvalid_output_valid", False))
            self._ramp_recovery_active = bool(payload.get("ramp_recovery_active", False))
            self._combined_hold_started_ts = float(payload.get("combined_hold_started_ts", 0.0) or 0.0)
            self._combined_hold_pct = float(payload.get("combined_hold_pct", 0.0) or 0.0)
            return True
        except Exception:
            return False
