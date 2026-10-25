"""Coordinate signal processing. Dedicated processors handle congestion, scheduler values,
and 5G NSA values. This module keeps BQ/CQ, structural-change and input-freeze
detection, plus the short signal history used for learning. It computes facts only; it
does not choose or execute actions.
"""

import copy
import logging
import math
import time
from collections import deque
from typing import Optional, Any
from collections.abc import Mapping

from arch4.interfaces import (
    ComputedSignals,
    Arch4Config,
    band_signature,
    normalize_band_list,
)
from arch4.core_contracts.signals import SignalSamplePort
from arch4.core_contracts.command_targets import core_lte_pci
from arch4.signal_processing.context import signal_context_key
from arch4.rtt_observation import RttObservation
from arch4.core.latency_traffic_qualifier import LatencyTrafficQualifier
from arch4.core.rsrq_baseline_state import RSRQBaselineStateStore
from arch4.config_views import build_signal_processing_config
from arch4.signal_processing.band_quality import score_band_quality
from arch4.signal_processing.combined_quality import FORMULA_REVISION, score_combined_quality
from arch4.core_contracts.quality_model import LTE_QUALITY_MODEL, NR_QUALITY_MODEL, UNKNOWN_QUALITY_MODEL, require_quality_model
from arch4.signal_processing.delivered_capacity import estimate_lte_delivered_capacity
from arch4.signal_processing.dl_bottleneck import assess_dl_bottleneck
from arch4.signal_processing.computed_congestion_signals import (
    authority_congestion_flags,
    canonical_congestion_pct,
    canonical_rtt_delta,
    congestion_rtt_delta_ready,
    congestion_rtt_high_ms,
    congestion_rtt_level,
    congestion_rtt_mod_ms,
    congestion_rtt_ref_ms,
    quiet_rtt_avg,
)
from arch4.signal_processing.policy_thresholds import policy_threshold_authority
from arch4.signal_processing import (
    SchedulerProcessor,
    NRProcessor,
    CATelemetryProcessor,
    CongestionModule,
    DLJitterProcessor,
)
from arch4.signal_processing.scheduler_processor import (
    scheduler_mode_from_matrix_trust,
)
from arch4.signal_processing.congestion_threshold_policy import (
    build_congestion_threshold_policy,
)

log = logging.getLogger("arch4.core.signal_computer")

_BASELINE_SAVE_RETRY_S = 30.0

_LTE_BW_TO_NRB = {
    1: 6,
    3: 15,
    5: 25,
    10: 50,
    15: 75,
    20: 100,
}


def _controlled_speedtest_dl_samples_mbps(modem: Any) -> list[float]:
    raw = getattr(modem, "controlled_speedtest_dl_samples_mbps", None)
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)):
        pieces = str(raw).replace(";", ",").split(",")
    else:
        try:
            pieces = list(raw)
        except TypeError:
            pieces = [raw]
    samples: list[float] = []
    for item in pieces:
        try:
            value = float(item)
        except (TypeError, ValueError):
            continue
        if value > 0.0:
            samples.append(value)
    return samples


def classify_cell_structural_change(
    *,
    band_changed: bool,
    pci_changed: bool,
    earfcn_changed: bool,
    radio_connected: bool = True,
    has_traffic: bool = False,
    ca_active: bool = False,
) -> tuple[str, bool]:
    """Classify cell movement by operational meaning, not only raw register deltas."""
    if not bool(radio_connected):
        return "service_loss_shift", True
    if bool(band_changed):
        return "anchor_move", True
    if bool(pci_changed or earfcn_changed):
        return "same_band_cell", bool(has_traffic and not ca_active)
    return "unknown", False


