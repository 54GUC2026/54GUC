"""Shared deterministic CA telemetry processor.

Owns derived CA display telemetry from the raw modem CA state. This keeps GUI
widgets as renderers: they receive percent/rate values, not raw samples to
calculate from. CAST uses the rolling wall-clock window; CAST_LD uses the last
DL-load-qualified samples so idle SCell policy does not dilute load behavior.
"""

from __future__ import annotations

from collections import deque

from arch4.interfaces import ComputedSignals


CA_DEMAND_LOAD_MBPS = 0.4
CA_IDLE_NEUTRAL_VALUE = 0.5
CA_LD_SAMPLE_WINDOW = 1800
_MAX_TIME_WINDOW_SAMPLES = 100_000

_CA_OUTPUT_FIELDS: tuple[str, ...] = (
    "ca_raw_state",
    "cart",
    "cast",
    "ca_stat_scope_key",
    "cart_ld",
    "cart_ld_valid",
    "cart_ld_display",
    "cart_ld_display_valid",
    "cast_ld",
    "cast_ld_valid",
    "cart_activity_per_hour",
    "cart_ld_activity",
    "ri_stability_pct",
    "ri_activity_per_hour",
    "ri_stability_valid",
    "ca_stat_pct",
    "ca_stat_ld_pct",
)


def ca_demand_load_mbps(dl_mbps: object = 0.0, ul_mbps: object = 0.0) -> float:
    """Return DL load in Mbps for CA demand.

    ``ul_mbps`` is accepted only as a compatibility argument; UL does not drive
    LTE CA/SCell demand in this model.
    """
    try:
        dl = float(dl_mbps or 0.0)
    except (TypeError, ValueError):
        dl = 0.0
    return max(0.0, dl)


def ca_demand_active_from_mbps(dl_mbps: object = 0.0, ul_mbps: object = 0.0) -> bool:
    return ca_demand_load_mbps(dl_mbps, ul_mbps) >= CA_DEMAND_LOAD_MBPS


def ca_bq_value_for_demand(
    *,
    ca_active: bool,
    demand_active: bool,
    radio_connected: bool = True,
) -> float:
    """CA component used by BQ/CQ.

    Idle CA state is neutral because the cell may deactivate SCells when there
    is no DL user data. Under DL load, CA ON is positive and CA OFF is negative.
    """
    if not bool(radio_connected):
        return 0.0
    if bool(demand_active):
        return 1.0 if bool(ca_active) else 0.0
    return CA_IDLE_NEUTRAL_VALUE


