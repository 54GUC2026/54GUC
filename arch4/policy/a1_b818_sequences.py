"""Retired A1 provider sequence behavior oracles.

Public producer, result-writer and guard entrypoints fail loudly. The retained
non-public algorithms document and test the mined overnight/morning gold.

A1 network pattern (Pančevo / similar sites):
  00:00-01:00  B1/B3 cells power down. Modem typically self-selects B20.
  01:00-02:00  Exploration window: brain may try up to 2 band probes
               to find a better option (e.g. distant B3 still alive).
  02:00-06:00  Night hold: keep best band from probes. If BAD_RF, force
               B20 as last action and hold it.  No further actions except
               catastrophic override (E_RADIO_DOWN, E_SESSION_RESET, etc).
  06:01        Morning sequence: B3 -> B1+B3 -> B1+B3+B20, 20 s apart.
               Unconditional for A1 -- cells are back on at 06:00.
  06:01-07:01  Post-morning hold (1 hour). STM/LTM collect data.
  07:01+       Daytime limited: allow 2 explorations, then normal operation.
"""

from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

import logging
import time
from typing import Any, Optional

from arch4.brain.band_action_learning_guard import screen_band_action
from arch4.interfaces import LeverAction, LeverType, SystemState

log = logging.getLogger("arch4.brain.decision")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_A1_EXPLORE_START_HOUR = 1
_A1_EXPLORE_END_HOUR = 2
_A1_NIGHT_HOLD_END_HOUR = 6
_A1_OVERNIGHT_EXPLORE_MAX = 2
_A1_DAYTIME_EXPLORE_MAX = 2
_A1_POST_MORNING_HOLD_S = 3600.0  # 1 hour
_A1_MORNING_PROTECT_BEFORE_S = 900.0
_A1_MORNING_PROTECT_AFTER_S = 900.0


def _cfg_float_allow_zero(engine: Any, name: str, default: float) -> float:
    raw = getattr(engine.cfg, name, default)
    if raw is None:
        return float(default)
    return float(raw)


def _a1_morning_b3_learning_block(
    engine: Any,
    modem: CellularIntelligenceGeneration,
    *,
    now: float,
) -> tuple[bool, str]:
    """Return whether memory has already confirmed B3 is a bad morning target."""
    memory = getattr(engine, "memory", None)
    if memory is None:
        return False, ""
    try:
        learned_guard = screen_band_action(
            memory,
            hour=time.localtime(now).tm_hour,
            bands=[3],
            now=now,
            current_sinr=float(getattr(modem.cellular, 'sinr_db', 0.0) or 0.0),
        )
    except Exception:
        return False, ""
    return bool(learned_guard.blocked), str(learned_guard.reason or "")


def _legacy_a1_morning_routine_guard_status_oracle(
    engine: Any,
    now: float,
) -> dict[str, Any]:
    """Return the A1 morning routine protection window for non-emergency TSAR work."""

    if not engine._is_a1_overnight_profile():
        return {"active": False}

    lt = time.localtime(now)
    start_hour = int(getattr(engine.cfg, "a1_morning_sequence_hour", 6) or 6)
    start_minute = int(getattr(engine.cfg, "a1_morning_sequence_minute", 1) or 1)
    start_tuple = (
        lt.tm_year,
        lt.tm_mon,
        lt.tm_mday,
        start_hour,
        start_minute,
        0,
        lt.tm_wday,
        lt.tm_yday,
        lt.tm_isdst,
    )
    start_ts = time.mktime(start_tuple)
    wait_s = max(0.0, _cfg_float_allow_zero(engine, "a1_morning_step_wait_s", 20.0))
    expected_complete_ts = start_ts + (3.0 * wait_s)

    st = getattr(engine, "_a1_morning", {}) or {}
    if bool(st.get("active", False)):
        expected_complete_ts = max(expected_complete_ts, float(st.get("check_at", 0.0) or 0.0))

    protect_start = start_ts - _A1_MORNING_PROTECT_BEFORE_S
    protect_end = expected_complete_ts + _A1_MORNING_PROTECT_AFTER_S
    if not (protect_start <= now <= protect_end):
        return {"active": False}

    if now < start_ts:
        phase = "pre_morning_routine"
    elif bool(st.get("active", False)):
        phase = "morning_routine_active"
    elif now <= expected_complete_ts:
        phase = "morning_routine_due"
    else:
        phase = "post_morning_routine"
    return {
        "active": True,
        "phase": phase,
        "reason": "a1_morning_routine_window",
        "protect_start_ts": protect_start,
        "routine_start_ts": start_ts,
        "expected_complete_ts": expected_complete_ts,
        "protect_end_ts": protect_end,
    }


