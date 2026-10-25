"""
arch4/signal_processing/congestion_module.py
═══════════════════════════════════════════════════════════════
Canonical congestion module entrypoint.

This module consolidates the full congestion stack:
  - raw-input RTT baseline / trust / recovery processing
  - raw-input RSRQ baseline / trust / hold processing
  - combined congestion scoring and stabilization
  - active baseline ownership for RTT and RSRQ

SignalComputer now feeds raw modem inputs into this module and consumes the
resulting canonical congestion surface. Projection exports remain stable,
but the live congestion lane no longer treats shared RTTProcessor /
RSRQProcessor validity outputs as its own truth. The ownership boundary now
lives here instead of being fragmented across SignalComputer, RTTProcessor,
RSRQProcessor, and CongestionAuthority.
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from arch4.rtt_observation import RttObservation

from arch4.core_contracts.signals import (
    LatencyTrafficQualification,
    SignalSamplePort,
)

from typing import Any, Mapping

from arch4.canonical_delta_display import build_delta_display_surface
from arch4.coerce import safe_float, safe_int
from arch4.interfaces import ComputedSignals
from arch4.signal_processing.congestion_authority import CongestionAuthority
from arch4.signal_processing.congestion_combiner import (
    CongestionInputContract,
    RSRQCongestionLane,
    RTTCongestionLane,
)
from arch4.signal_processing.congestion_threshold_policy import (
    CongestionThresholdPolicy,
    require_congestion_threshold_policy,
)
from arch4.signal_processing.rsrq_processor import RSRQProcessor
from arch4.signal_processing.rtt_processor import RTTProcessor


def _delta_display_previous_surface(base_status: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(base_status, Mapping) or not base_status:
        return {}
    return {
        "rtt_delta_display_ms": base_status.get("rtt_delta_display_ms"),
        "rtt_delta_display_valid": base_status.get("rtt_delta_display_valid"),
        "rtt_delta_display_held": base_status.get("rtt_delta_display_held"),
        "rtt_delta_display_live_ts": base_status.get("rtt_delta_display_live_ts"),
        "rsrq_delta_display_db": base_status.get("rsrq_delta_display_db"),
        "rsrq_delta_display_valid": base_status.get("rsrq_delta_display_valid"),
        "rsrq_delta_display_held": base_status.get("rsrq_delta_display_held"),
        "rsrq_delta_display_live_ts": base_status.get("rsrq_delta_display_live_ts"),
        "rsrq_baseline_display_db": base_status.get("rsrq_baseline_display_db"),
        "rsrq_baseline_display_valid": base_status.get("rsrq_baseline_display_valid"),
        "rsrq_baseline_display_held": base_status.get("rsrq_baseline_display_held"),
        "rsrq_baseline_display_live_ts": base_status.get("rsrq_baseline_display_live_ts"),
    }


def build_computed_status_surface(
    computed: ComputedSignals,
    *,
    base_status: Mapping[str, Any] | None = None,
    current_status: Mapping[str, Any] | None = None,
    now_ts: float,
    rsrq_supported: bool,
) -> dict[str, Any]:
    """Build the status/export surface owned by RTT/CQ/congestion processors."""
    out: dict[str, Any] = {}

    out["rtt_ms"] = safe_float(getattr(computed, "rtt_ms", 0.0), 0.0)
    out["rtt_sliding_avg"] = safe_float(getattr(computed, "rtt_sliding_avg", 0.0), 0.0)
    out["rtt_congestion_avg"] = safe_float(
        getattr(computed, "rtt_congestion_avg", getattr(computed, "rtt_sliding_avg", 0.0)),
        0.0,
    )
    out["rtt_recent_median"] = safe_float(
        getattr(computed, "rtt_recent_median", getattr(computed, "rtt_sliding_avg", 0.0)),
        0.0,
    )
    out["rtt_baseline"] = safe_float(getattr(computed, "rtt_baseline", 0.0), 0.0)
    out["rtt_effective_baseline"] = safe_float(getattr(computed, "rtt_effective_baseline", 0.0), 0.0)
    out["rtt_delta"] = safe_float(getattr(computed, "rtt_delta", 0.0), 0.0)
    out["rtt_delta_valid"] = bool(getattr(computed, "rtt_delta_valid", False))
    out["rtt_quiet_delta"] = safe_float(getattr(computed, "rtt_quiet_delta", 0.0), 0.0)
    out["rtt_quiet_held"] = bool(getattr(computed, "rtt_quiet_held", False))
    out["rtt_delta_block_reason"] = str(getattr(computed, "rtt_delta_block_reason", "") or "")
    out["rtt_probe_ok"] = bool(getattr(computed, "rtt_probe_ok", True))
    out["rtt_pressure_level"] = str(getattr(computed, "rtt_pressure_level", "normal") or "normal")
    out["rtt_low_load_latency_valid"] = bool(
        getattr(computed, "rtt_low_load_latency_valid", False)
    )
    out["rtt_low_load_latency_reason"] = str(
        getattr(computed, "rtt_low_load_latency_reason", "") or ""
    )
    out["rtt_low_load_latency_mbps"] = safe_float(
        getattr(computed, "rtt_low_load_latency_mbps", 0.0),
        0.0,
    )
    out["rtt_dl_jitter_bucket"] = str(getattr(computed, "rtt_dl_jitter_bucket", "") or "")
    jitter_valid = bool(getattr(computed, "rtt_dl_jitter_valid", False))
    out["rtt_dl_jitter_trimmed_ms"] = (
        safe_float(getattr(computed, "rtt_dl_jitter_trimmed_ms", 0.0), 0.0)
        if jitter_valid
        else None
    )
    out["rtt_dl_jitter_baseline_ms"] = (
        safe_float(getattr(computed, "rtt_dl_jitter_baseline_ms", 0.0), 0.0)
        if jitter_valid
        else None
    )
    out["rtt_dl_jitter_delta_ms"] = (
        safe_float(getattr(computed, "rtt_dl_jitter_delta_ms", 0.0), 0.0)
        if jitter_valid
        else None
    )
    out["rtt_dl_jitter_pct"] = (
        safe_float(getattr(computed, "rtt_dl_jitter_pct", 0.0), 0.0)
        if jitter_valid
        else None
    )
    out["rtt_dl_jitter_valid"] = jitter_valid
    out["rtt_dl_jitter_reason"] = str(
        getattr(computed, "rtt_dl_jitter_reason", "") or ""
    )

    cq_cell = safe_float(getattr(computed, "cq_cell", 0.0), 0.0)
    cq_valid = bool(getattr(computed, "cq_vector_a_valid", False))
    cq_source = str(getattr(computed, "cq_cell_source", "warming") or "warming")
    out["cq_cell"] = cq_cell
    out["cq_cell_valid"] = cq_valid
    out["cq_cell_source"] = cq_source
    out["cq_cqi_source"] = str(getattr(computed, "cq_cqi_source", "none") or "none")
    out["cq_coverage"] = safe_float(getattr(computed, "cq_coverage", 0.0), 0.0)
    out["cq_vector_a_valid"] = bool(getattr(computed, "cq_vector_a_valid", False))
    out["bq_cell"] = safe_float(getattr(computed, "bq_cell", 0.0), 0.0)
    out["bq_cell_valid"] = bool(getattr(computed, "bq_cell_valid", False))
    out["bq_cell_source"] = str(getattr(computed, "bq_cell_source", "warming") or "warming")
    out["cqb_cell"] = safe_float(getattr(computed, "cqb_cell", 0.0), 0.0)
    out["cqb_cell_valid"] = bool(getattr(computed, "cqb_cell_valid", False))
    out["cqb_cell_source"] = str(
        getattr(computed, "cqb_cell_source", "warming") or "warming"
    )
    out["cqb_coverage"] = safe_float(
        getattr(computed, "cqb_coverage", 0.0), 0.0
    )

    out["cqi_mcs_expected_mcs"] = safe_int(getattr(computed, "cqi_mcs_expected_mcs", 0), 0)
    out["cqi_mcs_observed_mcs"] = safe_int(getattr(computed, "cqi_mcs_observed_mcs", 0), 0)
    out["cqi_mcs_gap_idx"] = safe_int(getattr(computed, "cqi_mcs_gap_idx", 0), 0)
    out["cqi_mcs_gap_ratio"] = safe_float(getattr(computed, "cqi_mcs_gap_ratio", 0.0), 0.0)
    out["cqi_mcs_gap_smoothed"] = safe_float(getattr(computed, "cqi_mcs_gap_smoothed", 0.0), 0.0)
    out["cqi_mcs_gap_valid"] = bool(getattr(computed, "cqi_mcs_gap_valid", False))
    out["congestion_qam_gap"] = safe_float(getattr(computed, "congestion_qam_gap", 0.0), 0.0)
    out["congestion_qualifier_high"] = bool(getattr(computed, "congestion_qualifier_high", False))
    out["congestion_qualifier_mod"] = bool(getattr(computed, "congestion_qualifier_mod", False))
    out["rsrq_baseline"] = safe_float(getattr(computed, "rsrq_baseline", 0.0), 0.0)
    out["rsrq_baseline_valid"] = bool(getattr(computed, "rsrq_baseline_valid", False))
    out["rsrq_baseline_samples"] = safe_int(getattr(computed, "rsrq_baseline_samples", 0), 0)
    out["rsrq_gap"] = safe_float(getattr(computed, "rsrq_gap", 0.0), 0.0)
    out["rsrq_gap_valid"] = bool(getattr(computed, "rsrq_gap_valid", False))

    delta_display_raw = dict(current_status or {})
    delta_display_raw.update(out)
    delta_display_raw["rtt_delta_display_ms"] = safe_float(getattr(computed, "rtt_delta_display_ms", 0.0), 0.0)
    delta_display_raw["rtt_delta_display_valid"] = bool(getattr(computed, "rtt_delta_display_valid", False))
    delta_display_raw["rtt_delta_display_held"] = bool(getattr(computed, "rtt_delta_display_held", False))
    delta_display_raw["rtt_delta_display_live_ts"] = safe_float(getattr(computed, "rtt_delta_display_live_ts", 0.0), 0.0)
    delta_display_raw["rsrq_delta_display_db"] = safe_float(getattr(computed, "rsrq_delta_display_db", 0.0), 0.0)
    delta_display_raw["rsrq_delta_display_valid"] = bool(getattr(computed, "rsrq_delta_display_valid", False))
    delta_display_raw["rsrq_delta_display_held"] = bool(getattr(computed, "rsrq_delta_display_held", False))
    delta_display_raw["rsrq_delta_display_live_ts"] = safe_float(getattr(computed, "rsrq_delta_display_live_ts", 0.0), 0.0)
    delta_display_raw["rsrq_baseline_display_db"] = safe_float(getattr(computed, "rsrq_baseline_display_db", 0.0), 0.0)
    delta_display_raw["rsrq_baseline_display_valid"] = bool(getattr(computed, "rsrq_baseline_display_valid", False))
    delta_display_raw["rsrq_baseline_display_held"] = bool(getattr(computed, "rsrq_baseline_display_held", False))
    delta_display_raw["rsrq_baseline_display_live_ts"] = safe_float(
        getattr(computed, "rsrq_baseline_display_live_ts", 0.0),
        0.0,
    )
    out.update(
        build_delta_display_surface(
            raw=delta_display_raw,
            previous=_delta_display_previous_surface(base_status),
            now_ts=now_ts,
            rsrq_supported=bool(rsrq_supported),
        )
    )

    congestion_internal_bucket = str(getattr(computed, "congestion_internal_bucket", "") or "")
    congestion_bucket = str(getattr(computed, "congestion_bucket", "") or "")
    congestion_state_bucket = str(getattr(computed, "congestion_state_bucket", "") or "")
    congestion_source = str(getattr(computed, "congestion_source", "") or "")
    congestion_pct_valid = bool(
        getattr(computed, "congestion_pct_valid", False)
        or congestion_bucket
        or congestion_source
    )
    out["congestion_supported"] = bool(getattr(computed, "congestion_supported", True))
    out["congestion_support_reason"] = str(getattr(computed, "congestion_support_reason", "") or "")
    out["congestion_pct"] = (
        safe_float(getattr(computed, "congestion_pct", 0.0), 0.0)
        if congestion_pct_valid
        else None
    )
    out["congestion_pct_valid"] = congestion_pct_valid
    congestion_display_valid = bool(
        getattr(computed, "congestion_display_valid", False)
    )
    out["congestion_display_pct"] = (
        safe_float(getattr(computed, "congestion_display_pct", 0.0), 0.0)
        if congestion_display_valid
        else None
    )
    out["congestion_display_valid"] = congestion_display_valid
    out["congestion_internal_bucket"] = congestion_internal_bucket if congestion_pct_valid else ""
    out["congestion_bucket"] = congestion_bucket if congestion_pct_valid else ""
    out["congestion_state_avg_pct"] = (
        safe_float(getattr(computed, "congestion_state_avg_pct", 0.0), 0.0)
        if congestion_pct_valid
        else 0.0
    )
    out["congestion_state_bucket"] = congestion_state_bucket if congestion_pct_valid else ""
    out["congestion_source"] = (
        congestion_source if congestion_source else "none"
    ) if congestion_pct_valid else ""
    out["congestion_fast_mode"] = str(getattr(computed, "congestion_fast_mode", "") or "")
    out["congestion_hold_mode"] = str(getattr(computed, "congestion_hold_mode", "") or "")
    out["congestion_trusted_rtt"] = bool(getattr(computed, "congestion_trusted_rtt", False))
    out["congestion_rtt_base_pct"] = safe_float(getattr(computed, "congestion_rtt_base_pct", 0.0), 0.0)
    out["congestion_trusted_rsrq"] = bool(getattr(computed, "congestion_trusted_rsrq", False))
    out["congestion_rsrq_available"] = bool(getattr(computed, "congestion_rsrq_available", False))
    out["congestion_rsrq_contributes"] = bool(getattr(computed, "congestion_rsrq_contributes", False))
    out["congestion_rsrq_reason"] = str(getattr(computed, "congestion_rsrq_reason", "") or "")
    out["congestion_rsrq_pct"] = safe_float(getattr(computed, "congestion_rsrq_pct", 0.0), 0.0)
    out["congestion_rsrq_gap_db"] = safe_float(getattr(computed, "congestion_rsrq_gap_db", 0.0), 0.0)
    out["congestion_rsrq_gap_threshold_db"] = safe_float(getattr(computed, "congestion_rsrq_gap_threshold_db", 0.0), 0.0)
    out["congestion_trusted_active_jitter"] = bool(getattr(computed, "congestion_trusted_active_jitter", False))
    out["congestion_active_jitter_pct"] = safe_float(getattr(computed, "congestion_active_jitter_pct", 0.0), 0.0)
    out["congestion_slope"] = safe_float(getattr(computed, "congestion_slope", 0.0), 0.0) if congestion_pct_valid else 0.0
    out["congestion_rising"] = bool(getattr(computed, "congestion_rising", False)) if congestion_pct_valid else False
    out["congestion_trend_display"] = (
        str(getattr(computed, "congestion_trend_display", "") or "").strip().upper()
        if congestion_pct_valid
        else ""
    )
    out["congestion_flags_valid"] = bool(getattr(computed, "congestion_flags_valid", False)) if congestion_pct_valid else False
    out["congestion_bad_now"] = bool(getattr(computed, "congestion_bad_now", False)) if congestion_pct_valid else False
    out["congestion_pressure_high"] = bool(getattr(computed, "congestion_pressure_high", False)) if congestion_pct_valid else False
    out["congestion_pressure_moderate"] = (
        bool(getattr(computed, "congestion_pressure_moderate", False))
        if congestion_pct_valid
        else False
    )
    out["congestion_kind_hint"] = str(getattr(computed, "congestion_kind_hint", "") or "") if congestion_pct_valid else ""
    return out


class CongestionModule:
    """Canonical congestion truth producer and owner."""

    def __init__(
        self,
        *,
        rtt_processor_kwargs: dict[str, Any] | None = None,
        rsrq_processor_kwargs: dict[str, Any] | None = None,
        **authority_kwargs: Any,
    ) -> None:
        self._rtt = RTTProcessor(**dict(rtt_processor_kwargs or {}))
        self._rsrq = RSRQProcessor(**dict(rsrq_processor_kwargs or {}))
        self._authority = CongestionAuthority(**authority_kwargs)
        self._runtime_capabilities: Any = None
        self._radio_load_supported_contexts: set[str] = set()

    @property
    def rtt_processor(self) -> RTTProcessor:
        return self._rtt

    @property
    def rsrq_processor(self) -> RSRQProcessor:
        return self._rsrq

    def set_runtime_capabilities(self, caps: Any) -> None:
        self._runtime_capabilities = caps

    @staticmethod
    def _sample_has_radio_load_evidence(modem: SignalSamplePort) -> bool:
        """Return whether this sample proves a usable radio-load lane."""

        rsrq_source = str(
            getattr(modem, "rsrq_source", "reported") or "reported"
        ).strip().lower()
        has_reported_rsrq = bool(
            rsrq_source == "reported"
            and float(getattr(modem, "rsrq_db", 0.0) or 0.0) < 0.0
        )
        has_rsrp = float(getattr(modem, "rsrp_dbm", 0.0) or 0.0) < 0.0
        has_rssi = float(getattr(modem, "rssi_dbm", 0.0) or 0.0) < 0.0
        return bool(has_reported_rsrq or (has_rsrp and has_rssi))

    def _radio_load_supported(
        self,
        modem: SignalSamplePort,
        *,
        ctx_key: str,
    ) -> bool:
        """Return stable runtime support for one exact radio context.

        Modem Live may omit RF fields in one partial read.  Such an omission is
        an invalid observation, not a capability change.  The first usable
        observation establishes support for this runtime context until an
        explicit session reset builds a fresh signal owner.
        """

        key = str(ctx_key or "").strip()
        if self._sample_has_radio_load_evidence(modem):
            if key:
                self._radio_load_supported_contexts.add(key)
            return True
        return bool(key and key in self._radio_load_supported_contexts)

    def set_default_baselines(
        self,
        *,
        rsrq_db: float,
        rtt_ms: float,
    ) -> dict[str, float]:
        """Retired mixed baseline setter; RSRQ baseline is live-sample owned."""

        del rsrq_db, rtt_ms
        raise RuntimeError("cgf_rsrq_baseline_default_retired_use_live_sample_owner")

    def set_default_rtt_baseline(self, *, rtt_ms: float) -> dict[str, float]:
        """Apply owner-validated RTT default without touching RSRQ baseline."""

        rtt = max(0.0, float(rtt_ms))
        self._rtt._default_baseline_ms = rtt
        return {"rtt_ms": rtt}

    def update_thresholds(
        self,
        *,
        policy: CongestionThresholdPolicy,
    ) -> None:
        applied = require_congestion_threshold_policy(policy)
        self._rtt.update_thresholds(
            rtt_warning_ms=float(applied.rtt_warning_ms),
            rtt_cliff_ms=float(applied.rtt_cliff_ms),
        )
        self._authority.update_thresholds(
            rtt_floor_ms=float(applied.rtt_floor_ms),
            rtt_ceiling_ms=float(applied.rtt_ceiling_ms),
            rsrq_gap_threshold_db=float(applied.rsrq_gap_threshold_db),
            rsrq_ceiling_db=float(applied.rsrq_ceiling_db),
            moderate_pct=float(applied.moderate_pct),
            high_pct=float(applied.high_pct),
            rsrq_moderate_db=float(applied.rsrq_moderate_db),
            rsrq_high_db=float(applied.rsrq_high_db),
            local_rf_ok_sinr_db=float(applied.local_rf_ok_sinr_db),
            local_rf_ok_rsrp_dbm=float(applied.local_rf_ok_rsrp_dbm),
            local_self_load_dl_mbps=float(applied.local_self_load_dl_mbps),
        )

    def update_weights(self, *, rtt_weight: float, rsrq_weight: float) -> dict[str, float]:
        return self._authority.update_weights(rtt_weight=rtt_weight, rsrq_weight=rsrq_weight)

    def support_verdict(
        self,
        cs: ComputedSignals,
        *,
        radio_load_supported: bool,
    ) -> tuple[bool, str]:
        caps = self._runtime_capabilities
        traffic_presence = True if caps is None else bool(getattr(caps, "supports_traffic_presence", False))
        traffic_volume = True if caps is None else bool(getattr(caps, "supports_traffic_volume", False))
        traffic_reporting = traffic_presence or traffic_volume
        if not traffic_reporting:
            return False, "traffic_reporting_unavailable"

        rtt_supported = bool(
            getattr(cs, "rtt_ms", 0.0)
            or getattr(cs, "rtt_sliding_avg", 0.0)
        )

        if not rtt_supported and not radio_load_supported:
            return False, "congestion_evidence_unavailable"

        return True, ""

    def _build_input_contract(
        self,
        *,
        cs: ComputedSignals,
        ctx_key: str,
        rsrq_ctx_key: str,
        rsrq_supported: bool,
    ) -> CongestionInputContract:
        return CongestionInputContract(
            rtt=RTTCongestionLane(
                ctx_key=str(ctx_key or ""),
                delta_ms=max(0.0, float(getattr(cs, "rtt_delta", 0.0) or 0.0)),
                valid=bool(getattr(cs, "rtt_delta_valid", False)),
                block_reason=str(getattr(cs, "rtt_delta_block_reason", "") or "").strip().lower(),
                # RTT is supplied by CGF's independent ping owner, not by a
                # modem capability.  A blocked observation is transient.
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
                ctx_key=str(rsrq_ctx_key or ""),
                gap_db=max(0.0, float(getattr(cs, "rsrq_gap", 0.0) or 0.0)),
                valid=bool(getattr(cs, "rsrq_gap_valid", False)),
                baseline_db=float(getattr(cs, "rsrq_baseline", 0.0) or 0.0),
                display_gap_db=max(0.0, float(getattr(cs, "rsrq_display_gap", 0.0) or 0.0)),
                display_valid=bool(getattr(cs, "rsrq_display_gap_valid", False)),
                display_held=bool(getattr(cs, "rsrq_display_gap_held", False)),
                supported=bool(rsrq_supported),
            ),
        )

    def process(
        self,
        *,
        modem: SignalSamplePort,
        rtt_observation: RttObservation,
        ctx_key: str,
        rsrq_ctx_key: str,
        traffic: LatencyTrafficQualification,
        rx_bps: float,
        tx_bps: float,
        cs: ComputedSignals,
    ) -> None:
        self._rtt.update(rtt_observation, traffic, ctx_key, cs)
        self._rsrq.update(
            float(getattr(modem, "rsrq_db", 0.0) or 0.0),
            rsrq_source=str(getattr(modem, "rsrq_source", "reported") or "reported"),
            ctx_key=rsrq_ctx_key,
            radio_connected=bool(getattr(modem, "radio_connected", False)),
            rtt_elevated=cs.rtt_elevated,
            rtt_alarm=cs.rtt_alarm,
            sinr_db=float(getattr(modem, "sinr_db", 0.0) or 0.0),
            rsrp_dbm=float(getattr(modem, "rsrp_dbm", 0.0) or 0.0),
            rssi_dbm=float(getattr(modem, "rssi_dbm", 0.0) or 0.0),
            rx_bps=rx_bps,
            tx_bps=tx_bps,
            has_traffic=bool(getattr(modem, "has_traffic", False)),
            cs=cs,
        )

        radio_load_supported = self._radio_load_supported(
            modem,
            ctx_key=rsrq_ctx_key,
        )
        congestion_supported, congestion_support_reason = self.support_verdict(
            cs,
            radio_load_supported=radio_load_supported,
        )
        cs.congestion_supported = bool(congestion_supported)
        cs.congestion_support_reason = str(congestion_support_reason or "")
        if congestion_supported:
            lanes = self._build_input_contract(
                cs=cs,
                ctx_key=ctx_key,
                rsrq_ctx_key=rsrq_ctx_key,
                rsrq_supported=radio_load_supported,
            )
            self._authority.update(
                cs,
                rsrq_ctx_key,
                lanes=lanes,
                has_traffic=bool(getattr(modem, "has_traffic", False)),
                sinr_db=float(getattr(modem, "sinr_db", 0.0) or 0.0),
                rsrp_dbm=float(getattr(modem, "rsrp_dbm", 0.0) or 0.0),
                throughput_dl_mbps=float(getattr(modem, "throughput_dl_mbps", 0.0) or 0.0),
            )
        else:
            self.mark_unsupported(cs, reason=congestion_support_reason)

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
        self._authority.finalize(
            cs,
            now_ts=now_ts,
            has_traffic=has_traffic,
            sinr_db=sinr_db,
            rsrp_dbm=rsrp_dbm,
            throughput_dl_mbps=throughput_dl_mbps,
        )

    def mark_unsupported(self, cs: ComputedSignals, *, reason: str = "") -> None:
        self._authority.mark_unsupported(cs, reason=reason)

    def export_rtt_state(self) -> dict[str, Any]:
        return self._rtt.export_state()

    def export_rsrq_state(self) -> dict[str, Any]:
        return self._rsrq.export_state()

    def export_rsrq_baseline_state(self) -> dict[str, Any]:
        return self._rsrq.export_baseline_state()

    def restore_rsrq_baseline_state(self, payload: dict[str, Any]) -> bool:
        return self._rsrq.restore_baseline_state(payload)

    def rsrq_baseline_state_dirty(self) -> bool:
        return self._rsrq.baseline_state_dirty()

    def mark_rsrq_baseline_state_saved(self) -> None:
        self._rsrq.mark_baseline_state_saved()

    def export_authority_state(self) -> dict[str, Any]:
        return self._authority.export_state()

    def export_state(self) -> dict[str, Any]:
        return {
            "rtt": self.export_rtt_state(),
            "rsrq": self.export_rsrq_state(),
            "authority": self.export_authority_state(),
        }

    def restore_state(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            rtt_payload = payload.get("rtt")
            rsrq_payload = payload.get("rsrq")
            authority_payload = payload.get("authority")
            if not all(
                isinstance(item, dict)
                for item in (rtt_payload, rsrq_payload, authority_payload)
            ):
                return False
            return bool(
                self._rtt.restore_state(rtt_payload)
                and self._rsrq.restore_state(rsrq_payload)
                and self._authority.restore_state(authority_payload)
            )
        except Exception:
            return False

    def admit_learned_baselines(
        self,
        *,
        rtt_ctx_key: str,
        rsrq_ctx_key: str,
        rtt_baseline_ms: float | None,
        rsrq_baseline_db: float | None,
        rsrq_learned_at: float,
    ) -> dict[str, bool]:
        """Route exact learned facts to the two retained active owners."""

        return {
            "rsrq_changed": bool(
                rsrq_baseline_db is not None
                and self._rsrq.admit_learned_baseline(
                    rsrq_ctx_key,
                    rsrq_baseline_db,
                    learned_at=rsrq_learned_at,
                )
            ),
            "rtt_changed": bool(
                rtt_baseline_ms is not None
                and self._rtt.admit_learned_baseline(
                    rtt_ctx_key,
                    rtt_baseline_ms,
                )
            ),
        }
