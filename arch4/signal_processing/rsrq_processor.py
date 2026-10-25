"""
arch4/signal_processing/rsrq_processor.py
═══════════════════════════════════════════════════════════════
RSRQ Signal Processor — single source of truth for all RSRQ-derived state.

Input:   rsrq_db (from modem, when truly reported)
         or derived same-context proxy from rsrp_dbm - rssi_dbm
         modem context (band, pci, earfcn, traffic, RF quality)

Outputs on ComputedSignals:
  .rsrq_baseline             THE baseline (per-context, age-corrected ratchet)
  .rsrq_baseline_valid       True when baseline has a value (default or learned)
  .rsrq_baseline_samples     always 0 — sample accumulation removed
  .rsrq_gap                  baseline - live_rsrq (positive = congested)
  .rsrq_gap_valid            True when gap is trustworthy (has a floor + low traffic)
  .rsrq_slope                linear slope of idle RSRQ (trend)
  .rsrq_degrading            True when slope indicates RF degradation

Baseline learning:
  - Per-context key: band|cell_id|earfcn|primary_bw_mhz (CA excluded)
  - Learns during qualifying idle windows (no traffic, good RF)
  - floor = baseline always. They are always kept in sync.
  - Fresh samples can only move the baseline toward 0 (better).
  - A baseline which has not been refreshed for 24 hours is relaxed by 2 dB.
    This is CGF signal-processing behaviour and does not depend on Intelligence.
  - On fresh CGF context/PBAND start: first trusted current sample is baseline.
  - CGF persists its own timestamped baseline state. Intelligence history may
    provide an exact-context bootstrap record, but is storage only and never
    owns aging or delta calculation.
  - Traffic gate: both DL and UL must stay under the learning threshold.
  - Live gap valid only when both DL and UL stay under the live threshold.

Trust level: HIGH
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Any

from arch4.interfaces import ComputedSignals
from arch4.signal_processing.context import rsrq_hold_context_base, signal_subscription_scope

log = logging.getLogger("arch4.signal_processing.rsrq")

_RSSI_DERIVED_MAX_DBM: float = -51.0
_BASELINE_AGE_INTERVAL_S: float = 24.0 * 60.0 * 60.0
_BASELINE_AGE_STEP_DB: float = 2.0
_BASELINE_STATE_SCHEMA: str = "CGFRsrqBaselineState.v1"


class RSRQProcessor:
    def __init__(
        self,
        *,
        baseline_dl_bps: float = 1_250_000.0,
        live_rx_bps: float = 1_250_000.0,
        slope_window_size: int = 30,
        slope_degrading_threshold: float = -0.05,
        cal_settle_samples: int = 30,
        gap_window_size: int = 10,
        valid_min_db: float = -25.0,
        valid_max_db: float = 0.0,
        display_hold_s: float = 30.0,
    ):
        self._baseline_dl_bps = baseline_dl_bps
        self._live_rx_bps = live_rx_bps
        self._slope_degrading_threshold = slope_degrading_threshold
        self._valid_min_db = float(valid_min_db)
        self._valid_max_db = float(valid_max_db)
        self._display_hold_s = max(0.0, float(display_hold_s))

        # floor = baseline always; both dicts always in sync via _set_floor()
        self._baselines: dict[str, float] = {}
        self._floor: dict[str, float] = {}
        self._floor_updated_at: dict[str, float] = {}
        self._session_bootstrap_contexts: set[str] = set()
        self._baseline_state_revision: int = 0
        self._baseline_state_saved_revision: int = 0
        self._cal_best: dict[str, float] = {}
        self._cal_samples: dict[str, list[float]] = {}
        self._cal_count: dict[str, int] = {}
        self._cal_settled: set[str] = set()
        self._cal_settle_samples: int = cal_settle_samples

        self._last_baseline_db: float = 0.0
        self._last_baseline_valid: bool = False
        self._last_gap_db: float = 0.0
        self._last_gap_valid: bool = False
        self._last_display_gap_db: float = 0.0
        self._last_display_valid_ts: float = 0.0
        self._last_display_ctx: str = ""

        self._slope_history: deque = deque(maxlen=slope_window_size)
        self._slope_scope: str = ""
        self._last_slope: float = 0.0
        self._last_degrading: bool = False

        self._gap_window_size = gap_window_size
        self._gap_windows: dict[str, deque] = {}

    @staticmethod
    def _format_bandwidth_key(bw_mhz: float) -> str:
        try:
            bw = float(bw_mhz or 0.0)
        except (TypeError, ValueError):
            bw = 0.0
        if bw <= 0.0:
            return "0"
        rounded = round(bw)
        if abs(bw - rounded) < 1e-6:
            return str(int(rounded))
        return f"{bw:.1f}"

    @classmethod
    def context_key(cls, band: int, cell_id: int, earfcn: int, bandwidth_mhz: float = 0.0) -> str:
        return f"{band}|{cell_id}|{earfcn}|{cls._format_bandwidth_key(bandwidth_mhz)}"

    @staticmethod
    def _hold_context_base(ctx_key: str) -> str:
        return rsrq_hold_context_base(ctx_key)

    @classmethod
    def _same_hold_context(cls, left: str, right: str) -> bool:
        left_key = str(left or "")
        right_key = str(right or "")
        if not left_key or not right_key:
            return False
        if left_key == right_key:
            return True
        return cls._hold_context_base(left_key) == cls._hold_context_base(right_key)

    @staticmethod
    def _identity_context_valid(ctx_key: str) -> bool:
        parts = str(ctx_key or "").split("|")
        if len(parts) < 3:
            return False
        try:
            band = int(float(parts[0] or 0))
            earfcn = int(float(parts[2] or 0))
        except (TypeError, ValueError):
            return False
        return band > 0 and earfcn > 0

    def _set_floor(
        self,
        ctx_key: str,
        value: float,
        *,
        updated_at: float,
        session_bootstrap: bool = False,
    ) -> None:
        """Set floor = baseline atomically. All floor updates must go through here."""
        stamp = float(updated_at)
        if not math.isfinite(stamp) or stamp <= 0.0:
            raise ValueError("rsrq_baseline_updated_at_invalid")
        self._baselines[ctx_key] = float(value)
        self._floor[ctx_key] = float(value)
        self._floor_updated_at[ctx_key] = stamp
        if session_bootstrap:
            self._session_bootstrap_contexts.add(ctx_key)
        else:
            self._session_bootstrap_contexts.discard(ctx_key)
        self._baseline_state_revision += 1

    def _promote_baseline(
        self,
        ctx_key: str,
        rsrq_db: float,
        *,
        updated_at: float,
    ) -> None:
        old_floor = self._floor.get(ctx_key)
        self._set_floor(
            ctx_key,
            rsrq_db,
            updated_at=updated_at,
            session_bootstrap=ctx_key in self._session_bootstrap_contexts,
        )
        log.info(
            "RSRQ baseline promoted: %s floor %.1f → %.1f dB",
            ctx_key, old_floor if old_floor is not None else -99.0, rsrq_db,
        )

    def _age_baseline_if_due(self, ctx_key: str, *, now_ts: float) -> float | None:
        baseline = self._baselines.get(ctx_key)
        updated_at = float(self._floor_updated_at.get(ctx_key, 0.0) or 0.0)
        if (
            baseline is None
            or updated_at <= 0.0
            or (now_ts - updated_at) < _BASELINE_AGE_INTERVAL_S
        ):
            return baseline
        aged = max(self._valid_min_db, float(baseline) - _BASELINE_AGE_STEP_DB)
        self._set_floor(ctx_key, aged, updated_at=now_ts)
        self._gap_windows.pop(ctx_key, None)
        log.info(
            "RSRQ baseline aged: %s floor %.1f → %.1f dB after 24h",
            ctx_key,
            baseline,
            aged,
        )
        return aged

    def admit_learned_baseline(
        self,
        ctx_key: str,
        baseline: float,
        *,
        learned_at: float,
    ) -> bool:
        """Admit one exact-context storage record; CGF retains behaviour ownership."""
        try:
            value = float(baseline)
            stamp = float(learned_at)
        except (TypeError, ValueError):
            return False
        if (
            not (self._valid_min_db <= value < self._valid_max_db)
            or not math.isfinite(stamp)
            or stamp <= 0.0
        ):
            return False
        previous = self._baselines.get(ctx_key)
        previous_stamp = float(self._floor_updated_at.get(ctx_key, 0.0) or 0.0)
        session_bootstrap = ctx_key in self._session_bootstrap_contexts
        if previous is not None and not session_bootstrap and stamp <= previous_stamp:
            return False
        if previous is not None and float(previous) >= value:
            if session_bootstrap:
                self._session_bootstrap_contexts.discard(ctx_key)
            return False
        self._set_floor(ctx_key, value, updated_at=stamp)
        return True

    def update(
        self,
        rsrq_db: float,
        *,
        rsrq_source: str = "reported",
        ctx_key: str,
        radio_connected: bool,
        rtt_elevated: bool,
        rtt_alarm: bool,
        sinr_db: float,
        rsrp_dbm: float,
        rssi_dbm: float = 0.0,
        rx_bps: float,
        tx_bps: float,
        has_traffic: bool,
        cs: ComputedSignals,
    ) -> None:
        now_ts = time.time()
        measurement_db, measurement_source = self._resolve_measurement(
            rsrq_db=rsrq_db,
            rsrq_source=rsrq_source,
            rsrp_dbm=rsrp_dbm,
            rssi_dbm=rssi_dbm,
        )
        # RSRQ trust owns its own traffic gating from raw counters. Do not
        # depend on the shared has_traffic flag, which may use a different
        # threshold or purpose elsewhere in the system.
        del has_traffic
        no_heavy_traffic = rx_bps <= self._baseline_dl_bps and tx_bps <= self._baseline_dl_bps

        context_valid = self._identity_context_valid(ctx_key)

        # ── Slope (idle only) ──
        scope = signal_subscription_scope(ctx_key)
        if scope != self._slope_scope:
            self._slope_history.clear()
            self._last_slope = 0.0
            self._last_degrading = False
            self._slope_scope = scope
        if context_valid and measurement_db is not None and no_heavy_traffic:
            self._slope_history.append(measurement_db)
            if len(self._slope_history) >= 10:
                values = list(self._slope_history)
                n = len(values)
                x_mean = (n - 1) / 2
                y_mean = sum(values) / n
                num = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(values))
                den = sum((i - x_mean) ** 2 for i in range(n))
                self._last_slope = num / den if den > 0 else 0.0
                self._last_degrading = self._last_slope < self._slope_degrading_threshold
        cs.rsrq_slope = self._last_slope
        cs.rsrq_degrading = self._last_degrading

        # ── Gate ──
        if not context_valid:
            self._last_gap_db = 0.0
            self._last_gap_valid = False
            self._emit_display_hold(cs, ctx_key=ctx_key, now_ts=now_ts)
            self._emit_cached(cs)
            return
        if not radio_connected:
            self._emit_display_hold(cs, ctx_key=ctx_key, now_ts=now_ts)
            self._emit_cached(cs)
            return
        if measurement_db is None:
            self._last_gap_db = 0.0
            self._last_gap_valid = False
            self._emit_display_hold(cs, ctx_key=ctx_key, now_ts=now_ts)
            self._emit_cached(cs)
            return

        if no_heavy_traffic and ctx_key not in self._cal_settled:
            prev_best = self._cal_best.get(ctx_key)
            if prev_best is None or measurement_db > prev_best:
                self._cal_best[ctx_key] = measurement_db
            samples = self._cal_samples.setdefault(ctx_key, [])
            samples.append(float(measurement_db))
            if len(samples) > max(self._cal_settle_samples, 1):
                del samples[: len(samples) - max(self._cal_settle_samples, 1)]
            self._cal_count[ctx_key] = len(samples)
            if self._cal_count[ctx_key] >= self._cal_settle_samples:
                self._cal_settled.add(ctx_key)

        # ── Resolve baseline (create floor on first encounter) ──
        baseline = self._baselines.get(ctx_key)
        live_sample_ok = rx_bps <= self._live_rx_bps and tx_bps <= self._live_rx_bps

        if baseline is None:
            floor_val = self._floor.get(ctx_key)
            if floor_val is not None:
                raise RuntimeError("rsrq_baseline_floor_owner_diverged")
            baseline = float(measurement_db)
            self._set_floor(
                ctx_key,
                baseline,
                updated_at=now_ts,
                session_bootstrap=True,
            )
        elif self._floor.get(ctx_key) != baseline:
            raise RuntimeError("rsrq_baseline_floor_owner_diverged")

        baseline = self._age_baseline_if_due(ctx_key, now_ts=now_ts)
        if baseline is None:
            raise RuntimeError("rsrq_baseline_missing_after_resolution")

        if live_sample_ok:
            if ctx_key not in self._gap_windows:
                self._gap_windows[ctx_key] = deque(maxlen=self._gap_window_size)
            if measurement_db > float(baseline):
                self._promote_baseline(
                    ctx_key,
                    measurement_db,
                    updated_at=now_ts,
                )
                baseline = float(measurement_db)
                self._gap_windows[ctx_key].clear()
            self._gap_windows[ctx_key].append(measurement_db)
            smoothed_rsrq = sum(self._gap_windows[ctx_key]) / len(self._gap_windows[ctx_key])
            gap = max(0.0, float(baseline) - smoothed_rsrq)
            self._last_baseline_db = float(baseline)
            self._last_baseline_valid = True
            self._last_gap_db = gap
            self._last_gap_valid = True   # floor exists → gap is trustworthy
            self._last_display_gap_db = gap
            self._last_display_valid_ts = now_ts
            self._last_display_ctx = str(ctx_key or "")
            cs.rsrq_display_gap = float(gap)
            cs.rsrq_display_gap_valid = True
            cs.rsrq_display_gap_held = False
        else:
            self._last_baseline_db = float(baseline)
            self._last_baseline_valid = True
            self._last_gap_db = 0.0
            self._last_gap_valid = False
            self._emit_display_hold(cs, ctx_key=ctx_key, now_ts=now_ts)

        self._emit_cached(cs)

    def _emit_display_hold(self, cs: ComputedSignals, *, ctx_key: str, now_ts: float) -> None:
        hold_ok = bool(
            self._last_display_valid_ts > 0.0
            and self._same_hold_context(self._last_display_ctx, ctx_key)
            and (now_ts - self._last_display_valid_ts) <= self._display_hold_s
        )
        if hold_ok:
            cs.rsrq_display_gap = float(self._last_display_gap_db)
            cs.rsrq_display_gap_valid = True
            cs.rsrq_display_gap_held = True
        else:
            cs.rsrq_display_gap = 0.0
            cs.rsrq_display_gap_valid = False
            cs.rsrq_display_gap_held = False

    def _resolve_measurement(
        self,
        *,
        rsrq_db: float,
        rsrq_source: str,
        rsrp_dbm: float,
        rssi_dbm: float,
    ) -> tuple[float | None, str]:
        try:
            rsrq_value = float(rsrq_db or 0.0)
        except (TypeError, ValueError):
            rsrq_value = 0.0
        source = str(rsrq_source or "reported").strip().lower()
        if source == "reported" and self._valid_min_db <= rsrq_value < self._valid_max_db:
            return rsrq_value, "reported"

        try:
            rsrp = float(rsrp_dbm or 0.0)
            rssi = float(rssi_dbm or 0.0)
        except (TypeError, ValueError):
            return None, "unavailable"
        if rsrp >= 0.0 or rsrp < -200.0:
            return None, "unavailable"
        if rssi >= _RSSI_DERIVED_MAX_DBM or rssi < -150.0:
            return None, "unavailable"
        derived = rsrp - rssi
        if derived >= self._valid_max_db or derived < self._valid_min_db:
            return None, "unavailable"
        return float(derived), "derived"

    def _emit_cached(self, cs: ComputedSignals) -> None:
        cs.rsrq_baseline = self._last_baseline_db
        cs.rsrq_baseline_valid = self._last_baseline_valid
        cs.rsrq_baseline_samples = 0  # sample accumulation removed
        cs.rsrq_gap = self._last_gap_db
        cs.rsrq_gap_valid = self._last_gap_valid

    def export_state(self) -> dict[str, Any]:
        return {
            "baselines": {k: float(v) for k, v in self._baselines.items()},
            "floor": {k: float(v) for k, v in self._floor.items()},
            "floor_updated_at": {
                k: float(v) for k, v in self._floor_updated_at.items()
            },
            "session_bootstrap_contexts": sorted(self._session_bootstrap_contexts),
            "baseline_state_revision": int(self._baseline_state_revision),
            "baseline_state_saved_revision": int(
                self._baseline_state_saved_revision
            ),
            "last_baseline_db": float(self._last_baseline_db),
            "last_baseline_valid": bool(self._last_baseline_valid),
            "last_gap_db": float(self._last_gap_db),
            "last_gap_valid": bool(self._last_gap_valid),
            "last_display_gap_db": float(self._last_display_gap_db),
            "last_display_valid_ts": float(self._last_display_valid_ts),
            "last_display_ctx": str(self._last_display_ctx or ""),
            "slope_history": [float(v) for v in self._slope_history],
            "slope_scope": self._slope_scope,
            "last_slope": float(self._last_slope),
            "last_degrading": bool(self._last_degrading),
        }

    def restore_state(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            raw_b = payload.get("baselines")
            if "baselines" in payload and isinstance(raw_b, dict):
                self._baselines = {k: float(v) for k, v in raw_b.items()
                                   if isinstance(v, (int, float))}
            raw_f = payload.get("floor")
            if "floor" in payload and isinstance(raw_f, dict):
                self._floor = {k: float(v) for k, v in raw_f.items()
                               if isinstance(v, (int, float))}
            raw_updated = payload.get("floor_updated_at")
            if "floor_updated_at" in payload and isinstance(raw_updated, dict):
                self._floor_updated_at = {
                    k: float(v)
                    for k, v in raw_updated.items()
                    if isinstance(v, (int, float))
                }
            raw_bootstrap = payload.get("session_bootstrap_contexts", [])
            if isinstance(raw_bootstrap, list):
                self._session_bootstrap_contexts = {
                    str(value) for value in raw_bootstrap if str(value)
                }
            self._baseline_state_revision = int(
                payload.get("baseline_state_revision", 0) or 0
            )
            self._baseline_state_saved_revision = int(
                payload.get("baseline_state_saved_revision", 0) or 0
            )
            self._last_baseline_db = float(payload.get("last_baseline_db", 0.0) or 0.0)
            self._last_baseline_valid = bool(payload.get("last_baseline_valid", False))
            self._last_gap_db = float(payload.get("last_gap_db", 0.0) or 0.0)
            self._last_gap_valid = bool(payload.get("last_gap_valid", False))
            self._last_display_gap_db = float(payload.get("last_display_gap_db", 0.0) or 0.0)
            self._last_display_valid_ts = float(payload.get("last_display_valid_ts", 0.0) or 0.0)
            self._last_display_ctx = str(payload.get("last_display_ctx", "") or "")
            self._slope_history = deque(
                [
                    float(v)
                    for v in list(payload.get("slope_history", []))[
                        -self._slope_history.maxlen :
                    ]
                ],
                maxlen=self._slope_history.maxlen,
            )
            self._slope_scope = str(payload.get("slope_scope", ""))
            self._last_slope = float(payload.get("last_slope", 0.0) or 0.0)
            self._last_degrading = bool(payload.get("last_degrading", False))
            if self._baselines != self._floor or any(
                ctx_key not in self._floor_updated_at
                for ctx_key in self._baselines
            ):
                return False
            return True
        except Exception:
            return False

    def export_baseline_state(self) -> dict[str, Any]:
        """Return the only durable CGF-owned RSRQ baseline representation."""

        return {
            "schema": _BASELINE_STATE_SCHEMA,
            "entries": {
                ctx_key: {
                    "baseline_db": float(value),
                    "updated_at": float(self._floor_updated_at[ctx_key]),
                }
                for ctx_key, value in self._baselines.items()
                if ctx_key in self._floor_updated_at
            },
        }

    def restore_baseline_state(self, payload: dict[str, Any]) -> bool:
        """Restore only the canonical durable baseline cells, never live windows."""

        if not isinstance(payload, dict) or payload.get("schema") != _BASELINE_STATE_SCHEMA:
            return False
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            return False
        restored_baselines: dict[str, float] = {}
        restored_timestamps: dict[str, float] = {}
        try:
            for ctx_key, raw in entries.items():
                if not isinstance(ctx_key, str) or not self._identity_context_valid(ctx_key):
                    return False
                if not isinstance(raw, dict) or set(raw) != {"baseline_db", "updated_at"}:
                    return False
                value = float(raw["baseline_db"])
                stamp = float(raw["updated_at"])
                if (
                    not self._valid_min_db <= value < self._valid_max_db
                    or not math.isfinite(stamp)
                    or stamp <= 0.0
                ):
                    return False
                restored_baselines[ctx_key] = value
                restored_timestamps[ctx_key] = stamp
        except (TypeError, ValueError):
            return False
        self._baselines = dict(restored_baselines)
        self._floor = dict(restored_baselines)
        self._floor_updated_at = restored_timestamps
        self._session_bootstrap_contexts.clear()
        self._baseline_state_revision += 1
        self._baseline_state_saved_revision = self._baseline_state_revision
        return True

    def baseline_state_dirty(self) -> bool:
        return self._baseline_state_revision != self._baseline_state_saved_revision

    def mark_baseline_state_saved(self) -> None:
        self._baseline_state_saved_revision = self._baseline_state_revision
