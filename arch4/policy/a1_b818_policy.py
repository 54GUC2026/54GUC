"""
A1 provider overnight policy helpers.

Retired provider-specific guardrails and recovery sequences retained only as
non-public behavior oracles. Public action/filter entrypoints fail loudly;
live ownership is ``ActionSourceStateAuthority`` plus the opportunity ledger.

A1 network behaviour:
  - 00:00-06:00: B1/B3 cells near this site are powered off by the provider.
                 Only B20 (800 MHz, 10 MHz BW) is reliably available.
                 Brain learns B20-vs-far-B3 tradeoff autonomously.
  - 06:00-00:00: B1/B3 power back on.  Full CA is possible:
                 B3 (20 MHz) + B1 (15 MHz) + B20 (10 MHz) = 45 MHz total.
                 Qualified band actions are allowed through.
                 If fewer than 3 bands are active, the CA-restore sequence
                 forces B3 -> B3+B1 -> B1+B3+B20 with 20 s waits.
"""

from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

import time
from typing import Any, List, Optional

from arch4.interfaces import LeverAction, LeverType, SystemState, is_emergency_state
from arch4.operator_profiles import render_action_alias_template


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_A1_DAY_START_HOUR = 6   # bands allowed from 06:00
_CA_RESTORE_STEP_WAIT_S = 20.0
_CA_RESTORE_SEQUENCE: List[List[int]] = [[3], [1, 3], [1, 3, 20]]
_CA_RESTORE_COOLDOWN_S = 120.0
_CA_RESTORE_VERIFY_S = 900.0   # 15 min: minimum settle time before judging
_CA_RESTORE_MAX_FAILURES = 2   # give up after 2 failures in same cell context


def _is_a1_daytime(now: float) -> bool:
    """Return True if local hour is in [06:00, 00:00) -- A1 cells are on."""
    hour = time.localtime(now).tm_hour
    return hour >= _A1_DAY_START_HOUR  # 6..23


def _cfg_float(engine: Any, *names: str, default: float) -> float:
    cfg = getattr(engine, "cfg", None)
    for name in names:
        if cfg is None:
            continue
        value = getattr(cfg, name, None)
        if value is not None:
            return float(value)
    return float(default)


def _cfg_nonnegative_float(engine: Any, *names: str, default: float) -> float:
    return max(0.0, _cfg_float(engine, *names, default=default))


def _cfg_int(engine: Any, *names: str, default: int, minimum: int = 0) -> int:
    cfg = getattr(engine, "cfg", None)
    for name in names:
        if cfg is None:
            continue
        value = getattr(cfg, name, None)
        if value is not None:
            return max(minimum, int(value))
    return max(minimum, int(default))


def _ca_restore_step_wait_s(engine: Any) -> float:
    return _cfg_nonnegative_float(engine, "a1_ca_restore_step_wait_s", default=_CA_RESTORE_STEP_WAIT_S)


def _ca_restore_cooldown_s(engine: Any) -> float:
    return _cfg_nonnegative_float(engine, "a1_ca_restore_cooldown_s", default=_CA_RESTORE_COOLDOWN_S)


def _ca_restore_verify_s(engine: Any) -> float:
    return _cfg_nonnegative_float(engine, "a1_ca_restore_verify_s", default=_CA_RESTORE_VERIFY_S)


def _ca_restore_max_failures(engine: Any) -> int:
    return _cfg_int(engine, "a1_ca_restore_max_failures", default=_CA_RESTORE_MAX_FAILURES, minimum=1)


# ---------------------------------------------------------------------------
# Daytime band-action gate  (replaces old blanket suppression)
# ---------------------------------------------------------------------------

def _legacy_suppress_a1_provider_daytime_band_action_oracle(
    engine: Any,
    action: Optional[LeverAction],
    *,
    now: Optional[float] = None,
) -> Optional[LeverAction]:
    """
    A1 overnight/provider band-action gate -- time-of-day aware.

    06:00-00:00 (daytime):  allow qualified band/full-radio actions through.
    00:00-06:00 (night):    suppress WORKING-state band/full-radio actions --
                            B1/B3 are powered off, so there is no point
                            thrashing. Non-WORKING states remain available for
                            explicit recovery policy.
    """
    if not action or action.lever not in {LeverType.BAND_SELECTION, LeverType.PBAND}:
        return action
    if not getattr(engine, "_is_a1_overnight_profile", lambda: False)():
        return action

    now = time.time() if now is None else float(now)

    # -- Daytime: let it through -------------------------------------------
    if _is_a1_daytime(now):
        return action

    # -- Night: only suppress WORKING on A1 overnight profile --------------
    if (
        engine.posture_authority.state == SystemState.WORKING
    ):
        engine._decision_policy_log().info(
            "Policy suppresses A1 overnight night WORKING band action: %s",
            action.reason,
        )
        return None

    # Non-WORKING states at night (CONGESTED, DEGRADED, etc.)
    # are allowed through -- the brain may still need to react.
    return action


