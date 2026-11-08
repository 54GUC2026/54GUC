"""
arch4/signal_processing/scheduler_processor.py
═══════════════════════════════════════════════════════════════
Scheduler Processor — GUI-only scheduler field normalizer.

Scheduler fields are not Brain decision, learning, prediction, congestion, or
quality inputs. They are normalized for display/export only when a modem
reports a trustworthy native scheduler source.

Runtime profile chooses one scheduler authority:
  - cqi  : native CQI reported by modem
  - mcs  : native MCS reported by modem
  - dual : both reported (prefer valid MCS, otherwise CQI)
  - none : no trustworthy native scheduler source

Approved bucket tables:
  CQI -> QAM -> representative MCS
    1-3   -> QPSK   -> 4
    4-6   -> 16QAM  -> 7
    7-11  -> 64QAM  -> 15
    12-15 -> 256QAM -> 24

  MCS -> QAM -> representative CQI
    4      -> QPSK   -> 2
    5-10   -> 16QAM  -> 5
    11-19  -> 64QAM  -> 9
    20-27  -> 256QAM -> 14
    0-3    -> invalid / ignore / hold previous

Missing-data policy:
  - Missing or invalid CQI/MCS stays unavailable. No synthetic hold/decay lane.
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

PROVENANCE_CQI_REPORTED = "cqi_reported"
PROVENANCE_MCS_REPORTED = "mcs_reported"
PROVENANCE_DUAL_CQI = "dual_cqi"
PROVENANCE_DUAL_MCS = "dual_mcs"
PROVENANCE_UNAVAILABLE = "unavailable"

TRUST_HIGH = "high"
TRUST_NONE = "none"

_LEVEL_TO_QAM = {
    1: 2,  # QPSK
    2: 4,  # 16QAM
    3: 6,  # 64QAM
    4: 8,  # 256QAM
}
_LEVEL_TO_CQI = {
    1: 2,
    2: 5,
    3: 9,
    4: 14,
}
_LEVEL_TO_MCS = {
    1: 4,
    2: 7,
    3: 15,
    4: 24,
}


@dataclass(frozen=True)
class SchedulerLane:
    qamt: int
    cqit: int
    mcst: int
    provenance: str
    trust: str

    @property
    def is_valid(self) -> bool:
        return self.trust != TRUST_NONE and self.qamt in {2, 4, 6, 8}


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _level_from_cqi(cqi: int) -> int:
    q = _safe_int(cqi)
    if 1 <= q <= 3:
        return 1
    if 4 <= q <= 6:
        return 2
    if 7 <= q <= 11:
        return 3
    if 12 <= q <= 15:
        return 4
    return 0


def _level_from_mcs(mcs: int) -> int:
    m = _safe_int(mcs)
    if m == 4:
        return 1
    if 5 <= m <= 10:
        return 2
    if 11 <= m <= 19:
        return 3
    if 20 <= m <= 27:
        return 4
    return 0


def _lane_from_level(level: int, provenance: str, trust: str) -> SchedulerLane:
    qam = int(_LEVEL_TO_QAM.get(level, 0))
    cqi = int(_LEVEL_TO_CQI.get(level, 0))
    mcs = int(_LEVEL_TO_MCS.get(level, 0))
    return SchedulerLane(
        qamt=qam,
        cqit=cqi,
        mcst=mcs,
        provenance=str(provenance or PROVENANCE_UNAVAILABLE),
        trust=str(trust or TRUST_NONE),
    )


class SchedulerProcessor:
    def __init__(self) -> None:
        self._last_level: int = 0
        self._cqi_missing_ticks: int = 0
        self._last_mode: str = "none"

    def _reset_if_mode_changed(self, mode: str) -> None:
        mode = str(mode or "none").strip().lower()
        if mode != self._last_mode:
            self._last_level = 0
            self._cqi_missing_ticks = 0
            self._last_mode = mode

    def _update_from_cqi(self, cqi: int, *, direct_provenance: str) -> SchedulerLane:
        level = _level_from_cqi(cqi)
        if level > 0:
            self._last_level = level
            self._cqi_missing_ticks = 0
            return _lane_from_level(level, direct_provenance, TRUST_HIGH)

        self._cqi_missing_ticks += 1
        return _lane_from_level(0, PROVENANCE_UNAVAILABLE, TRUST_NONE)

    def _update_from_mcs(self, mcs: int, *, direct_provenance: str) -> SchedulerLane:
        level = _level_from_mcs(mcs)
        if level > 0:
            self._last_level = level
            self._cqi_missing_ticks = 0
            return _lane_from_level(level, direct_provenance, TRUST_HIGH)

        return _lane_from_level(0, PROVENANCE_UNAVAILABLE, TRUST_NONE)

    def update(
        self,
        *,
        cqi: int,
        mcs_dl: int,
        cs: Any,
        mode: str,
    ) -> SchedulerLane:
        mode = str(mode or "none").strip().lower()
        if mode not in {"none", "cqi", "mcs", "dual"}:
            raise ValueError(f"scheduler_matrix_mode_invalid:{mode}")
        self._reset_if_mode_changed(mode)

        cqi_val = _safe_int(cqi)
        mcs_val = _safe_int(mcs_dl)

        if mode == "cqi":
            lane = self._update_from_cqi(cqi_val, direct_provenance=PROVENANCE_CQI_REPORTED)
        elif mode == "mcs":
            lane = self._update_from_mcs(mcs_val, direct_provenance=PROVENANCE_MCS_REPORTED)
        elif mode == "dual":
            if _level_from_mcs(mcs_val) > 0:
                lane = self._update_from_mcs(mcs_val, direct_provenance=PROVENANCE_DUAL_MCS)
            else:
                lane = self._update_from_cqi(cqi_val, direct_provenance=PROVENANCE_DUAL_CQI)
        else:
            lane = _lane_from_level(0, PROVENANCE_UNAVAILABLE, TRUST_NONE)

        setattr(cs, "qamt", int(lane.qamt))
        setattr(cs, "cqit", int(lane.cqit))
        setattr(cs, "mcst", int(lane.mcst))
        setattr(cs, "sched_provenance", str(lane.provenance))
        setattr(cs, "sched_trust", str(lane.trust))
        return lane

    def export_state(self) -> dict[str, Any]:
        return {
            "last_level": int(self._last_level),
            "cqi_missing_ticks": int(self._cqi_missing_ticks),
            "last_mode": str(self._last_mode),
        }

    def restore_state(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            self._last_level = int(payload.get("last_level", 0) or 0)
            self._cqi_missing_ticks = int(payload.get("cqi_missing_ticks", 0) or 0)
            self._last_mode = str(payload.get("last_mode", "none") or "none")
            return True
        except Exception:
            return False


def scheduler_mode_from_matrix_trust(value: Any) -> str:
    """Resolve scheduler semantics from canonical Matrix metadata only."""

    trust = dict(value) if isinstance(value, Mapping) else {}
    usable = {"confirmed", "expected", "supported", "synthetic"}
    cqi = any(
        str(trust.get(name) or "").strip().lower() in usable
        for name in ("cqi", "cqi0")
    )
    mcs = str(trust.get("mcs_dl0") or "").strip().lower() in usable
    if cqi and mcs:
        return "dual"
    if mcs:
        return "mcs"
    if cqi:
        return "cqi"
    return "none"
