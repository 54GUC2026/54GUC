"""
arch4/brain/decision_support.py
───────────────────────────────────────────────────────────────────────────────
Shared decision support helpers.

Owns low-risk helper methods reused across multiple decision mixins:
  - state-policy config access
  - throughput hold checks
  - band-action construction
  - neighbor-cell lookup
  - CA quality helpers

This file is intentionally helper-only. It does not own state-specific
decision policy.
───────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import logging
import time
from typing import Any, Optional

from arch4.interfaces import (
    LeverAction,
    LeverType,
SystemState,
    is_emergency_state,
    is_fault_state,
)
from arch4.control.manual_queue import build_authority_lever_action
from arch4.smart_loop.action_availability import evaluate_action_availability
from arch4.policy.incident_action_policy import (
    incident_class_name,
    incident_requires_immediate_recovery,
)
from arch4.brain.band_action_learning_guard import (
    historical_throughput_top_quartile,
    screen_band_action,
)
from arch4.brain.ca_contract import ca_ld_stable
from arch4.brain.memory_band_learning_query_store import (
    band_learning_evidence as memory_band_learning_evidence,
    known_good_bandset as memory_known_good_bandset,
    should_hold_known_outage_hour as memory_should_hold_known_outage_hour,
)
from arch4.brain.memory_cell_knowledge_owner import (
    get_cell_snapshot as memory_get_cell_snapshot,
    iter_cell_snapshots as memory_iter_cell_snapshots,
)

log = logging.getLogger("arch4.brain.decision")


class DecisionSupportMixin:
    def _availability_scalar_probe_allowed(
        self,
        *,
        cooldown: str,
        reason_code: str,
        reason: str,
        source: str,
        now: Optional[float] = None,
    ) -> bool:
        ts = time.time() if now is None else float(now)
        state = self.posture_authority.current_posture.state
        probe = build_authority_lever_action(
            lever=LeverType.BAND_SELECTION,
            params={"_availability_probe": source},
            reason=reason,
            intent=source,
            objective_id=source,
            source="brain_availability_probe",
        )
        constraint = dict(getattr(self, "_constraint_vector", {}) or {})
        constraint["health_status"] = dict(getattr(self, "_health_status", {}) or {})
        constraint["incident_assessment"] = dict(getattr(self, "_incident_assessment", {}) or {})
        verdict = evaluate_action_availability(
            action=probe,
            now=ts,
            state_name=getattr(state, "name", str(state or "")),
            is_fault_state=is_fault_state(state),
            is_emergency_state=is_emergency_state(state),
            constraint=constraint,
            radio_stability_hold_until=float(
                self.action_source_state_authority.band_action_policy_status(
                    now=ts
                ).get("radio_stability_hold_until_ts", 0.0)
                or 0.0
            ),
            cooldowns=getattr(self, "cooldowns", None),
            objective_feedback_keys=(),
            objective_feedback_reason={},
            scalar_cooldowns=[{
                "name": cooldown,
                "reason_code": reason_code,
                "reason": reason,
            }],
        )
        if bool(verdict.get("allowed", False)):
            return True
        record = getattr(self, "_record_action_availability_block", None)
        if callable(record):
            record(action=probe, verdict=verdict, source=source)
        return False

    def _emit_brain_posture_deferred(
        self,
        *,
        lane: str,
        reason: str,
        state: str = "",
        posture: str = "",
        detail: str = "",
    ) -> None:
        self._emit_runtime_event(
            "brain_posture_deferred",
            lane=str(lane or ""),
            reason=str(reason or ""),
            state=str(state or ""),
            posture=str(posture or ""),
            detail=str(detail or ""),
        )

    def _note_brain_posture_deferred(
        self,
        *,
        lane: str,
        reason: str,
        verdict: str = "hold",
        summary: str = "",
        state: str = "",
        posture: str = "",
        detail: str = "",
    ) -> None:
        reason_text = str(reason or "").strip()
        summary_text = str(summary or "").strip() or "TSAR deferred before creating a proposal."
        self._emit_brain_posture_deferred(
            lane=lane,
            reason=reason_text,
            state=state,
            posture=posture,
            detail=detail,
        )
        self._note_tsar_soft_no_go(
            verdict=verdict,
            reason=reason_text,
            summary=summary_text,
        )

    def _state_policy_config(self):
        return getattr(self, "decision_state_cfg", self.cfg)

    def _throughput_hold_active(
        self,
        modem: CellularIntelligenceGeneration,
        computed: Optional["DerivedSignalFrames"] = None,
    ) -> bool:
        """True when current throughput is good enough that probing is not needed.

        Decision logic (in order):
          1. If disabled in config → never hold
          2. If exploration pressure is high enough (net > 0.45) → override hold,
             something is deteriorating and the brain should act
          3. LTM-percentile: if current DL is in the top 25% of what LTM has
             seen at this hour in this cell context → hold (nothing to gain)
          4. Fallback: use fixed 60 Mbps threshold (no LTM data yet)

        This replaces the old fixed-threshold model. The brain holds when it
        has already achieved near-optimal throughput for the current conditions,
        not when it crosses an arbitrary absolute number.
        """
        cfg = self._state_policy_config()
        soft = getattr(self, "_thresholds", {}).get("soft", {})
        if hasattr(self, "cfg"):
            enabled = bool(self.cfg.v5_good_dl_hold_enabled)
        elif "v5_good_dl_hold_enabled" in soft:
            enabled = bool(soft["v5_good_dl_hold_enabled"])
        else:
            enabled = bool(cfg.v5_good_dl_hold_enabled)
        if not enabled:
            return False

        current_dl = float(modem.traffic.dl_mbps)

        ep = getattr(self, "_exploration_pressure", None)
        if ep is not None and ep.net_pressure > 0.45:
            return False

        hour = time.localtime().tm_hour
        ltm_top_25 = self._ltm_throughput_top_quartile(hour)
        if ltm_top_25 is not None:
            return current_dl >= ltm_top_25

        target = float(soft.get("v5_good_dl_hold_mbps", cfg.v5_good_dl_hold_mbps))
        return current_dl >= max(0.1, target)

    def _ltm_throughput_top_quartile(self, hour: int) -> Optional[float]:
        """Return the top-25th-percentile throughput from LTM at this hour."""
        return historical_throughput_top_quartile(self.historical, hour=hour)

    def _make_band_action(
        self,
        *,
        bands,
        reason: str,
        intent: str,
        objective_id: str = "",
        params: Optional[dict] = None,
        consult_feedback: bool = True,
        modem: Optional[CellularIntelligenceGeneration] = None,
    ) -> Optional[LeverAction]:
        """Build BAND_SELECTION action with centralized proposer feedback consult."""
        norm = []
        seen = set()
        for b in (bands or []):
            try:
                bi = int(b)
            except (TypeError, ValueError):
                continue
            if bi <= 0 or bi in seen:
                continue
            seen.add(bi)
            norm.append(bi)
        if not norm:
            return None

        if not self._band_selection_supported_for_proposals():
            self._emit_runtime_event(
                "proposal_suppressed",
                gate="unsupported_lever",
                intent=intent,
                bands=list(norm),
                reason="band_selection_not_supported_by_modem_profile",
            )
            return None

        action_params = dict(params or {})
        if (
            modem is not None
            and not bool(action_params.get("forced_sequence"))
        ):
            now = time.time()
            hour = time.localtime(now).tm_hour
            learned_guard = screen_band_action(
                self.memory,
                hour=hour,
                bands=norm,
                now=now,
                current_sinr=float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0),
            )
            if learned_guard.blocked:
                self._emit_runtime_event(
                    "learning_band_deferred",
                    intent=intent,
                    bands=list(norm),
                    reason=str(learned_guard.reason or ""),
                    hour=int(hour),
                )
                return None
            action_params["_brain_learning_screened"] = True

        action_params["bands"] = list(norm)
        action = build_authority_lever_action(
            lever=LeverType.BAND_SELECTION,
            params=action_params,
            reason=reason,
            intent=intent,
            objective_id=objective_id or self._objective_id_for_bands(intent or "bands", norm),
            source="brain_proposal_band_selection",
        )
        return self._proposal_action_available(
            action,
            modem=modem,
            source="proposal:band_selection",
            check_objective=bool(consult_feedback and intent),
        )

    def _make_restore_ca_action(
        self,
        modem: CellularIntelligenceGeneration,
        *,
        reason: str,
        source: str = "restore_ca",
        intent: str = "restore_ca",
        params: Optional[dict] = None,
    ) -> Optional[LeverAction]:
        """Build the single canonical restore-CA action candidate."""

        memory = getattr(self, "memory", None)
        target = memory_known_good_bandset(memory) if memory is not None else None
        norm: list[int] = []
        seen: set[int] = set()
        for raw in list(target or []):
            try:
                band = int(raw)
            except (TypeError, ValueError):
                continue
            if band <= 0 or band in seen:
                continue
            seen.add(band)
            norm.append(band)
        if len(norm) <= 1:
            return None

        if self._is_routeros_family_profile():
            self._restore_ca_routeros_passive_suppressed(
                modem,
                source=source,
                reason="RouterOS band writes drop LTE link; restore CA stays passive",
                target_bands=norm,
            )
            return None

        objective_id = self._objective_id_restore_ca(norm)
        return self._make_band_action(
            bands=norm,
            reason=reason,
            intent=str(intent or "restore_ca"),
            objective_id=objective_id,
            params=dict(params or {"sequential": True}),
            modem=modem,
        )

    def _restore_ca_routeros_passive_suppressed(
        self,
        modem: CellularIntelligenceGeneration,
        *,
        source: str,
        reason: str,
        target_bands: Optional[list[int]] = None,
    ) -> bool:
        """Record passive RouterOS restore-CA suppression inside the restore-CA owner."""

        if not self._is_routeros_family_profile():
            return False
        bands: list[int] = []
        seen: set[int] = set()
        for raw in list(target_bands or []):
            try:
                band = int(raw)
            except (TypeError, ValueError):
                continue
            if band <= 0 or band in seen:
                continue
            seen.add(band)
            bands.append(band)
        if not bands:
            for carrier in list(getattr(modem.cellular, 'carriers', []) or []):
                try:
                    band = int(getattr(carrier, "band", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if band > 0 and band not in seen:
                    seen.add(band)
                    bands.append(band)
        if not bands:
            memory = getattr(self, "memory", None)
            target = memory_known_good_bandset(memory) if memory is not None else None
            for raw in list(target or []):
                try:
                    band = int(raw)
                except (TypeError, ValueError):
                    continue
                if band > 0 and band not in seen:
                    seen.add(band)
                    bands.append(band)
        if len(bands) <= 1:
            return False
        band_sig = "+".join(str(band) for band in sorted(bands))
        if not self.cooldowns.is_blocked_keyed("restore_ca_routeros_suppressed", band_sig):
            self.cooldowns.set_keyed("restore_ca_routeros_suppressed", band_sig, hold_s=300.0)
            self._emit_runtime_event(
                "restore_ca_routeros_suppressed",
                band_sig=band_sig,
                active_band_sig=band_sig,
                source=str(source or "restore_ca"),
                reason=str(reason or "RouterOS band writes drop LTE link; restore CA stays passive"),
            )
        return True

    def _make_pband_action(
        self,
        *,
        pband: int,
        ca_bands,
        reason: str,
        intent: str,
        objective_id: str = "",
        consult_feedback: bool = True,
        modem: Optional[CellularIntelligenceGeneration] = None,
    ) -> Optional[LeverAction]:
        try:
            pband_i = int(pband)
        except (TypeError, ValueError):
            return None
        if pband_i <= 0:
            return None

        norm_dl = []
        seen = {pband_i}
        for raw_band in list(ca_bands or []):
            try:
                band_i = int(raw_band)
            except (TypeError, ValueError):
                continue
            if band_i <= 0 or band_i in seen:
                continue
            seen.add(band_i)
            norm_dl.append(band_i)
        requested_pool = [pband_i, *norm_dl]
        ca_pool = sorted(set(requested_pool))
        action_key = ""
        if len(ca_pool) > 1:
            pool_sig = "+".join(f"B{band}" for band in sorted(ca_pool))
            action_key = f"pband_ca:B{pband_i}:pool:{pool_sig}"

        if not self._pband_supported_for_proposals():
            self._emit_runtime_event(
                "proposal_suppressed",
                gate="unsupported_lever",
                intent=intent,
                bands=list(requested_pool),
                reason="pband_not_supported_by_modem_profile",
            )
            return None

        action = build_authority_lever_action(
            lever=LeverType.PBAND,
            params={
                "pband": pband_i,
                "ca_pool": list(ca_pool),
                "_pband_action_key": action_key,
                # Legacy payload aliases remain for older queued/runtime records.
                "dl_bands": list(norm_dl),
            },
            reason=reason,
            intent=intent,
            objective_id=objective_id or action_key or self._objective_id_for_bands(intent or "pband", requested_pool),
            source="brain_proposal_pband",
        )
        return self._proposal_action_available(
            action,
            modem=modem,
            source="proposal:pband",
            check_objective=bool(consult_feedback and intent),
        )

    def _make_cell_lock_action(
        self,
        *,
        pci,
        earfcn=None,
        unlock: bool = False,
        reason: str,
        intent: str = "",
        objective_id: str = "",
        params: Optional[dict] = None,
        consult_feedback: bool = True,
    ) -> Optional[LeverAction]:
        action_params = dict(params or {})
        if unlock:
            action_params["unlock"] = True
        else:
            try:
                pci_i = int(pci)
                earfcn_i = int(earfcn)
            except (TypeError, ValueError):
                return None
            if pci_i <= 0 or earfcn_i <= 0:
                return None
            action_params["pci"] = pci_i
            action_params["earfcn"] = earfcn_i

        action = build_authority_lever_action(
            lever=LeverType.CELL_LOCK,
            params=action_params,
            reason=reason,
            intent=intent,
            objective_id=objective_id,
            source="brain_proposal_cell_lock",
        )
        return self._proposal_action_available(
            action,
            modem=None,
            source="proposal:cell_lock",
            check_objective=bool(consult_feedback and objective_id),
        )

    def _band_selection_supported_for_proposals(self) -> bool:
        """Return whether brain policy may even propose BAND_SELECTION."""
        constraints = getattr(self, "constraints", None)
        if constraints is not None and hasattr(constraints, "band_select_available"):
            return bool(getattr(constraints, "band_select_available"))

        capabilities = getattr(self, "capabilities", None)
        if capabilities is not None and hasattr(capabilities, "supports_band_selection"):
            return bool(getattr(capabilities, "supports_band_selection"))

        return False

    def _pband_supported_for_proposals(self) -> bool:
        constraints = getattr(self, "constraints", None)
        if constraints is not None and hasattr(constraints, "supports_pband"):
            return bool(getattr(constraints, "supports_pband"))

        capabilities = getattr(self, "capabilities", None)
        if capabilities is not None and hasattr(capabilities, "supports_pband"):
            return bool(getattr(capabilities, "supports_pband"))

        return self._band_selection_supported_for_proposals()

    def _find_best_neighbor_cell(self, modem: CellularIntelligenceGeneration) -> Optional[tuple]:
        """Find best alternative cell from neighbor list or memory."""
        if modem.cellular.neighbors:
            candidates = [
                n for n in modem.cellular.neighbors
                if n.pci != modem.cellular.pci
                and n.pci > 0
                and -140 <= n.rsrp_dbm <= -44
                and n.rsrp_dbm > -110
            ]
            if candidates:
                best = max(candidates, key=lambda n: n.rsrp_dbm)
                return (best.pci, best.earfcn)

        current_pci = modem.cellular.pci
        known_cells = [
            (pci, ck) for pci, ck in memory_iter_cell_snapshots(self.memory)
            if pci != current_pci and ck.avg_throughput > 0
        ]
        if known_cells:
            def _cell_score(pci_ck: tuple) -> float:
                _, ck = pci_ck
                # Prefer structural band/cell quality once the BQ lane is live.
                # During transition, fall back to legacy CQ memory, then throughput.
                base = (
                    float(getattr(ck, "avg_bq_cell", 0.0) or 0.0)
                    or float(getattr(ck, "avg_cq_cell", 0.0) or 0.0)
                    or float(getattr(ck, "avg_throughput", 0.0) or 0.0)
                )
                # Bandwidth bonus: 20 MHz cell scores higher than 10 MHz.
                # Normalised: 20 MHz = 1.0, 10 MHz = 0.5, 5 MHz = 0.25.
                # Max bonus ~10% — quality still dominates.
                bw = float(getattr(ck, "observed_bw_mhz", 0.0) or 0.0)
                bw_bonus = 0.10 * min(1.0, bw / 20.0) if bw > 0.0 else 0.0
                # NR tiebreaker: attach rate + NR SINR quality.
                # Zero on 4G-only hardware (nr_total_count stays 0).
                # Max bonus ~15% — LTE quality always dominates.
                if ck.nr_total_count >= 5:
                    nr_attach_rate = ck.nr_attach_count / ck.nr_total_count
                    nr_sinr_bonus = ck.nr_sinr_ema / 30.0  # normalise: 30 dB = 1.0
                    nr_bonus = 0.10 * nr_attach_rate + 0.05 * min(1.0, nr_sinr_bonus)
                else:
                    nr_bonus = 0.0
                return base * (1.0 + bw_bonus + nr_bonus)

            best_pci, best_ck = max(known_cells, key=_cell_score)
            return (best_pci, best_ck.earfcn)

        return None

    def _has_good_ca_quality(self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames | None = None) -> bool:
        """Good CA quality means load-qualified CA is stable with ≥20 MHz primary BW."""
        if self._ca_lost_state:
            return False
        if computed is None or not ca_ld_stable(computed):
            return False
        pcell = next((c for c in modem.cellular.carriers if c.is_pcell), None)
        if pcell is None:
            return False
        return float(pcell.bandwidth_mhz or 0.0) >= 20.0

    # ── NR band preference constants ──────────────────────────────────────
    _NR_SINR_USEFUL_DB: float = 10.0     # 16QAM solid — NR clearly contributing
    _NR_SINR_MARGINAL_DB: float = 3.0    # QPSK low — NR barely worth the cost
    _NR_ATTACH_RATE_GOOD: float = 0.5    # >= 50% attachment to prefer a band
    _NR_ATTACH_RATE_BAD: float = 0.2     # < 20% → band not working on this cell
    _NR_MIN_OBS_TICKS: int = 150         # ~5 min at 2s/tick before any decision
    _NR_ROLLBACK_TIME_S: float = 1800.0  # 30 min max before rollback
    _NR_ROLLBACK_SINR_DB: float = 0.0    # SINR below this → immediate rollback
    _NR_ROLLBACK_BAD_TICKS: int = 10     # consecutive bad SINR ticks to trigger
    _NR_COOLDOWN_TICKS: int = 150        # ~5 min cooldown after manual override
    _NR_CAPACITY_BANDS: frozenset = frozenset({7, 78})
    _NR_COVERAGE_BANDS: frozenset = frozenset({28})
    _NR_ALL_TARGET_BANDS: list = [7, 28, 78]

    def _evaluate_nr_band_preference(
        self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames
    ) -> Optional[LeverAction]:
        """Deleted direct selector; canonical NR policy is a source producer."""
        raise RuntimeError("old_nr_preference_action_selector_deleted")

    def _nr_lock_action(self, target_state: str, reason: str) -> LeverAction:
        raise RuntimeError("old_nr_preference_action_materializer_deleted")

    # ── Cell optimization constants ───────────────────────────────────────
    _CELL_OPT_MIN_CELLS: int = 2           # need at least 2 cells with data
    _CELL_OPT_MIN_TICKS: int = 120         # ~10 min per cell before comparing
    _CELL_OPT_MIN_SCORE_GAP: float = 0.15  # 15% better to justify switch
    _CELL_OPT_COOLDOWN_S: float = 1800.0   # 30 min between cell opt actions
    _CELL_OPT_MAX_TARGET_AGE_S: float = 900.0  # target must be seen in the last 15 min
    _CELL_OPT_RF_CLIFF_COOLDOWN_S: float = 600.0  # hold exploration for 10 min after BAD_RF entry
    _KNOWN_OUTAGE_CELL_SCORE_GAP_PENALTY: float = 0.15  # require +15% extra gap for cell moves in dead windows
    _KNOWN_OUTAGE_CELL_RSRP_DELTA_PENALTY_DB: float = 4.0  # require +4 dB extra for explore-cell probes in dead windows
    _KNOWN_OUTAGE_SOFT_LANES: frozenset[str] = frozenset({"cell_optimization", "explore_cell_probe"})
    _ESCAPE_PROBE_ALLOWED_LANES: frozenset[str] = frozenset({"explore_band_probe"})

    def _known_outage_signal(
        self,
        modem: Optional[CellularIntelligenceGeneration],
    ) -> tuple[bool, str, int, float]:
        memory = getattr(self, "memory", None)
        if modem is None or memory is None:
            return False, "", 0, 0.0
        now = time.time()
        hour = time.localtime(now).tm_hour
        blocked, reason = memory_should_hold_known_outage_hour(
            memory,
            hour=hour,
            now=now,
            current_sinr=float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0),
        )
        return bool(blocked), str(reason or ""), int(hour), float(now)

    def _note_known_outage_signal(
        self,
        modem: Optional[CellularIntelligenceGeneration],
        *,
        lane: str,
        kind: str,
        reason: str,
        hour: int,
        now: float,
    ) -> None:
        if modem is None:
            return
        notices = getattr(self, "_known_outage_hold_notice", {})
        notice_lane = f"{kind}:{lane}"
        lane_key = f"h{int(hour):02d}|{reason}"
        last = notices.get(notice_lane, {})
        last_ts = float(last.get("ts", 0.0) or 0.0)
        if last.get("key") != lane_key or (now - last_ts) >= 1800.0:
            self._emit_runtime_event(
                kind,
                lane=lane,
                current_pci=int(getattr(modem.cellular, 'pci', 0) or 0),
                sinr_db=float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0),
                reason=reason,
            )
            notices[notice_lane] = {"key": lane_key, "ts": now}
            self._known_outage_hold_notice = notices

    def _should_hold_known_outage_window(
        self,
        modem: Optional[CellularIntelligenceGeneration],
        *,
        lane: str,
    ) -> bool:
        if lane in self._KNOWN_OUTAGE_SOFT_LANES:
            return False
        blocked, reason, hour, now = self._known_outage_signal(modem)
        notices = getattr(self, "_known_outage_hold_notice", {})
        notice_lane = f"known_outage_hold:{lane}"
        if not blocked:
            if notice_lane in notices:
                notices.pop(notice_lane, None)
                self._known_outage_hold_notice = notices
            return False
        self._note_known_outage_signal(
            modem,
            lane=lane,
            kind="known_outage_hold",
            reason=reason,
            hour=hour,
            now=now,
        )
        return True

    def _known_outage_penalty_active(
        self,
        modem: Optional[CellularIntelligenceGeneration],
        *,
        lane: str,
    ) -> bool:
        blocked, reason, hour, now = self._known_outage_signal(modem)
        notices = getattr(self, "_known_outage_hold_notice", {})
        notice_lane = f"known_outage_penalty:{lane}"
        if not blocked:
            if notice_lane in notices:
                notices.pop(notice_lane, None)
                self._known_outage_hold_notice = notices
            return False
        self._note_known_outage_signal(
            modem,
            lane=lane,
            kind="known_outage_penalty",
            reason=reason,
            hour=hour,
            now=now,
        )
        return True

    def _escape_probe_cfg(self):
        return getattr(self, "decision_pipeline_cfg", self.cfg)

    @staticmethod
    def _escape_probe_context_key(modem: Optional[CellularIntelligenceGeneration]) -> str:
        if modem is None:
            return "unknown"
        serving_band = int(getattr(modem.cellular, 'primary_band', 0) or 0)
        cell_id = int(getattr(modem.cellular, 'cell_id', 0) or 0)
        earfcn = int(getattr(modem.cellular, 'earfcn_dl', getattr(modem.cellular, 'earfcn_dl', 0)) or 0)
        ca_sig = str(getattr(modem.cellular, 'ca_band_signature', "") or "") or "-"
        return f"B{serving_band}|CID{cell_id}|E{earfcn}|{ca_sig}"

    def _prune_escape_probe_state(
        self,
        *,
        now: float,
        suppression_window_s: float,
        context_interval_s: float,
    ) -> None:
        max_keep_s = max(float(suppression_window_s), float(context_interval_s), 60.0)
        cutoff = now - max_keep_s
        events = list(getattr(self, "_exploration_suppression_events", []) or [])
        self._exploration_suppression_events = [
            (float(ts), str(gate), str(lane))
            for ts, gate, lane in events
            if float(ts) >= cutoff
        ]

        ctx_map = dict(getattr(self, "_escape_probe_last_by_context", {}) or {})
        self._escape_probe_last_by_context = {
            str(k): float(v) for k, v in ctx_map.items() if float(v) >= cutoff
        }

        pending = dict(getattr(self, "_escape_probe_pending_by_lane", {}) or {})
        cleaned: dict[str, dict[str, Any]] = {}
        for lane, meta in pending.items():
            if not isinstance(meta, dict):
                continue
            expires_at = float(meta.get("expires_at", 0.0) or 0.0)
            if expires_at > 0.0 and now > expires_at:
                continue
            cleaned[str(lane)] = dict(meta)
        self._escape_probe_pending_by_lane = cleaned

    def _record_exploration_suppression(
        self,
        *,
        gate: str,
        lane: str,
        now: float,
    ) -> None:
        events = list(getattr(self, "_exploration_suppression_events", []) or [])
        events.append((float(now), str(gate), str(lane)))
        self._exploration_suppression_events = events
        cfg = self._escape_probe_cfg()
        self._prune_escape_probe_state(
            now=float(now),
            suppression_window_s=float(getattr(cfg, "escape_probe_suppression_window_s", 7200.0) or 7200.0),
            context_interval_s=float(getattr(cfg, "escape_probe_context_interval_s", 5400.0) or 5400.0),
        )

    def _escape_probe_suppression_snapshot(
        self,
        *,
        now: float,
        window_s: float,
    ) -> tuple[int, dict[str, int]]:
        cutoff = float(now) - float(window_s)
        by_gate: dict[str, int] = {}
        total = 0
        for ts, gate, _lane in list(getattr(self, "_exploration_suppression_events", []) or []):
            if float(ts) < cutoff:
                continue
            gs = str(gate)
            by_gate[gs] = int(by_gate.get(gs, 0)) + 1
            total += 1
        return int(total), by_gate

    def _escape_probe_posture_snapshot(self) -> tuple[float, float]:
        posture = getattr(getattr(self, "aggressiveness", None), "_current_posture", None)
        if posture is None:
            return 1.0, 1.0
        quality_confidence = float(getattr(posture, "quality_confidence", 1.0) or 1.0)
        posture_modifier = float(getattr(posture, "posture_modifier", 1.0) or 1.0)
        return quality_confidence, posture_modifier

    def _maybe_arm_escape_probe(
        self,
        modem: Optional[CellularIntelligenceGeneration],
        *,
        lane: str,
        gate: str,
        now: float,
    ) -> tuple[bool, str, dict[str, Any]]:
        cfg = self._escape_probe_cfg()
        details: dict[str, Any] = {}
        if not bool(getattr(cfg, "escape_probe_enabled", True)):
            return False, "disabled", details
        if lane not in self._ESCAPE_PROBE_ALLOWED_LANES:
            return False, "lane_not_allowed", details
        if modem is None:
            return False, "missing_modem", details
        if bool(getattr(cfg, "escape_probe_require_idle", True)) and bool(
            getattr(modem.traffic, 'has_traffic', False)
        ):
            return False, "traffic_active", details

        window_s = float(getattr(cfg, "escape_probe_suppression_window_s", 7200.0) or 7200.0)
        min_suppressions = int(getattr(cfg, "escape_probe_suppression_min_count", 50) or 50)
        bypassable_share_min = float(getattr(cfg, "escape_probe_bypassable_share_min", 0.60) or 0.60)
        quality_conf_max = float(getattr(cfg, "escape_probe_quality_confidence_max", 0.50) or 0.50)
        posture_modifier_max = float(getattr(cfg, "escape_probe_posture_modifier_max", 0.70) or 0.70)
        global_interval_s = float(getattr(cfg, "escape_probe_global_interval_s", 1800.0) or 1800.0)
        context_interval_s = float(getattr(cfg, "escape_probe_context_interval_s", 5400.0) or 5400.0)

        self._prune_escape_probe_state(
            now=float(now),
            suppression_window_s=window_s,
            context_interval_s=context_interval_s,
        )

        suppression_total, by_gate = self._escape_probe_suppression_snapshot(
            now=float(now),
            window_s=window_s,
        )
        bypassable_count = int(by_gate.get("post_fault_cooldown", 0) + by_gate.get("rf_cliff_cooldown", 0))
        bypassable_share = (float(bypassable_count) / float(max(1, suppression_total)))
        quality_confidence, posture_modifier = self._escape_probe_posture_snapshot()
        context_key = self._escape_probe_context_key(modem)
        details.update(
            {
                "suppression_window_s": float(window_s),
                "suppression_total": int(suppression_total),
                "suppression_bypassable_count": int(bypassable_count),
                "suppression_bypassable_share": round(float(bypassable_share), 3),
                "quality_confidence": round(float(quality_confidence), 3),
                "posture_modifier": round(float(posture_modifier), 3),
                "context_key": str(context_key),
            }
        )
        if suppression_total < max(1, min_suppressions):
            return False, "suppression_below_threshold", details
        if bypassable_share < max(0.0, min(1.0, bypassable_share_min)):
            return False, "suppression_mix_not_bypassable", details
        if quality_confidence >= quality_conf_max:
            return False, "quality_confidence_not_low", details
        if posture_modifier >= posture_modifier_max:
            return False, "posture_not_conservative", details

        if gate == "rf_cliff_cooldown":
            min_sinr_db = float(getattr(cfg, "escape_probe_rf_cliff_sinr_min_db", 3.0) or 3.0)
            min_ticks = int(getattr(cfg, "escape_probe_rf_cliff_sinr_ticks", 2) or 2)
            current_sinr = float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0)
            good_ticks = int(getattr(self, "_escape_probe_rf_cliff_good_ticks", 0) or 0)
            details["rf_cliff_sinr_db"] = round(current_sinr, 2)
            details["rf_cliff_good_ticks"] = int(good_ticks)
            if current_sinr <= min_sinr_db:
                return False, "rf_cliff_sinr_too_low", details
            if good_ticks < max(1, min_ticks):
                return False, "rf_cliff_not_stable", details

        global_last = float(getattr(self, "_escape_probe_last_ts", 0.0) or 0.0)
        if global_interval_s > 0.0 and (now - global_last) < global_interval_s:
            details["remaining_global_s"] = max(0, int(round(global_interval_s - (now - global_last))))
            return False, "global_rate_limited", details

        context_last = float((getattr(self, "_escape_probe_last_by_context", {}) or {}).get(context_key, 0.0) or 0.0)
        if context_interval_s > 0.0 and (now - context_last) < context_interval_s:
            details["remaining_context_s"] = max(0, int(round(context_interval_s - (now - context_last))))
            return False, "context_rate_limited", details

        self._escape_probe_seq = int(getattr(self, "_escape_probe_seq", 0) or 0) + 1
        escape_probe_id = f"ep_{self._escape_probe_seq}"
        details["escape_probe_id"] = escape_probe_id
        meta = {
            "escape_probe": True,
            "escape_probe_id": str(escape_probe_id),
            "escape_reason": "guard_starvation_escape",
            "escape_bypassed_guards": [str(gate)],
            "escape_lane": str(lane),
            "escape_context_key": str(context_key),
            "escape_armed_at": float(now),
            "expires_at": float(now + max(4.0, 2.0 * float(getattr(self.cfg, "sample_interval_s", 2.0) or 2.0))),
        }
        self._escape_probe_last_ts = float(now)
        ctx_last_by_key = dict(getattr(self, "_escape_probe_last_by_context", {}) or {})
        ctx_last_by_key[str(context_key)] = float(now)
        self._escape_probe_last_by_context = ctx_last_by_key
        pending = dict(getattr(self, "_escape_probe_pending_by_lane", {}) or {})
        pending[str(lane)] = dict(meta)
        self._escape_probe_pending_by_lane = pending
        return True, "armed", details

    def _consume_escape_probe_metadata(self, lane: str) -> Optional[dict[str, Any]]:
        pending = dict(getattr(self, "_escape_probe_pending_by_lane", {}) or {})
        meta = pending.pop(str(lane), None)
        self._escape_probe_pending_by_lane = pending
        if not isinstance(meta, dict):
            return None
        expires_at = float(meta.get("expires_at", 0.0) or 0.0)
        if expires_at > 0.0 and time.time() > expires_at:
            return None
        return dict(meta)

    def _tag_escape_probe_action(
        self,
        action: Optional[LeverAction],
        *,
        lane: str,
        meta: Optional[dict[str, Any]],
    ) -> Optional[LeverAction]:
        if action is None or not isinstance(meta, dict):
            return action
        if action.lever != LeverType.BAND_SELECTION:
            self._emit_runtime_event(
                "exploration_escape_probe_discarded",
                lane=lane,
                reason="non_band_action",
                selected_lever=str(getattr(action.lever, "name", "") or ""),
                escape_probe_id=str(meta.get("escape_probe_id", "") or ""),
            )
            return action

        params = dict(action.params or {})
        params["escape_probe"] = True
        params["escape_probe_id"] = str(meta.get("escape_probe_id", "") or "")
        params["escape_reason"] = str(meta.get("escape_reason", "") or "")
        params["escape_bypassed_guards"] = list(meta.get("escape_bypassed_guards", []) or [])
        params["escape_lane"] = str(meta.get("escape_lane", "") or lane)
        params["escape_context_key"] = str(meta.get("escape_context_key", "") or "")

        reason = str(action.reason or "")
        probe_id = str(meta.get("escape_probe_id", "") or "")
        if probe_id and probe_id not in reason:
            reason = f"{reason} [escape_probe:{probe_id}]".strip()

        tagged = build_authority_lever_action(
            lever=action.lever,
            params=params,
            reason=reason,
            intent=action.intent,
            objective_id=action.objective_id,
            source="brain_escape_probe",
        )
        self._emit_runtime_event(
            "exploration_escape_probe_action",
            lane=lane,
            escape_probe_id=probe_id,
            bypassed_guards=list(params.get("escape_bypassed_guards", []) or []),
            objective_id=str(tagged.objective_id or ""),
            intent=str(tagged.intent or ""),
            bands=list(params.get("bands", []) or []),
        )
        return tagged

    def _check_exploration_hold(
        self,
        modem: Optional[CellularIntelligenceGeneration],
        *,
        lane: str,
        event_kind: str = "exploration_suppressed",
    ) -> bool:
        """Check if exploration is blocked, reading from per-tick safety verdicts.

        This is the new unified entry point for exploration hold checks.
        Reads from self._tick_safety (evaluated once at top of decide())
        instead of re-evaluating all conditions per-lane.

        For bypassable holds (rf_cliff, post_fault), only the explore_band_probe
        lane can trigger escape hatch bypass. All other lanes block uniformly
        (maximum common denominator per Joe's direction).

        Returns True if exploration should be held, False if clear to proceed.
        """
        safety = getattr(self, "_tick_safety", None)
        if safety is None:
            now = time.time()
            self._record_exploration_suppression(
                gate="tick_safety_unavailable",
                lane=lane,
                now=now,
            )
            self._emit_runtime_event(
                event_kind,
                gate="tick_safety_unavailable",
                lane=lane,
                incident_class="unknown",
                recommended_next_step="publish_tick_safety_before_exploration",
                current_pci=int(getattr(modem.cellular, 'pci', 0) or 0) if modem is not None else 0,
            )
            if hasattr(self, "_set_prediction_gate_reason"):
                self._set_prediction_gate_reason("tick_safety_unavailable")
            return True

        now = time.time()
        current_pci = int(getattr(modem.cellular, 'pci', 0) or 0) if modem is not None else 0

        # ── Known outage hold (checked first, per original order) ─────
        if safety.known_outage_hold:
            if hasattr(self, "_set_prediction_gate_reason"):
                self._set_prediction_gate_reason(str(safety.blocking_gate() or "known_outage_hold"))
            return True

        # ── Universal incident hold (no escape possible) ──────────────
        if safety.exploration_blocked:
            if self._allow_mikrotik_congestion_probe_release(
                modem=modem,
                lane=lane,
                safety=safety,
            ):
                self._emit_runtime_event(
                    "mikrotik_incident_probe_release",
                    gate=safety.incident_gate or "exploration_blocked",
                    lane=lane,
                    incident_class=safety.incident_class,
                    current_pci=current_pci,
                )
                return False
            gate = safety.incident_gate or "exploration_blocked"
            if hasattr(self, "_set_prediction_gate_reason"):
                self._set_prediction_gate_reason(str(gate))
            self._record_exploration_suppression(gate=gate, lane=lane, now=now)
            self._emit_runtime_event(
                event_kind,
                gate=gate,
                lane=lane,
                incident_class=safety.incident_class,
                recommended_next_step="",
                current_pci=current_pci,
            )
            return True

        # ── Bypassable holds (rf_cliff, post_fault) ───────────────────
        if safety.bypassable_hold_active:
            gate = safety.blocking_gate()
            posture = getattr(
                getattr(self, "posture_authority", None),
                "current_posture",
                None,
            )

            # Track RF cliff good ticks for escape hatch evaluation
            if safety.rf_cliff_hold:
                cfg = self._escape_probe_cfg()
                min_sinr = float(getattr(cfg, "escape_probe_rf_cliff_sinr_min_db", 3.0) or 3.0)
                current_sinr = float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0) if modem else 0.0
                if current_sinr > min_sinr:
                    self._escape_probe_rf_cliff_good_ticks = int(
                        getattr(self, "_escape_probe_rf_cliff_good_ticks", 0) or 0
                    ) + 1
                else:
                    self._escape_probe_rf_cliff_good_ticks = 0

            # Record suppression before attempting escape
            self._record_exploration_suppression(gate=gate, lane=lane, now=now)

            # Attempt escape hatch (only for eligible lanes)
            armed, escape_reason, escape_details = self._maybe_arm_escape_probe(
                modem, lane=lane, gate=gate, now=now
            )

            if armed:
                # Escape probe bypasses the hold
                if safety.rf_cliff_hold:
                    self._emit_runtime_event(
                        "exploration_escape_probe_bypass",
                        gate=gate,
                        lane=lane,
                        current_pci=current_pci,
                        remaining_s=max(0, int(round(safety.rf_cliff_remaining_s))),
                        rf_cliff_reason=str(
                            getattr(
                                posture,
                                "rf_cliff_event_reason",
                                "",
                            )
                            or ""
                        ),
                        **escape_details,
                    )
                else:  # post_fault_hold
                    self._emit_runtime_event(
                        "exploration_escape_probe_bypass",
                        gate=gate,
                        lane=lane,
                        current_pci=current_pci,
                        post_fault_class=safety.post_fault_class,
                        remaining_s=max(0, int(round(safety.post_fault_remaining_s))),
                        **escape_details,
                    )
                return False  # Escape armed, proceed with exploration

            # No escape — emit suppression event
            if hasattr(self, "_set_prediction_gate_reason"):
                self._set_prediction_gate_reason(str(gate))
            if safety.rf_cliff_hold:
                self._emit_runtime_event(
                    event_kind,
                    gate=gate,
                    lane=lane,
                    current_pci=current_pci,
                    remaining_s=max(0, int(round(safety.rf_cliff_remaining_s))),
                    rf_cliff_reason=str(
                        getattr(posture, "rf_cliff_event_reason", "")
                        or ""
                    ),
                    escape_probe_candidate=False,
                    escape_probe_reason=str(escape_reason),
                    **escape_details,
                )
            else:  # post_fault_hold
                self._emit_runtime_event(
                    event_kind,
                    gate=gate,
                    lane=lane,
                    current_pci=current_pci,
                    post_fault_class=safety.post_fault_class,
                    remaining_s=max(0, int(round(safety.post_fault_remaining_s))),
                    escape_probe_candidate=False,
                    escape_probe_reason=str(escape_reason),
                    **escape_details,
                )
            return True

        # Reset RF cliff good ticks when no RF cliff hold
        self._escape_probe_rf_cliff_good_ticks = 0

        return False

    def _allow_mikrotik_congestion_probe_release(
        self,
        *,
        modem: Optional[CellularIntelligenceGeneration],
        lane: str,
        safety: Any,
    ) -> bool:
        del modem, lane, safety
        return False

    def _mikrotik_probe_release_missing_learning(
        self,
        modem: Optional[CellularIntelligenceGeneration],
    ) -> bool:
        if modem is None:
            return False
        norm = self._current_active_bands(modem)
        if not norm:
            return False
        memory = getattr(self, "memory", None)
        if memory is None:
            return False
        try:
            hour = time.localtime().tm_hour
            evidence = dict(memory_band_learning_evidence(memory, hour=hour, bands=norm) or {})
        except Exception:
            return False
        stm_attempts = float(evidence.get("stm_attempts", 0.0) or 0.0)
        ltm_attempts = float(evidence.get("ltm_attempts", 0.0) or 0.0)
        return stm_attempts <= 0.0 and ltm_attempts <= 0.0

    def _stable_link_for_best_effort_cell_opt(
        self,
        modem: CellularIntelligenceGeneration,
        computed: Optional[DerivedSignalFrames],
    ) -> bool:
        if computed is None or not bool(getattr(modem.cellular, 'radio_connected', False)):
            return False
        rtt_warning_ms = float(getattr(self.cfg, "rtt_warning_ms", 150.0) or 150.0)
        stable_cq = float(getattr(self.cfg, "bouncer_stable_cq_threshold", 0.65) or 0.65)
        stable_sinr = float(getattr(self.cfg, "bouncer_stable_sinr_db", 8.0) or 8.0)
        return (
            float(getattr(computed.latency, 'rtt_sliding_avg', 0.0) or 0.0) > 0.0
            and float(getattr(computed.latency, 'rtt_sliding_avg', 0.0) or 0.0) <= rtt_warning_ms
            and float(getattr(computed.quality, 'cq_cell', 0.0) or 0.0) >= stable_cq
            and float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0) >= stable_sinr
        )

    def _best_effort_huawei_cell_opt(self) -> bool:
        from arch4.operator_profiles import FAMILY_HUAWEI_HILINK, infer_backend_family
        backend = str(getattr(self.cfg, "modem_backend", "") or "").strip().lower()
        family = infer_backend_family(backend)
        return (
            not bool(getattr(self.constraints, "pci_lock_available", False))
            and family == FAMILY_HUAWEI_HILINK
        )

    def _evaluate_cell_optimization(
        self, modem: CellularIntelligenceGeneration, computed: DerivedSignalFrames
    ) -> Optional[LeverAction]:
        """Evaluate whether a cell switch would improve quality.

        Uses accumulated cell knowledge (CQ, bandwidth, NR attach rate) to
        identify if a better cell exists than the current serving cell.

        Two execution paths:
          - supports_cell_lock → CELL_LOCK action (MC888/G5B: deterministic)
          - no cell lock → BAND_SELECTION with hook-band trick (B818: best effort)

        Hook-band trick (no cell lock):
          Find a band the target cell supports but current cell doesn't.
          Lock to that band only → modem must attach to the target cell.
          Then expand band set → modem keeps the anchor, adds CA.

        State (on self):
          _cell_opt_cooldown_until: timestamp — don't re-evaluate before this
          _cell_opt_last_target_pci: last PCI we optimized toward
        """
        if not hasattr(self, "_cell_opt_cooldown_until"):
            self._cell_opt_last_target_pci: int = 0

        now = time.time()
        if not self._availability_scalar_probe_allowed(
            cooldown="cell_opt",
            reason_code="cell_opt",
            reason="cell optimization cooldown active",
            source="cell_optimization",
            now=now,
        ):
            return None

        current_pci = int(getattr(modem.cellular, 'pci', 0) or 0)
        if current_pci <= 0:
            return None

        known_outage_penalty = self._known_outage_penalty_active(
            modem,
            lane="cell_optimization",
        )

        if self._check_exploration_hold(
            modem,
            lane="cell_optimization",
            event_kind="cell_opt_suppressed",
        ):
            return None

        # Gather cells with enough observation data
        scored_cells = []
        for pci, ck in memory_iter_cell_snapshots(self.memory):
            if ck.times_served < self._CELL_OPT_MIN_TICKS:
                continue
            if pci != current_pci:
                last_seen = float(getattr(ck, "last_seen", 0.0) or 0.0)
                target_age_s = (now - last_seen) if last_seen > 0.0 else float("inf")
                if target_age_s > self._CELL_OPT_MAX_TARGET_AGE_S:
                    continue
            score = self._cell_score_for_opt(ck)
            scored_cells.append((pci, ck, score))

        if len(scored_cells) < self._CELL_OPT_MIN_CELLS:
            return None

        # Find current cell score
        current_entry = next(
            ((p, ck, s) for p, ck, s in scored_cells if p == current_pci),
            None,
        )
        if current_entry is None:
            return None
        current_score = current_entry[2]
        if current_score <= 0.0:
            return None

        # Find best alternative
        alternatives = [
            (p, ck, s) for p, ck, s in scored_cells if p != current_pci
        ]
        if not alternatives:
            return None

        best_pci, best_ck, best_score = max(alternatives, key=lambda x: x[2])
        best_last_seen = float(getattr(best_ck, "last_seen", 0.0) or 0.0)
        best_last_seen_age_s = (now - best_last_seen) if best_last_seen > 0.0 else float("inf")

        # Require significant gap
        gap = (best_score - current_score) / max(current_score, 0.001)
        min_gap = float(self._CELL_OPT_MIN_SCORE_GAP)
        if known_outage_penalty:
            min_gap += float(self._KNOWN_OUTAGE_CELL_SCORE_GAP_PENALTY)
        if gap < min_gap:
            return None

        if best_last_seen_age_s > self._CELL_OPT_MAX_TARGET_AGE_S:
            self._emit_runtime_event(
                "cell_opt_suppressed",
                gate="target_stale",
                current_pci=int(current_pci),
                target_pci=int(best_pci),
                target_last_seen_age_s=float(best_last_seen_age_s),
                target_times_served=int(getattr(best_ck, "times_served", 0) or 0),
            )
            return None

        current_state = self.posture_authority.current_posture.state
        if (
            self._best_effort_huawei_cell_opt()
            and current_state == SystemState.WORKING
            and self._stable_link_for_best_effort_cell_opt(modem, computed)
        ):
            self._emit_runtime_event(
                "cell_opt_suppressed",
                gate="stable_best_effort_huawei",
                current_pci=int(current_pci),
                target_pci=int(best_pci),
                target_last_seen_age_s=float(best_last_seen_age_s),
                current_score=float(current_score),
                target_score=float(best_score),
            )
            return None

        # Don't re-target the same PCI we just tried
        if best_pci == self._cell_opt_last_target_pci:
            self.cooldowns.set("cell_opt", hold_s=self._CELL_OPT_COOLDOWN_S, now=now)
            return None

        reason = (
            f"Cell opt: PCI {best_pci} score {best_score:.3f} vs "
            f"current PCI {current_pci} score {current_score:.3f} "
            f"(+{gap * 100:.0f}%); seen {best_last_seen_age_s:.0f}s ago; "
            f"served {int(getattr(best_ck, 'times_served', 0) or 0)} ticks"
        )
        common_params = {
            "cell_opt_current_pci": int(current_pci),
            "cell_opt_target_pci": int(best_pci),
            "cell_opt_current_score": round(float(current_score), 4),
            "cell_opt_target_score": round(float(best_score), 4),
            "cell_opt_target_last_seen_age_s": round(float(best_last_seen_age_s), 1),
            "cell_opt_target_times_served": int(getattr(best_ck, "times_served", 0) or 0),
        }

        # Path 1: direct cell lock (MC888, G5B)
        if self.constraints.pci_lock_available:
            action = self._make_cell_lock_action(
                pci=best_pci,
                earfcn=best_ck.earfcn,
                params={
                    "cell_opt_best_effort": False,
                    **common_params,
                },
                reason=f"{reason}; deterministic lock",
                intent="cell_optimization",
                objective_id=f"cell_opt:PCI{best_pci}:E{best_ck.earfcn}",
            )
            if action is not None:
                self.cooldowns.set("cell_opt", hold_s=self._CELL_OPT_COOLDOWN_S, now=now)
                self._cell_opt_last_target_pci = best_pci
            return action

        # Path 2: hook-band trick (B818 — no cell lock)
        hook_band = self._find_hook_band(current_pci, best_pci, best_ck)
        if hook_band:
            action = self._make_band_action(
                bands=[hook_band],
                params={
                    "bands": [hook_band],
                    "cell_opt_target_pci": best_pci,
                    "cell_opt_best_effort": True,
                    "cell_opt_hook_band": int(hook_band),
                    **common_params,
                },
                reason=f"{reason}; hook band B{hook_band} to steer toward PCI {best_pci} [best effort]",
                intent="cell_optimization_hook",
                objective_id=f"cell_opt_hook:PCI{best_pci}:B{hook_band}",
                modem=modem,
            )
            if action is not None:
                self.cooldowns.set("cell_opt", hold_s=self._CELL_OPT_COOLDOWN_S, now=now)
                self._cell_opt_last_target_pci = best_pci
            return action

        # No hook band found — try simple band bounce as last resort
        target_band = best_ck.band
        if target_band > 0 and target_band != modem.cellular.primary_band:
            action = self._make_band_action(
                bands=[target_band],
                params={
                    "bands": [target_band],
                    "cell_opt_target_pci": best_pci,
                    "cell_opt_best_effort": True,
                    "cell_opt_hook_band": int(target_band),
                    **common_params,
                },
                reason=f"{reason}; bounce to B{target_band} (target cell primary band) [best effort]",
                intent="cell_optimization_bounce",
                objective_id=f"cell_opt_bounce:PCI{best_pci}:B{target_band}",
                modem=modem,
            )
            if action is not None:
                self.cooldowns.set("cell_opt", hold_s=self._CELL_OPT_COOLDOWN_S, now=now)
                self._cell_opt_last_target_pci = best_pci
            return action

        return None

    @staticmethod
    def _cell_score_for_opt(ck) -> float:
        """Score a cell for optimization comparison.

        Same logic as _cell_score in _find_best_neighbor_cell but callable
        with just a CellKnowledge object (no tuple unpacking).
        """
        base = (
            float(getattr(ck, "avg_bq_cell", 0.0) or 0.0)
            or float(getattr(ck, "avg_cq_cell", 0.0) or 0.0)
            or float(getattr(ck, "avg_throughput", 0.0) or 0.0)
        )
        # Bandwidth bonus
        bw = float(getattr(ck, "observed_bw_mhz", 0.0) or 0.0)
        bw_bonus = 0.10 * min(1.0, bw / 20.0) if bw > 0.0 else 0.0
        # NR bonus
        if ck.nr_total_count >= 5:
            nr_attach_rate = ck.nr_attach_count / ck.nr_total_count
            nr_sinr_bonus = ck.nr_sinr_ema / 30.0
            nr_bonus = 0.10 * nr_attach_rate + 0.05 * min(1.0, nr_sinr_bonus)
        else:
            nr_bonus = 0.0
        return base * (1.0 + bw_bonus + nr_bonus)

    @staticmethod
    def _parse_bands_from_signature(sig: str) -> set:
        """Extract band numbers from a CA signature like 'B1+B3+B20'."""
        bands = set()
        for part in str(sig or "").replace("B", "").split("+"):
            try:
                b = int(part.strip())
                if b > 0:
                    bands.add(b)
            except (ValueError, TypeError):
                continue
        return bands

    def _find_hook_band(self, current_pci: int, target_pci: int, target_ck) -> Optional[int]:
        """Find a band the target cell supports but current cell doesn't.

        If such a band exists, locking to it forces the modem off the
        current cell and onto a cell that supports that band — hopefully
        the target. After settling, the band set can be expanded.

        Returns the hook band number, or None if no unique band exists.
        """
        current_ck = memory_get_cell_snapshot(self.memory, current_pci)
        if current_ck is None:
            return None

        # Build band sets from CA signatures and primary bands
        current_bands = self._parse_bands_from_signature(
            current_ck.best_ca_signature
        )
        current_bands.add(current_ck.band)

        target_bands = self._parse_bands_from_signature(
            target_ck.best_ca_signature
        )
        target_bands.add(target_ck.band)

        # Bands unique to target cell — not supported by current cell
        unique_to_target = target_bands - current_bands
        if not unique_to_target:
            return None

        # Prefer mid-capacity bands (B1, B3, B7) over coverage (B20, B28)
        preferred_order = [1, 3, 7, 20, 28, 8, 32, 38, 40, 41, 42, 43]
        for b in preferred_order:
            if b in unique_to_target:
                return b

        # Fall back to any unique band
        return min(unique_to_target)
