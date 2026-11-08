"""
Brain-owned runtime incident tracker.

This module is intentionally narrow:
- detect only hard runtime incidents
- track incident episode start / resolve timing
- emit a small compatibility payload for runtime consumers

It does not try to diagnose soft causes or invent parallel congestion truth.
"""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import logging
import time
from collections import deque
from typing import Any, Dict, List, Optional

from arch4.coerce import safe_bool
from arch4.interfaces import (
    Arch4Config,
    LeverType,
PolicyProfile,
    SystemState,
)
from arch4.brain.memory import Memory
from arch4.brain.memory_action_history_owner import iter_action_history_reversed
from arch4.brain.incident_rca import build_runtime_incident_root_cause
from arch4.brain.outcome_profile import has_ca_capacity, is_bad_rf_capacity_winner
from arch4.signal_processing.policy_thresholds import policy_threshold_authority
from arch4.brain.rf_truth import assess_rf_truth
from arch4.brain.ca_contract import cart_ld_active, cart_ld_valid

log = logging.getLogger("arch4.brain.runtime_incident")


RECOVERY_INTENTS = {
    "restore_ca",
    "degraded_recovery",
    "congested_recovery",
    "emergency_recovery",
    "emergency_restart",
    "emergency_fast_restart",
    "emergency_radio_down_recover",
    "mikrotik_emergency_recover",
}
MUTATING_LEVERS = {
    LeverType.BAND_SELECTION,
    LeverType.BAND_LOCK,
    LeverType.CELL_LOCK,
    LeverType.RESTART,
    LeverType.PBAND,
}
NON_PHYSICAL_ACTION_EXECUTION_STATUSES = {
    "lifecycle_event",
    "staged_roi",
}
NON_PHYSICAL_OUTCOME_STAGES = {
    "staged_roi",
}


