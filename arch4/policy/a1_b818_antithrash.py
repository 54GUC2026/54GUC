"""
A1+B818 anti-thrash helpers.

Retains the provider-specific hold/freeze algorithm only as a non-public
behavior oracle. Public action/filter entrypoints fail loudly.
"""

from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

import time
from typing import Any, List, Optional

from arch4.control.manual_queue import build_authority_lever_action
from arch4.interfaces import LeverAction, LeverType, is_fault_state


def _cfg_float_allow_zero(engine: Any, name: str, default: float) -> float:
    raw = getattr(engine.cfg, name, default)
    if raw is None:
        return float(default)
    return float(raw)


def is_strict_a1_band_action(action: LeverAction) -> bool:
    if action.lever != LeverType.BAND_SELECTION:
        return False
    reason = action.reason or ""
    if action.params.get("forced_sequence"):
        return True
    return reason.startswith("A1 big-dip recovery step") or reason.startswith("Morning sequence step")


def _legacy_apply_a1_restricted_band_filter_oracle(
    engine: Any,
    action: Optional[LeverAction],
    modem: CellularIntelligenceGeneration,
    now: float,
) -> Optional[LeverAction]:
    if not action or action.lever not in {LeverType.BAND_SELECTION, LeverType.PBAND}:
        return action
    if not engine._is_a1_restricted_night_window(now, modem):
        return action
    if action.lever == LeverType.PBAND:
        pband = int(action.params.get("pband", 0) or 0)
        bands = list(action.params.get("ca_pool") or [])
        if pband > 0 and pband not in bands:
            bands.insert(0, pband)
    else:
        bands = action.params.get("bands") or []
    filtered = engine._filter_a1_restricted_bands(list(bands), modem, now)
    if not filtered:
        return None
    current = sorted(set(int(b) for b in bands if int(b) > 0))
    if current == filtered:
        return action
    params_before = dict(action.params or {})
    tsar_candidate_action = str(params_before.get("_tsar_candidate_action", "") or "")
    tsar_target_owner = str(params_before.get("_tsar_target_pool_owner", "") or "")
    ca_pool_owned = bool(
        action.lever == LeverType.PBAND
        or params_before.get("_ca_pool_reduction")
        or params_before.get("_ca_pool_expansion_trial")
        or (
            params_before.get("_selected_action_command")
            and params_before.get("_selected_action_owner_class") == "optimization"
        )
        or tsar_candidate_action in {"reduce_ca_pool", "stabilise_ca_pool"}
        or tsar_target_owner == "arch4.smart_loop.closed_loop"
    )
    if ca_pool_owned:
        if hasattr(engine, "_emit_runtime_event"):
            engine._emit_runtime_event(
                "a1_restricted_filter_blocked_tsar_ca_pool_mutation",
                selected_bands=list(current),
                restricted_bands=list(filtered),
                tsar_candidate_action=tsar_candidate_action,
                target_pool_owner=tsar_target_owner,
            )
        return None
    params = dict(action.params)
    params["bands"] = filtered
    reason = f"{action.reason} [A1 night restriction: B3/B20 only]"
    return build_authority_lever_action(
        lever=action.lever,
        params=params,
        reason=reason,
        intent=action.intent,
        objective_id=action.objective_id,
        source="a1_b818_antithrash",
    )


def apply_a1_restricted_band_filter(
    engine: Any,
    action: Optional[LeverAction],
    modem: CellularIntelligenceGeneration,
    now: float,
) -> Optional[LeverAction]:
    del engine, action, modem, now
    raise RuntimeError("old_post_owner_provider_restricted_filter_deleted")


def a1_pick_freeze_bands(
    engine: Any,
    modem: CellularIntelligenceGeneration,
    now: float,
) -> List[int]:
    active = engine._active_bands(modem)
    if active:
        out = engine._filter_a1_restricted_bands(active, modem, now)
        if out:
            return out
    return engine._a1_restricted_band_pool(modem, now)