def suppress_a1_provider_daytime_band_action(
    engine: Any,
    action: Optional[LeverAction],
    *,
    now: Optional[float] = None,
) -> Optional[LeverAction]:
    del engine, action, now
    raise RuntimeError("old_provider_daytime_filter_owner_deleted")


def suppress_b818_daytime_band_action(
    engine: Any,
    action: Optional[LeverAction],
    *,
    now: Optional[float] = None,
) -> Optional[LeverAction]:
    del engine, action, now
    raise RuntimeError("old_provider_daytime_filter_owner_deleted")


# ---------------------------------------------------------------------------
# CA-restore sequence: B3 -> B1+B3 -> B1+B3+B20 with 20 s waits
# ---------------------------------------------------------------------------

def _legacy_maybe_ca_restore_sequence_oracle(
    engine: Any,
    modem: CellularIntelligenceGeneration,
    action: Optional[LeverAction],
    now: float,
) -> Optional[LeverAction]:
    """
    If fewer than 3 A1 bands are active during daytime (06-00), intercept the
    brain's band action and replace it with a stepped CA-restore sequence:

        step 0:  force B3 only          (anchor the 20 MHz primary)
        step 1:  force B3 + B1          (add 15 MHz CA)
        step 2:  force B1 + B3 + B20    (add 10 MHz CA -> 45 MHz total)

    Each step waits 20 s before proceeding to the next.

    Learning from failure:
        After the sequence completes, wait 15 minutes and verify whether all
        3 bands held.  If they dropped back, record a failure for this cell
        context (PCI).  After 2 failures in the same cell context, stop
        trying -- let the brain handle it or do nothing.  The failure counter
        resets when the cell changes (different PCI) or a new day starts.

    If all 3 bands are already active, returns the original action unchanged
    so the brain handles single-band optimisation (cell changes, primary
    swaps, etc.) on its own.
    """
    if not getattr(engine, "_is_a1_overnight_profile", lambda: False)():
        return action
    if not _is_a1_daytime(now):
        return action

    # Count distinct active bands.
    active = _active_band_set(modem)
    target_bands = {1, 3, 20}

    st = _ca_restore_state(engine)
    cell_key = _cell_context_key(modem, now)

    # -- Verification check: did a previous sequence hold? -----------------
    _check_verify(engine, st, modem, now)

    if active >= target_bands:
        # All 3 bands active -- brain handles single-band decisions itself.
        # If we were waiting to verify, the _check_verify above handled it.
        return action

    # -- Check if we've given up on this cell context ----------------------
    max_failures = _ca_restore_max_failures(engine)
    if st["fail_count"] >= max_failures and st["fail_cell"] == cell_key:
        # Already failed twice in this cell -- don't keep hammering.
        # Let the brain do its own thing (or nothing).
        return action

    # Reset failure tracking if cell context changed.
    if st["fail_cell"] != cell_key:
        st["fail_cell"] = cell_key
        st["fail_count"] = 0

    # -- Run the stepped sequence ------------------------------------------
    _maybe_reset_ca_restore(engine, st, now)

    # Still in cooldown from a recently completed sequence.
    completed = float(st.get("completed_at", 0.0) or 0.0)
    cooldown_s = _ca_restore_cooldown_s(engine)
    if completed > 0.0 and (now - completed) < cooldown_s:
        return action

    # Start a new sequence if not already running.
    if not st["active"]:
        st["active"] = True
        st["step"] = 0
        st["next_step_at"] = now
        engine._decision_policy_log().info(
            "CA-restore sequence started: active bands %s, target %s (attempt %d/%d for cell %s)",
            sorted(active), sorted(target_bands),
            st["fail_count"] + 1, max_failures, cell_key,
        )

    # Wait for next step.
    if now < st["next_step_at"]:
        return None  # swallow the brain's action, we're mid-sequence

    step_wait_s = _ca_restore_step_wait_s(engine)
    step = int(st["step"])
    seq = _CA_RESTORE_SEQUENCE
    if step >= len(seq):
        # Sequence complete -- start verification timer.
        verify_s = _ca_restore_verify_s(engine)
        st["active"] = False
        st["completed_at"] = now
        st["verify_at"] = now + verify_s
        engine._decision_policy_log().info(
            "CA-restore sequence completed, verifying in %ds", int(verify_s),
        )
        return None

    bands = list(seq[step])
    st["step"] = step + 1
    st["next_step_at"] = now + step_wait_s

    day_key = time.strftime("%Y-%m-%d", time.localtime(now))
    return engine._make_band_action(
        bands=bands,
        reason=f"CA-restore step {step + 1}/{len(seq)}: B{'+B'.join(str(b) for b in bands)}",
        intent="ca_restore",
        objective_id=f"ca_restore:{day_key}:{step + 1}",
        params={"sequential": False, "forced_sequence": True},
        consult_feedback=False,
    )