class RuntimeIncidentTracker:
    def __init__(
        self,
        cfg: Arch4Config,
        memory: Memory,
        historical: Any | None = None,
    ):
        del historical
        self.cfg = cfg
        self.memory = memory
        self._hard = policy_threshold_authority(cfg).get("hard", {})
        self._runtime_events: List[Dict[str, Any]] = []
        self._episode_seq: int = 0
        self._active_incident: Optional[Dict[str, Any]] = None
        self._last_assessment: Dict[str, Any] = {}
        self._sinr_window: deque[tuple[float, float]] = deque(maxlen=10_000)
        self._sinr_history: deque[float] = deque(maxlen=4)
        self._rf_spike_threshold_db: float = 8.0
        self._rf_spike_active: bool = False
        self._rf_spike_recovery_ticks: int = 0
        self._ca_loss_resolve_hold_s: float = max(
            30.0,
            float(getattr(cfg, "sample_interval_s", 2.0) or 2.0) * 6.0,
        )
        self._incident_resolve_hold_s: float = max(
            30.0,
            float(getattr(cfg, "sample_interval_s", 2.0) or 2.0) * 6.0,
        )
        self._ca_loss_operator_min_s: float = max(
            30.0,
            float(getattr(cfg, "ca_lost_operator_min_s", 120.0) or 120.0),
        )
        self._ca_loss_candidate: Dict[str, Any] = {}
        self._data_path_probe_candidate: Dict[str, Any] = {}
        self._data_path_probe_confirm_s: float = max(
            20.0,
            float(getattr(cfg, "data_path_probe_confirm_s", 20.0) or 20.0),
        )
        self._soft_observation: Dict[str, Any] = {}
        self._soft_observation_summary_s: float = max(
            120.0,
            float(getattr(cfg, "sample_interval_s", 2.0) or 2.0) * 60.0,
        )
        self._startup_bootstrap_incident_grace_started_at: float = 0.0
        self._startup_bootstrap_incident_grace_s: float = max(
            10.0,
            float(getattr(cfg, "startup_incident_grace_s", 30.0) or 30.0),
        )

    def refresh_thresholds(self) -> None:
        self._hard = policy_threshold_authority(self.cfg).get("hard", {})

    def note_calibration_handoff(self, handoff: Optional[Dict[str, Any]]) -> None:
        del handoff

    def arm_startup_bootstrap_grace(self, *, now: Optional[float] = None) -> None:
        self._startup_bootstrap_incident_grace_started_at = float(time.time() if now is None else now)

    def clear_startup_bootstrap_grace(self) -> None:
        self._startup_bootstrap_incident_grace_started_at = 0.0

    def _startup_bootstrap_grace_status(self, *, now: float) -> Dict[str, Any]:
        started = float(self._startup_bootstrap_incident_grace_started_at or 0.0)
        if started <= 0.0:
            return {"active": False, "remaining_s": 0.0, "age_s": 0.0}
        age_s = max(0.0, float(now) - started)
        remaining_s = max(0.0, float(self._startup_bootstrap_incident_grace_s) - age_s)
        active = bool(remaining_s > 0.0)
        if not active:
            self._startup_bootstrap_incident_grace_started_at = 0.0
        return {"active": active, "remaining_s": remaining_s, "age_s": age_s}

    def get_last_assessment(self) -> Dict[str, Any]:
        return dict(self._last_assessment or {})

    def drain_runtime_events(self) -> List[Dict[str, Any]]:
        out = list(self._runtime_events)
        self._runtime_events.clear()
        return out

    def note_connection_lost(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        ts = float(now or time.time())
        assessment = self._incident_result(
            incident_class="radio_disconnected",
            incident_subtype="radio_down",
            severity="critical",
            confidence=0.98,
            self_induced=False,
            suppress_exploration=True,
            recovery_candidate=True,
            recommended_next_step="recover_radio",
            recommended_next_step_reason="radio is not connected",
            health_stage="connection_lost",
            health_action_hint="wait_reconnect",
        )
        bootstrap = self._startup_bootstrap_grace_status(now=ts)
        if bool(bootstrap.get("active", False)):
            assessment = self._incident_result(
                incident_class="startup_bootstrap_observe",
                incident_subtype="radio_down_bootstrap_grace",
                severity="moderate",
                confidence=0.72,
                suppress_exploration=False,
                recovery_candidate=False,
                recommended_next_step="observe_bootstrap",
                recommended_next_step_reason="startup bootstrap grace is active; waiting for modem identity/path to settle",
                health_stage="warning",
                health_action_hint="observe",
            )
            assessment["startup_bootstrap_grace_active"] = True
            assessment["startup_bootstrap_grace_remaining_s"] = float(bootstrap.get("remaining_s", 0.0) or 0.0)
        return self._finalize_assessment(assessment=assessment, now=ts)

    def assess(
        self,
        *,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        state: Optional[SystemState],
        controller_posture: Any = None,
        health_status: Optional[Dict[str, Any]] = None,
        policy_profile: Optional[PolicyProfile] = None,
        now: Optional[float] = None,
        ca_lost_filtered: Optional[bool] = None,
        radio_mutation_window: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        del policy_profile
        ts = float(now or time.time())
        rf_spike = self._detect_rf_spike(
            sinr_db=float(modem.cellular.sinr_db),
            rsrp_dbm=float(modem.cellular.rsrp_dbm),
            radio_connected=bool(modem.cellular.radio_connected),
        )
        live = self._live_snapshot(
            modem=modem,
            computed=computed,
            state=state,
            controller_posture=controller_posture,
            health_status=health_status,
            now=ts,
            ca_lost_filtered=ca_lost_filtered,
        )
        live["rf_spike"] = rf_spike
        if isinstance(radio_mutation_window, dict) and safe_bool(radio_mutation_window.get("active", False)):
            live["radio_mutation_window_active"] = True
            live["radio_mutation_window_reason"] = str(radio_mutation_window.get("reason", "") or "")
            live["radio_mutation_window_reasons"] = list(radio_mutation_window.get("reasons") or [])
            live["radio_mutation_window_remaining_s"] = float(
                radio_mutation_window.get("remaining_s", 0.0) or 0.0
            )
            live["radio_mutation_workflow_id"] = str(radio_mutation_window.get("workflow_id", "") or "")
            live["radio_mutation_workflow_active"] = safe_bool(radio_mutation_window.get("workflow_active", False))
            live["radio_mutation_sequence_step"] = int(radio_mutation_window.get("sequence_step", 0) or 0)
            live["radio_mutation_sequence_total"] = int(radio_mutation_window.get("sequence_total", 0) or 0)
        stm = self._stm_snapshot(now=ts)
        live["startup_bootstrap_grace"] = self._startup_bootstrap_grace_status(now=ts)
        assessment = self._classify(live=live, stm=stm)
        return self._finalize_assessment(assessment=assessment, now=ts, live=live)

    def _finalize_assessment(
        self,
        *,
        assessment: Dict[str, Any],
        now: float,
        live: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        incident_class = str(assessment.get("incident_class", "healthy") or "healthy")
        assessment["root_cause"] = build_runtime_incident_root_cause(
            incident_class=incident_class,
            live=live,
            assessment=assessment,
        )
        assessment["brain_health"] = self._brain_health_broadcast(assessment, live=live)
        episode_id, incident_age_s = self._update_episode(assessment=assessment, now=now)
        assessment["episode_id"] = episode_id
        assessment["incident_age_s"] = float(incident_age_s)
        self._last_assessment = dict(assessment)
        return dict(assessment)

    def _brain_health_broadcast(
        self,
        assessment: Dict[str, Any],
        *,
        live: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Brain-approved public health message.

        Producers may report symptoms, but this broadcast is the single
        user-facing diagnosis contract.  Low-confidence or low-severity
        observations deliberately stay as "investigating/observing" instead
        of naming a hard cause.
        """
        live_map = live if isinstance(live, dict) else {}
        incident_class = str(assessment.get("incident_class", "healthy") or "healthy")
        severity = str(assessment.get("severity", "none") or "none").lower()
        confidence = max(0.0, min(1.0, float(assessment.get("confidence", 0.0) or 0.0)))
        next_step = str(assessment.get("recommended_next_step", "none") or "none")
        reason = str(assessment.get("recommended_next_step_reason", "") or "")
        cause = str((assessment.get("root_cause") or {}).get("probable_cause", "") or "")
        operator_visible = bool(
            incident_class != "healthy"
            and (
                severity in {"high", "critical"}
                or bool(assessment.get("suppress_exploration", False))
                or bool(assessment.get("recovery_candidate", False))
                or bool(assessment.get("self_induced", False))
            )
        )
        status = "healthy"
        if incident_class != "healthy":
            status = "confirmed" if operator_visible and confidence >= 0.80 else "investigating"

        if incident_class == "healthy":
            message = "Health normal. Brain has no confirmed incident."
        elif status == "investigating":
            message = "Brain sees unusual symptoms and is collecting evidence before naming the cause."
        elif incident_class == "startup_bootstrap_observe":
            message = "Startup bootstrap is settling. Brain is observing before naming a service incident."
        elif incident_class == "radio_disconnected":
            message = "Radio contact is down. Brain confirmed a connection incident and is holding normal actions."
        elif incident_class == "rf_collapse":
            message = "RF collapse confirmed. Brain is in protected recovery and will avoid normal exploration."
        elif incident_class == "rf_event_spike":
            message = "Sudden RF event confirmed. Brain is holding changes while the signal stabilizes."
        elif incident_class == "data_path_degraded":
            message = "Data path problem confirmed while modem RF still looks usable. Brain is holding radio changes."
        elif incident_class == "radio_ca_loss":
            message = "Carrier aggregation loss confirmed under load. Brain is using controlled recovery."
        elif incident_class == "post_action_instability":
            message = "Recent modem action caused instability. Brain is backing off while the link settles."
        else:
            message = "Brain confirmed a runtime incident and is selecting the safest next step."

        evidence = {
            "state": str(live_map.get("state", "") or ""),
            "health_stage": str(live_map.get("health_stage", "") or ""),
            "radio_connected": bool(live_map.get("radio_connected", False)),
            "cart_ld": int(live_map.get("cart_ld", 0) or 0),
            "cart_ld_valid": bool(live_map.get("cart_ld_valid", False)),
            "ca_signature": str(live_map.get("ca_signature", "") or ""),
            "sinr_db": float(live_map.get("sinr_db", 0.0) or 0.0),
            "rsrp_dbm": float(live_map.get("rsrp_dbm", 0.0) or 0.0),
            "rsrq_db": float(live_map.get("rsrq_db", 0.0) or 0.0),
            "rtt_ms": float(live_map.get("rtt_ms", 0.0) or 0.0),
            "congestion_pct": float(live_map.get("congestion_pct", 0.0) or 0.0),
            "rf_truth_verdict": str(live_map.get("rf_truth_verdict", "") or ""),
            "rf_truth_reason": str(live_map.get("rf_truth_reason", "") or ""),
        }
        return {
            "source": "brain_health",
            "status": status,
            "incident_class": incident_class,
            "severity": severity,
            "confidence": confidence,
            "operator_visible": operator_visible,
            "user_message": message,
            "cause": cause,
            "recommended_next_step": next_step,
            "recommended_next_step_reason": reason,
            "evidence": evidence,
        }

    def _incident_result(
        self,
        *,
        incident_class: str = "healthy",
        incident_subtype: str = "none",
        severity: str = "none",
        confidence: float = 1.0,
        self_induced: bool = False,
        suppress_exploration: bool = False,
        recovery_candidate: bool = False,
        recommended_next_step: str = "none",
        recommended_next_step_reason: str = "no incident",
        health_stage: str = "healthy",
        health_action_hint: str = "none",
    ) -> Dict[str, Any]:
        return {
            "incident_class": str(incident_class or "healthy"),
            "incident_subtype": str(incident_subtype or "none"),
            "severity": str(severity or "none"),
            "confidence": float(confidence),
            "self_induced": bool(self_induced),
            "suppress_exploration": bool(suppress_exploration),
            "recovery_candidate": bool(recovery_candidate),
            "recommended_next_step": str(recommended_next_step or "none"),
            "recommended_next_step_reason": str(recommended_next_step_reason or "no incident"),
            "health_stage": str(health_stage or "healthy"),
            "health_action_hint": str(health_action_hint or "none"),
        }

    def _resolve_active_incident(self, *, now: float) -> None:
        if not self._active_incident:
            return
        prev = dict(self._active_incident)
        self._runtime_events.append(
            {
                "kind": "incident_resolved",
                "episode_id": str(prev.get("episode_id", "") or ""),
                "incident_class": str(prev.get("incident_class", "") or ""),
                "resolved_after_s": max(0.0, now - float(prev.get("started_at", now) or now)),
            }
        )
        self._active_incident = None

    def _start_incident_episode(
        self,
        *,
        now: float,
        incident_class: str,
        next_step: str,
        severity: str,
        root_cause: Dict[str, Any],
        episode_key: str,
    ) -> None:
        self._episode_seq += 1
        self._active_incident = {
            "episode_id": f"inc_{self._episode_seq}",
            "started_at": now,
            "incident_class": incident_class,
            "recommended_next_step": next_step,
            "severity": severity,
            "episode_key": episode_key,
            "root_cause": dict(root_cause or {}),
        }
        self._runtime_events.append(
            {
                "kind": "incident_started",
                "episode_id": self._active_incident["episode_id"],
                "incident_class": incident_class,
                "severity": severity,
                "recommended_next_step": next_step,
                "root_cause": dict(root_cause or {}),
            }
        )

    @staticmethod
    def _ca_loss_context_key(root_cause: Dict[str, Any]) -> str:
        parts = [
            str(root_cause.get("primary_band", "") or ""),
            str(root_cause.get("selected_band_set_signature", "") or ""),
            str(root_cause.get("active_band_set_signature", "") or ""),
            str(root_cause.get("ca_signature", "") or ""),
        ]
        return "|".join(parts)

    @staticmethod
    def _episode_context_key(incident_class: str, root_cause: Dict[str, Any]) -> str:
        """Return the durable context key used for incident lifecycle ownership."""
        cause = str(root_cause.get("probable_cause", "") or "")
        if incident_class in {"rf_collapse", "rf_event_spike", "radio_ca_loss"}:
            parts = [
                incident_class,
                cause,
                str(root_cause.get("primary_band", "") or ""),
                str(root_cause.get("selected_band_set_signature", "") or ""),
                str(root_cause.get("active_band_set_signature", "") or ""),
                str(root_cause.get("ca_signature", "") or ""),
            ]
            return "|".join(parts)
        if incident_class == "data_path_degraded":
            parts = [
                incident_class,
                cause,
                str(root_cause.get("connection_status_code", "") or ""),
                str(root_cause.get("traffic_invalid_reason", "") or ""),
            ]
            return "|".join(parts)
        return "|".join([incident_class, cause])

    @staticmethod
    def _is_operator_incident(assessment: Dict[str, Any]) -> bool:
        """Return True when an assessment deserves a user-facing incident episode.

        Low-value observations still stay visible in assessment/runtime detail,
        but they must not create start/resolve churn in the event stream.
        """
        incident_class = str(assessment.get("incident_class", "healthy") or "healthy")
        if incident_class == "healthy":
            return False
        severity = str(assessment.get("severity", "none") or "none").lower()
        return bool(
            severity in {"high", "critical"}
            or assessment.get("suppress_exploration", False)
            or assessment.get("recovery_candidate", False)
            or assessment.get("self_induced", False)
        )

    def _record_soft_observation(
        self,
        *,
        assessment: Dict[str, Any],
        now: float,
        root_cause: Dict[str, Any],
    ) -> Dict[str, Any]:
        incident_class = str(assessment.get("incident_class", "healthy") or "healthy")
        severity = str(assessment.get("severity", "none") or "none")
        next_step = str(assessment.get("recommended_next_step", "none") or "none")
        key = f"{incident_class}|{severity}|{next_step}"
        current = self._soft_observation if isinstance(self._soft_observation, dict) else {}
        if str(current.get("key", "") or "") != key:
            current = {
                "key": key,
                "first_seen_ts": float(now),
                "last_seen_ts": float(now),
                "last_emit_ts": float(now),
                "count": 0,
                "incident_class": incident_class,
                "severity": severity,
                "recommended_next_step": next_step,
                "root_cause": dict(root_cause or {}),
            }
        current["count"] = int(current.get("count", 0) or 0) + 1
        current["last_seen_ts"] = float(now)
        current["root_cause"] = dict(root_cause or {})
        self._soft_observation = current

        elapsed_since_emit = float(now) - float(current.get("last_emit_ts", 0.0) or 0.0)
        if elapsed_since_emit >= self._soft_observation_summary_s and int(current.get("count", 0) or 0) > 1:
            current["last_emit_ts"] = float(now)
            duration_s = max(0.0, float(now) - float(current.get("first_seen_ts", now) or now))
            self._runtime_events.append(
                {
                    "kind": "incident_observation_summary",
                    "incident_class": incident_class,
                    "severity": severity,
                    "recommended_next_step": next_step,
                    "repeat_count": int(current.get("count", 0) or 0),
                    "duration_s": duration_s,
                    "operator_incident": False,
                    "root_cause": dict(root_cause or {}),
                }
            )
        return dict(current)

    def _update_episode(self, *, assessment: Dict[str, Any], now: float) -> tuple[str, float]:
        incident_class = str(assessment.get("incident_class", "healthy") or "healthy")
        next_step = str(assessment.get("recommended_next_step", "none") or "none")
        severity = str(assessment.get("severity", "none") or "none")
        root_cause = (
            dict(assessment.get("root_cause", {}))
            if isinstance(assessment.get("root_cause"), dict)
            else {}
        )
        active = incident_class != "healthy"

        if not active:
            if self._active_incident and str(self._active_incident.get("incident_class", "") or "") in {
                "rf_collapse",
                "rf_event_spike",
            }:
                clear_since = self._active_incident.get("clear_since")
                if clear_since is None:
                    self._active_incident["clear_since"] = now
                    clear_since = now
                clear_age_s = max(0.0, now - float(clear_since or now))
                if clear_age_s < self._incident_resolve_hold_s:
                    episode_id = str(self._active_incident.get("episode_id", "") or "")
                    age_s = max(0.0, now - float(self._active_incident.get("started_at", now) or now))
                    assessment["operator_incident"] = True
                    assessment["incident_clear_pending"] = True
                    assessment["incident_clear_hold_s"] = float(self._incident_resolve_hold_s)
                    return episode_id, age_s
            if (
                self._active_incident
                and str(self._active_incident.get("incident_class", "") or "") == "radio_ca_loss"
            ):
                clear_since = self._active_incident.get("clear_since")
                if clear_since is None:
                    self._active_incident["clear_since"] = now
                    clear_since = now
                clear_age_s = max(0.0, now - float(clear_since or now))
                if clear_age_s < self._ca_loss_resolve_hold_s:
                    episode_id = str(self._active_incident.get("episode_id", "") or "")
                    age_s = max(0.0, now - float(self._active_incident.get("started_at", now) or now))
                    return episode_id, age_s
            self._resolve_active_incident(now=now)
            assessment["operator_incident"] = False
            return "", 0.0

        if not self._is_operator_incident(assessment):
            if (
                self._active_incident
                and str(self._active_incident.get("incident_class", "") or "") == "radio_ca_loss"
                and incident_class == "ca_capacity_reduced_observe"
            ):
                previous_root = (
                    self._active_incident.get("root_cause", {})
                    if isinstance(self._active_incident.get("root_cause", {}), dict)
                    else {}
                )
                if self._ca_loss_context_key(previous_root) == self._ca_loss_context_key(root_cause):
                    soft = self._record_soft_observation(
                        assessment=assessment,
                        now=now,
                        root_cause=root_cause,
                    )
                    episode_id = str(self._active_incident.get("episode_id", "") or "")
                    age_s = max(0.0, now - float(self._active_incident.get("started_at", now) or now))
                    assessment["operator_incident"] = False
                    assessment["observation_repeat_count"] = int(soft.get("count", 0) or 0)
                    assessment["incident_state_active"] = True
                    return episode_id, age_s
            self._resolve_active_incident(now=now)
            soft = self._record_soft_observation(
                assessment=assessment,
                now=now,
                root_cause=root_cause,
            )
            assessment["operator_incident"] = False
            assessment["observation_repeat_count"] = int(soft.get("count", 0) or 0)
            return "", 0.0

        episode_key = self._episode_context_key(incident_class, root_cause)
        if self._active_incident is None:
            self._start_incident_episode(
                now=now,
                incident_class=incident_class,
                next_step=next_step,
                severity=severity,
                root_cause=root_cause,
                episode_key=episode_key,
            )
        else:
            prev = self._active_incident
            if (
                str(prev.get("incident_class", "")) != incident_class
                or str(prev.get("episode_key", "") or "") != episode_key
            ):
                self._resolve_active_incident(now=now)
                self._start_incident_episode(
                    now=now,
                    incident_class=incident_class,
                    next_step=next_step,
                    severity=severity,
                    root_cause=root_cause,
                    episode_key=episode_key,
                )
                episode_id = str(self._active_incident.get("episode_id", "") or "")
                return episode_id, 0.0
            self._active_incident.pop("clear_since", None)
            self._active_incident["recommended_next_step"] = next_step
            self._active_incident["severity"] = severity
            self._active_incident["root_cause"] = dict(root_cause or {})

        episode_id = str(self._active_incident.get("episode_id", "") or "")
        age_s = max(0.0, now - float(self._active_incident.get("started_at", now) or now))
        assessment["operator_incident"] = True
        return episode_id, age_s

    def _detect_rf_spike(self, sinr_db: float, rsrp_dbm: float, radio_connected: bool) -> Dict[str, Any]:
        del rsrp_dbm
        if not radio_connected:
            self._sinr_history.clear()
            self._rf_spike_active = False
            return {"spike_detected": False, "drop_db": 0.0, "recovering": False}

        self._sinr_history.append(float(sinr_db))
        if len(self._sinr_history) < 2:
            return {"spike_detected": False, "drop_db": 0.0, "recovering": False}

        window_peak = max(self._sinr_history)
        drop_db = float(window_peak - float(sinr_db))
        was_active = self._rf_spike_active
        spike_detected = drop_db >= self._rf_spike_threshold_db and float(sinr_db) < 8.0

        if spike_detected:
            self._rf_spike_active = True
            self._rf_spike_recovery_ticks = 0
        elif was_active:
            self._rf_spike_recovery_ticks += 1
            if self._rf_spike_recovery_ticks >= 3:
                self._rf_spike_active = False

        recovering = was_active and not spike_detected and self._rf_spike_recovery_ticks > 0
        return {
            "spike_detected": bool(spike_detected),
            "drop_db": round(float(drop_db), 1),
            "recovering": bool(recovering),
            "sinr_db": round(float(sinr_db), 1),
            "window_peak_sinr_db": round(float(window_peak), 1),
        }

    def _sinr_window_metrics(self, *, now: float, sinr_db: float) -> tuple[float, int]:
        window_s = max(
            float(getattr(self.cfg, "sample_interval_s", 2.0) or 2.0) * 3.0,
            float(self._hard.get("emergency_sinr_persist_s", getattr(self.cfg, "emergency_sinr_persist_s", 20.0)) or 0.0),
        )
        self._sinr_window.append((now, float(sinr_db)))
        cutoff = float(now) - float(window_s)
        while self._sinr_window and float(self._sinr_window[0][0]) < cutoff:
            self._sinr_window.popleft()
        if not self._sinr_window:
            return float(sinr_db), 0
        samples = [float(sample) for _, sample in self._sinr_window]
        return float(sum(samples) / len(samples)), len(samples)

    def _live_snapshot(
        self,
        *,
        modem: CellularIntelligenceGeneration,
        computed: DerivedSignalFrames,
        state: Optional[SystemState],
        controller_posture: Any,
        health_status: Optional[Dict[str, Any]],
        now: float,
        ca_lost_filtered: Optional[bool],
    ) -> Dict[str, Any]:
        health_status = dict(health_status or {})
        health_stage = str(health_status.get("stage", "healthy") or "healthy")
        action_hint = str(health_status.get("action_hint", "none") or "none")
        overall_ok = bool(health_status.get("overall_ok", True))
        emergency_class = str(
            getattr(controller_posture, "emergency_class", "") or ""
        )
        sinr_floor = float(self._hard.get("emergency_sinr_floor", getattr(self.cfg, "emergency_sinr_floor", 0.0)) or 0.0)
        sinr_gate = float(self._hard.get("sinr_feasibility_gate_db", getattr(self.cfg, "sinr_feasibility_gate_db", 3.0)) or 3.0)
        sinr_window_avg_db, sinr_window_samples = self._sinr_window_metrics(now=now, sinr_db=float(modem.cellular.sinr_db))
        cq_cell = float(computed.quality.cq_cell or 0.0)
        ca_capacity_available = has_ca_capacity(modem)
        low_sinr_window = bool(
            int(sinr_window_samples) >= 2
            and float(sinr_window_avg_db) <= sinr_floor
        )
        weak_rsrp = bool(float(modem.cellular.rsrp_dbm) <= -110.0)
        weak_rsrq = bool(float(modem.cellular.rsrq_db) <= -14.0)
        weak_cq = bool(cq_cell > 0.0 and cq_cell <= 0.45)
        ca_ld_active = cart_ld_active(computed)
        ca_ld_known = cart_ld_valid(computed)
        ca_loss_under_load = bool(
            modem.traffic.has_traffic
            and (
                ca_lost_filtered if ca_lost_filtered is not None else (ca_ld_known and not ca_ld_active)
            )
        )
        rf_truth = assess_rf_truth(
            modem=modem,
            computed=computed,
            cfg=self.cfg,
            low_sinr_persistent=low_sinr_window,
        )
        return {
            "ts": now,
            "state": state.name if isinstance(state, SystemState) else str(state or ""),
            "health_stage": health_stage,
            "health_action_hint": action_hint,
            "overall_ok": overall_ok,
            "ping_ok": bool(health_status.get("ping_ok", True)),
            "ping_fail_streak": int(health_status.get("ping_fail_streak", 0) or 0),
            "ping_success_streak": int(health_status.get("ping_success_streak", 0) or 0),
            "health_recovery_candidate": bool(health_status.get("recovery_candidate", False)),
            "radio_connected": bool(modem.cellular.radio_connected),
            "has_traffic": bool(modem.traffic.has_traffic),
            "traffic_valid": bool(getattr(modem.traffic, 'traffic_valid', True)),
            "traffic_state": str(getattr(modem.traffic, 'traffic_state', "") or ""),
            "traffic_state_detail": str(getattr(modem.traffic, 'traffic_state_detail', "") or ""),
            "traffic_invalid_reason": str(getattr(modem.traffic, 'traffic_invalid_reason', "") or ""),
            "packet_session_connected": getattr(modem.fault, 'packet_session_connected', None)
            if isinstance(getattr(modem.fault, 'packet_session_connected', None), bool) else None,
            "connection_status_code": str(getattr(modem.fault, 'connection_status_code', "") or ""),
            "cart_ld": int(getattr(computed.quality, 'cart_ld', 0) or 0),
            "cart_ld_valid": bool(ca_ld_known),
            "primary_band": int(getattr(modem.cellular, 'primary_band', getattr(modem.cellular, 'serving_band', 0)) or 0),
            "selected_band_set_signature": str(getattr(modem.cellular, 'selected_band_set_signature', "") or ""),
            "active_band_set_signature": str(getattr(modem.cellular, 'active_band_set_signature', "") or ""),
            "ca_signature": str(getattr(modem.cellular, 'ca_band_signature', "") or ""),
            "ca_lost_filtered": bool(ca_lost_filtered) if ca_lost_filtered is not None else None,
            "ca_capacity_available": bool(ca_capacity_available),
            "bad_rf_capacity_winner": bool(is_bad_rf_capacity_winner(modem, computed, self.cfg)),
            "rf_truth": rf_truth.as_dict(),
            "rf_truth_verdict": rf_truth.verdict,
            "rf_truth_confirmed_bad": bool(rf_truth.confirmed_bad_rf),
            "rf_truth_rules_out_bad": bool(rf_truth.rules_out_bad_rf),
            "rf_truth_reason": rf_truth.reason,
            "sinr_db": float(modem.cellular.sinr_db),
            "rsrp_dbm": float(modem.cellular.rsrp_dbm),
            "rsrq_db": float(modem.cellular.rsrq_db),
            "cq_cell": float(cq_cell),
            "congestion_pct": float(getattr(computed.quality, 'congestion_pct', 0.0) or 0.0),
            "rtt_ms": float(getattr(computed.latency, 'rtt_sliding_avg', getattr(computed.latency, 'rtt_ms', 0.0)) or 0.0),
            "session_uptime_s": float(getattr(modem.action, 'session_uptime_s', 0.0) or 0.0),
            "sinr_window_avg_db": float(sinr_window_avg_db),
            "sinr_window_samples": int(sinr_window_samples),
            "good_rf": bool(modem.cellular.sinr_db >= max(8.0, sinr_gate) and modem.cellular.rsrp_dbm >= -100.0),
            "low_rf": bool(
                low_sinr_window
                or weak_rsrp
                or weak_rsrq
                or weak_cq
                or rf_truth.verdict in {"bad", "watch"}
            ),
            "low_sinr_window": bool(low_sinr_window),
            "weak_rsrp": bool(weak_rsrp),
            "weak_rsrq": bool(weak_rsrq),
            "weak_cq": bool(weak_cq),
            "ca_loss_under_load": bool(ca_loss_under_load),
            "emergency_class": emergency_class,
            "nsa_active": bool(modem.cellular.nsa_active),
            "nr_just_dropped": bool(getattr(computed.quality, 'nr_just_dropped', False)),
            "nr_recovering": bool(getattr(computed.quality, 'nr_recovering', False)),
            "nr_thermal_drop": bool(
                getattr(computed.quality, 'nr_just_dropped', False)
                and bool(modem.fault.thermal_emergency_active)
                and float(modem.cellular.rsrp_dbm) >= -100.0
                and float(modem.cellular.sinr_db) >= 5.0
            ),
        }

    def _ca_loss_observation_age_s(self, live: Dict[str, Any]) -> float:
        """Track how long CA has been missing while load is present.

        The CA active bit is a register state, not an incident by itself.  Treat
        it as an incident only after persistence or service impact proves that
        the reduced capacity matters.
        """
        now = float(live.get("ts", 0.0) or 0.0)
        if not bool(live.get("ca_loss_under_load", False)):
            self._ca_loss_candidate = {}
            return 0.0
        key = "|".join(
            [
                str(live.get("primary_band", "") or ""),
                str(live.get("selected_band_set_signature", "") or ""),
                str(live.get("active_band_set_signature", "") or ""),
                str(live.get("ca_signature", "") or ""),
            ]
        )
        current = self._ca_loss_candidate if isinstance(self._ca_loss_candidate, dict) else {}
        if str(current.get("key", "") or "") != key:
            current = {
                "key": key,
                "first_seen_ts": now,
                "last_seen_ts": now,
                "count": 0,
            }
        current["count"] = int(current.get("count", 0) or 0) + 1
        current["last_seen_ts"] = now
        self._ca_loss_candidate = current
        return max(0.0, now - float(current.get("first_seen_ts", now) or now))

    def _ca_loss_performance_impacted(self, live: Dict[str, Any]) -> bool:
        """Return True only when the canonical health/outcome lane says CA loss matters.

        CA presence is a capacity state.  Do not rediscover health here from
        RTT/CQ/congestion; those inputs already feed the health/congestion
        authorities.  Incident classification only joins canonical CA state
        with the current health outcome.
        """
        stage = str(live.get("health_stage", "healthy") or "healthy")
        overall_ok = bool(live.get("overall_ok", True))
        action_hint = str(live.get("health_action_hint", "none") or "none")
        return bool(
            (not overall_ok and stage in {"degraded_hold", "degraded_persist", "connection_lost"})
            or bool(live.get("health_recovery_candidate", False))
            or action_hint in {
                "escalation_candidate",
                "recover",
                "recover_radio",
                "wait_reconnect",
                "emergency_recovery",
            }
        )

    def _low_rf_requires_critical_incident(self, live: Dict[str, Any]) -> bool:
        """Return True only when low-RF evidence is strong enough to block Brain work.

        No single RF variable is a collapse signal on CAT-class routers.  A
        CA-capable lane can have ugly SINR or weak RSRP while RSRQ/CQ and user
        outcome remain usable.  Keep critical incident ownership for
        corroborated RF loss, not for isolated radio metrics.
        """
        if not bool(live.get("low_rf", False)):
            return False
        if bool(live.get("rf_truth_rules_out_bad", False)):
            return False
        if bool(live.get("rf_truth_confirmed_bad", False)):
            return True
        if str(live.get("rf_truth_verdict", "") or "") != "bad":
            return False
        if bool(live.get("bad_rf_capacity_winner", False)):
            return False

        sinr = float(live.get("sinr_db", 0.0) or 0.0)
        rsrp = float(live.get("rsrp_dbm", 0.0) or 0.0)
        rsrq = float(live.get("rsrq_db", 0.0) or 0.0)
        cq = float(live.get("cq_cell", 0.0) or 0.0)
        congestion = float(live.get("congestion_pct", 0.0) or 0.0)
        rtt = float(live.get("rtt_ms", 0.0) or 0.0)
        ca_capacity = bool(live.get("ca_capacity_available", False))
        ca_active = bool(
            live.get("cart_ld_valid", False)
            and int(live.get("cart_ld", 0) or 0)
        )
        strong_serving_rf = bool(
            rsrp >= -100.0
            and rsrq >= -13.5
            and (cq >= 0.55 or cq <= 0.0)
        )

        quality_lane_usable = bool(
            ca_capacity
            and sinr >= 5.0
            and rsrq >= -12.5
            and (cq >= 0.55 or cq <= 0.0)
            and (congestion <= 65.0 or congestion <= 0.0)
            and (rtt <= 180.0 or rtt <= 0.0)
        )
        if quality_lane_usable:
            return False

        low_sinr = bool(live.get("low_sinr_window", False))
        weak_rsrp = bool(live.get("weak_rsrp", False)) or rsrp <= -110.0
        weak_rsrq = bool(live.get("weak_rsrq", False)) or rsrq <= -14.0
        weak_cq = bool(live.get("weak_cq", False)) or (cq > 0.0 and cq <= 0.45)
        outcome_bad = bool(
            (congestion >= 75.0 and congestion > 0.0)
            or (rtt >= 180.0 and rtt > 0.0)
        )
        if (
            ca_capacity
            and rtt >= 180.0
            and congestion <= 65.0
            and sinr >= 5.0
            and rsrq >= -13.5
            and not low_sinr
        ):
            return False
        if bool(live.get("good_rf", False)) and not outcome_bad:
            return False
        if (not low_sinr) and sinr >= 5.0 and rsrp >= -100.0 and not outcome_bad:
            return False
        if ca_capacity and not outcome_bad and sinr >= 5.0 and rtt <= 120.0 and congestion <= 65.0:
            return False
        rf_evidence_count = sum(
            1 for flag in (low_sinr, weak_rsrp, weak_rsrq, weak_cq) if bool(flag)
        )
        if (
            low_sinr
            and rf_evidence_count == 1
            and (ca_active or ca_capacity)
            and strong_serving_rf
        ):
            return False

        if rf_evidence_count >= 2:
            return True
        if low_sinr and (outcome_bad or not ca_capacity):
            return True
        if (weak_rsrp or weak_rsrq or weak_cq) and outcome_bad:
            return True

        return False

    @staticmethod
    def _rf_path_usable_for_data_incident(live: Dict[str, Any]) -> bool:
        """Return True when RF identity/quality is good enough to blame the data path.

        This is a naming guard: a bad WAN/downstream probe must not be called
        RF collapse while the live modem registers still show a connected,
        usable serving cell.
        """
        if not bool(live.get("radio_connected", False)):
            return False
        sinr = float(live.get("sinr_db", 0.0) or 0.0)
        rsrp = float(live.get("rsrp_dbm", 0.0) or 0.0)
        rsrq = float(live.get("rsrq_db", 0.0) or 0.0)
        cq = float(live.get("cq_cell", 0.0) or 0.0)
        primary_band = int(live.get("primary_band", 0) or 0)
        has_identity = bool(
            primary_band > 0
            or str(live.get("ca_signature", "") or "").strip()
            or str(live.get("active_band_set_signature", "") or "").strip()
        )
        usable_quality = bool(
            sinr >= 3.0
            and rsrp >= -115.0
            and rsrq >= -16.0
            and (cq >= 0.35 or cq <= 0.0)
        )
        strong_serving = bool(rsrp >= -100.0 and rsrq >= -13.5)
        ca_ld_active = bool(
            live.get("cart_ld_valid", False)
            and int(live.get("cart_ld", 0) or 0)
        )
        return bool(has_identity and (usable_quality or strong_serving or ca_ld_active))

    def _data_path_degraded_observation(self, live: Dict[str, Any]) -> Dict[str, Any]:
        packet_invalid = bool(live.get("packet_session_connected") is False)
        traffic_invalid = bool(live.get("traffic_valid") is False)
        if packet_invalid or traffic_invalid:
            incident_subtype = "packet_session_invalid" if packet_invalid else "traffic_sample_invalid"
            probe_age_s = self._data_path_probe_observation_age_s(live, incident_subtype=incident_subtype)
            live["data_path_probe_candidate_age_s"] = float(probe_age_s)
            live["data_path_probe_confirmation_s"] = float(self._data_path_probe_confirm_s)
            if probe_age_s < self._data_path_probe_confirm_s:
                live["data_path_probe_confirmation_pending"] = True
                return {}
            return self._incident_result(
                incident_class="data_path_degraded",
                incident_subtype=incident_subtype,
                severity="moderate",
                confidence=0.82,
                suppress_exploration=True,
                recovery_candidate=False,
                recommended_next_step="hold_radio_changes",
                recommended_next_step_reason=(
                    "modem RF registers are usable, but packet-session traffic counters are not trustworthy"
                ),
                health_stage=str(live.get("health_stage", "healthy") or "healthy"),
                health_action_hint=str(live.get("health_action_hint", "none") or "none"),
            )
        stage = str(live.get("health_stage", "healthy") or "healthy")
        if stage in {"healthy", "warning"} and bool(live.get("overall_ok", True)):
            self._data_path_probe_candidate = {}
            return {}
        if not self._rf_path_usable_for_data_incident(live):
            self._data_path_probe_candidate = {}
            return {}

        ping_fail_streak = int(live.get("ping_fail_streak", 0) or 0)
        rtt = float(live.get("rtt_ms", 0.0) or 0.0)
        ping_failed = ping_fail_streak > 0 or not bool(live.get("ping_ok", True))
        latency_failed = rtt >= 250.0
        if not ping_failed and not latency_failed:
            self._data_path_probe_candidate = {}
            return {}
        incident_subtype = "ping_fail_streak" if ping_fail_streak > 0 else "high_latency_path"
        probe_age_s = self._data_path_probe_observation_age_s(live, incident_subtype=incident_subtype)
        live["data_path_probe_candidate_age_s"] = float(probe_age_s)
        live["data_path_probe_confirmation_s"] = float(self._data_path_probe_confirm_s)
        if bool(live.get("traffic_valid", True)) and bool(live.get("has_traffic", False)):
            live["data_path_probe_deferred_by_traffic"] = True
            return {}
        if probe_age_s < self._data_path_probe_confirm_s:
            live["data_path_probe_confirmation_pending"] = True
            return {}
        persistent = bool(stage in {"degraded_persist", "connection_lost"} or ping_fail_streak >= 3)
        if (
            persistent
            and bool(live.get("radio_connected", False))
            and bool(live.get("traffic_valid", True))
            and not bool(live.get("has_traffic", False))
        ):
            live["downstream_collision_suspected"] = True
        return self._incident_result(
            incident_class="data_path_degraded",
            incident_subtype=incident_subtype,
            severity="high" if persistent else "moderate",
            confidence=0.84 if persistent else 0.74,
            suppress_exploration=bool(persistent),
            recovery_candidate=False,
            recommended_next_step="observe_data_path" if not persistent else "hold_radio_changes",
            recommended_next_step_reason=(
                "modem RF registers are usable, but WAN/downstream probes are failing"
                if ping_fail_streak > 0
                else f"modem RF registers are usable, but RTT is elevated at {rtt:.0f} ms"
            ),
            health_stage=stage,
            health_action_hint=str(live.get("health_action_hint", "none") or "none"),
        )

    def _data_path_probe_observation_age_s(self, live: Dict[str, Any], *, incident_subtype: str) -> float:
        """Track how long WAN probe symptoms persist before naming data-path failure.

        Speed tests and active sessions can legitimately distort ping/RTT probes.
        Hard modem facts such as invalid packet-session state bypass this timer;
        this guard applies only to probe-derived symptoms.
        """
        now = float(live.get("ts", 0.0) or 0.0)
        key = "|".join(
            [
                str(incident_subtype or ""),
                str(live.get("primary_band", "") or ""),
                str(live.get("ca_signature", "") or ""),
            ]
        )
        current = self._data_path_probe_candidate if isinstance(self._data_path_probe_candidate, dict) else {}
        if str(current.get("key", "") or "") != key:
            current = {
                "key": key,
                "first_seen_ts": now,
                "last_seen_ts": now,
                "count": 0,
            }
        current["count"] = int(current.get("count", 0) or 0) + 1
        current["last_seen_ts"] = now
        self._data_path_probe_candidate = current
        return max(0.0, now - float(current.get("first_seen_ts", now) or now))

    def _noncritical_low_rf_observation(self, live: Dict[str, Any]) -> Dict[str, Any]:
        if not bool(live.get("low_rf", False)):
            return {}
        sinr = float(live.get("sinr_db", 0.0) or 0.0)
        rsrp = float(live.get("rsrp_dbm", 0.0) or 0.0)
        rsrq = float(live.get("rsrq_db", 0.0) or 0.0)
        cq = float(live.get("cq_cell", 0.0) or 0.0)
        congestion = float(live.get("congestion_pct", 0.0) or 0.0)
        rtt = float(live.get("rtt_ms", 0.0) or 0.0)
        ca_active = bool(
            live.get("cart_ld_valid", False)
            and int(live.get("cart_ld", 0) or 0)
        )
        ca_capacity = bool(live.get("ca_capacity_available", False))

        usable_rf = bool(sinr >= 5.0 and rsrq >= -13.5 and (cq >= 0.45 or cq <= 0.0))
        usable_except_sinr = bool(
            rsrp >= -100.0
            and rsrq >= -13.5
            and (cq >= 0.55 or cq <= 0.0)
        )
        if rtt >= 180.0 and (usable_rf or usable_except_sinr or ca_active or ca_capacity) and congestion <= 95.0:
            return self._incident_result(
                incident_class="latency_keepalive_path",
                incident_subtype="rtt_keepalive_spike",
                severity="moderate",
                confidence=0.78,
                suppress_exploration=False,
                recovery_candidate=False,
                recommended_next_step="observe_keepalive_path",
                recommended_next_step_reason="latency/keepalive spike with usable RF and no RF collapse",
                health_stage="warning",
                health_action_hint="observe",
            )
        if (
            str(live.get("rf_truth_verdict", "") or "") == "watch"
            and (
                str(live.get("health_stage", "healthy") or "healthy") != "healthy"
                or not ca_capacity
                or rtt >= 180.0
                or congestion >= 75.0
            )
        ):
            stage = str(live.get("health_stage", "healthy") or "healthy")
            return self._incident_result(
                incident_class="rf_quality_watch",
                incident_subtype="insufficient_bad_rf_proof",
                severity="moderate" if stage != "healthy" else "low",
                confidence=0.62,
                suppress_exploration=False,
                recovery_candidate=False,
                recommended_next_step="collect_rf_evidence",
                recommended_next_step_reason=str(
                    live.get("rf_truth_reason", "")
                    or "RF symptoms are present, but BAD_RF is not proven"
                ),
                health_stage="warning" if stage == "healthy" else stage,
                health_action_hint="observe",
            )
        return {}

    def _stm_snapshot(self, *, now: float) -> Dict[str, Any]:
        history = list(iter_action_history_reversed(self.memory))
        recent_actions = [
            rec
            for rec in history
            if (now - float(getattr(rec, "timestamp", 0.0) or 0.0)) <= 300.0
        ]
        recent_mutating = [
            rec
            for rec in recent_actions
            if getattr(rec, "lever", None) in MUTATING_LEVERS
            and self._is_physical_mutating_action_record(rec)
        ]
        latest_mutating = recent_mutating[0] if recent_mutating else None
        latest_age_s = (
            max(0.0, now - float(getattr(latest_mutating, "timestamp", 0.0) or 0.0))
            if latest_mutating is not None
            else None
        )
        recent_objectives = [
            rec
            for rec in history
            if (now - float(getattr(rec, "timestamp", 0.0) or 0.0)) <= 900.0
            and str(getattr(rec, "objective_stage", "") or "")
        ]
        recent_recovery = [
            rec
            for rec in recent_objectives
            if str(getattr(rec, "proposal_intent", "") or "") in RECOVERY_INTENTS
            or str(getattr(rec, "proposal_intent", "") or "").startswith("emergency_")
            or str(getattr(rec, "proposal_intent", "") or "").endswith("_recovery")
        ]
        return {
            "recent_mutating_action_age_s": latest_age_s,
            "recent_mutating_lever": (
                getattr(getattr(latest_mutating, "lever", None), "name", "")
                if latest_mutating
                else ""
            ),
            "recent_mutating_success": bool(getattr(latest_mutating, "success", False))
            if latest_mutating is not None
            else False,
            "recent_mutating_improvement": float(getattr(latest_mutating, "improvement", 0.0) or 0.0)
            if latest_mutating is not None
            else 0.0,
            "recent_mutating_hard_fail": bool(getattr(latest_mutating, "hard_fail", False))
            if latest_mutating is not None
            else False,
            "recent_mutating_outcome_label": str(getattr(latest_mutating, "outcome_label", "") or "").strip().lower()
            if latest_mutating is not None
            else "",
            "self_induced_candidate": bool(
                latest_mutating is not None
                and latest_age_s is not None
                and latest_age_s <= 120.0
            ),
            "recovery_attempts_recent": len(recent_recovery),
        }

    @staticmethod
    def _is_physical_mutating_action_record(record: Any) -> bool:
        """Return True only for records that represent a command sent to hardware.

        Action history also stores referee/learning records.  Those records may
        carry the original mutating lever for Brain readability, but they are
        not a modem mutation and must not create a post-action incident window.
        """
        execution_status = str(getattr(record, "execution_status", "") or "").strip().lower()
        if execution_status in NON_PHYSICAL_ACTION_EXECUTION_STATUSES:
            return False
        verdict_stage = str(getattr(record, "outcome_verdict_stage", "") or "").strip().lower()
        if verdict_stage in NON_PHYSICAL_OUTCOME_STAGES:
            return False
        if str(getattr(record, "roi_parent_trace_id", "") or "").strip():
            return False
        params = getattr(record, "action_params", {}) or {}
        if isinstance(params, dict) and str(params.get("_roi_parent_trace_id", "") or "").strip():
            return False
        return True

    def _should_defer_post_action_instability(self, *, live: Dict[str, Any], stm: Dict[str, Any]) -> bool:
        stage = str(live.get("health_stage", "healthy") or "healthy")
        if stage not in {"warning", "degraded_hold"}:
            return False
        age_s = float(stm.get("recent_mutating_action_age_s", 0.0) or 0.0)
        settle_s = max(
            0.0,
            float(getattr(self.cfg, "radio_change_settle_s", 0.0) or 0.0),
            float(getattr(self.cfg, "autonomous_connect_settle_s", 0.0) or 0.0),
        )
        if settle_s <= 0.0 or age_s > settle_s:
            return False
        if not bool(stm.get("recent_mutating_success", False)) or bool(stm.get("recent_mutating_hard_fail", False)):
            return False
        improvement = float(stm.get("recent_mutating_improvement", 0.0) or 0.0)
        outcome_label = str(stm.get("recent_mutating_outcome_label", "") or "").strip().lower()
        if improvement < 0.0 and outcome_label not in {"good", "neutral"}:
            return False
        ping_fail_streak = int(live.get("ping_fail_streak", 0) or 0)
        ping_failed = ping_fail_streak > 0 or not bool(live.get("ping_ok", True))
        latency_failed = float(live.get("rtt_ms", 0.0) or 0.0) >= 250.0
        if not ping_failed and not latency_failed:
            return False
        if self._low_rf_requires_critical_incident(live):
            return False
        if bool(live.get("rf_truth_confirmed_bad", False)):
            return False
        if bool(live.get("ca_loss_under_load", False)):
            return False
        rf_usable = bool(
            live.get("rf_truth_rules_out_bad", False)
            or live.get("good_rf", False)
            or (
                float(live.get("rsrp_dbm", 0.0) or 0.0) >= -100.0
                and float(live.get("rsrq_db", 0.0) or 0.0) >= -13.5
                and float(live.get("cq_cell", 0.0) or 0.0) >= 0.45
            )
        )
        return rf_usable

    def _classify(self, *, live: Dict[str, Any], stm: Dict[str, Any]) -> Dict[str, Any]:
        stage = str(live.get("health_stage", "healthy") or "healthy")
        health_bad = stage != "healthy" or not bool(live.get("overall_ok", True))
        recovery_attempts = int(stm.get("recovery_attempts_recent", 0) or 0)
        nr_transition_active = bool(live.get("nr_just_dropped", False)) or bool(live.get("nr_recovering", False)) or bool(live.get("nr_thermal_drop", False))

        result = self._incident_result(
            health_stage=stage,
            health_action_hint=str(live.get("health_action_hint", "none") or "none"),
        )

        if bool(live.get("radio_mutation_window_active", False)):
            result.update(
                {
                    "planned_radio_quiet_active": True,
                    "radio_mutation_window_reason": str(live.get("radio_mutation_window_reason", "") or ""),
                    "radio_mutation_window_remaining_s": float(
                        live.get("radio_mutation_window_remaining_s", 0.0) or 0.0
                    ),
                    "radio_mutation_workflow_id": str(live.get("radio_mutation_workflow_id", "") or ""),
                    "radio_mutation_sequence_step": int(live.get("radio_mutation_sequence_step", 0) or 0),
                    "radio_mutation_sequence_total": int(live.get("radio_mutation_sequence_total", 0) or 0),
                    "recommended_next_step_reason": "planned radio mutation is still settling",
                }
            )
            return result

        if not bool(live.get("radio_connected", False)):
            result.update(
                self._incident_result(
                    incident_class="radio_disconnected",
                    incident_subtype="radio_down",
                    severity="critical",
                    confidence=0.98,
                    suppress_exploration=True,
                    recovery_candidate=True,
                    recommended_next_step="recover_radio",
                    recommended_next_step_reason="radio is not connected",
                    health_stage="connection_lost",
                    health_action_hint="wait_reconnect",
                )
            )
            return result

        if (
            health_bad
            and bool(stm.get("self_induced_candidate", False))
            and not self._should_defer_post_action_instability(live=live, stm=stm)
        ):
            result.update(
                self._incident_result(
                    incident_class="post_action_instability",
                    incident_subtype=str(stm.get("recent_mutating_lever", "") or "recent_mutation").lower(),
                    severity="high" if stage == "degraded_persist" else "moderate",
                    confidence=0.85,
                    self_induced=True,
                    suppress_exploration=True,
                    recovery_candidate=False,
                    recommended_next_step="settle_backoff",
                    recommended_next_step_reason="health degraded soon after a recent mutating action",
                    health_stage=stage,
                    health_action_hint=str(live.get("health_action_hint", "none") or "none"),
                )
            )
            return result

        data_path = self._data_path_degraded_observation(live)
        if data_path:
            bootstrap = live.get("startup_bootstrap_grace") if isinstance(live.get("startup_bootstrap_grace"), dict) else {}
            if bool(bootstrap.get("active", False)):
                result.update(
                    self._incident_result(
                        incident_class="startup_bootstrap_observe",
                        incident_subtype="data_path_bootstrap_grace",
                        severity="moderate",
                        confidence=0.72,
                        suppress_exploration=False,
                        recovery_candidate=False,
                        recommended_next_step="observe_bootstrap",
                        recommended_next_step_reason=(
                            "startup bootstrap grace is active; waiting for modem identity/path to settle"
                        ),
                        health_stage="warning",
                        health_action_hint="observe",
                    )
                )
                result["startup_bootstrap_grace_active"] = True
                result["startup_bootstrap_grace_remaining_s"] = float(bootstrap.get("remaining_s", 0.0) or 0.0)
                return result
            result.update(data_path)
            return result

        if bool(live.get("ca_loss_under_load", False)):
            ca_loss_age_s = self._ca_loss_observation_age_s(live)
            ca_loss_impacted = self._ca_loss_performance_impacted(live)
            if not ca_loss_impacted:
                result.update(
                    self._incident_result(
                        incident_class="ca_capacity_reduced_observe",
                        incident_subtype="ca_missing_under_load_service_usable",
                        severity="low",
                        confidence=0.72,
                        suppress_exploration=False,
                        recovery_candidate=False,
                        recommended_next_step="observe_ca_recovery",
                        recommended_next_step_reason=(
                            "CA is missing under load, but RF/latency evidence is still usable"
                        ),
                        health_stage=stage,
                        health_action_hint=str(live.get("health_action_hint", "none") or "none"),
                    )
                )
                result["ca_loss_observation_age_s"] = float(ca_loss_age_s)
                result["ca_loss_operator_min_s"] = float(self._ca_loss_operator_min_s)
                return result
            result.update(
                self._incident_result(
                    incident_class="radio_ca_loss",
                    incident_subtype="ca_capacity_loss_impacted",
                    severity="high" if (health_bad or ca_loss_impacted) else "moderate",
                    confidence=0.88,
                    suppress_exploration=True,
                    recovery_candidate=True,
                    recommended_next_step=(
                        "controlled_restart_candidate" if recovery_attempts >= 2 else "recover_ca"
                    ),
                    recommended_next_step_reason=(
                        "canonical health is degraded while CA is absent; repeated recovery attempts already exist"
                        if recovery_attempts >= 2
                        else "canonical health is degraded while CA is absent"
                    ),
                    health_stage=stage,
                    health_action_hint=str(live.get("health_action_hint", "none") or "none"),
                )
            )
            result["ca_loss_observation_age_s"] = float(ca_loss_age_s)
            result["ca_loss_operator_min_s"] = float(self._ca_loss_operator_min_s)
            return result

        self._ca_loss_observation_age_s(live)

        low_rf_critical = self._low_rf_requires_critical_incident(live)
        rf_emergency_class = str(live.get("emergency_class", "") or "") in {"E_RF_COLLAPSE", "BAD_RF"}
        if low_rf_critical and (rf_emergency_class or not nr_transition_active):
            result.update(
                self._incident_result(
                    incident_class="rf_collapse",
                    incident_subtype="low_rf",
                    severity="critical",
                    confidence=0.90,
                    suppress_exploration=True,
                    recovery_candidate=True,
                    recommended_next_step="emergency_recovery",
                    recommended_next_step_reason="RF is below emergency floor or collapse is already indicated",
                    health_stage="degraded_persist",
                    health_action_hint="escalation_candidate",
                )
            )
            return result

        noncritical_rf = self._noncritical_low_rf_observation(live)
        if noncritical_rf:
            result.update(noncritical_rf)
            return result

        rf_spike = live.get("rf_spike", {})
        if (
            isinstance(rf_spike, dict)
            and bool(rf_spike.get("spike_detected", False))
            and not nr_transition_active
            and not bool(live.get("rf_truth_rules_out_bad", False))
        ):
            result.update(
                self._incident_result(
                    incident_class="rf_event_spike",
                    incident_subtype="rapid_sinr_drop",
                    severity="critical",
                    confidence=0.88,
                    suppress_exploration=True,
                    recovery_candidate=True,
                    recommended_next_step="emergency_recovery",
                    recommended_next_step_reason=(
                        f"RF event spike: SINR dropped {rf_spike.get('drop_db', 0):.1f} dB rapidly"
                    ),
                    health_stage="degraded_persist",
                    health_action_hint="escalation_candidate",
                )
            )
            return result

        return result
