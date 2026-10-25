"""
arch4/control/band_resolver.py
═══════════════════════════════════════════════════════════════
Canonical band-intent resolver.

Single authority for translating caller intent into a full-set target:
  - set/full-set replace
  - add-band merge into current full-set
  - ordered/cumulative sequence normalization

Used by decision pipeline prechecks and modem backends.

── Seed 2: Dependency Boundary Contract ─────────────────────────────────────

This module is a PURE DATA TRANSFORMER. It has no side effects.

What it accepts (stable contract):
  - A caller intent (band list, sequence flag, merge flag)
  - A current modem band context (serving band, deployed bands)

What it returns:
  - A resolved full-set target (list of integers)
  - A resolution report (why this set was chosen)

What it does NOT do:
  - Read from config, memory, or any runtime state
  - Call modem APIs or issue actions
  - Make policy decisions (e.g. "should we switch bands?")
  - Know about SystemState, incidents, or calibration

Callers that ARE allowed to use this module:
  - DecisionPipelineMixin (translates intent to modem target)
  - MikroTikModem / HuaweiHiLinkModem (normalizes band params before AT write)

Callers that should NOT be added:
  - CoreBouncer (it doesn't decide bands)
  - CalibrationAuthority (it doesn't resolve modem targets)
  - Any GUI component (GUI submits intent, brain resolves it)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Dict, Any

from arch4.coerce import safe_bool

PHYSICAL_ALL_BAND_RELEASE_CONFIRM_TOKEN = "physical_all_band_release_v1"


def _norm_unique_ordered(values: Iterable[Any]) -> List[int]:
    out: List[int] = []
    seen: set[int] = set()
    for v in values:
        try:
            bi = int(v)
        except (TypeError, ValueError):
            continue
        if bi <= 0 or bi in seen:
            continue
        seen.add(bi)
        out.append(bi)
    return out


def _norm_unique_sorted(values: Iterable[Any]) -> List[int]:
    return sorted(set(_norm_unique_ordered(values)))


def _band_sig(bands: List[int]) -> str:
    return "+".join(f"B{b}" for b in sorted(set(int(b) for b in bands if int(b) > 0)))


def _cumulative_steps(ordered_bands: List[int]) -> List[List[int]]:
    steps: List[List[int]] = []
    acc: List[int] = []
    for b in _norm_unique_ordered(ordered_bands):
        acc.append(int(b))
        steps.append(sorted(set(acc)))
    return steps


def is_auto_band_release(params: Dict[str, Any] | None) -> bool:
    """
    Return True when the caller explicitly requests "restore auto/all bands".

    Manual GUI release currently submits:
      {"bands": [], "auto": True}

    Keep this helper centralized so backends do not each invent their own
    interpretation of the same release intent.
    """
    p = params or {}
    if not safe_bool(p.get("auto"), False):
        return False
    requested = _norm_unique_ordered(p.get("bands", []) or [])
    return len(requested) == 0


def auto_band_release_confirmed(params: Dict[str, Any] | None) -> bool:
    """Return true only for an explicitly confirmed all-band/auto release."""
    p = params or {}
    return (
        is_auto_band_release(p)
        and str(p.get("auto_release_confirm") or "").strip()
        == PHYSICAL_ALL_BAND_RELEASE_CONFIRM_TOKEN
    )


def auto_band_release_rejection_reason(params: Dict[str, Any] | None) -> str:
    if not is_auto_band_release(params):
        return ""
    if auto_band_release_confirmed(params):
        return ""
    return "auto band release requires fresh explicit confirmation"



@dataclass(frozen=True)
class BandResolution:
    requested_bands: List[int]
    current_bands: List[int]
    target_bands: List[int]
    target_signature: str
    mode: str
    sequence: str
    sequential: bool
    changed: bool
    steps: List[List[int]]


def resolve_band_intent(
    params: Dict[str, Any] | None,
    *,
    current_bands: Iterable[Any] | None = None,
    morning_sequence: List[int] | None = None,
) -> BandResolution:
    """
    Resolve BAND_SELECTION intent into canonical full-set target.

    Rules:
      - `band_mode=add` => merge requested with current full-set.
      - otherwise       => replace with requested full-set.
      - `sequence=morning` => deterministic ordered sequence, default [3,1,20].
      - `sequential=True` produces cumulative step list.
    """
    p = params or {}
    current = _norm_unique_sorted(current_bands or [])
    mode = str(p.get("band_mode", "set") or "set").strip().lower()
    sequence = str(p.get("sequence", "") or "").strip().lower()
    requested = _norm_unique_ordered(p.get("bands", []) or [])

    if sequence == "morning":
        requested = _norm_unique_ordered(morning_sequence or [3, 1, 20])

    requested_set = set(requested)
    current_set = set(current)

    if mode == "add":
        target_set = current_set | requested_set
    else:
        target_set = requested_set

    target = sorted(target_set)
    sequential = safe_bool(p.get("sequential", False), False) or sequence == "morning"

    # Preserve deterministic order for explicit sequences; otherwise sorted full-set.
    if sequential and requested:
        ordered_for_steps: List[int] = []
        for b in requested:
            if b in target_set and b not in ordered_for_steps:
                ordered_for_steps.append(b)
        for b in target:
            if b not in ordered_for_steps:
                ordered_for_steps.append(b)
    else:
        ordered_for_steps = list(target)

    steps = _cumulative_steps(ordered_for_steps) if sequential and len(target) > 1 else ([target] if target else [])

    return BandResolution(
        requested_bands=requested,
        current_bands=current,
        target_bands=target,
        target_signature=_band_sig(target),
        mode=mode,
        sequence=sequence,
        sequential=sequential,
        changed=(set(target) != current_set),
        steps=steps,
    )