def maybe_ca_restore_sequence(
    engine: Any,
    modem: CellularIntelligenceGeneration,
    action: Optional[LeverAction],
    now: float,
) -> Optional[LeverAction]:
    del engine, modem, action, now
    raise RuntimeError("old_provider_ca_restore_action_owner_deleted")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _active_band_set(modem: CellularIntelligenceGeneration) -> set:
    """Return set of distinct active band numbers from modem carriers."""
    active = set()
    for c in getattr(modem.cellular, 'carriers', []) or []:
        try:
            bi = int(getattr(c, "band", 0) or 0)
        except (TypeError, ValueError):
            bi = 0
        if bi > 0:
            active.add(bi)
    if not active:
        pb = int(getattr(modem.cellular, 'primary_band', 0) or 0)
        if pb > 0:
            active.add(pb)
    return active


def _cell_context_key(modem: CellularIntelligenceGeneration, now: float) -> str:
    """Build a context key from cell_id + day.  Resets learning on cell change or new day."""
    cell_id = int(getattr(modem.cellular, 'cell_id', 0) or 0)
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    return f"{cell_id}:{day}"


def _check_verify(engine: Any, st: dict, modem: CellularIntelligenceGeneration, now: float) -> None:
    """Check if a completed sequence held after the 15-minute verification window."""
    verify_at = float(st.get("verify_at", 0.0) or 0.0)
    if verify_at <= 0.0 or now < verify_at:
        return  # no pending verification, or not time yet

    # Verification window reached -- check if all 3 bands held.
    st["verify_at"] = 0.0
    active = _active_band_set(modem)
    target_bands = {1, 3, 20}
    verify_s = _ca_restore_verify_s(engine)
    max_failures = _ca_restore_max_failures(engine)

    if active >= target_bands:
        # Success -- all 3 bands held for 15 minutes.
        st["fail_count"] = 0
        engine._decision_policy_log().info(
            "CA-restore verified: all 3 bands held after %ds", int(verify_s),
        )
    else:
        # Failed -- bands dropped back.
        st["fail_count"] = int(st.get("fail_count", 0) or 0) + 1
        engine._decision_policy_log().info(
            "CA-restore FAILED verification: only %s active after %ds (failure %d/%d for cell %s)",
            sorted(active), int(verify_s),
            st["fail_count"], max_failures, st.get("fail_cell", "?"),
        )
        if st["fail_count"] >= max_failures:
            engine._decision_policy_log().info(
                "CA-restore giving up for cell %s -- %d failures, letting brain handle it",
                st.get("fail_cell", "?"), st["fail_count"],
            )


def _ca_restore_state(engine: Any) -> dict:
    """Get or initialise per-engine CA-restore state dict."""
    if not hasattr(engine, "_a1_ca_restore"):
        engine._a1_ca_restore = {
            "active": False,
            "step": 0,
            "next_step_at": 0.0,
            "completed_at": 0.0,
            "verify_at": 0.0,
            "fail_count": 0,
            "fail_cell": "",
        }
    # Ensure new keys exist for engines with old state dicts.
    st = engine._a1_ca_restore
    st.setdefault("verify_at", 0.0)
    st.setdefault("fail_count", 0)
    st.setdefault("fail_cell", "")
    return st


def _maybe_reset_ca_restore(engine: Any, st: dict, now: float) -> None:
    """Reset sequence state if cooldown has elapsed since last completion."""
    if st["active"]:
        return
    completed = float(st.get("completed_at", 0.0) or 0.0)
    cooldown_s = _ca_restore_cooldown_s(engine)
    if completed > 0.0 and (now - completed) >= cooldown_s:
        st["completed_at"] = 0.0