def a1_morning_routine_guard_status(
    engine: Any,
    now: float,
) -> dict[str, Any]:
    del engine, now
    raise RuntimeError("old_provider_morning_guard_owner_deleted")


# ===================================================================
# Overnight recovery orchestrator
# ===================================================================

def _legacy_decide_a1_overnight_recovery_oracle(
    engine: Any,
    modem: CellularIntelligenceGeneration,
    now: float,
) -> tuple[Optional[LeverAction], bool]:
    """A1 overnight orchestrator -- exploration window + night hold.

    Returns (action_or_None, consumed).
    - consumed=True means the caller should NOT run generic state dispatch.
    - consumed=False means normal brain logic proceeds.
    """
    if not engine._is_a1_overnight_profile():
        return None, False

    lt = time.localtime(now)
    hour = lt.tm_hour
    day_key = time.strftime("%Y-%m-%d", lt)

    st = engine._a1_overnight
    if st["day"] != day_key:
        st.update({
            "day": day_key,
            "phase": "idle",
            "explore_count": 0,
            "best_band": [],
            "hold_until": 0.0,
            "b20_forced": False,
            "daytime_explore_count": 0,
            "daytime_explore_limit_until": 0.0,
        })

    # -- Phase transitions based on clock -----------------------------------

    # Before 01:00: not our window yet, let normal policy handle.
    if hour < _A1_EXPLORE_START_HOUR and st["phase"] == "idle":
        return None, False

    # 01:00-02:00: enter or stay in explore_window.
    if _A1_EXPLORE_START_HOUR <= hour < _A1_EXPLORE_END_HOUR:
        if st["phase"] == "idle":
            st["phase"] = "explore_window"
            st["explore_count"] = 0
            st["best_band"] = _current_bands(modem)
            engine._emit_runtime_event(
                "a1_overnight_explore_start",
                best_band=list(st["best_band"]),
            )
            log.info("A1 overnight: exploration window open (01-02), max %d probes",
                     _A1_OVERNIGHT_EXPLORE_MAX)

    # 02:00+: if still exploring, transition to night hold.
    if hour >= _A1_EXPLORE_END_HOUR and st["phase"] == "explore_window":
        _enter_night_hold(engine, st, modem, now)

    # 06:00+: night hold ends, hand off to morning sequence.
    if hour >= _A1_NIGHT_HOLD_END_HOUR and st["phase"] == "night_hold":
        st["phase"] = "done"
        log.info("A1 overnight: night hold ended at 06:00")

    # -- Phase behaviour ----------------------------------------------------

    if st["phase"] == "explore_window":
        return _handle_explore_window(engine, st, modem, now)

    if st["phase"] == "night_hold":
        return _handle_night_hold(engine, st, modem, now)

    if st["phase"] == "daytime_limited":
        return _handle_daytime_limited(engine, st, modem, now)

    # phases "idle" and "done": don't interfere.
    return None, False


def decide_a1_overnight_recovery(
    engine: Any,
    modem: CellularIntelligenceGeneration,
    now: float,
) -> tuple[Optional[LeverAction], bool]:
    del engine, modem, now
    raise RuntimeError("old_provider_overnight_action_owner_deleted")


def _current_bands(modem: CellularIntelligenceGeneration) -> list:
    bands = set()
    for c in getattr(modem.cellular, 'carriers', []) or []:
        try:
            bi = int(getattr(c, "band", 0) or 0)
        except (TypeError, ValueError):
            bi = 0
        if bi > 0:
            bands.add(bi)
    if not bands:
        pb = int(getattr(modem.cellular, 'primary_band', 0) or 0)
        if pb > 0:
            bands.add(pb)
    return sorted(bands)


def _enter_night_hold(engine: Any, st: dict, modem: CellularIntelligenceGeneration, now: float) -> None:
    st["phase"] = "night_hold"
    if not st["best_band"]:
        st["best_band"] = _current_bands(modem)
    current_state = engine.posture_authority.current_posture.state
    if current_state == SystemState.BAD_RF:
        st["b20_forced"] = True
    engine._emit_runtime_event(
        "a1_overnight_night_hold",
        explore_count=st["explore_count"],
        best_band=list(st["best_band"]),
        b20_forced=st["b20_forced"],
    )
    log.info("A1 overnight: night hold (until 06:00). probes=%d best=%s b20=%s",
             st["explore_count"], st["best_band"], st["b20_forced"])