def _legacy_guard_a1_antithrash_oracle(
    engine: Any,
    action: Optional[LeverAction],
    modem: CellularIntelligenceGeneration,
    now: float,
) -> Optional[LeverAction]:
    if not engine._is_a1_restricted_night_window(now, modem):
        return action

    st = engine._a1_antithrash
    day_key = time.strftime("%Y-%m-%d", time.localtime(now))
    if st["day"] != day_key:
        st.update(
            {
                "day": day_key,
                "hold_started": False,
                "hold_until": 0.0,
                "retest_done": False,
                "freeze_bands": [],
                "freeze_applied": False,
                "action_times": [],
                "strict_remaining": 0,
                "strict_last_at": 0.0,
            }
        )

    window_s = max(0.0, _cfg_float_allow_zero(engine, "a1_night_antithrash_window_s", 300.0))
    st["action_times"] = [t for t in st["action_times"] if (now - t) <= window_s]

    # Enter hold when churn spikes in low-RF / emergency periods.
    max_actions = max(1, int(engine.cfg.a1_night_antithrash_max_band_actions))
    burst = len(st["action_times"]) >= max_actions
    emergency_pressure = is_fault_state(engine.posture_authority.state) and engine.posture_authority.current_posture.time_in_state_s >= 20.0
    if st["hold_until"] <= now and (burst and (emergency_pressure or len(st["action_times"]) >= (max_actions + 2))):
        hold_s = max(0.0, _cfg_float_allow_zero(engine, "a1_night_antithrash_hold_s", 120.0))
        st["hold_until"] = now + hold_s
        st["hold_started"] = True
        st["retest_done"] = False
        st["freeze_applied"] = False
        st["freeze_bands"] = engine._a1_pick_freeze_bands(modem, now)
        st["strict_remaining"] = max(1, int(engine.cfg.a1_night_antithrash_strict_max_actions))
        st["strict_last_at"] = 0.0
        engine._emit_runtime_event(
            "a1_antithrash_hold_started",
            hold_s=int(hold_s),
            freeze_bands=list(st["freeze_bands"]),
            recent_band_actions=len(st["action_times"]),
            emergency_state=bool(is_fault_state(engine.posture_authority.state)),
        )
        engine._decision_policy_log().warning(
            "A1 anti-thrash hold started for %.0f min (freeze=%s, recent_actions=%d, emergency=%s)",
            hold_s / 60.0,
            st["freeze_bands"],
            len(st["action_times"]),
            is_fault_state(engine.posture_authority.state),
        )

    # Hold active: allow only strict recovery sequence, otherwise freeze/hold.
    if st["hold_until"] > now:
        if action and action.lever == LeverType.BAND_SELECTION and engine._is_strict_a1_band_action(action):
            wait_s = max(0.0, _cfg_float_allow_zero(engine, "a1_night_antithrash_strict_step_wait_s", 60.0))
            if st["strict_remaining"] > 0 and (st["strict_last_at"] <= 0.0 or (now - st["strict_last_at"]) >= wait_s):
                st["strict_remaining"] -= 1
                st["strict_last_at"] = now
                st["action_times"].append(now)
                return action
            return None
        if (
            action
            and action.lever == LeverType.PBAND
            and action.params.get("_selected_action_command")
            and action.params.get("_selected_action_owner_class") == "optimization"
        ):
            # Never replace a claimed SmartLoop PBAND command with a different
            # freeze action. Returning None lets the canonical pipeline reject
            # the original command and keep command/proof identity truthful.
            return None
        if not st["freeze_applied"] and st["freeze_bands"]:
            st["freeze_applied"] = True
            st["action_times"].append(now)
            return engine._make_band_action(
                bands=list(st["freeze_bands"]),
                reason=f"A1 anti-thrash hold: freeze best known {list(st['freeze_bands'])}",
                intent="night_hold",
                objective_id=engine._objective_id_for_bands("night_hold", st["freeze_bands"]),
                params={"sequential": True},
                consult_feedback=False,
            )
        return None

    # Hold expired: allow one re-test, then re-enter hold.
    if st["hold_started"] and not st["retest_done"]:
        if action and action.lever == LeverType.BAND_SELECTION:
            st["retest_done"] = True
            st["freeze_applied"] = False
            hold_s = max(0.0, _cfg_float_allow_zero(engine, "a1_night_antithrash_hold_s", 120.0))
            st["hold_until"] = now + hold_s
            st["strict_remaining"] = 0
            st["strict_last_at"] = 0.0
            st["action_times"].append(now)
            engine._emit_runtime_event(
                "a1_antithrash_single_retest",
                hold_s=int(hold_s),
                retest_reason=action.reason,
            )
            return action
        return None

    if action and action.lever in {LeverType.BAND_SELECTION, LeverType.PBAND}:
        st["action_times"].append(now)
    return action


def guard_a1_antithrash(
    engine: Any,
    action: Optional[LeverAction],
    modem: CellularIntelligenceGeneration,
    now: float,
) -> Optional[LeverAction]:
    del engine, action, modem, now
    raise RuntimeError("old_provider_antithrash_guard_owner_deleted")
