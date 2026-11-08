"""The only Core traffic-qualification owner for RTT and jitter processing."""

from __future__ import annotations

import math

from arch4.core_contracts.signals import LatencyTrafficQualification


class LatencyTrafficQualifier:
    """Translate one accepted traffic observation into processor semantics."""

    LATENCY_MAX_DL_MBPS = 10.0
    JITTER_MIN_DL_MBPS = 0.0
    JITTER_MAX_DL_MBPS = 10.0
    OPERATIONAL_MAX_UL_MBPS = 5.0
    OPERATIONAL_RELEASE_UL_MBPS = 4.0

    def __init__(
        self,
        *,
        rtt_baseline_rx_bps: float = 125_000.0,
        rtt_baseline_tx_bps: float = 125_000.0,
        rtt_dl_busy_bps: float = 1_250_000.0,
        rtt_dl_release_bps: float = 1_000_000.0,
        rtt_dl_instant_busy_bps: float = 1_875_000.0,
        rtt_ul_busy_bps: float = OPERATIONAL_MAX_UL_MBPS * 125_000.0,
        rtt_ul_release_bps: float = OPERATIONAL_RELEASE_UL_MBPS * 125_000.0,
        jitter_ul_quiet_bps: float = OPERATIONAL_MAX_UL_MBPS * 125_000.0,
    ) -> None:
        self._rtt_baseline_rx_bps = max(0.0, float(rtt_baseline_rx_bps))
        self._rtt_baseline_tx_bps = max(0.0, float(rtt_baseline_tx_bps))
        self._rtt_dl_busy_bps = max(0.0, float(rtt_dl_busy_bps))
        self._rtt_dl_release_bps = max(
            0.0, min(float(rtt_dl_release_bps), self._rtt_dl_busy_bps)
        )
        self._rtt_dl_instant_busy_bps = max(
            self._rtt_dl_busy_bps, float(rtt_dl_instant_busy_bps)
        )
        self._rtt_ul_busy_bps = max(0.0, float(rtt_ul_busy_bps))
        self._rtt_ul_release_bps = max(
            0.0, min(float(rtt_ul_release_bps), self._rtt_ul_busy_bps)
        )
        self._jitter_ul_quiet_bps = max(0.0, float(jitter_ul_quiet_bps))
        self._latency_max_dl_bps = self.LATENCY_MAX_DL_MBPS * 125_000.0
        self._jitter_min_dl_bps = self.JITTER_MIN_DL_MBPS * 125_000.0
        self._jitter_max_dl_bps = self.JITTER_MAX_DL_MBPS * 125_000.0

    def qualify(
        self,
        *,
        rx_bps: float,
        tx_bps: float,
        traffic_valid: bool,
        invalid_reason: str = "",
    ) -> LatencyTrafficQualification:
        rx = float(rx_bps)
        tx = float(tx_bps)
        valid = bool(traffic_valid) and all(
            math.isfinite(value) and value >= 0.0 for value in (rx, tx)
        )
        if not valid:
            reason = str(invalid_reason or "traffic_rate_unavailable").strip().lower()
            return LatencyTrafficQualification(
                valid=False,
                invalid_reason=reason,
                rtt_baseline_idle=False,
                rtt_busy_now=False,
                rtt_release_safe=False,
                rtt_instant_busy=False,
                rtt_busy_mode="",
                low_load_latency_valid=False,
                low_load_latency_reason=reason,
                jitter_valid=False,
                jitter_reason=reason,
                jitter_load_mbps=0.0,
            )

        rtt_busy_mode = ""
        if tx > self._rtt_ul_busy_bps:
            rtt_busy_mode = "ul"
        elif rx > self._rtt_dl_busy_bps:
            rtt_busy_mode = "dl"
        upload_quiet = tx <= self._jitter_ul_quiet_bps
        low_load_latency_valid = rx <= self._latency_max_dl_bps and upload_quiet
        if not upload_quiet:
            low_load_latency_reason = "upload_contamination"
        elif rx > self._latency_max_dl_bps:
            low_load_latency_reason = "download_above_latency_window"
        else:
            low_load_latency_reason = ""
        jitter_valid = (
            self._jitter_min_dl_bps <= rx <= self._jitter_max_dl_bps
            and upload_quiet
        )
        if not upload_quiet:
            jitter_reason = "upload_contamination"
        elif rx < self._jitter_min_dl_bps:
            jitter_reason = "download_below_jitter_window"
        elif rx > self._jitter_max_dl_bps:
            jitter_reason = "download_above_jitter_window"
        else:
            jitter_reason = ""
        return LatencyTrafficQualification(
            valid=True,
            invalid_reason="",
            rtt_baseline_idle=bool(
                rx <= self._rtt_baseline_rx_bps
                and tx <= self._rtt_baseline_tx_bps
            ),
            rtt_busy_now=bool(
                rx > self._rtt_dl_busy_bps or tx > self._rtt_ul_busy_bps
            ),
            rtt_release_safe=bool(
                rx <= self._rtt_dl_release_bps
                and tx <= self._rtt_ul_release_bps
            ),
            rtt_instant_busy=bool(
                rx >= self._rtt_dl_instant_busy_bps
                or tx > self._rtt_ul_busy_bps
            ),
            rtt_busy_mode=rtt_busy_mode,
            low_load_latency_valid=low_load_latency_valid,
            low_load_latency_reason=low_load_latency_reason,
            jitter_valid=jitter_valid,
            jitter_reason=jitter_reason,
            jitter_load_mbps=rx / 125_000.0,
        )


__all__ = ("LatencyTrafficQualifier",)