def _legacy_decide_a1_b818_big_dip_oracle(
    engine: Any,
    modem: CellularIntelligenceGeneration,
    now: float,
) -> tuple[Optional[LeverAction], bool]:
    """
    Event-driven A1+B818 last-resort fallback:
    if B1/B3 attachment is unavailable for a sustained E_RADIO_DOWN window,
    force B20 and hold it for a while instead of treating B20 as a normal
    preferred recovery target.
    """
    if not engine._is_a1_overnight_profile():
        return None, False
    if not is_emergency_state(engine.posture_authority.state):
        return None, False

    cls = getattr(engine.posture_authority.current_posture, "emergency_class", "")
    if cls != "E_RADIO_DOWN":
        return None, False

    now_lt = time.localtime(now)
    day_key = time.strftime("%Y-%m-%d", now_lt)
    st = engine._a1_big_dip
    if st["day"] != day_key:
        st.update(
            {"day": day_key, "active": False, "step": 0, "last_action_at": 0.0, "hold_until": 0.0}
        )

    if st["hold_until"] > now:
        return None, True
    if st["active"] and st["hold_until"] <= now:
        st.update({"active": False, "step": 0, "last_action_at": 0.0, "hold_until": 0.0})

    fallback_persist_s = _cfg_nonnegative_float(
        engine,
        "hilink_radio_drop_fallback_persist_s",
        "a1_big_dip_b20_fallback_persist_s",
        default=300.0,
    )
    if float(getattr(engine.posture_authority.current_posture, "time_in_state_s", 0.0) or 0.0) < fallback_persist_s:
        return None, False

    # If the modem has a usable B1/B3 anchor, do not force B20.
    primary_band = int(getattr(modem.cellular, 'primary_band', 0) or 0)
    if modem.cellular.radio_connected and primary_band in {1, 3}:
        return None, False

    available = set(getattr(engine, "deployed_bands", set()) or set())
    if primary_band > 0:
        available.add(primary_band)
    if 20 not in available:
        return None, False

    target = [20]
    current_bands = sorted(c.band for c in modem.cellular.carriers)

    # Trigger fallback on first qualifying sustained radio-down episode in a day.
    if not st["active"]:
        st["active"] = True
        st["step"] = 0
        st["last_action_at"] = 0.0
        event_meta = render_action_alias_template(
            "a1_huawei_radio_drop_recovery",
            day_key=day_key,
            backend=str(getattr(getattr(engine, "cfg", None), "modem_backend", "") or ""),
            provider_plmn=str(getattr(engine, "_policy_provider_plmn", lambda: "")() or ""),
        )
        engine._emit_runtime_event(
            "a1_big_dip_start",
            emergency_class=cls,
            sequence=[target],
            **event_meta,
        )
        for event_kind in event_meta.get("event_aliases", []):
            engine._emit_runtime_event(
                event_kind,
                emergency_class=cls,
                sequence=[target],
                **event_meta,
            )

    hold_s = _cfg_nonnegative_float(
        engine,
        "hilink_radio_drop_hold_s",
        "a1_big_dip_hold_s",
        default=3600.0,
    )
    st["step"] = 1
    st["last_action_at"] = now
    st["hold_until"] = now + hold_s
    event_meta = render_action_alias_template(
        "a1_huawei_radio_drop_recovery",
        day_key=day_key,
        backend=str(getattr(getattr(engine, "cfg", None), "modem_backend", "") or ""),
        provider_plmn=str(getattr(engine, "_policy_provider_plmn", lambda: "")() or ""),
    )
    engine._emit_runtime_event(
        "a1_big_dip_hold_started",
        hold_s=int(hold_s),
        final_bands=list(target),
        **event_meta,
    )
    for event_kind in event_meta.get("hold_event_aliases", []):
        engine._emit_runtime_event(
            event_kind,
            hold_s=int(hold_s),
            final_bands=list(target),
            **event_meta,
        )

    if current_bands == target:
        return None, True

    return engine._make_band_action(
        bands=target,
        reason=(event_meta.get("reason_text") or "HiLink radio-drop fallback: force B20 after sustained B1/B3 attach failure"),
        intent="big_dip",
        objective_id=f"big_dip:{day_key}:b20_fallback",
        params={
            "sequential": True,
            "forced_sequence": False,
            "b20_fallback": True,
            **{k: v for k, v in event_meta.items() if k not in {"event_aliases", "hold_event_aliases", "reason_text"}},
        },
        consult_feedback=False,
    ), True


def decide_a1_b818_big_dip(
    engine: Any,
    modem: CellularIntelligenceGeneration,
    now: float,
) -> tuple[Optional[LeverAction], bool]:
    del engine, modem, now
    raise RuntimeError("old_provider_radio_drop_action_owner_deleted")
