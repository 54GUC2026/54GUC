"""
Sliding-window anchor flip instability detector.

This detector observes repeated modem-initiated serving anchor moves. It does
not choose a band and does not blacklist anything; it only publishes an
advisory signal for the Brain/TSAR candidate lane.
"""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Mapping

from arch4.interfaces import SystemState


@dataclass
class AnchorFlipMonitor:
    window_s: float = 600.0
    command_collision_window_s: float = 300.0
    active_flips: int = 5
    command_collision_active_flips: int = 3
    clear_s: float = 240.0
    _flips: Deque[Dict[str, Any]] = field(default_factory=deque)
    _active: bool = False
    _active_since: float = 0.0
    _clear_since: float = 0.0
    _provider_plmn: str = ""
    _provider_name: str = ""
    _last_status: Dict[str, Any] = field(default_factory=dict)
    _samples: Deque[Dict[str, Any]] = field(default_factory=deque)

    def set_provider_context(
        self,
        provider_plmn: str = "",
        provider_name: str = "",
        *,
        force: bool = False,
    ) -> None:
        plmn = str(provider_plmn or "").strip()
        name = str(provider_name or "").strip()
        if not plmn and not name:
            return
        if force or not self._provider_plmn:
            self._provider_plmn = plmn or self._provider_plmn
        if force or not self._provider_name:
            self._provider_name = name or self._provider_name

    def observe(
        self,
        *,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        state: SystemState,
        now: float | None = None,
        ca_recovery_trend: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        ts = time.time() if now is None else float(now)
        provider_observed = str(getattr(modem.identity, 'provider_plmn', "") or "").strip()
        provider_name_observed = str(getattr(modem.identity, 'provider_name', "") or "").strip()
        if provider_observed or provider_name_observed:
            self.set_provider_context(provider_observed, provider_name_observed)
        provider = self._provider_plmn or provider_observed
        provider_name = self._provider_name or provider_name_observed

        transition = modem.radio_transition
        fresh_change = bool(transition.fresh and transition.structural_fresh)
        service_impact = bool(transition.service_impact)
        change_class = str(transition.change_class or "").strip()
        change_kind = str(transition.change_kind or "").strip()
        current_band = int(transition.after.primary_band or 0)
        previous_band = int(transition.before.primary_band or 0)
        command_collision_context = bool(
            transition.source == "lower_controller_during_54guc_command"
        )
        radio_connected = bool(getattr(modem.cellular, 'radio_connected', False))
        self._record_sample(ts=ts, modem=modem, computed=computed, radio_connected=radio_connected)

        excluded = (
            not radio_connected
            or not bool(transition.attribution_available)
            or not bool(transition.lower_controller_learning_opportunity)
        )
        should_count = (
            fresh_change
            and service_impact
            and change_class in {"anchor_move", "service_loss_shift"}
            and current_band > 0
            and previous_band > 0
            and current_band != previous_band
            and not excluded
        )
        if should_count:
            self._flips.append(
                {
                    "ts": ts,
                    "from_band": previous_band,
                    "to_band": current_band,
                    "pci_before": int(getattr(computed.quality, 'cell_change_pci_before', 0) or 0),
                    "pci": int(getattr(modem.cellular, 'pci', 0) or 0),
                    "earfcn_before": int(getattr(computed.quality, 'cell_change_earfcn_before', 0) or 0),
                    "earfcn": int(getattr(modem.cellular, 'earfcn_dl', 0) or 0),
                    "ca_sig_before": str(getattr(computed.quality, 'cell_change_ca_sig_before', "") or ""),
                    "ca_sig": str(getattr(modem.cellular, 'ca_band_signature', "") or ""),
                    "command_collision_context": command_collision_context,
                    "change_kind": change_kind,
                    "source": str(transition.source or ""),
                    "transition_id": str(transition.transition_id or ""),
                }
            )

        cutoff = ts - float(self.window_s)
        while self._flips and float(self._flips[0].get("ts", 0.0) or 0.0) < cutoff:
            self._flips.popleft()

        post_cutoff = ts - float(self.command_collision_window_s)
        command_collision_flips = [
            flip
            for flip in self._flips
            if bool(flip.get("command_collision_context"))
            and float(flip.get("ts", 0.0) or 0.0) >= post_cutoff
        ]
        active_now = (
            len(self._flips) >= int(self.active_flips)
            or len(command_collision_flips) >= int(self.command_collision_active_flips)
        )
        if active_now and not self._active:
            self._active_since = ts
        self._active = bool(active_now or self._active)

        if len(self._flips) == 0:
            if self._clear_since <= 0.0:
                self._clear_since = ts
            elif ts - self._clear_since >= float(self.clear_s):
                self._active = False
                self._active_since = 0.0
        else:
            self._clear_since = 0.0

        return self._status(
            ts=ts,
            provider=provider,
            provider_name=provider_name,
            current_band=current_band,
            excluded=excluded,
            exclude_reason=(
                "disconnected"
                if not radio_connected
                else "attribution_unavailable"
                if not bool(transition.attribution_available)
                else "not_lower_controller_selection"
                if not bool(transition.lower_controller_learning_opportunity)
                else ""
            ),
            command_collision_flips=len(command_collision_flips),
            selection_source=str(transition.source or ""),
            attribution_available=bool(transition.attribution_available),
            attribution_error=str(transition.attribution_error or ""),
            modem=modem,
            computed=computed,
            ca_recovery_trend=ca_recovery_trend,
        )

    def status(self) -> Dict[str, Any]:
        return dict(self._last_status)

    def _status(
        self,
        *,
        ts: float,
        provider: str,
        provider_name: str,
        current_band: int,
        excluded: bool,
        exclude_reason: str,
        command_collision_flips: int,
        selection_source: str,
        attribution_available: bool,
        attribution_error: str,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        ca_recovery_trend: Mapping[str, Any] | None,
    ) -> Dict[str, Any]:
        involved_bands = sorted(
            {
                int(band)
                for flip in self._flips
                for band in (flip.get("from_band"), flip.get("to_band"))
                if int(band or 0) > 0
            }
        )
        last_flip = dict(self._flips[-1]) if self._flips else {}
        hour = int(time.localtime(ts).tm_hour)
        behavior_grade = self._build_host_behavior_grade(
            ts=ts,
            provider=provider,
            provider_name=provider_name,
            current_band=current_band,
            involved_bands=involved_bands,
            last_flip=last_flip,
            command_collision_flips=command_collision_flips,
            selection_source=selection_source,
            attribution_available=attribution_available,
            attribution_error=attribution_error,
            excluded=excluded,
            exclude_reason=exclude_reason,
            modem=modem,
            computed=computed,
            ca_recovery_trend=ca_recovery_trend,
        )
        recovery_allowed = bool(behavior_grade.get("recovery_allowed", False))
        hold_reason = str(behavior_grade.get("hold_reason", "") or "")
        reason = ""
        if self._active:
            reason = (
                f"provider={provider_name or provider or '-'} hour={hour:02d} "
                f"anchor_flip_instability flips={len(self._flips)} "
                f"bands={','.join('B' + str(b) for b in involved_bands)}"
            )
        self._last_status = {
            "schema": "anchor_flip_instability_v2",
            "ts": float(ts),
            "window_s": float(self.window_s),
            "command_collision_window_s": float(self.command_collision_window_s),
            "flip_count": int(len(self._flips)),
            "command_collision_flip_count": int(command_collision_flips),
            "candidate": bool(
                recovery_allowed
                or len(self._flips) >= int(self.active_flips) - 1
                or command_collision_flips >= int(self.command_collision_active_flips) - 1
            ),
            "recovery_allowed": recovery_allowed,
            "hold_reason": hold_reason,
            "host_behavior_grade": behavior_grade,
            "active": bool(self._active),
            "active_since": float(self._active_since or 0.0),
            "current_band": int(current_band),
            "involved_bands": involved_bands,
            "last_flip": last_flip,
            "excluded": bool(excluded),
            "exclude_reason": str(exclude_reason or ""),
            "context": {
                "provider_plmn": str(provider or ""),
                "provider_name": str(provider_name or ""),
                "hour": hour,
                "pband": int(current_band),
                "involved_bands": involved_bands,
                "command_collision_context": bool(command_collision_flips > 0),
                "selection_source": str(selection_source or ""),
                "attribution_available": bool(attribution_available),
                "attribution_error": str(attribution_error or ""),
            },
            "reason": reason,
        }
        return dict(self._last_status)

    def _record_sample(
        self,
        *,
        ts: float,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        radio_connected: bool,
    ) -> None:
        self._samples.append(
            {
                "ts": float(ts),
                "radio_connected": bool(radio_connected),
                "band": int(getattr(modem.cellular, 'primary_band', 0) or 0),
                "ca_active": bool(getattr(modem.cellular, 'ca_active', False)),
                "ca_sig": str(getattr(modem.cellular, 'ca_band_signature', "") or ""),
                "sinr_db": self._float(getattr(modem.cellular, 'sinr_db', 0.0)),
                "rsrq_db": self._float(getattr(modem.cellular, 'rsrq_db', 0.0)),
                "rtt_ms": self._float(getattr(computed.latency, 'rtt_sliding_avg', getattr(computed.latency, 'rtt_ms', 0.0))),
                "congestion_pct": self._float(getattr(computed.quality, 'congestion_pct', 0.0)),
            }
        )
        cutoff = float(ts) - float(self.window_s)
        while self._samples and float(self._samples[0].get("ts", 0.0) or 0.0) < cutoff:
            self._samples.popleft()

    def _build_host_behavior_grade(
        self,
        *,
        ts: float,
        provider: str,
        provider_name: str,
        current_band: int,
        involved_bands: list[int],
        last_flip: Mapping[str, Any],
        command_collision_flips: int,
        selection_source: str,
        attribution_available: bool,
        attribution_error: str,
        excluded: bool,
        exclude_reason: str,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        ca_recovery_trend: Mapping[str, Any] | None,
    ) -> Dict[str, Any]:
        flip_count = int(len(self._flips))
        active_flip_threshold = max(1, int(self.active_flips))
        post_threshold = max(1, int(self.command_collision_active_flips))
        flip_pressure = min(1.0, float(flip_count) / float(active_flip_threshold))
        post_pressure = min(1.0, float(command_collision_flips) / float(post_threshold))
        current_truth = {
            "radio_connected": bool(getattr(modem.cellular, 'radio_connected', False)),
            "current_band": int(current_band),
            "ca_active": bool(getattr(modem.cellular, 'ca_active', False)),
            "ca_signature": str(getattr(modem.cellular, 'ca_band_signature', "") or ""),
            "sinr_db": round(self._float(getattr(modem.cellular, 'sinr_db', 0.0)), 2),
            "rsrq_db": round(self._float(getattr(modem.cellular, 'rsrq_db', 0.0)), 2),
            "rtt_ms": round(self._float(getattr(computed.latency, 'rtt_sliding_avg', getattr(computed.latency, 'rtt_ms', 0.0))), 1),
            "congestion_pct": round(self._float(getattr(computed.quality, 'congestion_pct', 0.0)), 1),
        }
        trend = self._trend_summary(ts=ts, computed=computed, ca_recovery_trend=ca_recovery_trend)
        provenance = {
            "selection_source": str(selection_source or ""),
            "attribution_available": bool(attribution_available),
            "attribution_error": str(attribution_error or ""),
            "command_collision_context": bool(command_collision_flips > 0),
            "excluded": bool(excluded),
            "exclude_reason": str(exclude_reason or ""),
            "last_flip_source": str(last_flip.get("source", "") or ""),
            "provider_plmn": str(provider or ""),
            "provider_name": str(provider_name or ""),
            "hour": int(time.localtime(ts).tm_hour),
        }
        outcome_memory = {
            "flip_count": flip_count,
            "active_flip_threshold": active_flip_threshold,
            "command_collision_flip_count": int(command_collision_flips),
            "command_collision_active_threshold": post_threshold,
            "involved_bands": list(involved_bands),
            "last_flip": dict(last_flip),
        }

        reasons: list[str] = []
        blockers: list[str] = []
        if flip_count >= active_flip_threshold:
            reasons.append("flip_pressure_high")
        if command_collision_flips >= post_threshold:
            reasons.append("command_collision_flip_pressure_high")
        if bool(trend.get("rf_load_degrading")):
            reasons.append("rf_or_load_degrading")
        if bool(trend.get("ca_worsening")):
            reasons.append("ca_metric_worsening")
        if bool(trend.get("ca_improving")):
            blockers.append("ca_metric_improving")
        if excluded:
            blockers.append(exclude_reason or "excluded")
        if not attribution_available:
            blockers.append("attribution_unavailable")

        recovery_allowed = bool(
            self._active
            and not blockers
            and (
                command_collision_flips >= post_threshold
                or bool(trend.get("rf_load_degrading"))
                or bool(trend.get("ca_worsening"))
                or flip_count >= active_flip_threshold + 2
            )
        )
        score = max(flip_pressure, post_pressure)
        if bool(trend.get("rf_load_degrading")):
            score += 0.15
        if bool(trend.get("ca_worsening")):
            score += 0.15
        if bool(trend.get("ca_improving")):
            score -= 0.35
        score = max(0.0, min(1.0, score))
        grade = "recover" if recovery_allowed else "observe" if self._active else "normal"
        hold_reason = ""
        if not recovery_allowed and blockers:
            hold_reason = ",".join(blockers)
        elif self._active and not recovery_allowed:
            hold_reason = "waiting_for_multidimensional_confirmation"
        return {
            "schema": "host_behavior_grade_v1",
            "grade": grade,
            "score": round(score, 3),
            "recovery_allowed": recovery_allowed,
            "hold_reason": hold_reason,
            "current_truth": current_truth,
            "trend": trend,
            "provenance": provenance,
            "outcome_memory": outcome_memory,
            "reasons": reasons,
            "blockers": blockers,
        }

    def _trend_summary(
        self,
        *,
        ts: float,
        computed: DerivedSignalFrames,
        ca_recovery_trend: Mapping[str, Any] | None,
    ) -> Dict[str, Any]:
        ca_trend = dict(ca_recovery_trend or {})
        sinr_10m, sinr_count_10m = self._avg_recent(ts, "sinr_db", 600.0)
        sinr_2m, sinr_count_2m = self._avg_recent(ts, "sinr_db", 120.0)
        rtt_10m, rtt_count_10m = self._avg_recent(ts, "rtt_ms", 600.0)
        rtt_2m, rtt_count_2m = self._avg_recent(ts, "rtt_ms", 120.0)
        cong_10m, cong_count_10m = self._avg_recent(ts, "congestion_pct", 600.0)
        cong_2m, cong_count_2m = self._avg_recent(ts, "congestion_pct", 120.0)
        sinr_delta = sinr_2m - sinr_10m if sinr_count_2m and sinr_count_10m else 0.0
        rtt_delta = rtt_2m - rtt_10m if rtt_count_2m and rtt_count_10m else 0.0
        congestion_delta = cong_2m - cong_10m if cong_count_2m and cong_count_10m else 0.0
        ca_improving = bool(ca_trend.get("improving") or ca_trend.get("hold_restore_ca"))
        ca_worsening = bool(ca_trend.get("worsening"))
        computed_degrading = bool(
            getattr(computed.quality, 'rsrq_degrading', False)
            or getattr(computed.quality, 'congestion_rising', False)
        )
        sampled_degrading = bool(
            (sinr_count_2m >= 2 and sinr_count_10m >= 3 and sinr_delta <= -2.0)
            or (rtt_count_2m >= 2 and rtt_count_10m >= 3 and rtt_delta >= 40.0)
            or (cong_count_2m >= 2 and cong_count_10m >= 3 and congestion_delta >= 15.0)
        )
        return {
            "ca": "improving" if ca_improving else "worsening" if ca_worsening else "unknown",
            "ca_improving": ca_improving,
            "ca_worsening": ca_worsening,
            "ca_recovery_trend": ca_trend,
            "rf_load": "degrading" if computed_degrading or sampled_degrading else "stable_or_unknown",
            "rf_load_degrading": bool(computed_degrading or sampled_degrading),
            "sinr_delta_2m_vs_10m_db": round(float(sinr_delta), 3),
            "rtt_delta_2m_vs_10m_ms": round(float(rtt_delta), 3),
            "congestion_delta_2m_vs_10m_pct": round(float(congestion_delta), 3),
            "computed_rsrq_degrading": bool(getattr(computed.quality, 'rsrq_degrading', False)),
            "computed_congestion_rising": bool(getattr(computed.quality, 'congestion_rising', False)),
            "sample_counts": {
                "sinr_10m": int(sinr_count_10m),
                "sinr_2m": int(sinr_count_2m),
                "rtt_10m": int(rtt_count_10m),
                "rtt_2m": int(rtt_count_2m),
                "congestion_10m": int(cong_count_10m),
                "congestion_2m": int(cong_count_2m),
            },
        }

    def _avg_recent(self, ts: float, key: str, window_s: float) -> tuple[float, int]:
        values = [
            self._float(sample.get(key))
            for sample in self._samples
            if float(ts) - float(sample.get("ts", 0.0) or 0.0) <= float(window_s)
            and bool(sample.get("radio_connected", False))
        ]
        if not values:
            return 0.0, 0
        return sum(values) / float(len(values)), len(values)

    @staticmethod
    def _float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None or value == "":
                return default
            return float(value)
        except (TypeError, ValueError):
            return default