class CATelemetryProcessor:
    """Own CART/CAST telemetry and demand-aware CA quality interpretation."""

    def __init__(
        self,
        *,
        rate_window_s: float = 3600.0,
        confirm_ticks: int = 3,
        ld_sample_window: int = CA_LD_SAMPLE_WINDOW,
    ) -> None:
        self.rate_window_s = max(1.0, float(rate_window_s or 3600.0))
        self.confirm_ticks = max(1, int(confirm_ticks or 3))
        self.ld_sample_window = max(1, int(ld_sample_window or CA_LD_SAMPLE_WINDOW))
        self._samples: deque[tuple[float, bool]] = deque(maxlen=_MAX_TIME_WINDOW_SAMPLES)
        self._stable_ca_active: bool | None = None
        self._ca_negative_ticks = 0
        self._stable_load_qualified: bool | None = None
        self._candidate_load_ticks = 0
        self._change_ts: deque[float] = deque(maxlen=_MAX_TIME_WINDOW_SAMPLES)
        self._ri_change_ts: deque[float] = deque(maxlen=_MAX_TIME_WINDOW_SAMPLES)
        self._ld_change_ts: deque[float] = deque(maxlen=_MAX_TIME_WINDOW_SAMPLES)
        self._scope_key = ""
        self._ld_samples: deque[bool] = deque(maxlen=self.ld_sample_window)
        self._ri_samples: deque[tuple[float, bool]] = deque(maxlen=_MAX_TIME_WINDOW_SAMPLES)
        self._last_ri_ready: bool | None = None
        self._last_ld_ca_active: bool | None = None
        self._last_sample_sequence = 0
        self._last_outputs: dict[str, object] = {}

    def _reset_session(self) -> None:
        self._samples.clear()
        self._stable_ca_active = None
        self._ca_negative_ticks = 0
        self._stable_load_qualified = None
        self._candidate_load_ticks = 0
        self._change_ts.clear()
        self._ri_change_ts.clear()
        self._ld_change_ts.clear()
        self._ld_samples.clear()
        self._ri_samples.clear()
        self._last_ri_ready = None
        self._last_ld_ca_active = None
        self._last_sample_sequence = 0
        self._last_outputs.clear()

    @staticmethod
    def _copy_outputs(cs: ComputedSignals) -> dict[str, object]:
        return {name: getattr(cs, name) for name in _CA_OUTPUT_FIELDS if hasattr(cs, name)}

    @staticmethod
    def _apply_outputs(cs: ComputedSignals, outputs: dict[str, object]) -> None:
        for name, value in outputs.items():
            if hasattr(cs, name):
                setattr(cs, name, value)

    @staticmethod
    def _transition_count(states: deque[bool]) -> int:
        changes = 0
        previous: bool | None = None
        for state in states:
            current = bool(state)
            if previous is not None and current != previous:
                changes += 1
            previous = current
        return changes

    def _update_stable_ca_gate(self, raw_ca_on: bool, now: float) -> bool:
        """Return CA active state with immediate assert and confirmed release."""
        raw = bool(raw_ca_on)
        initialized = self._stable_ca_active is not None
        previous = bool(self._stable_ca_active) if initialized else False
        if raw:
            if self._stable_ca_active is not True:
                self._stable_ca_active = True
                if initialized and previous is False:
                    self._change_ts.append(now)
            self._ca_negative_ticks = 0
            return True
        if self._stable_ca_active is not True:
            self._stable_ca_active = False
            self._ca_negative_ticks = 0
            return False
        self._ca_negative_ticks += 1
        if self._ca_negative_ticks >= self.confirm_ticks:
            self._stable_ca_active = False
            self._change_ts.append(now)
            self._ca_negative_ticks = 0
        return bool(self._stable_ca_active is True)

    def _update_stable_load_gate(self, raw_load_qualified: bool) -> bool:
        """Return DL demand state with immediate assert and confirmed release.

        CA_LD is the canonical CA-under-load observation lane for Brain and GUI.
        A first positive DL sample starts the observation window immediately;
        only a full negative confirmation window ends it. This protects CA_LD
        from both modem CA flicker and threshold flicker around 0.4 Mbps.
        """
        raw = bool(raw_load_qualified)
        if raw:
            self._stable_load_qualified = True
            self._candidate_load_ticks = 0
            return True
        if self._stable_load_qualified is not True:
            self._stable_load_qualified = False
            self._candidate_load_ticks = 0
            return False
        self._candidate_load_ticks += 1
        if self._candidate_load_ticks >= self.confirm_ticks:
            self._stable_load_qualified = False
            self._candidate_load_ticks = 0
        return bool(self._stable_load_qualified is True)

    def update(
        self,
        *,
        ca_active: bool,
        radio_connected: bool,
        now_ts: float,
        cs: ComputedSignals,
        scope_key: str = "",
        rx_bps: float = 0.0,
        ri: int = 0,
        sample_sequence: int = 0,
        sample_fresh: bool = True,
    ) -> None:
        now = float(now_ts or 0.0)
        raw_ca_on = bool(ca_active)
        connected = bool(radio_connected)
        normalized_scope_key = str(scope_key or "").strip()
        if normalized_scope_key and normalized_scope_key != self._scope_key:
            self._scope_key = normalized_scope_key
            self._reset_session()

        try:
            sequence = int(sample_sequence or 0)
        except (TypeError, ValueError):
            sequence = 0
        fresh = bool(sample_fresh)
        if not fresh:
            if self._last_outputs:
                self._apply_outputs(cs, self._last_outputs)
            return
        if self._last_outputs and sequence > 0 and sequence == self._last_sample_sequence:
            self._apply_outputs(cs, self._last_outputs)
            return

        cs.ca_raw_state = 1 if raw_ca_on else 0

        if connected:
            ca_on = self._update_stable_ca_gate(raw_ca_on, now)
            self._samples.append((now, ca_on))
            ri_value = int(ri or 0)
            if ri_value > 0:
                ri_ready = ri_value >= 2
                self._ri_samples.append((now, ri_ready))
                if self._last_ri_ready is None:
                    self._last_ri_ready = ri_ready
                elif ri_ready != self._last_ri_ready:
                    self._ri_change_ts.append(now)
                    self._last_ri_ready = ri_ready
        else:
            ca_on = bool(self._stable_ca_active) if self._stable_ca_active is not None else raw_ca_on

        cs.cart = 1 if ca_on else 0

        dl_mbps = float(rx_bps or 0.0) / 125_000.0
        raw_load_qualified = connected and ca_demand_active_from_mbps(dl_mbps)
        load_qualified = connected and self._update_stable_load_gate(raw_load_qualified)
        if load_qualified:
            self._ld_samples.append(ca_on)
            if self._last_ld_ca_active is None:
                self._last_ld_ca_active = ca_on
            elif ca_on != self._last_ld_ca_active:
                self._ld_change_ts.append(now)
                self._last_ld_ca_active = ca_on
            cs.cart_ld = 1 if ca_on else 0
            cs.cart_ld_valid = True
        else:
            cs.cart_ld = 0
            cs.cart_ld_valid = False

        if cs.cart_ld_valid:
            cs.cart_ld_display = cs.cart_ld
            cs.cart_ld_display_valid = True
        else:
            cs.cart_ld_display = 0
            cs.cart_ld_display_valid = False

        cutoff = now - self.rate_window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        while self._change_ts and self._change_ts[0] < cutoff:
            self._change_ts.popleft()
        while self._ri_samples and self._ri_samples[0][0] < cutoff:
            self._ri_samples.popleft()
        while self._ri_change_ts and self._ri_change_ts[0] < cutoff:
            self._ri_change_ts.popleft()
        while self._ld_change_ts and self._ld_change_ts[0] < cutoff:
            self._ld_change_ts.popleft()

        seen_ticks = len(self._samples)
        active_ticks = sum(1 for _ts, active in self._samples if active)
        ri_seen_ticks = len(self._ri_samples)
        ri_ready_ticks = sum(1 for _ts, ready in self._ri_samples if ready)
        ld_seen_ticks = len(self._ld_samples)
        ld_active_ticks = sum(1 for active in self._ld_samples if active)
        cs.ca_stat_scope_key = self._scope_key
        cs.cast = (
            (float(active_ticks) / float(seen_ticks)) * 100.0
            if seen_ticks > 0
            else 0.0
        )
        cs.cart_activity_per_hour = (float(len(self._change_ts)) * 3600.0) / self.rate_window_s
        cs.ri_stability_pct = (
            (float(ri_ready_ticks) / float(ri_seen_ticks)) * 100.0
            if ri_seen_ticks > 0
            else 0.0
        )
        cs.ri_activity_per_hour = (float(len(self._ri_change_ts)) * 3600.0) / self.rate_window_s
        cs.ri_stability_valid = ri_seen_ticks > 0
        cs.cart_ld_activity = (float(len(self._ld_change_ts)) * 3600.0) / self.rate_window_s
        cs.cast_ld = (
            (float(ld_active_ticks) / float(ld_seen_ticks)) * 100.0
            if ld_seen_ticks > 0
            else 0.0
        )
        cs.cast_ld_valid = ld_seen_ticks > 0
        cs.ca_stat_pct = cs.cast
        cs.ca_stat_ld_pct = cs.cast_ld
        cs.ca_stat_ld_valid = cs.cast_ld_valid
        if sequence > 0:
            self._last_sample_sequence = sequence
        self._last_outputs = self._copy_outputs(cs)

    def ca_quality_value(
        self,
        *,
        radio_connected: bool,
        cart: int,
        now_ts: float,
        rx_bps: float,
        tx_bps: float = 0.0,
    ) -> tuple[float, str]:
        """Return demand-aware CA value for BQ/CQ.

        Raw CART/CAST remain telemetry. This lane answers only: how should CA
        participate in current BQ/CQ?
        """
        dl_mbps = float(rx_bps or 0.0) / 125_000.0
        ul_mbps = float(tx_bps or 0.0) / 125_000.0
        load_mbps = ca_demand_load_mbps(dl_mbps, ul_mbps)
        demand_active = bool(radio_connected and self._stable_load_qualified is True)
        ca_on = bool(cart)

        ca_value = ca_bq_value_for_demand(
            ca_active=ca_on,
            demand_active=demand_active,
            radio_connected=bool(radio_connected),
        )
        reason = (
            f"demand_window:{'on' if ca_on else 'off'}:{load_mbps:.3f}Mbps"
            if demand_active
            else f"idle_neutral:{load_mbps:.3f}Mbps"
        )
        return float(ca_value), reason
