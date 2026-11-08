from arch4.controller_frames import DerivedSignalFrames
"""
arch4/brain/historical.py
═══════════════════════════════════════════════════════════════
ARCH4 v1 — Historical Journal (Axis 3)

Records everything. Decides nothing. Raises flags.

The brain writes every tick (what it saw) and after every action
(what happened). The brain reads before acting (what worked?
what failed?). The journal can also raise advisories — simple
flags that say "cliff ahead" or "risky combo" — but never
generates actions itself.

Three operations:
  WRITE  — record_tick(), record_action_outcome()
  READ   — what_worked(), what_failed(), get_hour_profile()
  ADVISE — is_cliff_ahead(), is_risky_combo()
═══════════════════════════════════════════════════════════════
"""

from arch4.controller_frames import CellularIntelligenceGeneration

import time
import logging
from pathlib import Path
from typing import Any, Optional, List, Dict, Mapping, Tuple

from arch4.interfaces import (
    ARCH_QUALITY_FORMULA_EPOCH,
    Arch4Config, HourlyPattern,
)
from arch4.evidence_scope import EvidenceScope, FormulaVector, evidence_scope_from_mapping
from arch4.config_views import build_historical_config
from arch4.brain.congestion_signals import quiet_rtt_avg
from arch4.brain.outcome_profile import is_bad_rf_capacity_winner
from arch4.memory_store import HistoricalStore
from arch4.brain.memory_action_history_owner import (
    action_roi_formula_vector,
)

log = logging.getLogger("arch4.brain.historical")
HISTORICAL_FORMULA_EPOCH = "hourly_context_ema_decay_v1"


def historical_formula_vector() -> FormulaVector:
    return FormulaVector.build(
        {
            "historical": HISTORICAL_FORMULA_EPOCH,
            "quality": str(ARCH_QUALITY_FORMULA_EPOCH or ""),
        }
    )


def action_history_formula_vector() -> FormulaVector:
    return action_roi_formula_vector()


def historical_evidence_scope(modem: CellularIntelligenceGeneration) -> Dict[str, Any]:
    return evidence_scope_from_mapping(
        {
            "identity_code": str(getattr(modem.identity, 'identity_code', "") or ""),
            "identity_status": str(getattr(modem.identity, 'identity_status', "") or ""),
            "identity_catalog_version": str(
                getattr(modem.identity, 'identity_catalog_version', "") or ""
            ),
            "identity_catalog_checksum": str(
                getattr(modem.identity, 'identity_catalog_checksum', "") or ""
            ),
            "session_scope_key": str(
                getattr(modem.generation, 'session_scope_key', "") or ""
            ),
            "source_epoch": str(getattr(modem.generation, 'source_epoch', "") or ""),
            "source_sequence": int(getattr(modem.generation, 'generation', 0) or 0),
            "sample_ts": float(getattr(modem.generation, 'sample_ts', 0.0) or 0.0),
            "sample_age_s": float(getattr(modem.generation, 'sample_age_s', 0.0) or 0.0),
            "provider_plmn": str(getattr(modem.identity, 'provider_plmn', "") or ""),
            "access_type": str(getattr(modem.identity, 'access_type', "") or ""),
            "serving_band": int(getattr(modem.cellular, 'serving_band', 0) or 0),
            "earfcn": int(getattr(modem.cellular, 'earfcn_dl', 0) or 0),
            "pci": int(getattr(modem.cellular, 'pci', 0) or 0),
        },
        formula_vector=historical_formula_vector(),
    ).as_dict()


