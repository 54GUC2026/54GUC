"""
arch4/signal_processing/congestion_authority.py
═══════════════════════════════════════════════════════════════
Dedicated congestion authority.

Owns canonical congestion outputs for the runtime:
  - fast congestion truth (`congestion_pct`)
  - stepped operator display truth (`congestion_display_pct`)
  - fast internal bucket (`congestion_internal_bucket`)
  - stable user-facing bucket (`congestion_bucket`)
  - canonical pressure flags for state consumers

The low-level combiner still computes the weighted RTT/RSRQ signal.
This authority wraps that combiner and becomes the single producer
for exported congestion state.
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from arch4.coerce import (
    CONGESTION_HIGH_ENTER_PCT,
    CONGESTION_MILD_ENTER_PCT,
    congestion_bucket_for_pct,
)
from arch4.interfaces import ComputedSignals, DEFAULT_CONGESTION_RSRQ_GAP_THRESHOLD_DB
from arch4.signal_processing.congestion_combiner import CongestionCombiner, CongestionInputContract


class CongestionAuthority:
    """Single-source congestion producer for signals/UI export."""

    _DISPLAY_STEP_PCT = 5.0

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
        trend_window_size: int = 90,
        trend_min_samples: int = 15,
        trend_rising_threshold: float = 3.0,
        moderate_pct: float = CONGESTION_MILD_ENTER_PCT,
        high_pct: float = CONGESTION_HIGH_ENTER_PCT,
        rsrq_moderate_db: float = 4.0,
        rsrq_high_db: float = 6.0,
        local_rf_ok_sinr_db: float = 6.0,
        local_rf_ok_rsrp_dbm: float = -90.0,
        local_self_load_dl_mbps: float = 10.0,
        mild_enter_ticks: int = 2,
        high_enter_ticks: int = 2,
        mild_clear_ticks: int = 4,
        high_clear_ticks: int = 5,
        clear_min_dwell_s: float = 10.0,
        state_confirm_window_s: float = 30.0,
    ):
        self._combiner = CongestionCombiner(
            rtt_weight=rtt_weight,
            rsrq_weight=rsrq_weight,
            rtt_floor_ms=rtt_floor_ms,
            rtt_ceiling_ms=rtt_ceiling_ms,
            rsrq_gap_threshold_db=rsrq_gap_threshold_db,
            rsrq_ceiling_db=rsrq_ceiling_db,
            rsrq_hold_s=rsrq_hold_s,
            trend_window_size=trend_window_size,
            trend_min_samples=trend_min_samples,
            trend_rising_threshold=trend_rising_threshold,
        )
        self._moderate_pct = float(moderate_pct)
        self._high_pct = float(high_pct)
        self._trend_rising_threshold = float(trend_rising_threshold)
        self._rsrq_moderate_db = float(rsrq_moderate_db)
        self._rsrq_high_db = float(rsrq_high_db)
        self._local_rf_ok_sinr_db = float(local_rf_ok_sinr_db)
        self._local_rf_ok_rsrp_dbm = float(local_rf_ok_rsrp_dbm)
        self._local_self_load_dl_mbps = float(local_self_load_dl_mbps)
        self._mild_enter_ticks = max(1, int(mild_enter_ticks))
        self._high_enter_ticks = max(1, int(high_enter_ticks))
        self._mild_clear_ticks = max(1, int(mild_clear_ticks))
        self._high_clear_ticks = max(1, int(high_clear_ticks))
        self._clear_min_dwell_s = max(0.0, float(clear_min_dwell_s))
        self._state_confirm_window_s = max(0.0, float(state_confirm_window_s))
        self._internal_bucket = "normal"
        self._user_bucket = "normal"
        self._state_bucket = "normal"
        self._state_avg_pct = 0.0
        self._display_pct = 0.0
        self._display_seeded = False
        self._pending_bucket = ""
        self._pending_ticks = 0
        self._user_bucket_changed_at = 0.0
        self._state_pending_bucket = ""
        self._state_pending_ticks = 0
        self._state_bucket_changed_at = 0.0
        self._state_samples: deque[tuple[float, float]] = deque(maxlen=10_000)

    def update_thresholds(
        self,
        *,
        rtt_floor_ms: float,
        rtt_ceiling_ms: float,
        rsrq_gap_threshold_db: float,
        rsrq_ceiling_db: float,
        moderate_pct: float | None = None,
        high_pct: float | None = None,
        rsrq_moderate_db: float | None = None,
        rsrq_high_db: float | None = None,
        local_rf_ok_sinr_db: float | None = None,
        local_rf_ok_rsrp_dbm: float | None = None,
        local_self_load_dl_mbps: float | None = None,
    ) -> None:
        self._combiner.update_thresholds(
            rtt_floor_ms=rtt_floor_ms,
            rtt_ceiling_ms=rtt_ceiling_ms,
            rsrq_gap_threshold_db=rsrq_gap_threshold_db,
            rsrq_ceiling_db=rsrq_ceiling_db,
        )
        if moderate_pct is not None:
            self._moderate_pct = float(moderate_pct)
        if high_pct is not None:
            self._high_pct = float(high_pct)
        if rsrq_moderate_db is not None:
            self._rsrq_moderate_db = float(rsrq_moderate_db)
        if rsrq_high_db is not None:
            self._rsrq_high_db = float(rsrq_high_db)
        if local_rf_ok_sinr_db is not None:
            self._local_rf_ok_sinr_db = float(local_rf_ok_sinr_db)
        if local_rf_ok_rsrp_dbm is not None:
            self._local_rf_ok_rsrp_dbm = float(local_rf_ok_rsrp_dbm)
        if local_self_load_dl_mbps is not None:
            self._local_self_load_dl_mbps = float(local_self_load_dl_mbps)

    def update_weights(self, *, rtt_weight: float, rsrq_weight: float) -> dict[str, float]:
        return self._combiner.update_weights(rtt_weight=rtt_weight, rsrq_weight=rsrq_weight)

    def update(
        self,
        cs: ComputedSignals,
        ctx_key: str,
        *,
        lanes: CongestionInputContract,
        has_traffic: bool = False,
        sinr_db: float = 0.0,
        rsrp_dbm: float = 0.0,
        throughput_dl_mbps: float = 0.0,
    ) -> None:
        self._combiner.update(
            cs,
            ctx_key,
            lanes=lanes,
        )
        self.finalize(
            cs,
            has_traffic=has_traffic,
            sinr_db=sinr_db,
            rsrp_dbm=rsrp_dbm,
            throughput_dl_mbps=throughput_dl_mbps,
        )

    def finalize(
        self,
        cs: ComputedSignals,
        *,
        now_ts: float | None = None,
        has_traffic: bool = False,
        sinr_db: float = 0.0,
        rsrp_dbm: float = 0.0,
        throughput_dl_mbps: float = 0.0,
    ) -> None:
        if now_ts is None:
            now_ts = time.time()

        valid = bool(getattr(cs, "congestion_pct_valid", False))
        if not valid:
            raw_pct = max(0.0, float(getattr(cs, "congestion_pct", 0.0) or 0.0))
            raw_internal_bucket = str(getattr(cs, "congestion_internal_bucket", "") or "").strip().lower()
            raw_user_bucket = str(getattr(cs, "congestion_bucket", "") or "").strip().lower()
            if (
                raw_pct > 0.0
                or raw_internal_bucket in {"normal", "mild", "high"}
                or raw_user_bucket in {"normal", "mild", "high"}
            ):
                valid = True

        if not valid:
            self._internal_bucket = "normal"
            self._user_bucket = "normal"
            self._state_bucket = "normal"
            self._state_avg_pct = 0.0
            self._pending_bucket = ""
            self._pending_ticks = 0
            self._user_bucket_changed_at = 0.0
            self._state_pending_bucket = ""
            self._state_pending_ticks = 0
            self._state_bucket_changed_at = 0.0
            self._state_samples.clear()

            cs.congestion_internal_bucket = ""
            cs.congestion_bucket = ""
            cs.congestion_display_pct = float(self._display_pct)
            cs.congestion_display_valid = False
            cs.congestion_state_avg_pct = 0.0
            cs.congestion_state_bucket = ""
            cs.congestion_source = ""
            cs.congestion_flags_valid = False
            cs.congestion_bad_now = False
            cs.congestion_pressure_high = False
            cs.congestion_pressure_moderate = False
            cs.congestion_kind_hint = ""
            cs.congestion_trend_display = ""
            return

        internal_bucket = str(
            getattr(cs, "congestion_internal_bucket", "") or getattr(cs, "congestion_bucket", "") or ""
        ).strip().lower()
        if internal_bucket not in {"normal", "mild", "high"}:
            internal_bucket = congestion_bucket_for_pct(
                cs.congestion_pct,
                previous_bucket=self._internal_bucket,
            )
        self._internal_bucket = internal_bucket
        cs.congestion_internal_bucket = internal_bucket

        target_pct = max(0.0, min(100.0, float(cs.congestion_pct or 0.0)))
        if not self._display_seeded:
            self._display_pct = target_pct
            self._display_seeded = True
        else:
            self._display_pct = self._step_toward(
                self._display_pct,
                target_pct,
                self._DISPLAY_STEP_PCT,
            )
        cs.congestion_display_pct = float(self._display_pct)
        cs.congestion_display_valid = True

        user_bucket = self._stable_user_bucket(internal_bucket, now_ts=now_ts)
        cs.congestion_bucket = user_bucket
        state_avg_pct, state_bucket = self._state_confirmation_bucket(cs.congestion_pct, now_ts=now_ts)
        cs.congestion_state_avg_pct = float(state_avg_pct)
        cs.congestion_state_bucket = str(state_bucket)
        bad_now, high_pressure, moderate_pressure = self._canonical_flags_from_congestion_lane(
            pct=float(getattr(cs, "congestion_pct", 0.0) or 0.0),
            internal_bucket=internal_bucket,
            user_bucket=user_bucket,
            state_bucket=state_bucket,
        )
        cs.congestion_flags_valid = True
        cs.congestion_bad_now = bool(bad_now)
        cs.congestion_pressure_high = bool(high_pressure)
        cs.congestion_pressure_moderate = bool(moderate_pressure)
        cs.congestion_kind_hint = self._classify_kind(
            has_traffic=has_traffic,
            sinr_db=sinr_db,
            rsrp_dbm=rsrp_dbm,
            throughput_dl_mbps=throughput_dl_mbps,
        )
        cs.congestion_trend_display = self._display_trend_for_slope(
            float(getattr(cs, "congestion_slope", 0.0) or 0.0)
        )

    def mark_unsupported(self, cs: ComputedSignals, *, reason: str = "") -> None:
        self._combiner.reset_runtime_state()
        self._internal_bucket = "normal"
        self._user_bucket = "normal"
        self._state_bucket = "normal"
        self._state_avg_pct = 0.0
        self._display_pct = 0.0
        self._display_seeded = False
        self._pending_bucket = ""
        self._pending_ticks = 0
        self._user_bucket_changed_at = 0.0
        self._state_pending_bucket = ""
        self._state_pending_ticks = 0
        self._state_bucket_changed_at = 0.0
        self._state_samples.clear()

        cs.congestion_supported = False
        cs.congestion_support_reason = str(reason or "")
        cs.congestion_pct = 0.0
        cs.congestion_raw_pct = 0.0
        cs.congestion_pct_valid = False
        cs.congestion_display_pct = 0.0
        cs.congestion_display_valid = False
        cs.congestion_internal_bucket = ""
        cs.congestion_bucket = ""
        cs.congestion_state_avg_pct = 0.0
        cs.congestion_state_bucket = ""
        cs.congestion_source = ""
        cs.congestion_rsrq_held = False
        cs.rsrq_display_gap = 0.0
        cs.rsrq_display_gap_valid = False
        cs.rsrq_display_gap_held = False
        cs.congestion_slope = 0.0
        cs.congestion_rising = False
        cs.congestion_trend_display = ""
        cs.congestion_flags_valid = False
        cs.congestion_bad_now = False
        cs.congestion_pressure_high = False
        cs.congestion_pressure_moderate = False
        cs.congestion_kind_hint = ""

    def _canonical_flags_from_congestion_lane(
        self,
        *,
        pct: float,
        internal_bucket: str,
        user_bucket: str,
        state_bucket: str,
    ) -> tuple[bool, bool, bool]:
        pct = max(0.0, min(100.0, float(pct or 0.0)))
        buckets = {
            str(internal_bucket or "").strip().lower(),
            str(user_bucket or "").strip().lower(),
            str(state_bucket or "").strip().lower(),
        }
        high = bool("high" in buckets or pct >= self._high_pct)
        moderate = bool(high or "mild" in buckets or pct >= self._moderate_pct)
        return high, high, moderate

    def _display_trend_for_slope(self, slope_pct_per_min: float) -> str:
        slope = float(slope_pct_per_min or 0.0)
        threshold = abs(float(self._trend_rising_threshold or 0.0))
        if slope > threshold:
            return "UP"
        if slope < -threshold:
            return "DOWN"
        return "STABLE"

    def _stable_user_bucket(self, target_bucket: str, *, now_ts: float) -> str:
        current = self._user_bucket if self._user_bucket in {"normal", "mild", "high"} else "normal"
        target = str(target_bucket or "normal").strip().lower()
        if target not in {"normal", "mild", "high"}:
            target = "normal"

        if current == target:
            self._pending_bucket = ""
            self._pending_ticks = 0
            if self._user_bucket_changed_at <= 0.0:
                self._user_bucket_changed_at = now_ts
            self._user_bucket = current
            return current

        if target != self._pending_bucket:
            self._pending_bucket = target
            self._pending_ticks = 1
        else:
            self._pending_ticks += 1

        current_level = self._severity(current)
        target_level = self._severity(target)

        if target_level > current_level:
            needed_ticks = self._high_enter_ticks if target == "high" else self._mild_enter_ticks
        else:
            if self._user_bucket_changed_at > 0.0 and (now_ts - self._user_bucket_changed_at) < self._clear_min_dwell_s:
                return current
            needed_ticks = self._high_clear_ticks if current == "high" else self._mild_clear_ticks

        if self._pending_ticks >= needed_ticks:
            self._user_bucket = target
            self._user_bucket_changed_at = now_ts
            self._pending_bucket = ""
            self._pending_ticks = 0
            return target
        return current

    def _state_confirmation_bucket(self, pct: float, *, now_ts: float) -> tuple[float, str]:
        pct = max(0.0, min(100.0, float(pct or 0.0)))
        self._state_samples.append((float(now_ts), pct))
        cutoff = float(now_ts) - max(0.0, self._state_confirm_window_s)
        while self._state_samples and self._state_samples[0][0] < cutoff:
            self._state_samples.popleft()

        if not self._state_samples:
            self._state_avg_pct = pct
            self._state_bucket = "normal"
            self._state_pending_bucket = ""
            self._state_pending_ticks = 0
            return self._state_avg_pct, self._state_bucket

        avg_pct = float(sum(sample_pct for _, sample_pct in self._state_samples) / len(self._state_samples))
        self._state_avg_pct = avg_pct

        ready = self._state_confirm_window_s <= 0.0
        if not ready:
            oldest_ts = float(self._state_samples[0][0])
            ready = (float(now_ts) - oldest_ts) >= self._state_confirm_window_s

        if ready:
            current = self._state_bucket if self._state_bucket in {"normal", "mild", "high"} else "normal"
            target = congestion_bucket_for_pct(avg_pct, previous_bucket=current)
            if current == target:
                self._state_pending_bucket = ""
                self._state_pending_ticks = 0
                if self._state_bucket_changed_at <= 0.0:
                    self._state_bucket_changed_at = now_ts
                self._state_bucket = current
            else:
                if target != self._state_pending_bucket:
                    self._state_pending_bucket = target
                    self._state_pending_ticks = 1
                else:
                    self._state_pending_ticks += 1

                current_level = self._severity(current)
                target_level = self._severity(target)
                if target_level > current_level:
                    needed_ticks = self._high_enter_ticks if target == "high" else self._mild_enter_ticks
                else:
                    if (
                        self._state_bucket_changed_at > 0.0
                        and (now_ts - self._state_bucket_changed_at) < self._clear_min_dwell_s
                    ):
                        return self._state_avg_pct, current
                    needed_ticks = self._high_clear_ticks if current == "high" else self._mild_clear_ticks

                if self._state_pending_ticks >= needed_ticks:
                    self._state_bucket = target
                    self._state_bucket_changed_at = now_ts
                    self._state_pending_bucket = ""
                    self._state_pending_ticks = 0
        elif self._state_bucket not in {"normal", "mild", "high"}:
            self._state_bucket = "normal"

        return self._state_avg_pct, self._state_bucket

    @staticmethod
    def _step_toward(current: float, target: float, step: float) -> float:
        if current < target:
            return min(target, current + step)
        if current > target:
            return max(target, current - step)
        return current

    @staticmethod
    def _severity(bucket: str) -> int:
        if bucket == "high":
            return 2
        if bucket == "mild":
            return 1
        return 0

    def _classify_kind(
        self,
        *,
        has_traffic: bool,
        sinr_db: float,
        rsrp_dbm: float,
        throughput_dl_mbps: float,
    ) -> str:
        if (
            has_traffic
            and float(sinr_db) >= self._local_rf_ok_sinr_db
            and float(rsrp_dbm) >= self._local_rf_ok_rsrp_dbm
            and float(throughput_dl_mbps) >= self._local_self_load_dl_mbps
        ):
            return "local_traffic"
        return "attached_cell"

    def export_state(self) -> dict[str, Any]:
        return {
            "combiner": self._combiner.export_state(),
            "internal_bucket": str(self._internal_bucket or "normal"),
            "user_bucket": str(self._user_bucket or "normal"),
            "state_bucket": str(self._state_bucket or "normal"),
            "state_avg_pct": float(self._state_avg_pct),
            "display_pct": float(self._display_pct),
            "display_seeded": bool(self._display_seeded),
            "state_confirm_window_s": float(self._state_confirm_window_s),
            "state_samples": [[float(ts), float(pct)] for ts, pct in list(self._state_samples)],
            "state_pending_bucket": str(self._state_pending_bucket or ""),
            "state_pending_ticks": int(self._state_pending_ticks),
            "state_bucket_changed_at": float(self._state_bucket_changed_at),
            "pending_bucket": str(self._pending_bucket or ""),
            "pending_ticks": int(self._pending_ticks),
            "user_bucket_changed_at": float(self._user_bucket_changed_at),
        }

    def restore_state(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            combiner_payload = payload.get("combiner")
            if not isinstance(combiner_payload, dict):
                return False
            if not self._combiner.restore_state(combiner_payload):
                return False
            self._internal_bucket = str(payload.get("internal_bucket", "normal") or "normal")
            self._user_bucket = str(payload.get("user_bucket", "normal") or "normal")
            self._state_bucket = str(payload.get("state_bucket", "normal") or "normal")
            self._state_avg_pct = float(payload.get("state_avg_pct", 0.0) or 0.0)
            self._display_pct = max(
                0.0,
                min(100.0, float(payload.get("display_pct", 0.0) or 0.0)),
            )
            self._display_seeded = bool(payload.get("display_seeded", False))
            self._state_confirm_window_s = max(
                0.0,
                float(payload.get("state_confirm_window_s", self._state_confirm_window_s) or 0.0),
            )
            raw_state_samples = list(payload.get("state_samples", []) or [])
            restored_samples: deque[tuple[float, float]] = deque(maxlen=10_000)
            for item in raw_state_samples:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    restored_samples.append((float(item[0]), float(item[1])))
            self._state_samples = restored_samples
            self._state_pending_bucket = str(payload.get("state_pending_bucket", "") or "")
            self._state_pending_ticks = int(payload.get("state_pending_ticks", 0) or 0)
            self._state_bucket_changed_at = float(payload.get("state_bucket_changed_at", 0.0) or 0.0)
            self._pending_bucket = str(payload.get("pending_bucket", "") or "")
            self._pending_ticks = int(payload.get("pending_ticks", 0) or 0)
            self._user_bucket_changed_at = float(payload.get("user_bucket_changed_at", 0.0) or 0.0)
            return True
        except Exception:
            return False