def _handle_explore_window(
    engine: Any, st: dict, modem: CellularIntelligenceGeneration, now: float,
) -> tuple[Optional[LeverAction], bool]:
    """01-02 exploration: allow up to 2 probes then freeze."""
    if st["explore_count"] >= _A1_OVERNIGHT_EXPLORE_MAX:
        _enter_night_hold(engine, st, modem, now)
        return None, True

    # Let normal brain decide. Counting happens in note_a1_overnight_action.
    return None, False


def _handle_night_hold(
    engine: Any, st: dict, modem: CellularIntelligenceGeneration, now: float,
) -> tuple[Optional[LeverAction], bool]:
    """02-06 night hold: keep best band, force B20 if BAD_RF."""
    current_state = engine.posture_authority.current_posture.state

    # Catastrophic override: E_RADIO_DOWN / E_SESSION_RESET pass through.
    if current_state in {SystemState.E_RADIO_DOWN, SystemState.E_SESSION_RESET}:
        return None, False

    # BAD_RF during hold: force B20 once.
    if current_state == SystemState.BAD_RF and not st["b20_forced"]:
        st["b20_forced"] = True
        available = set(getattr(engine, "deployed_bands", set()) or set())
        if 20 in available:
            engine._emit_runtime_event("a1_overnight_b20_rescue")
            log.info("A1 overnight: BAD_RF during hold, forcing B20")
            return engine._make_band_action(
                bands=[20],
                reason="A1 overnight: BAD_RF during hold, force B20",
                intent="night_hold",
                objective_id=f"overnight_b20_rescue:{st['day']}",
                params={"sequential": False, "forced_sequence": True},
                consult_feedback=False,
            ), True

    # Otherwise block everything -- consumed=True stops state dispatch.
    return None, True


def _legacy_note_a1_overnight_action_oracle(
    engine: Any,
    action: LeverAction,
    now: float,
) -> None:
    """Called after a successful band action to count overnight probes."""
    if not engine._is_a1_overnight_profile():
        return
    if action.lever not in {LeverType.BAND_SELECTION, LeverType.PBAND}:
        return
    st = engine._a1_overnight
    if st["phase"] == "explore_window":
        st["explore_count"] += 1
        engine._emit_runtime_event(
            "a1_overnight_explore_probe",
            count=st["explore_count"],
            max=_A1_OVERNIGHT_EXPLORE_MAX,
        )
        log.info("A1 overnight: probe %d/%d",
                 st["explore_count"], _A1_OVERNIGHT_EXPLORE_MAX)
    elif st["phase"] == "daytime_limited":
        st["daytime_explore_count"] += 1
        engine._emit_runtime_event(
            "a1_daytime_limited_probe",
            count=st["daytime_explore_count"],
            max=_A1_DAYTIME_EXPLORE_MAX,
        )


def note_a1_overnight_action(
    engine: Any,
    action: LeverAction,
    now: float,
) -> None:
    del engine, action, now
    raise RuntimeError("old_provider_result_writer_deleted")


def _handle_daytime_limited(
    engine: Any, st: dict, modem: CellularIntelligenceGeneration, now: float,
) -> tuple[Optional[LeverAction], bool]:
    """Post-morning-hold: allow 2 explorations then normal."""
    if now > st["daytime_explore_limit_until"]:
        st["phase"] = "done"
        return None, False
    if st["daytime_explore_count"] >= _A1_DAYTIME_EXPLORE_MAX:
        st["phase"] = "done"
        log.info("A1 daytime: limited probes used, normal operation resumes")
        return None, False
    return None, False


# ===================================================================
# Morning sequence: B3 -> B1+B3 -> B1+B3+B20, 20 s apart
# ===================================================================

def decide_a1_b818_morning_sequence(
    engine: Any,
    modem: CellularIntelligenceGeneration,
    now: float,
) -> Optional[LeverAction]:
    del engine, modem, now
    raise RuntimeError("old_provider_morning_action_owner_deleted")


def decide_a1_provider_overnight_recovery(
    engine: Any,
    modem: CellularIntelligenceGeneration,
    now: float,
) -> tuple[Optional[LeverAction], bool]:
    """Provider-owned alias; older b818 name remains for compatibility."""
    return decide_a1_overnight_recovery(engine, modem, now)


def decide_a1_provider_morning_sequence(
    engine: Any,
    modem: CellularIntelligenceGeneration,
    now: float,
) -> Optional[LeverAction]:
    """Provider-owned alias; older b818 name remains for compatibility."""
    return decide_a1_b818_morning_sequence(engine, modem, now)