class SignalComputer:
    """Compute derived signals from raw modem observations. Processor state is kept between
    samples, and each update returns one ComputedSignals value.
    """

    def __init__(
        self,
        config: Arch4Config,
        *,
        rsrq_baseline_store: RSRQBaselineStateStore | None = None,
    ):
        self.cfg = config
        self.signal_cfg = build_signal_processing_config(config)
        self._hard_thresholds = policy_threshold_authority(config).get("hard", {})
        self._sample_interval_s = max(
            0.1, float(getattr(config, "sample_interval_s", 2.0) or 2.0)
        )

        def _samples_for(seconds: float) -> int:
            return max(1, int(round(float(seconds) / self._sample_interval_s)))

        # Signal processors
        congestion_dl_threshold_mbps = float(
            getattr(config, "congestion_self_load_dl_mbps", 10.0) or 10.0
        )
        congestion_dl_threshold_bps = max(0.0, congestion_dl_threshold_mbps * 125_000.0)
        congestion_dl_release_bps = max(
            0.0, min(congestion_dl_threshold_bps, congestion_dl_threshold_bps * 0.8)
        )
        congestion_dl_instant_busy_bps = max(
            congestion_dl_threshold_bps,
            congestion_dl_threshold_bps * 1.5,
        )
        self._latency_traffic = LatencyTrafficQualifier(
            rtt_dl_busy_bps=congestion_dl_threshold_bps,
            rtt_dl_release_bps=congestion_dl_release_bps,
            rtt_dl_instant_busy_bps=congestion_dl_instant_busy_bps,
        )

        self._scheduler = SchedulerProcessor()
        self._nr = NRProcessor()
        self._ca_telemetry = CATelemetryProcessor()
        self._dl_jitter = DLJitterProcessor(
            pct_scale_ms=float(
                getattr(config, "jitter_formula_pct_scale_ms", 5.0) or 5.0
            ),
            visible_scale=float(
                getattr(config, "jitter_formula_visible_scale", 3.0) or 3.0
            ),
            low_frac=float(getattr(config, "jitter_formula_low_frac", 0.20) or 0.20),
            trim_frac=float(getattr(config, "jitter_formula_trim_frac", 0.10) or 0.10),
        )

        threshold_policy = build_congestion_threshold_policy(
            signal_cfg=self.signal_cfg,
            config=config,
            hard_thresholds=self._hard_thresholds,
            source="SignalComputer.__init__",
        )
        self._congestion = CongestionModule(
            rtt_processor_kwargs={
                "ema_alpha": self.signal_cfg.rtt_ema_alpha,
                "sliding_window_size": self.signal_cfg.rtt_sliding_window,
                "rtt_warning_ms": threshold_policy.rtt_warning_ms,
                "rtt_cliff_ms": threshold_policy.rtt_cliff_ms,
                "rtt_mod_ms": float(self.signal_cfg.congestion_qualifier_rtt_mod_ms),
                "congestion_window_size": int(self.signal_cfg.v5_rtt_congestion_window),
                "recent_window_size": int(self.signal_cfg.v5_rtt_recent_window),
                "default_baseline_ms": float(
                    getattr(config, "rtt_baseline_default", 55.0) or 55.0
                ),
            },
            rsrq_processor_kwargs={
                "baseline_dl_bps": congestion_dl_threshold_bps,
                "live_rx_bps": congestion_dl_threshold_bps,
            },
            rtt_floor_ms=threshold_policy.rtt_floor_ms,
            rtt_ceiling_ms=threshold_policy.rtt_ceiling_ms,
            rsrq_gap_threshold_db=threshold_policy.rsrq_gap_threshold_db,
            rsrq_ceiling_db=threshold_policy.rsrq_ceiling_db,
            moderate_pct=threshold_policy.moderate_pct,
            high_pct=threshold_policy.high_pct,
            rsrq_moderate_db=threshold_policy.rsrq_moderate_db,
            rsrq_high_db=threshold_policy.rsrq_high_db,
            local_rf_ok_sinr_db=threshold_policy.local_rf_ok_sinr_db,
            local_rf_ok_rsrp_dbm=threshold_policy.local_rf_ok_rsrp_dbm,
            local_self_load_dl_mbps=threshold_policy.local_self_load_dl_mbps,
            rtt_weight=float(
                getattr(config, "congestion_rtt_weight", 50.0) or 0.0
            ),
            rsrq_weight=float(
                getattr(config, "congestion_rsrq_weight", 50.0) or 0.0
            ),
            state_confirm_window_s=float(
                getattr(config, "congestion_state_confirm_window_s", 30.0) or 0.0
            ),
        )
        self._rsrq_baseline_store = rsrq_baseline_store
        self._rsrq_baseline_store_error = ""
        self._rsrq_baseline_retry_at = 0.0
        self._rsrq_physical_scope = ""
        # Structural change detection
        self._last_pci: int = -1
        self._last_earfcn: int = -1
        self._last_primary_band: int = -1
        self._last_structural_sample_sequence: int = 0
        self._last_structural_sample_ts: float = 0.0
        self._structural_change_detected: bool = False
        self._structural_change_kind: str = ""
        self._structural_change_samples: int = 0

        # Input freeze detection
        self._last_signal_fingerprint: tuple = ()
        self._input_freeze_samples: int = 0

        # BQ and CQ processing
        cq_window = self.signal_cfg.v5_cq_window_samples
        self._bq_scores: deque = deque(maxlen=cq_window)
        self._quality_model = ""
        self._combined_context = None
        self._combined_capacities: deque = deque(maxlen=cq_window)
        self._cqb_scores: deque = deque(maxlen=cq_window)
        self._cq_congestion_pcts: deque = deque(maxlen=cq_window)
        self._bq_last_value: Optional[float] = None
        self._bq_restored: bool = False
        self._cq_last_value: Optional[float] = None
        self._cq_restored: bool = False
        self._cqb_last_structural: Optional[float] = None
        self._cq_congestion_band: Optional[int] = None

        self._runtime_capabilities: Any = None

    def persist_durable_state(self) -> None:
        """Persist accepted signal state; called only after compute succeeds."""

        store = self._rsrq_baseline_store
        if store is None or not self._congestion.rsrq_baseline_state_dirty():
            return
        if time.monotonic() < self._rsrq_baseline_retry_at:
            return
        try:
            state = self._congestion.export_rsrq_baseline_state()
            # Unconfirmed live samples may be displayed but cannot seed the
            # next session. Old unqualified entries stay retained, never used
            # by the qualified production context lookup.
            state["entries"] = {key: value for key, value in state["entries"].items()
                                if "|sim=unconfirmed|" not in key and "|net=unregistered|" not in key}
            store.save(state, physical_scope=self._rsrq_physical_scope)
            self._congestion.mark_rsrq_baseline_state_saved()
            if self._rsrq_baseline_store_error:
                log.info("CGF RSRQ baseline persistence recovered")
            self._rsrq_baseline_store_error = ""
            self._rsrq_baseline_retry_at = 0.0
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if not self._rsrq_baseline_store_error:
                log.error("CGF RSRQ baseline persistence failed: %s", error)
            self._rsrq_baseline_store_error = error
            self._rsrq_baseline_retry_at = time.monotonic() + _BASELINE_SAVE_RETRY_S

    def baseline_persistence_status(self) -> dict[str, Any]:
        """Read-only save health; the signal owner retains unsaved revisions."""

        return {
            "owner": "54guc.core.rsrq_baseline_state",
            "healthy": not bool(self._rsrq_baseline_store_error),
            "last_error": self._rsrq_baseline_store_error,
            "pending": bool(
                self._rsrq_baseline_store is not None
                and self._congestion.rsrq_baseline_state_dirty()
            ),
        }

    def refresh_thresholds(self) -> None:
        """Reload every config-owned signal parameter as one consumer ACK."""
        self._hard_thresholds = policy_threshold_authority(self.cfg).get("hard", {})
        self._congestion.update_thresholds(
            policy=build_congestion_threshold_policy(
                signal_cfg=self.signal_cfg,
                config=self.cfg,
                hard_thresholds=self._hard_thresholds,
                source="SignalComputer.refresh_thresholds",
            )
        )
        rtt = max(
            0.0,
            min(
                100.0,
                float(getattr(self.cfg, "congestion_rtt_weight", 50.0) or 0.0),
            ),
        )
        rsrq = max(
            0.0,
            min(
                100.0,
                float(getattr(self.cfg, "congestion_rsrq_weight", 50.0) or 0.0),
            ),
        )
        if (rtt + rsrq) <= 0.0:
            rtt = 50.0
            rsrq = 50.0
        self._congestion.update_weights(rtt_weight=rtt, rsrq_weight=rsrq)
        self._congestion.set_default_rtt_baseline(
            rtt_ms=float(getattr(self.cfg, "rtt_baseline_default", 55.0) or 55.0)
        )
        self._dl_jitter.update_formula(**self._cfg_jitter_formula_status())

    def bind_config_generation(
        self,
        config: Arch4Config,
        *,
        generation_id: str,
        config_fingerprint: str,
    ) -> None:
        """Bind one immutable generation before refreshing cached processors."""

        self.cfg = config
        self.signal_cfg = build_signal_processing_config(config)
        self._config_generation_id = str(generation_id or "")
        self._config_fingerprint = str(config_fingerprint or "")
        self.refresh_thresholds()

    def get_congestion_weights_status(self) -> dict[str, float]:
        authority_state = self._congestion.export_authority_state()
        state = (
            dict(authority_state.get("combiner") or {})
            if isinstance(authority_state, dict)
            else {}
        )

        def _weight(name: str) -> float:
            value = state.get(name, 50.0)
            if value is None:
                return 50.0
            return float(value)

        return {
            "rtt_weight": _weight("rtt_weight"),
            "rsrq_weight": _weight("rsrq_weight"),
            "active_rtt_weight": _weight("active_rtt_weight"),
            "active_rsrq_weight": _weight("active_rsrq_weight"),
        }

    def set_runtime_capabilities(self, caps: Any) -> None:
        """Attach current modem capability truth for signal-side feature gating."""
        self._runtime_capabilities = caps
        self._congestion.set_runtime_capabilities(caps)

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
        """Apply the remaining config-owned RTT default baseline."""

        return self._congestion.set_default_rtt_baseline(rtt_ms=rtt_ms)

    def admit_learned_signal_baselines(
        self,
        *,
        serving_band: int,
        subscription_key: str,
        network_plmn: str,
        cell_id: int,
        earfcn: int,
        bandwidth_mhz: float,
        rsrq_baseline_db: float | None,
        rtt_baseline_ms: float | None,
        learned_at: float,
    ) -> dict[str, bool]:
        """Load matching History baselines into the active signal processors."""

        if any(type(value) is not int for value in (serving_band, cell_id, earfcn)):
            raise TypeError("signal_learned_baseline_context_integer_required")
        if serving_band <= 0 or cell_id <= 0 or earfcn < 0:
            raise ValueError("signal_learned_baseline_context_invalid")
        bandwidth = float(bandwidth_mhz)
        if bandwidth < 0.0:
            raise ValueError("signal_learned_baseline_bandwidth_invalid")
        if not subscription_key or not network_plmn:
            raise ValueError("signal_learned_baseline_subscription_network_required")
        rtt_ctx_key = signal_context_key(band=serving_band, cell_id=cell_id,
                                        earfcn=earfcn, subscription_key=subscription_key,
                                        network_plmn=network_plmn)
        rsrq_ctx_key = (
            rtt_ctx_key + "|" + self._format_bandwidth_key(bandwidth)
        )
        return self._congestion.admit_learned_baselines(
            rtt_ctx_key=rtt_ctx_key,
            rsrq_ctx_key=rsrq_ctx_key,
            rtt_baseline_ms=rtt_baseline_ms,
            rsrq_baseline_db=rsrq_baseline_db,
            rsrq_learned_at=float(learned_at),
        )

    def capture_transaction_state(self) -> dict[str, Any]:
        """Capture every mutable signal-owner field before one admitted update.

        This image exists only to roll back one failed accepted-generation
        transaction. It is not a durable checkpoint or Intelligence history.

        Configuration and attached read-only collaborators keep their original
        identities.  Every other field is deeply copied so processor objects,
        deques, counters, CA telemetry, structural detection and precursor
        history form one atomic state image.
        """

        external = (
            self.cfg,
            self.signal_cfg,
            self._runtime_capabilities,
            self._rsrq_baseline_store,
        )
        memo = {id(value): value for value in external if value is not None}
        return {
            "schema": "signal_compute_transaction_state.v1",
            "state": copy.deepcopy(self.__dict__, memo),
        }

    def restore_transaction_state(self, payload: dict[str, Any] | None) -> bool:
        """Restore saved processor state after an update fails."""

        if not isinstance(payload, dict):
            return False
        if str(payload.get("schema") or "") != "signal_compute_transaction_state.v1":
            return False
        state = payload.get("state")
        if not isinstance(state, dict):
            return False
        required = {"cfg", "signal_cfg", "_congestion", "_ca_telemetry"}
        if not required.issubset(state):
            return False
        self.__dict__.clear()
        self.__dict__.update(state)
        # Session snapshots contain algorithm state, not current config.
        # Reapply the active config after restoring a snapshot.
        self.refresh_thresholds()
        return True

    def reset_session_state(self) -> bool:
        """Clear all observation-derived state for an explicitly adopted modem.

        Runtime configuration and read-only collaborators survive.  Signal
        history, processor baselines, holds, counters, CA state, congestion,
        jitter, CQ/BQ and structural-change state are rebuilt so one physical
        modem/session can never seed another implicitly.
        """

        runtime_capabilities = self._runtime_capabilities
        fresh = type(self)(
            self.cfg,
            rsrq_baseline_store=self._rsrq_baseline_store,
        )
        fresh._runtime_capabilities = runtime_capabilities
        fresh._congestion.set_runtime_capabilities(runtime_capabilities)
        self.__dict__.clear()
        self.__dict__.update(fresh.__dict__)
        return True

    # Context keys

    @staticmethod
    def _context_key(modem: SignalSamplePort) -> str:
        """The current subscription/network qualifies the per-cell baseline."""
        band = int(
            getattr(modem, "serving_band", 0) or getattr(modem, "primary_band", 0) or 0
        )
        cell_id = int(getattr(modem, "cell_id", 0) or 0)
        earfcn = int(getattr(modem, "earfcn_dl", getattr(modem, "earfcn", 0)) or 0)
        sim = getattr(modem, "sim_network", None)
        subscription = sim.get("subscription_key", "") if isinstance(sim, Mapping) else ""
        network = str(getattr(modem, "provider_plmn", "") or "")
        return signal_context_key(band=band, cell_id=cell_id, earfcn=earfcn,
                                  subscription_key=subscription, network_plmn=network)

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
    def _rsrq_context_key(cls, modem: SignalSamplePort) -> str:
        """RSRQ context key: band|cell_id|earfcn|primary_bw_mhz.

        RSRQ delta, including the RSRP-RSSI baseline path, is only
        comparable inside a stable LTE measurement bandwidth. Bandwidth
        therefore participates in the RSRQ context even when the serving
        band/cell/EARFCN stay the same.
        """
        base = cls._context_key(modem)
        bw = 0.0
        try:
            bw = float(getattr(modem, "dl_bandwidth_mhz", 0.0) or 0.0)
        except Exception:
            bw = 0.0
        if bw <= 0.0:
            try:
                bw = float(getattr(modem, "primary_bw_mhz", 0.0) or 0.0)
            except Exception:
                bw = 0.0
        return f"{base}|{cls._format_bandwidth_key(bw)}"

    @staticmethod
    def _ca_stat_scope_key(modem: SignalSamplePort) -> str:
        """CAST reset boundary: provider + serving cell + configured CA pool.

        The live CA combo is intentionally excluded. CAST measures how often CA
        is active inside this context, so a CA on/off flip must not reset it.
        """

        def _int_attr(*names: str) -> int:
            for name in names:
                try:
                    value = int(getattr(modem, name, 0) or 0)
                except Exception:
                    value = 0
                if value:
                    return value
            return 0

        provider = str(
            getattr(modem, "provider_plmn", "")
            or getattr(modem, "plmn", "")
            or getattr(modem, "provider_name", "")
            or ""
        ).strip()
        band = _int_attr("serving_band", "primary_band")
        cell_id = _int_attr("cell_id")
        pci = _int_attr("pci")
        earfcn = _int_attr("earfcn_dl", "earfcn")
        pool_sig = str(
            getattr(modem, "configured_band_pool_signature", "")
            or getattr(modem, "selected_band_set_signature", "")
            or ""
        ).strip()
        if not pool_sig:
            pool = (
                getattr(modem, "configured_band_pool", None)
                or getattr(modem, "selected_band_set", None)
                or []
            )
            pool_sig = band_signature(normalize_band_list(pool))
        return (
            f"scope={SignalComputer._context_key(modem)}|provider={provider}|cell={cell_id}|pci={pci}|"
            f"band={band}|earfcn={earfcn}|pool={pool_sig}"
        )

    # Main update

    def update(
        self, modem: SignalSamplePort, rtt_observation: RttObservation
    ) -> ComputedSignals:
        """
        Compute all derived signals from this sample's modem input + RTT.

        Args:
            modem: raw modem observation
            rtt_observation: Modem Live's existing labelled probe and validity.

        Returns:
            ComputedSignals struct for decision engine
        """
        if not isinstance(rtt_observation, RttObservation):
            raise TypeError("signal_compute_typed_rtt_observation_required")
        if self._rsrq_baseline_store is not None:
            scope = modem.session_scope_key
            if not scope:
                raise ValueError("signal_baseline_physical_scope_required")
            if scope != self._rsrq_physical_scope:
                if self._rsrq_physical_scope:
                    raise RuntimeError("signal_baseline_device_change_requires_session_reset")
                self._rsrq_physical_scope = scope
                try:
                    restored = self._rsrq_baseline_store.load(physical_scope=scope)
                    if restored and not self._congestion.restore_rsrq_baseline_state(restored):
                        raise ValueError("cgf_rsrq_baseline_processor_state_invalid")
                except (ValueError, TypeError, OSError) as exc:
                    self._rsrq_baseline_store_error = f"{type(exc).__name__}: {exc}"
                    log.error("CGF RSRQ baseline state rejected; starting from live truth: %s",
                              self._rsrq_baseline_store_error)
        cs = ComputedSignals()
        ctx_key = self._context_key(modem)
        rsrq_ctx_key = self._rsrq_context_key(modem)
        now_ts = time.time()

        rx_bps = float(getattr(modem, "rx_bytes_per_sec", 0.0) or 0.0)
        tx_bps = float(getattr(modem, "tx_bytes_per_sec", 0.0) or 0.0)
        sample_sequence = int(getattr(modem, "sample_sequence", 0) or 0)
        sample_fresh = bool(getattr(modem, "sample_fresh", True))
        latency_traffic = self._latency_traffic.qualify(
            rx_bps=rx_bps,
            tx_bps=tx_bps,
            traffic_valid=bool(getattr(modem, "traffic_valid", True)),
            invalid_reason=str(
                getattr(modem, "traffic_invalid_reason", "") or ""
            ),
        )
        cs.rtt_low_load_latency_valid = latency_traffic.low_load_latency_valid
        cs.rtt_low_load_latency_reason = latency_traffic.low_load_latency_reason
        cs.rtt_low_load_latency_mbps = latency_traffic.jitter_load_mbps
        self._ca_telemetry.update(
            ca_active=bool(getattr(modem, "ca_active", False)),
            radio_connected=bool(getattr(modem, "radio_connected", False)),
            now_ts=now_ts,
            cs=cs,
            scope_key=self._ca_stat_scope_key(modem),
            rx_bps=rx_bps,
            ri=int(getattr(modem, "ri", 0) or 0),
            sample_sequence=sample_sequence,
            sample_fresh=sample_fresh,
        )
        filtered_ca_active = bool(cs.cart)

        # Compute jitter before congestion. This keeps one RTT-derived value and
        # avoids reading RTT again from a display hold.
        self._dl_jitter.update(
            rtt_observation=rtt_observation,
            traffic=latency_traffic,
            ctx_key=ctx_key,
            cs=cs,
        )
        self._congestion.process(
            modem=modem,
            rtt_observation=rtt_observation,
            ctx_key=ctx_key,
            rsrq_ctx_key=rsrq_ctx_key,
            traffic=latency_traffic,
            rx_bps=rx_bps,
            tx_bps=tx_bps,
            cs=cs,
        )

        # Scheduler values are for display only.
        self._scheduler.update(
            cqi=int(getattr(modem, "cqi", 0) or 0),
            mcs_dl=int(
                getattr(modem, "mcs_dl0", 0) or getattr(modem, "mcs_dl", 0) or 0
            ),
            cs=cs,
            mode=scheduler_mode_from_matrix_trust(
                getattr(modem, "scheduler_field_trust", {})
            ),
        )

        # Structural change detection
        self._detect_structural_change(
            modem,
            cs,
            filtered_ca_active=filtered_ca_active,
        )

        # CQ and congestion processing
        self._update_v5_cq_and_congestion_qualifier(modem, cs)
        self._apply_canonical_congestion_projection(modem, cs)

        # 5G NSA processing
        self._nr.update(
            nsa_snapshot=getattr(modem, "nsa_snapshot"),
            cs=cs,
        )

        capacity = estimate_lte_delivered_capacity(
            modem,
            _controlled_speedtest_dl_samples_mbps(modem),
            ca_active=filtered_ca_active,
        )
        cs.delivered_capacity_valid = bool(capacity.valid)
        cs.delivered_capacity_ratio = float(capacity.ratio)
        cs.delivered_capacity_best_dl_mbps = float(capacity.best_dl_mbps)
        cs.delivered_capacity_total_bw_mhz = float(capacity.total_bw_mhz)
        cs.delivered_capacity_ceiling_mbps = float(capacity.ceiling_mbps)
        cs.delivered_capacity_sample_count = int(capacity.sample_count)
        cs.delivered_capacity_source = str(capacity.source or "unavailable")
        dl_bottleneck = assess_dl_bottleneck(
            modem,
            cs,
            ca_active=filtered_ca_active,
        )
        cs.dl_bottleneck_state = dl_bottleneck.state
        cs.dl_bottleneck_reason = dl_bottleneck.reason
        cs.dl_bottleneck_proof_level = dl_bottleneck.proof_level
        cs.dl_bottleneck_missing_evidence = tuple(dl_bottleneck.missing_evidence)
        cs.dl_bottleneck_rejected_explanations = tuple(
            dl_bottleneck.rejected_explanations
        )
        cs.dl_bottleneck_evidence = dict(dl_bottleneck.evidence)
        cs.dl_bottleneck_owner = dl_bottleneck.owner
        cs.dl_bottleneck_schema_version = dl_bottleneck.schema_version

        # Input freeze detection
        self._detect_input_freeze(
            modem,
            cs,
            filtered_ca_active=filtered_ca_active,
        )

        return cs

    # Structural change detection

    def _detect_structural_change(
        self,
        modem: SignalSamplePort,
        cs: ComputedSignals,
        *,
        filtered_ca_active: bool,
    ) -> None:
        """Detect changes in serving cell identity."""
        if not bool(modem.radio_connected):
            cs.cell_structural_change = False
            cs.cell_structural_change_fresh = False
            cs.cell_structural_change_kind = ""
            return

        sample_sequence = int(getattr(modem, "sample_sequence", 0) or 0)
        sample_ts = float(getattr(modem, "timestamp", 0.0) or 0.0)
        last_sequence = int(getattr(self, "_last_structural_sample_sequence", 0) or 0)
        last_sample_ts = float(getattr(self, "_last_structural_sample_ts", 0.0) or 0.0)
        observed_pci = getattr(modem, "pci", None)
        pci = -1 if observed_pci is None else core_lte_pci(observed_pci)
        earfcn = int(getattr(modem, "earfcn_dl", 0) or getattr(modem, "earfcn", 0) or 0)
        band = int(
            getattr(modem, "serving_band", 0) or getattr(modem, "primary_band", 0) or 0
        )
        stale_sequence = (
            sample_sequence > 0
            and last_sequence > 0
            and sample_sequence <= last_sequence
        )
        stale_timestamp = (
            sample_sequence <= 0
            and sample_ts > 0.0
            and last_sample_ts > 0.0
            and sample_ts <= last_sample_ts
        )
        if stale_sequence or stale_timestamp:
            same_current_cell = (
                (pci < 0 or self._last_pci < 0 or pci == self._last_pci)
                and (
                    earfcn <= 0 or self._last_earfcn <= 0 or earfcn == self._last_earfcn
                )
                and (
                    band <= 0
                    or self._last_primary_band <= 0
                    or band == self._last_primary_band
                )
            )
            cs.cell_structural_change = bool(
                self._structural_change_detected and same_current_cell
            )
            cs.cell_structural_change_fresh = False
            cs.cell_structural_change_kind = str(
                self._structural_change_kind if same_current_cell else ""
            )
            cs.cell_change_pci_before = 0
            cs.cell_change_earfcn_before = 0
            cs.cell_change_band_before = 0
            cs.cell_change_ca_sig_before = ""
            cs.cell_structural_change_class = ""
            cs.cell_structural_change_service_impact = False
            return

        if self._last_pci < 0:
            self._last_pci = pci
            self._last_earfcn = earfcn
            self._last_primary_band = band
            self._last_structural_sample_sequence = sample_sequence
            self._last_structural_sample_ts = sample_ts
            cs.cell_structural_change = False
            cs.cell_structural_change_fresh = False
            cs.cell_structural_change_kind = ""
            return

        changes = []
        if pci >= 0 and self._last_pci >= 0 and pci != self._last_pci:
            changes.append("pci")
        if earfcn > 0 and self._last_earfcn > 0 and earfcn != self._last_earfcn:
            changes.append("earfcn")
        if band > 0 and self._last_primary_band > 0 and band != self._last_primary_band:
            changes.append("band")
        # Save the old state for self-switch logging before updating _last_*.
        if changes:
            cs.cell_change_pci_before = self._last_pci
            cs.cell_change_earfcn_before = self._last_earfcn
            cs.cell_change_band_before = self._last_primary_band
            cs.cell_change_ca_sig_before = ""
            cls, service_impact = classify_cell_structural_change(
                band_changed="band" in changes,
                pci_changed="pci" in changes,
                earfcn_changed="earfcn" in changes,
                radio_connected=bool(modem.radio_connected),
                has_traffic=bool(getattr(modem, "has_traffic", False)),
                ca_active=bool(filtered_ca_active),
            )
            cs.cell_structural_change_class = cls
            cs.cell_structural_change_service_impact = bool(service_impact)
        else:
            cs.cell_change_pci_before = 0
            cs.cell_change_earfcn_before = 0
            cs.cell_change_band_before = 0
            cs.cell_change_ca_sig_before = ""
            cs.cell_structural_change_class = ""
            cs.cell_structural_change_service_impact = False

        self._last_pci = pci
        self._last_earfcn = earfcn
        self._last_primary_band = band
        self._last_structural_sample_sequence = sample_sequence
        self._last_structural_sample_ts = sample_ts

        fresh_change = bool(changes)
        if changes:
            self._structural_change_detected = True
            self._structural_change_kind = "+".join(changes)
            self._structural_change_samples = 3
        elif self._structural_change_samples > 0:
            self._structural_change_samples -= 1
            if self._structural_change_samples == 0:
                self._structural_change_detected = False
                self._structural_change_kind = ""

        cs.cell_structural_change = bool(self._structural_change_detected)
        cs.cell_structural_change_fresh = bool(fresh_change)
        cs.cell_structural_change_kind = str(self._structural_change_kind)

    # Input freeze detection

    def _detect_input_freeze(
        self,
        modem: SignalSamplePort,
        cs: ComputedSignals,
        *,
        filtered_ca_active: bool,
    ) -> None:
        """Track repeated identical modem-side register snapshots.

        Stable RF is normal on quantized modems. Treat a feed as frozen only
        when the live sample marker and progress fields repeat too.
        """
        band = int(
            getattr(modem, "serving_band", 0) or getattr(modem, "primary_band", 0) or 0
        )
        observed_pci = getattr(modem, "pci", None)
        if not bool(getattr(modem, "radio_connected", False)) or band == 0 or observed_pci is None:
            self._last_signal_fingerprint = ()
            self._input_freeze_samples = 0
            cs.input_freeze_samples = 0
            return
        pci = core_lte_pci(observed_pci)

        def _float_attr(name: str, default: float = 0.0) -> float:
            try:
                return float(getattr(modem, name, default) or default)
            except (TypeError, ValueError):
                return default

        def _int_attr(name: str, default: int = 0) -> int:
            try:
                return int(getattr(modem, name, default) or default)
            except (TypeError, ValueError):
                return default

        sample_ts_ms = int(round(max(0.0, _float_attr("timestamp")) * 1000.0))
        rx_kbps_bucket = int(round(max(0.0, _float_attr("rx_bytes_per_sec")) / 1024.0))
        tx_kbps_bucket = int(round(max(0.0, _float_attr("tx_bytes_per_sec")) / 1024.0))

        fingerprint = (
            sample_ts_ms,
            round(_float_attr("rsrp_dbm"), 1),
            round(_float_attr("sinr_db"), 1),
            round(_float_attr("rsrq_db"), 1),
            band,
            pci,
            _int_attr("earfcn"),
            _int_attr("cell_id"),
            bool(filtered_ca_active),
            str(getattr(modem, "ca_band_signature", "") or ""),
            int(_float_attr("session_uptime_s")),
            rx_kbps_bucket,
            tx_kbps_bucket,
            _int_attr("mcs_dl"),
            _int_attr("mcs_ul"),
            _int_attr("mcs_dl0"),
            _int_attr("mcs_dl1"),
            _int_attr("cqi"),
            _int_attr("cqi0"),
            _int_attr("cqi1"),
            _int_attr("ri"),
        )

        if fingerprint == self._last_signal_fingerprint:
            self._input_freeze_samples += 1
        else:
            self._input_freeze_samples = 0
            self._last_signal_fingerprint = fingerprint

        cs.input_freeze_samples = self._input_freeze_samples

    # CQ and congestion processing

    @staticmethod
    def _clamp01(x: float) -> float:
        return max(0.0, min(1.0, float(x)))

    @staticmethod
    def _median(values: list[float]) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        n = len(s)
        m = n // 2
        if n % 2 == 1:
            return float(s[m])
        return float((s[m - 1] + s[m]) / 2.0)

    def _congestion_penalty_band_from_pct(self, pct: float) -> int:
        pct = max(0.0, min(100.0, float(pct)))
        if self._cq_congestion_band is None:
            if pct <= 50.0:
                self._cq_congestion_band = 0
            elif pct <= 60.0:
                self._cq_congestion_band = 1
            elif pct <= 70.0:
                self._cq_congestion_band = 2
            elif pct <= 80.0:
                self._cq_congestion_band = 3
            elif pct <= 90.0:
                self._cq_congestion_band = 4
            else:
                self._cq_congestion_band = 5
            return int(self._cq_congestion_band)

        band = int(self._cq_congestion_band)
        while band < 5:
            boundary_up = 50.0 + (10.0 * float(band))
            if pct > (boundary_up + 5.0):
                band += 1
            else:
                break
        while band > 0:
            boundary_down = 50.0 + (10.0 * float(band - 1))
            if pct < (boundary_down - 5.0):
                band -= 1
            else:
                break
        self._cq_congestion_band = int(max(0, min(5, band)))
        return int(self._cq_congestion_band)

    def _update_v5_cq_and_congestion_qualifier(
        self, modem: SignalSamplePort, cs: ComputedSignals
    ) -> None:
        """Compute structural BQ and live CQ with congestion overlay.

        Structural BQ:
          SINR + RSRP + RI + CA + BW

        Live CQ:
          smoothed_BQ * (1 - congestion penalty)

        Congestion penalty bands:
          <=50 -> 0%
          51-60 -> 10%
          61-70 -> 20%
          71-80 -> 30%
          81-90 -> 40%
          91-100 -> 50%
        with +/-5% hysteresis at each threshold.
        """
        model = require_quality_model(modem.quality_model)
        if self._quality_model and model not in {self._quality_model, UNKNOWN_QUALITY_MODEL}:
            raise ValueError("signal_device_quality_class_changed_requires_session_reset")
        if model != UNKNOWN_QUALITY_MODEL:
            self._quality_model = model
        cs.quality_model = model
        if model != LTE_QUALITY_MODEL:
            self._update_combined_quality(modem, cs)
            return
        cq_min_hold = max(1, int(self.signal_cfg.v5_cq_hold_last_min_samples))
        min_samples = max(1, int(self.signal_cfg.v5_cq_min_samples))

        ca_bq_value, _ca_quality_reason = self._ca_telemetry.ca_quality_value(
            radio_connected=bool(getattr(modem, "radio_connected", False)),
            cart=int(getattr(cs, "cart", 0) or 0),
            now_ts=time.time(),
            rx_bps=float(getattr(modem, "rx_bytes_per_sec", 0.0) or 0.0),
            tx_bps=float(getattr(modem, "tx_bytes_per_sec", 0.0) or 0.0),
        )
        signal_values = {
            "sinr_db": float(getattr(modem, "sinr_db", 0.0) or 0.0),
            "rsrp_db": float(getattr(modem, "rsrp_dbm", 0.0) or 0.0),
            "ri": int(getattr(modem, "ri", 0) or 0),
            "ca_value": ca_bq_value,
            "supports_ca": bool(
                getattr(modem, "matrix_ca_metric_supported", False)
            ),
        }
        bq_info = score_band_quality(
            **signal_values,
            bw_mhz=float(getattr(modem, "primary_bw_mhz", 0.0) or 0.0),
            supports_bw=bool(
                getattr(modem, "matrix_bandwidth_supported", False)
            ),
        )
        cqb_info = score_band_quality(
            **signal_values,
            supports_bw=False,
        )

        fresh_bq_sample = bool(
            bool(getattr(modem, "radio_connected", False))
            and int(bq_info.get("metric_count", 0) or 0) >= 2
            and float(bq_info.get("quality_score", 0.0) or 0.0) > 0.0
        )
        if fresh_bq_sample:
            self._bq_scores.append(float(bq_info.get("quality_score", 0.0) or 0.0))
        fresh_cqb_sample = bool(
            bool(getattr(modem, "radio_connected", False))
            and int(cqb_info.get("metric_count", 0) or 0) >= 2
            and float(cqb_info.get("quality_score", 0.0) or 0.0) > 0.0
        )
        if fresh_cqb_sample:
            self._cqb_scores.append(
                float(cqb_info.get("quality_score", 0.0) or 0.0)
            )

        bq_valid = len(self._bq_scores)
        bq_window = float(max(1, self._bq_scores.maxlen or 1))
        bq_cov = float(bq_valid) / bq_window
        if bq_valid >= cq_min_hold:
            bq_cell = float(sum(self._bq_scores) / bq_valid)
            self._bq_last_value = bq_cell
            if fresh_bq_sample:
                self._bq_restored = False
                bq_source = "live"
            else:
                bq_source = "restored" if self._bq_restored else "hold"
            bq_valid_now = True
        elif self._bq_last_value is not None:
            bq_cell = float(self._bq_last_value)
            bq_source = "restored" if self._bq_restored else "hold"
            bq_valid_now = True
        else:
            bq_cell = 0.0
            bq_source = "warming"
            bq_valid_now = False

        raw_congestion_pct = canonical_congestion_pct(cs)
        self._cq_congestion_pcts.append(float(raw_congestion_pct))
        cong_valid = len(self._cq_congestion_pcts)
        cong_avg = (
            float(sum(self._cq_congestion_pcts) / cong_valid) if cong_valid > 0 else 0.0
        )
        penalty_band = self._congestion_penalty_band_from_pct(cong_avg)
        penalty = 0.10 * float(penalty_band)

        cqb_valid = len(self._cqb_scores)
        cqb_window = float(max(1, self._cqb_scores.maxlen or 1))
        cqb_cov = float(cqb_valid) / cqb_window
        if cqb_valid >= cq_min_hold:
            cqb_structural = float(sum(self._cqb_scores) / cqb_valid)
            self._cqb_last_structural = cqb_structural
            if fresh_cqb_sample:
                cqb_source = "live"
            else:
                cqb_source = "hold"
            cqb_valid_now = True
        elif self._cqb_last_structural is not None:
            cqb_structural = float(self._cqb_last_structural)
            cqb_source = "hold"
            cqb_valid_now = True
        else:
            cqb_structural = 0.0
            cqb_source = "warming"
            cqb_valid_now = False

        if not bool(getattr(modem, "radio_connected", False)):
            cqb_cell = 0.0
            cqb_source = "live"
            cqb_valid_now = True
        elif cqb_valid_now:
            cqb_cell = self._clamp01(cqb_structural * (1.0 - penalty))
        else:
            cqb_cell = 0.0

        if not bool(getattr(modem, "radio_connected", False)):
            cq_cell = 0.0
            cq_source = "live"
            cq_valid = True
        elif bq_valid_now:
            cq_cell = self._clamp01(float(bq_cell) * (1.0 - penalty))
            self._cq_last_value = cq_cell
            cq_source = str(bq_source)
            cq_valid = True
        elif self._cq_last_value is not None:
            cq_cell = float(self._cq_last_value)
            cq_source = "restored" if self._cq_restored else "hold"
            cq_valid = True
        else:
            cq_cell = 0.0
            cq_source = "warming"
            cq_valid = False

        cs.bq_cell = float(bq_cell)
        cs.bq_cell_valid = bool(bq_valid_now)
        cs.bq_cell_source = str(bq_source)

        cs.cq_cell = float(cq_cell)
        cs.cq_cell_source = str(cq_source)
        cs.cq_coverage = float(bq_cov)
        cs.cq_vector_a_valid = bool(bq_valid >= min_samples)
        cs.cq_cell_display = float(cq_cell)
        cs.cq_cell_display_valid = bool(cq_valid)
        cs.cq_cell_display_held = cq_source in {"hold", "restored"}

        cs.cqb_cell = float(cqb_cell)
        cs.cqb_cell_valid = bool(cqb_valid >= min_samples)
        cs.cqb_cell_source = str(cqb_source)
        cs.cqb_coverage = float(cqb_cov)

        cs.cq_cqi_source = "structural_bq"

        # Congestion comes from the RTT delta and RSRQ gap path below. Keep the
        # old field names because saved checkpoints and consumers use them.

    def _update_combined_quality(self, modem: SignalSamplePort, cs: ComputedSignals) -> None:
        inputs = copy.deepcopy(modem.quality_radio_inputs)
        ca_value = None
        ca_reason = "ca_observation_unavailable"
        if inputs.get("network_type") == "NR SA (5G)" and inputs.get("nr_active") is True:
            ca_value, ca_reason = 0.0, "lte_ca_not_applicable_nr_sa"
        elif type(inputs.get("ca_active")) is bool:
            rx = getattr(modem, "rx_bytes_per_sec", None)
            tx = getattr(modem, "tx_bytes_per_sec", None)
            if (getattr(modem, "traffic_valid", True) is True
                    and all(type(rate) in (int, float) and math.isfinite(rate)
                            and rate >= 0 for rate in (rx, tx))):
                ca_value, ca_reason = self._ca_telemetry.ca_quality_value(
                    radio_connected=inputs.get("connected") is True,
                    cart=cs.cart,
                    now_ts=time.time(),
                    rx_bps=rx,
                    tx_bps=tx,
                )
            else:
                ca_reason = "ca_traffic_unavailable"
        inputs["formula_revision"] = FORMULA_REVISION
        inputs["ca_quality_value"] = ca_value
        inputs["ca_quality_reason"] = ca_reason
        cs.quality_radio_inputs = inputs
        result = score_combined_quality(inputs, ca_quality_value=ca_value) if cs.quality_model == NR_QUALITY_MODEL else dict(
            valid=False, reason="device_quality_class_unavailable")
        cs.quality_reason = result["reason"]
        # A missing sample cannot silently become a last-known healthy learning value.
        if not result["valid"]:
            cs.bq_cell_source = cs.cq_cell_source = cs.cqb_cell_source = "unavailable"
            return
        key = (inputs.get("network_type"), inputs.get("nr_active"), result["total_bw_mhz"])
        if key != self._combined_context:
            self._combined_context = key
            self._bq_scores.clear()
            self._cqb_scores.clear()
            self._combined_capacities.clear()
        self._bq_scores.append(result["bq"])
        self._cqb_scores.append(result["rf"])
        self._combined_capacities.append(result["capacity"])
        self._cq_congestion_pcts.append(float(canonical_congestion_pct(cs)))
        congestion = sum(self._cq_congestion_pcts) / len(self._cq_congestion_pcts)
        factor = 1.0 - .1 * self._congestion_penalty_band_from_pct(congestion)
        cs.bq_cell = sum(self._bq_scores)/len(self._bq_scores)
        cs.cq_cell = cs.bq_cell * factor
        cs.cqb_cell = sum(self._cqb_scores)/len(self._cqb_scores)*factor
        cs.bq_cell_valid = cs.cq_cell_display_valid = True
        cs.cq_cell_display = cs.cq_cell
        cs.bq_cell_source = cs.cq_cell_source = cs.cqb_cell_source = "live"
        cs.cq_coverage = cs.cqb_coverage = len(self._bq_scores)/max(1,self._bq_scores.maxlen)
        cs.cq_vector_a_valid = cs.cqb_cell_valid = len(self._bq_scores) >= max(1,int(self.signal_cfg.v5_cq_min_samples))
        cs.quality_capacity_fraction = sum(self._combined_capacities)/len(self._combined_capacities)
        cs.quality_total_bw_mhz = result["total_bw_mhz"]
        cs.quality_valid = cs.cqb_cell_valid

    def _apply_canonical_congestion_projection(
        self, modem: SignalSamplePort, cs: ComputedSignals
    ) -> None:
        """Copy canonical congestion into the old fields used by saved data and GUI
        readers. Values must come from the current congestion path; MCS must not become
        another congestion owner.
        """
        pct = canonical_congestion_pct(cs)
        pct01 = self._clamp01(pct / 100.0)
        _, high_pressure, moderate_pressure = authority_congestion_flags(cs)
        rtt_valid, rtt_delta_ms = canonical_rtt_delta(cs)

        # Keep a quiet, baseline-aware RTT view for existing consumers.
        quiet_avg = quiet_rtt_avg(cs)
        if quiet_avg > 0.0:
            cs.rtt_congestion_avg = float(quiet_avg)
            cs.rtt_recent_median = float(quiet_avg)
        cs.rtt_pressure_level = congestion_rtt_level(
            cs, baseline_level=cs.rtt_pressure_level
        )

        # These aliases mirror the canonical congestion severity.
        cs.congestion_qam_gap = float(pct01)
        cs.congestion_qualifier_high = bool(high_pressure)
        cs.congestion_qualifier_mod = bool(moderate_pressure)

    def _cfg_jitter_formula_status(self) -> dict[str, float]:
        return {
            "pct_scale_ms": float(
                getattr(self.cfg, "jitter_formula_pct_scale_ms", 5.0) or 5.0
            ),
            "visible_scale": float(
                getattr(self.cfg, "jitter_formula_visible_scale", 3.0) or 3.0
            ),
            "trim_frac": float(
                getattr(self.cfg, "jitter_formula_trim_frac", 0.10) or 0.10
            ),
            "low_frac": float(
                getattr(self.cfg, "jitter_formula_low_frac", 0.20) or 0.20
            ),
        }

    def get_dl_jitter_formula_status(self) -> dict[str, Any]:
        return self._dl_jitter.formula_status()