class HistoricalJournal:
    """
    Passive hour-keyed journal with advisory flags.

    Brain writes every tick. Brain reads before acting.
    Journal raises flags but never initiates actions.
    """

    def __init__(self, config: Arch4Config, root_dir: Optional[Path | str] = None):
        self.cfg = config
        self.history_cfg = build_historical_config(config)
        self.store = HistoricalStore(root_dir) if root_dir else None

        # 24 hourly observation slots
        self.hours: Dict[int, HourlyPattern] = {
            h: HourlyPattern(hour=h) for h in range(24)
        }

        # Per-cell hourly patterns: keyed by (pci, primary_band) → list of 24 HourlyPattern.
        # The global self.hours has no cell context and can be polluted when the modem
        # roams between cells. cell_hours gives per-cell RTT/SINR history so
        # is_cliff_ahead() and congestion trend use the right cell's data.
        # Key format: "PCI{pci}:B{band}" e.g. "PCI303:B3"
        self.cell_hours: Dict[str, Dict[int, HourlyPattern]] = {}

        # Per-hour band performance: {hour: {sig: (total_tp, count)}}
        self.band_perf: Dict[int, Dict[str, Tuple[float, int]]] = {
            h: {} for h in range(24)
        }

        # Per-hour action outcomes: {hour: [record, ...]}
        self.action_outcomes: Dict[int, List[dict]] = {
            h: [] for h in range(24)
        }

        # Band anchor drift log — per hour, records rotation attempts and outcomes.
        # Shape: {hour: [{"target": int, "actual": int, "stuck": bool,
        #                  "sinr": float, "ca_sig": str, "t": float}, ...]}
        # Used to learn: "at hour H, does B3 anchor hold after rotation?"
        # If stuck_rate at a given hour is low, skip straight to cell lock.
        self.band_drift_events: Dict[int, List[dict]] = {
            h: [] for h in range(24)
        }
        self.selection_provenance: Dict[int, Dict[str, Dict[str, Any]]] = {
            h: {} for h in range(24)
        }
        self._provider_namespaces: Dict[str, dict] = {}
        self._current_provider_plmn: str = ""
        self._total_samples: int = 0
        self._save_count: int = 0
        self._last_save_reason: str = ""
        self._save_reason_counts: Dict[str, int] = {}
        self._delayed_reward_projection_keys: set[str] = set()
        self._band_drift_projection_keys: set[str] = set()
        self._current_evidence_scope: Dict[str, Any] = {}
        self._evidence_quarantine: List[Dict[str, Any]] = []
        # Hourly decay: old observations fade so new cell configs are adopted quickly.
        # Applied once per day (86400s) when record_tick is called.
        # Decay rate: 2% per day → ~30-day half-life for slow-changing cells.
        # Faster cells (provider reconfigs, band changes) will override sooner.
        self._last_decay_ts: float = 0.0
        self._decay_rate_per_day: float = 0.02
        self._capture_current_namespace()

    def refresh_thresholds(self) -> None:
        """Refresh grouped historical config after calibration profile switch."""
        self.history_cfg = build_historical_config(self.cfg)

    # ═══════════════════════════════════════════════════════
    # WRITE — brain feeds data in
    # ═══════════════════════════════════════════════════════

    def record_tick(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames,
                    hour: Optional[int] = None,
                    state_name: str = "") -> None:
        """Called every tick. Updates hourly observation averages.

        Applies daily decay once per day so old observations fade and the brain
        adapts to provider reconfigurations, cell changes, and seasonal patterns.
        """
        try:
            scope = EvidenceScope.from_mapping(historical_evidence_scope(modem))
        except (TypeError, ValueError) as exc:
            self._quarantine_evidence(
                reason=str(exc),
                row={"kind": "historical_tick", "sample_sequence": int(getattr(modem.generation, 'generation', 0) or 0)},
            )
            return
        self._bind_evidence_scope(scope)
        state = str(state_name or "")
        if state in {"E_RADIO_DOWN", "E_SESSION_RESET", "E_CONGESTION_STORM"}:
            return
        if state == "BAD_RF" and not is_bad_rf_capacity_winner(modem, computed, self.cfg):
            return

        now = time.time()
        if hour is None:
            hour = time.localtime(now).tm_hour

        # Apply hourly decay once per day
        if self._last_decay_ts == 0.0:
            self._last_decay_ts = now
        elif now - self._last_decay_ts >= 86400.0:
            self._apply_hourly_decay()
            self._last_decay_ts = now

        hp = self.hours[hour]
        hp.samples += 1
        self._total_samples += 1
        alpha = max(0.01, 1.0 / min(hp.samples, 200))

        hp.avg_sinr += alpha * (modem.cellular.sinr_db - hp.avg_sinr)
        hp.avg_throughput_mbps += alpha * (modem.traffic.dl_mbps - hp.avg_throughput_mbps)
        hp.avg_rtt += alpha * (quiet_rtt_avg(computed) - hp.avg_rtt)

        ca_val = 1.0 if modem.cellular.ca_active else 0.0
        hp.ca_active_ratio += alpha * (ca_val - hp.ca_active_ratio)

        # ── NR hourly tracking ────────────────────────────────────────
        # Track NR attachment ratio and NR SINR per hour. On 4G-only
        # hardware nsa_active is always False so nr_attach_ratio stays 0.
        nsa_val = 1.0 if bool(getattr(modem.cellular, 'nsa_active', False)) else 0.0
        hp.nr_attach_ratio += alpha * (nsa_val - hp.nr_attach_ratio)
        if nsa_val > 0.0:
            nr_sinr = float(getattr(modem.cellular, 'nr_sinr_db', 0.0) or 0.0)
            hp.nr_sinr_avg += alpha * (nr_sinr - hp.nr_sinr_avg)

        # Per-cell hourly pattern: keyed by PCI + primary band so cliff prediction
        # uses this specific cell's historical RTT, not a global polluted average.
        pci = int(getattr(modem.cellular, 'pci', 0) or 0)
        primary_band = int(getattr(modem.cellular, 'primary_band', 0) or 0)
        if pci > 0 and primary_band > 0 and bool(getattr(modem.cellular, 'radio_connected', False)):
            cell_key = f"PCI{pci}:B{primary_band}"
            if cell_key not in self.cell_hours:
                self.cell_hours[cell_key] = {h: HourlyPattern(hour=h) for h in range(24)}
            chp = self.cell_hours[cell_key][hour]
            chp.samples += 1
            calpha = max(0.01, 1.0 / min(chp.samples, 200))
            chp.avg_rtt += calpha * (quiet_rtt_avg(computed) - chp.avg_rtt)
            chp.avg_sinr += calpha * (modem.cellular.sinr_db - chp.avg_sinr)
            chp.avg_throughput_mbps += calpha * (modem.traffic.dl_mbps - chp.avg_throughput_mbps)
            # Per-cell NR hourly tracking
            chp.nr_attach_ratio += calpha * (nsa_val - chp.nr_attach_ratio)
            if nsa_val > 0.0:
                nr_sinr = float(getattr(modem.cellular, 'nr_sinr_db', 0.0) or 0.0)
                chp.nr_sinr_avg += calpha * (nr_sinr - chp.nr_sinr_avg)

        # Track throughput per band signature
        sig = modem.cellular.ca_band_signature
        if sig and modem.traffic.dl_mbps > 0:
            perf = self.band_perf[hour]
            if sig not in perf:
                perf[sig] = (0.0, 0)
            total, count = perf[sig]
            perf[sig] = (total + modem.traffic.dl_mbps, count + 1)

        transition = modem.radio_transition
        source = str(transition.source or "attribution_unavailable")
        selected_signature = "+".join(
            f"B{band}" for band in transition.after.selected_band_set
        )
        provenance_key = f"{source}|B{primary_band}|{selected_signature or '-'}"
        provenance = self.selection_provenance[hour].setdefault(
            provenance_key,
            {
                "source": source,
                "primary_band": primary_band,
                "selected_band_signature": selected_signature,
                "sample_count": 0,
                "episode_count": 0,
                "last_episode_id": "",
                "last_transition_id": "",
                "last_ts": 0.0,
            },
        )
        provenance["sample_count"] = int(provenance.get("sample_count", 0) or 0) + 1
        episode_id = str(transition.episode_id or "")
        if episode_id and episode_id != str(provenance.get("last_episode_id") or ""):
            provenance["episode_count"] = int(
                provenance.get("episode_count", 0) or 0
            ) + 1
            provenance["last_episode_id"] = episode_id
        if transition.fresh:
            provenance["last_transition_id"] = str(transition.transition_id or "")
        provenance["last_ts"] = now

    def _apply_hourly_decay(self) -> None:
        """Fade all hourly patterns toward neutral once per day.

        Without decay, observations from months ago have the same weight as
        yesterday's — the brain cannot adapt when a provider reconfigures a
        cell, adds a band, or changes capacity allocation.

        Decay rate: 2% per day (self._decay_rate_per_day).
        - After 30 days without new data: values fade ~45% toward zero.
        - After 90 days: ~84% faded — strong new data easily overrides.
        - Active hours (many ticks per day) are constantly refreshed by the
          EMA in record_tick, so decay only affects stale hours.

        What decays:
          - avg_sinr, avg_rtt, avg_throughput_mbps, avg_mcs, ca_active_ratio
          - nr_attach_ratio, nr_sinr_avg
          - sample_count (makes EMA more responsive to new data after decay)
        What does NOT decay:
          - best_band_signature, worst_band_signature (kept as advisory labels)
          - action_outcomes (kept for risky-combo detection, bounded separately)
        """
        decay = max(0.0, 1.0 - float(self._decay_rate_per_day))
        for h in range(24):
            hp = self.hours[h]
            if hp.samples < 10:
                continue  # don't decay sparse hours — too little signal
            # Fade metric averages toward zero (neutral / unknown)
            hp.avg_sinr *= decay
            hp.avg_mcs *= decay
            hp.avg_throughput_mbps *= decay
            hp.avg_rtt *= decay
            hp.ca_active_ratio *= decay
            hp.nr_attach_ratio *= decay
            hp.nr_sinr_avg *= decay
            # Reduce effective sample count so new observations carry more weight
            hp.samples = max(10, int(hp.samples * decay))

        # Also decay band_perf counts so stale entries don't dominate throughput ranking
        for h in range(24):
            perf = self.band_perf[h]
            for sig in list(perf.keys()):
                total, count = perf[sig]
                new_count = max(1, int(count * decay))
                # Preserve the average, reduce the weight
                new_total = total * (new_count / max(count, 1))
                perf[sig] = (new_total, new_count)

    def record_action_outcome(self, hour: int, bands: List[int],
                               throughput_after: float, success: bool,
                               metadata: Optional[dict] = None) -> bool:
        """Called after an action completes. Records what happened."""
        evidence_raw = (
            metadata.get("evidence_scope")
            if isinstance(metadata, dict)
            and isinstance(metadata.get("evidence_scope"), dict)
            else {}
        )
        try:
            evidence = EvidenceScope.from_mapping(evidence_raw)
            current = EvidenceScope.from_mapping(self._current_evidence_scope)
            if evidence.formula_vector != action_history_formula_vector():
                raise ValueError("historical_action_formula_vector_mismatch")
            if not evidence.same_physical_namespace(current):
                raise ValueError("historical_action_scope_foreign")
        except (TypeError, ValueError) as exc:
            self._quarantine_evidence(
                reason=str(exc),
                row={"kind": "historical_action_outcome", "metadata": dict(metadata or {})},
            )
            return False
        sig = "B" + "+B".join(str(b) for b in sorted(bands))
        self.action_outcomes[hour].append({
            "bands": sorted(bands),
            "sig": sig,
            "tp": throughput_after,
            "ok": success,
            "red": False,
            "t": time.time(),
            "meta": metadata or {},
        })
        # Keep bounded
        if len(self.action_outcomes[hour]) > 200:
            self.action_outcomes[hour] = self.action_outcomes[hour][-100:]
        return True

    def record_classified_action_outcome(self, **_: Any) -> Dict[str, Any]:
        raise RuntimeError(
            "generic_classified_historical_learning_forbidden:"
            "use record_terminal_reward_outcome"
        )

    def record_terminal_reward_outcome(
        self,
        *,
        reward_entry: Mapping[str, Any],
        action_record: Any,
        action: Any,
        provider_plmn: str = "",
        provider_name: str = "",
        emergency_class: str = "",
        hour: Optional[int] = None,
        source: str = "classified_action_record",
        throughput_after: Optional[float] = None,
        success: Optional[bool] = None,
        metadata_extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Project one canonical terminal reward into Historical memory."""
        from arch4.terminal_reward_contract import terminal_reward_entry_error

        reward_row = dict(reward_entry or {})
        reward_error = terminal_reward_entry_error(reward_row)
        if reward_error:
            return {
                "recorded": False,
                "reason": f"terminal_reward_invalid:{reward_error}",
            }
        if action_record is None:
            return {"recorded": False, "reason": "missing_action_record"}
        projection_key = str(dict(metadata_extra or {}).get("reward_ledger_key") or "")
        if projection_key != str(reward_row.get("ledger_key") or ""):
            return {
                "recorded": False,
                "reason": "terminal_reward_projection_key_mismatch",
            }
        if projection_key and projection_key in self._delayed_reward_projection_keys:
            return {
                "recorded": True,
                "reason": "delayed_reward_projection_already_recorded",
                "duplicate": True,
                "reward_ledger_key": projection_key,
            }
        params = dict(getattr(action_record, "action_params", {}) or {})
        if not params and action is not None:
            params = dict(getattr(action, "params", {}) or {})
        bands = []
        for raw_band in params.get("bands", []) or []:
            try:
                band = int(raw_band)
            except (TypeError, ValueError):
                continue
            if band > 0:
                bands.append(band)
        if hour is None:
            ts = float(getattr(action_record, "timestamp", 0.0) or time.time())
            hour = time.localtime(ts).tm_hour
        outcome_label = str(getattr(action_record, "outcome_label", "") or "").strip().lower()
        metadata = {
            "source": str(source or "classified_action_record"),
            "provider_plmn": str(provider_plmn or getattr(action_record, "provider_plmn", "") or ""),
            "provider_name": str(provider_name or getattr(action_record, "provider_name", "") or ""),
            "emergency_class": str(emergency_class or getattr(action_record, "emergency_class", "") or ""),
            "decision_trace_id": str(getattr(action_record, "decision_trace_id", "") or ""),
            "objective_id": str(getattr(action_record, "proposal_objective_id", "") or ""),
            "intent": str(getattr(action_record, "proposal_intent", "") or ""),
            "reason": str(getattr(action_record, "proposal_reason", "") or ""),
            "outcome_label": outcome_label,
            "hard_fail": bool(getattr(action_record, "hard_fail", False)),
            "referee_level": str(getattr(action_record, "referee_outcome_level", "") or ""),
            "referee_code": str(getattr(action_record, "referee_outcome_code", "") or ""),
            "learning_commit_status": str(getattr(action_record, "learning_commit_status", "") or ""),
            "learning_ltm_applied": bool(getattr(action_record, "learning_ltm_applied", False)),
            "ca_signature_after": str(getattr(action_record, "ca_signature_after", "") or ""),
            "results": [
                {"band": band, "outcome": outcome_label or "classified"}
                for band in bands
            ],
            "evidence_scope": dict(
                getattr(action_record, "evidence_scope", {}) or {}
            ),
        }
        if metadata_extra:
            metadata.update(dict(metadata_extra))
        recorded = self.record_action_outcome(
            hour=int(hour),
            bands=bands,
            throughput_after=(
                float(throughput_after)
                if throughput_after is not None
                else float(getattr(action_record, "throughput_after_mbps", 0.0) or 0.0)
            ),
            success=(bool(success) if success is not None else outcome_label == "good"),
            metadata=metadata,
        )
        if not recorded:
            return {"recorded": False, "reason": "evidence_scope_quarantined"}
        if projection_key:
            self._delayed_reward_projection_keys.add(projection_key)
        result = {
            "recorded": True,
            "reason": str(source or "classified_action_record"),
        }
        if projection_key:
            result.update({"duplicate": False, "reward_ledger_key": projection_key})
        return result

    def record_band_drift(self, hour: int, target_band: int, actual_band: int,
                          stuck: bool, sinr: float, ca_sig: str,
                          evidence_scope: Optional[Dict[str, Any]] = None,
                          projection_key: str = "") -> bool:
        """
        Record the result of a band anchor rotation check.

        Called by _check_anchor_enforcement ~5 minutes after a rotation fires,
        once we know whether the modem held the requested anchor or drifted back.

        This builds a per-hour picture of modem anchor stability:
          - If B3 rotations at hour 1-5 always stick → rotation is sufficient
          - If B3 rotations at hour 7-9 always drift → skip rotation, lock directly

        The drift_rate() advisory below reads this data.
        """
        key = str(projection_key or "").strip()
        if key and key in self._band_drift_projection_keys:
            return False
        try:
            evidence = EvidenceScope.from_mapping(dict(evidence_scope or {}))
            current = EvidenceScope.from_mapping(self._current_evidence_scope)
            if not evidence.same_runtime_namespace(current):
                raise ValueError("historical_band_drift_scope_foreign")
        except (TypeError, ValueError) as exc:
            self._quarantine_evidence(
                reason=str(exc),
                row={"kind": "historical_band_drift", "target_band": target_band},
            )
            return False
        event = {
            "target": target_band,
            "actual": actual_band,
            "stuck": stuck,
            "sinr": sinr,
            "ca_sig": ca_sig,
            "t": time.time(),
            "evidence_scope": evidence.as_dict(),
            "projection_key": key,
        }
        self.band_drift_events[hour].append(event)
        # Keep bounded — last 50 events per hour is plenty
        if len(self.band_drift_events[hour]) > 50:
            self.band_drift_events[hour] = self.band_drift_events[hour][-50:]
        if key:
            self._band_drift_projection_keys.add(key)
            if len(self._band_drift_projection_keys) > 1024:
                self._band_drift_projection_keys = set(
                    sorted(self._band_drift_projection_keys)[-1024:]
                )

        if stuck:
            log.info("Hour %d: B%d anchor held (stuck=True)", hour, target_band)
        else:
            log.info("Hour %d: B%d anchor drifted → B%d (stuck=False)",
                     hour, target_band, actual_band)
        return True

    def band_drift_projection_completed(self, projection_key: str) -> bool:
        """Return exact durable completion truth for one anchor observation."""

        key = str(projection_key or "").strip()
        if not key:
            raise ValueError("historical_band_drift_projection_key_required")
        return key in self._band_drift_projection_keys

    def current_evidence_scope(self) -> Dict[str, Any]:
        """Expose validated Historical namespace truth to boundary projectors."""

        scope = EvidenceScope.from_mapping(self._current_evidence_scope)
        if scope.formula_vector != historical_formula_vector():
            raise ValueError("historical_formula_vector_mismatch")
        return scope.as_dict()

    # ═══════════════════════════════════════════════════════
    # READ — brain queries before acting
    # ═══════════════════════════════════════════════════════

    def what_worked(self, hour: int) -> Optional[List[int]]:
        """
        What band combo delivered the best throughput at this hour?
        Returns band list or None if not enough data.
        """
        perf = self.band_perf.get(hour, {})
        if not perf:
            return None

        best_sig, best_avg = None, 0.0
        for sig, (total, count) in perf.items():
            if count < 3:
                continue
            avg = total / count
            if avg > best_avg:
                best_avg = avg
                best_sig = sig

        if best_sig is None:
            return None

        return self._parse_sig(best_sig)

    def what_failed(self, hour: int, bands: List[int]) -> Optional[dict]:
        """
        Did this exact band combo fail or red-line at this hour?
        Returns the failure record, or None if clean history.
        """
        target = sorted(bands)
        outcomes = self.action_outcomes.get(hour, [])

        for outcome in reversed(outcomes):
            if outcome["bands"] == target:
                if outcome["red"] or not outcome["ok"]:
                    return outcome
                # Found it and it was fine
                return None

        # Never tried — no data, not a failure
        return None

    def get_hour_profile(self, hour: int) -> HourlyPattern:
        """Get the learned averages for a specific hour."""
        return self.hours[hour % 24]

    def total_samples(self) -> int:
        """Total sample count for the currently bound provider namespace."""
        return int(self._total_samples)

    def get_runtime_status(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "total_samples": int(self._total_samples),
            "save_count": int(self._save_count),
            "last_save_reason": str(self._last_save_reason or ""),
            "save_reason_counts": dict(self._save_reason_counts or {}),
        }
        if self.store is not None:
            out["store"] = self.store.stats()
        return out

    def has_data(self, hour: int) -> bool:
        """Do we have meaningful data for this hour?"""
        return self.hours[hour].samples >= self.history_cfg.hourly_pattern_min_samples

    def get_avg_throughput(self, hour: int, band_sig: str) -> Optional[float]:
        """Average throughput for a band combo at a specific hour."""
        perf = self.band_perf.get(hour, {})
        if band_sig not in perf:
            return None
        total, count = perf[band_sig]
        if count < 3:
            return None
        return total / count

    def get_band_outcome_stats(self, hour: int, band: int) -> dict:
        """
        Aggregate band probe outcomes recorded in action_outcomes metadata.

        Returns counts for provider-scoped current namespace / given hour.
        This powers provider+hour penalties for unreachable or weak bands.
        """
        stats = {
            "samples": 0,
            "requested_and_serving": 0,
            "requested_present_not_anchor": 0,
            "requested_absent_fallback_attach": 0,
            "weak_attach_unusable": 0,
            "action_failed_or_modem_fault": 0,
        }
        for outcome in self.action_outcomes.get(hour, []):
            meta = outcome.get("meta") or {}
            for r in meta.get("results", []) or []:
                if int(r.get("band", -1)) != int(band):
                    continue
                kind = str(r.get("outcome") or "")
                stats["samples"] += 1
                if kind in stats:
                    stats[kind] += 1
        return stats

    def band_unreachable_penalty(self, hour: int, band: int,
                                 min_samples: int = 2) -> float:
        """
        Soft prior penalty (0..1.5 typical) for provider+hour band reachability.

        High when historical outcomes show fallback attaches or weak/unusable
        attaches for the requested band at this hour.
        """
        s = self.get_band_outcome_stats(hour, band)
        n = s["samples"]
        if n < min_samples:
            return 0.0
        fallback_rate = s["requested_absent_fallback_attach"] / n
        weak_rate = s["weak_attach_unusable"] / n
        fault_rate = s["action_failed_or_modem_fault"] / n
        present_rate = (s["requested_and_serving"] + s["requested_present_not_anchor"]) / n
        # Penalize unreachable/weak strongly, but reduce penalty if historically present.
        penalty = (fallback_rate * 1.0) + (weak_rate * 0.8) + (fault_rate * 1.2) - (present_rate * 0.4)
        return max(0.0, penalty)

    # ═══════════════════════════════════════════════════════
    # ADVISE — flags the brain can check, not actions
    # ═══════════════════════════════════════════════════════

    def is_cliff_ahead(
        self,
        current_hour: int,
        serving_pci: int = 0,
        primary_band: int = 0,
    ) -> bool:
        """Advisory: is congestion incoming in the next 1-3 hours?

        Upgraded from F1.6 single-hour check to a 3-hour rising trend:
          - Uses per-cell hourly patterns when PCI+band are provided (most accurate)
          - Falls back to global hourly patterns when no cell context given
          - Fires on a rising RTT *trend* across 3 hours, not just a single spike
            This reduces false positives from one noisy hour

        Threshold: RTT rising by >15ms/h over 3 hours, or next hour >80ms avg.
        """
        min_s = self.history_cfg.hourly_pattern_min_samples

        # Select per-cell patterns if available
        cell_key = f"PCI{serving_pci}:B{primary_band}" if serving_pci > 0 and primary_band > 0 else ""
        cell_data = self.cell_hours.get(cell_key) if cell_key else None

        def _rtt(h: int) -> Optional[float]:
            """RTT for hour h — prefers cell-specific data."""
            if cell_data:
                chp = cell_data.get(h % 24)
                if chp and chp.samples >= max(3, min_s // 3):
                    return chp.avg_rtt
            hp = self.hours[h % 24]
            return hp.avg_rtt if hp.samples >= min_s else None

        rtt_now = _rtt(current_hour)
        rtt_p1 = _rtt(current_hour + 1)
        rtt_p2 = _rtt(current_hour + 2)

        # Single-hour cliff (backward compat): next hour is historically heavy
        if rtt_p1 is not None and rtt_p1 > 80.0:
            return True

        # 3-hour rising trend: congestion is building
        if rtt_now is not None and rtt_p1 is not None and rtt_p2 is not None:
            rising_to_p1 = rtt_p1 - rtt_now > 15.0
            rising_to_p2 = rtt_p2 - rtt_now > 25.0
            if rising_to_p1 and rising_to_p2:
                return True

        return False

    def congestion_trend(
        self,
        current_hour: int,
        serving_pci: int = 0,
        primary_band: int = 0,
    ) -> str:
        """Advisory: is cell load rising, falling, or stable over the next 3 hours?

        Returns: "rising" | "falling" | "stable" | "unknown"
        Uses per-cell patterns when PCI+band available, global otherwise.
        Considers a 3-hour window so the brain can prepare earlier.
        """
        min_s = self.history_cfg.hourly_pattern_min_samples
        cell_key = f"PCI{serving_pci}:B{primary_band}" if serving_pci > 0 and primary_band > 0 else ""
        cell_data = self.cell_hours.get(cell_key) if cell_key else None

        def _rtt(h: int) -> Optional[float]:
            if cell_data:
                chp = cell_data.get(h % 24)
                if chp and chp.samples >= max(3, min_s // 3):
                    return chp.avg_rtt
            hp = self.hours[h % 24]
            return hp.avg_rtt if hp.samples >= min_s else None

        r0 = _rtt(current_hour)
        r1 = _rtt(current_hour + 1)
        r2 = _rtt(current_hour + 2)

        if r0 is None or r1 is None:
            return "unknown"

        delta_1h = r1 - r0
        delta_2h = (r2 - r0) if r2 is not None else delta_1h

        if delta_1h > 15.0 or delta_2h > 25.0:
            return "rising"
        if delta_1h < -15.0 or delta_2h < -25.0:
            return "falling"
        return "stable"

    def get_load_trend(self, current_hour: int) -> str:
        """Backward-compat wrapper for congestion_trend with no cell context."""
        return self.congestion_trend(current_hour)

    def is_risky_combo(self, hour: int, bands: List[int]) -> bool:
        """Advisory: has this band combo caused a red line at this hour?

        Brain should think twice before using this combo again.
        Returns True if there's a red-line record for these bands at this hour.
        """
        target = sorted(bands)
        outcomes = self.action_outcomes.get(hour, [])
        for outcome in reversed(outcomes):
            if outcome["bands"] == target and outcome["red"]:
                return True
        return False

    def is_degradation_window(
        self,
        hour: int,
        serving_pci: int = 0,
        primary_band: int = 0,
    ) -> bool:
        """Advisory: is this hour historically a known-bad window on this cell?

        Returns True when the hourly profile shows persistently high RTT and
        low throughput — indicating cell congestion that exploration probing
        cannot fix. The brain should hold the LTM-best config and stop wasting
        probes.

        Two criteria (either fires):
          A) avg_rtt ≥ rtt_threshold AND avg_throughput ≤ tp_threshold
          B) avg_rtt ≥ severe_rtt_threshold (alone — severe latency)

        Uses per-cell patterns when PCI+band are provided, global otherwise.
        """
        min_s = self.history_cfg.hourly_pattern_min_samples
        rtt_gate = self.history_cfg.degradation_window_rtt_ms
        tp_gate = self.history_cfg.degradation_window_throughput_mbps
        severe_rtt = self.history_cfg.degradation_window_severe_rtt_ms

        # Prefer per-cell patterns
        hp = None
        if serving_pci > 0 and primary_band > 0:
            cell_key = f"PCI{serving_pci}:B{primary_band}"
            cell_data = self.cell_hours.get(cell_key)
            if cell_data:
                chp = cell_data.get(hour % 24)
                if chp and chp.samples >= max(3, min_s // 3):
                    hp = chp

        # Fall back to global
        if hp is None:
            ghp = self.hours[hour % 24]
            if ghp.samples >= min_s:
                hp = ghp

        if hp is None:
            return False  # not enough data

        # Criterion A: combined RTT + throughput gate
        if hp.avg_rtt >= rtt_gate and hp.avg_throughput_mbps <= tp_gate:
            return True

        # Criterion B: severe RTT alone
        if hp.avg_rtt >= severe_rtt:
            return True

        return False

    def anchor_drift_rate(self, hour: int, band: int,
                          min_samples: int = 3) -> Optional[float]:
        """
        Advisory: what fraction of rotations to B{band} drifted at this hour?

        Returns 0.0 (always sticks) to 1.0 (always drifts), or None if
        insufficient data. The brain can use this to decide:
          - drift_rate < 0.3  → rotation is usually enough, try it
          - drift_rate >= 0.3 → modem reliably overrides at this hour,
                                skip rotation and go straight to cell lock
        """
        events = self.band_drift_events.get(hour, [])
        relevant = [e for e in events if e["target"] == band]
        if len(relevant) < min_samples:
            return None
        drift_count = sum(1 for e in relevant if not e["stuck"])
        return drift_count / len(relevant)

    # ═══════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════

    @staticmethod
    def _parse_sig(sig: str) -> Optional[List[int]]:
        """Parse 'B1+B3+B20' into [1, 3, 20]."""
        try:
            parts = sig.replace("B", "").split("+")
            bands = sorted(int(p.strip()) for p in parts if p.strip())
            return bands if bands else None
        except (ValueError, AttributeError):
            return None

    def set_provider_context(self, provider_plmn: str) -> None:
        plmn = (provider_plmn or "").strip()
        if plmn == self._current_provider_plmn:
            return
        self._capture_current_namespace()
        ns = self._provider_namespaces.get(plmn)
        if ns is None:
            ns = self._new_namespace()
            self._provider_namespaces[plmn] = ns
        self._bind_namespace(ns)
        self._current_provider_plmn = plmn
        log.info("Historical journal provider context: %s", plmn or "<legacy-global>")

    def _quarantine_evidence(self, *, reason: str, row: Dict[str, Any]) -> None:
        self._evidence_quarantine.append(
            {
                "schema": "EvidenceQuarantine.v1",
                "owner": "arch4.brain.historical",
                "reason": str(reason or "evidence_scope_invalid"),
                "quarantined_at": time.time(),
                "row": dict(row or {}),
            }
        )
        if len(self._evidence_quarantine) > 128:
            self._evidence_quarantine = self._evidence_quarantine[-128:]

    def _bind_evidence_scope(self, scope: EvidenceScope) -> None:
        current: Optional[EvidenceScope]
        try:
            current = EvidenceScope.from_mapping(self._current_evidence_scope)
        except (TypeError, ValueError):
            current = None
        if current is not None and not current.same_runtime_namespace(scope):
            self._quarantine_evidence(
                reason="historical_runtime_scope_changed",
                row={
                    "kind": "historical_namespace",
                    "evidence_scope": current.as_dict(),
                    "state": self._serialize_namespace(),
                },
            )
            provider = str(self._current_provider_plmn or "")
            self._provider_namespaces = {}
            self._bind_namespace(self._new_namespace())
            self._current_provider_plmn = provider
            self._provider_namespaces[provider] = {
                "hours": self.hours,
                "band_perf": self.band_perf,
                "action_outcomes": self.action_outcomes,
                "band_drift_events": self.band_drift_events,
                "cell_hours": self.cell_hours,
                "selection_provenance": self.selection_provenance,
            }
        self._current_evidence_scope = scope.as_dict()

    # ═══════════════════════════════════════════════════════
    # Persistence
    # ═══════════════════════════════════════════════════════

    def get_state_for_save(self) -> dict:
        """Serialize for memory persistence."""
        self._capture_current_namespace()
        state = self._serialize_namespace()
        state["providers"] = {
            plmn: self._serialize_namespace(ns)
            for plmn, ns in self._provider_namespaces.items()
        }
        state["provider_context"] = self._current_provider_plmn
        state["delayed_reward_projection_keys"] = sorted(self._delayed_reward_projection_keys)
        state["band_drift_projection_keys"] = sorted(
            self._band_drift_projection_keys
        )[-1024:]
        state["evidence_scope"] = dict(self._current_evidence_scope)
        state["evidence_quarantine"] = list(self._evidence_quarantine[-128:])
        return state

    def save_state(self, *, reason: str = "manual") -> None:
        del reason
        raise RuntimeError(
            "historical_direct_save_owner_deleted:"
            "use_engine_checkpoint_coordinator"
        )

    def load_state_from_store(self) -> bool:
        raise RuntimeError(
            "historical_direct_load_owner_deleted:"
            "use_selected_engine_checkpoint"
        )

    def _serialize_namespace(self, ns: Optional[dict] = None) -> dict:
        ns = ns or {
            "hours": self.hours,
            "band_perf": self.band_perf,
            "action_outcomes": self.action_outcomes,
            "band_drift_events": self.band_drift_events,
            "selection_provenance": self.selection_provenance,
        }
        return {
            "last_decay_ts": float(self._last_decay_ts),
            "hours": {
                str(h): {
                    "samples": hp.samples,
                    "avg_sinr": hp.avg_sinr,
                    "avg_mcs": hp.avg_mcs,
                    "avg_throughput_mbps": hp.avg_throughput_mbps,
                    "avg_rtt": hp.avg_rtt,
                    "ca_active_ratio": hp.ca_active_ratio,
                    "nr_attach_ratio": hp.nr_attach_ratio,
                    "nr_sinr_avg": hp.nr_sinr_avg,
                }
                for h, hp in ns["hours"].items()
            },
            "band_perf": {
                str(h): {sig: [t, c] for sig, (t, c) in perf.items()}
                for h, perf in ns["band_perf"].items()
                if perf
            },
            "action_outcomes": {
                str(h): outcomes[-50:]  # keep last 50 per hour
                for h, outcomes in ns["action_outcomes"].items()
                if outcomes
            },
            "band_drift_events": {
                str(h): events[-50:]   # keep last 50 per hour
                for h, events in ns["band_drift_events"].items()
                if events
            },
            "selection_provenance": {
                str(h): dict(rows)
                for h, rows in ns.get("selection_provenance", {}).items()
                if rows
            },
        }

    def load_state(self, data: dict) -> None:
        """Load from memory persistence."""
        try:
            loaded_scope = EvidenceScope.from_mapping(
                dict(data.get("evidence_scope") or {})
            )
            if loaded_scope.formula_vector != historical_formula_vector():
                raise ValueError("historical_formula_vector_mismatch")
        except (TypeError, ValueError) as exc:
            self._quarantine_evidence(
                reason=f"historical_snapshot_unscoped:{exc}",
                row={"kind": "historical_snapshot", "state": dict(data or {})},
            )
            return
        self._current_evidence_scope = loaded_scope.as_dict()
        self._evidence_quarantine = [
            dict(row)
            for row in data.get("evidence_quarantine", [])[-128:]
            if isinstance(row, dict)
        ] + self._evidence_quarantine
        self._evidence_quarantine = self._evidence_quarantine[-128:]
        self._load_into_namespace(data)
        self._delayed_reward_projection_keys = {
            str(key)
            for key in data.get("delayed_reward_projection_keys", [])
            if str(key or "")
        }
        self._band_drift_projection_keys = {
            str(key)
            for key in data.get("band_drift_projection_keys", [])[-1024:]
            if str(key or "")
        }
        self._capture_current_namespace()  # legacy/global bucket
        for plmn, ns_d in data.get("providers", {}).items():
            self._provider_namespaces[plmn] = self._new_namespace()
            self._bind_namespace(self._provider_namespaces[plmn])
            self._load_into_namespace(ns_d)
        # Restore current provider context (binds back to requested namespace)
        self._bind_namespace(self._provider_namespaces.get("", self._new_namespace()))
        self._current_provider_plmn = ""
        self.set_provider_context(data.get("provider_context", "") or "")

        log.info("Loaded journal: %d total samples across 24 hours", self._total_samples)
        # Restore decay timestamp so we don't re-decay immediately on first tick
        self._last_decay_ts = float(data.get("last_decay_ts", 0.0) or 0.0)

    def _load_into_namespace(self, data: dict) -> None:
        for h_str, hp_d in data.get("hours", {}).items():
            h = int(h_str)
            hp = self.hours[h]
            hp.samples = hp_d.get("samples", 0)
            hp.avg_sinr = hp_d.get("avg_sinr", 0)
            hp.avg_mcs = hp_d.get("avg_mcs", 0)
            hp.avg_throughput_mbps = hp_d.get("avg_throughput_mbps", 0)
            hp.avg_rtt = hp_d.get("avg_rtt", 0.0)
            hp.ca_active_ratio = hp_d.get("ca_active_ratio", 0)
            hp.nr_attach_ratio = float(hp_d.get("nr_attach_ratio", 0.0) or 0.0)
            hp.nr_sinr_avg = float(hp_d.get("nr_sinr_avg", 0.0) or 0.0)

        for h_str, perf_d in data.get("band_perf", {}).items():
            h = int(h_str)
            self.band_perf[h] = {}
            for sig, vals in perf_d.items():
                self.band_perf[h][sig] = (vals[0], vals[1])

        for h_str, outcomes in data.get("action_outcomes", {}).items():
            self.action_outcomes[int(h_str)] = outcomes

        for h_str, events in data.get("band_drift_events", {}).items():
            self.band_drift_events[int(h_str)] = events

        for h_str, rows in data.get("selection_provenance", {}).items():
            if isinstance(rows, dict):
                self.selection_provenance[int(h_str)] = {
                    str(key): dict(value)
                    for key, value in rows.items()
                    if isinstance(value, dict)
                }

    def _new_namespace(self) -> dict:
        return {
            "hours": {h: HourlyPattern(hour=h) for h in range(24)},
            "band_perf": {h: {} for h in range(24)},
            "action_outcomes": {h: [] for h in range(24)},
            "band_drift_events": {h: [] for h in range(24)},
            "selection_provenance": {h: {} for h in range(24)},
        }

    def _capture_current_namespace(self) -> None:
        self._provider_namespaces[self._current_provider_plmn] = {
            "hours": self.hours,
            "band_perf": self.band_perf,
            "action_outcomes": self.action_outcomes,
            "band_drift_events": self.band_drift_events,
            "cell_hours": self.cell_hours,
            "selection_provenance": self.selection_provenance,
        }

    def _bind_namespace(self, ns: dict) -> None:
        self.hours = ns["hours"]
        self.band_perf = ns["band_perf"]
        self.action_outcomes = ns["action_outcomes"]
        self.band_drift_events = ns["band_drift_events"]
        self.selection_provenance = ns.get(
            "selection_provenance", {h: {} for h in range(24)}
        )
        self.cell_hours = ns.get("cell_hours", {})
        self._total_samples = sum(hp.samples for hp in self.hours.values())
