"""LearningAuthority context observation owner."""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import time
from typing import Any, Dict, Optional

from arch4.brain.calibration_authority import calibration_fact_confidence_from_handoff
from arch4.brain.learning_authority_context_builder_owner import (
    LearningContext,
    build_learning_context,
    learning_context_payload,
)
from arch4.interfaces import PolicyProfile, SystemState
from arch4.learning_gate_contract import LearningGateDecision

OWNER = "arch4.brain.learning_authority_context_owner"


def _safe_dict_snapshot(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    try:
        return dict(value)
    except RuntimeError:
        return {}


def _safe_context_stat_snapshots(value: Any) -> list[Dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    try:
        return [dict(v) for v in value.values() if isinstance(v, dict)]
    except RuntimeError:
        return []


def _missing_learning_gate() -> LearningGateDecision:
    return LearningGateDecision.from_mapping(
        {
            "learning_allowed": False,
            "gate_input_complete": False,
            "block_reason": "learning_gate_missing",
            "missing_inputs": ["learning_gate"],
        }
    )


class LearningAuthorityContextOwner:
    """Own learning context snapshots, counters, and context-shift events."""

    def __init__(self) -> None:
        self._runtime_events: list[dict] = []
        self._context_stats: Dict[str, Dict[str, Any]] = {}
        self._provider_plmn: str = ""
        self._provider_name: str = ""
        self._last_context_key: str = ""
        self._last_context: Dict[str, Any] = {}
        self._last_calibration_handoff: Dict[str, Any] = {}

    def set_provider_context(self, provider_plmn: str, provider_name: str = "") -> None:
        self._provider_plmn = str(provider_plmn or "").strip()
        self._provider_name = str(provider_name or "").strip()

    def observe_tick(
        self,
        *,
        modem: Optional[CellularIntelligenceGeneration],
        computed: Optional[DerivedSignalFrames],
        state: Optional[SystemState],
        policy_profile: Optional[PolicyProfile],
        calibration_handoff: Optional[Dict[str, Any]],
        learning_gate: Optional[Dict[str, Any]],
        memory: Any,
        learning_cfg: Any,
    ) -> None:
        if modem is None or computed is None:
            return
        handoff = calibration_handoff if isinstance(calibration_handoff, dict) else {}
        self._last_calibration_handoff = dict(handoff)
        ctx = build_learning_context(
            modem=modem,
            state=state,
            policy_profile=policy_profile,
            calibration_handoff=handoff,
            memory=memory,
            learning_cfg=learning_cfg,
            current_provider_plmn=self._provider_plmn,
            current_provider_name=self._provider_name,
        )
        key = ctx.key()
        st = self._context_stats.setdefault(
            key,
            {
                "ticks": 0,
                "blocked_ticks": 0,
                "sinr_ema": 0.0,
                "rtt_ema": 0.0,
                "cq_ema": 0.0,
                "last_ts": 0.0,
                "selection_source_counts": {},
                "selection_episode_count": 0,
                "last_selection_episode_id": "",
            },
        )
        transition = modem.radio_transition
        selection_source = str(transition.source or "attribution_unavailable")
        source_counts = dict(st.get("selection_source_counts") or {})
        source_counts[selection_source] = int(source_counts.get(selection_source, 0) or 0) + 1
        st["selection_source_counts"] = source_counts
        episode_id = str(transition.episode_id or "")
        if episode_id and episode_id != str(st.get("last_selection_episode_id") or ""):
            st["selection_episode_count"] = int(
                st.get("selection_episode_count", 0) or 0
            ) + 1
            st["last_selection_episode_id"] = episode_id
        calibration_fact_confidence = calibration_fact_confidence_from_handoff(handoff)
        gate = (
            LearningGateDecision.from_mapping(learning_gate)
            if isinstance(learning_gate, dict)
            else _missing_learning_gate()
        )
        compact_window = gate.radio_mutation_window or {}

        if not gate.learning_allowed:
            st["blocked_ticks"] = int(st.get("blocked_ticks", 0) or 0) + 1
            st["last_blocked_ts"] = float(time.time())
            should_emit = (
                key != self._last_context_key
                or bool(self._last_context.get("learning_update_allowed", True))
                or str(self._last_context.get("learning_block_reason", "") or "") != gate.block_reason
            )
            self._last_context_key = key
            self._last_context = learning_context_payload(
                ctx=ctx,
                handoff=handoff,
                learning_update_allowed=False,
                learning_block_reason=gate.block_reason,
                compact_window=compact_window,
                calibration_fact_confidence=calibration_fact_confidence,
            )
            self._last_context["radio_transition"] = {
                "source": selection_source,
                "episode_id": episode_id,
                "fresh": bool(transition.fresh),
                "lower_controller_learning_opportunity": bool(
                    transition.lower_controller_learning_opportunity
                ),
            }
            if should_emit:
                self._emit_runtime_event("learning_context_shift", **self._last_context)
            return

        st["ticks"] = int(st.get("ticks", 0)) + 1
        alpha = max(0.01, 1.0 / min(int(st["ticks"]), 200))
        st["sinr_ema"] += alpha * (float(modem.cellular.sinr_db) - float(st["sinr_ema"]))
        st["rtt_ema"] += alpha * (float(computed.latency.rtt_sliding_avg) - float(st["rtt_ema"]))
        st["cq_ema"] += alpha * (float(computed.quality.cq_cell) - float(st["cq_ema"]))
        st["last_ts"] = float(time.time())

        if key != self._last_context_key or not bool(self._last_context.get("learning_update_allowed", True)):
            self._last_context_key = key
            self._last_context = learning_context_payload(
                ctx=ctx,
                handoff=handoff,
                learning_update_allowed=True,
                learning_block_reason="",
                compact_window=compact_window,
                calibration_fact_confidence=calibration_fact_confidence,
            )
            self._last_context["radio_transition"] = {
                "source": selection_source,
                "episode_id": episode_id,
                "fresh": bool(transition.fresh),
                "lower_controller_learning_opportunity": bool(
                    transition.lower_controller_learning_opportunity
                ),
            }
            self._emit_runtime_event("learning_context_shift", **self._last_context)

    def runtime_status(self) -> Dict[str, Any]:
        context_values = _safe_context_stat_snapshots(self._context_stats)
        selection_source_counts: Dict[str, int] = {}
        for value in context_values:
            for source, count in dict(value.get("selection_source_counts") or {}).items():
                selection_source_counts[str(source)] = int(
                    selection_source_counts.get(str(source), 0) or 0
                ) + int(count or 0)
        return {
            "contexts_seen": len(context_values),
            "context_updates": sum(int(v.get("ticks", 0) or 0) for v in context_values),
            "blocked_context_updates": sum(
                int(v.get("blocked_ticks", 0) or 0) for v in context_values
            ),
            "selection_source_counts": selection_source_counts,
            "selection_episode_count": sum(
                int(v.get("selection_episode_count", 0) or 0)
                for v in context_values
            ),
            "last_context": _safe_dict_snapshot(self._last_context),
            "last_calibration_handoff": _safe_dict_snapshot(self._last_calibration_handoff),
        }

    def drain_runtime_events(self) -> list[dict]:
        out = list(self._runtime_events)
        self._runtime_events.clear()
        return out

    def _emit_runtime_event(self, kind: str, **payload: Any) -> None:
        self._runtime_events.append({"kind": kind, **payload})


__all__ = [
    "OWNER",
    "LearningAuthorityContextOwner",
    "LearningContext",
]
