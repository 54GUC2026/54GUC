"""Live-state applicability owner for canonical action shapes.

Shape legality answers whether an action is internally complete. Applicability
answers the separate question: would that legal action change the currently
accepted modem state? It never reads Modem Live, executes a command, scores an
arm, or owns policy. Callers must supply an accepted immutable state projection.

The no-effect comparisons preserve the useful vB5.3
``brain.proposal_validity`` behavior while removing its duplicate shape owner.
Unknown current state is never guessed into a rejection.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping

from arch4.interfaces import LeverType
from arch4.core_contracts.command_targets import core_lte_pci


OWNER = "arch4.action_applicability"
SCHEMA = "CanonicalActionApplicability.v1"
STATE_SCHEMA = "CanonicalActionApplicabilityState.v1"


@dataclass(frozen=True, slots=True)
class ActionApplicability:
    applicable: bool
    code: str = ""
    reason: str = ""
    comparison_basis: str = ""
    target_values: tuple[int, ...] = ()
    current_values: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "owner": OWNER,
            "applicable": bool(self.applicable),
            "code": str(self.code or ""),
            "reason": str(self.reason or ""),
            "comparison_basis": str(self.comparison_basis or ""),
            "target_values": list(self.target_values),
            "current_values": list(self.current_values),
        }


def canonical_applicability_state(current: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project only accepted fields used by deterministic no-effect checks."""

    row = dict(current) if isinstance(current, Mapping) else {}
    selected = _bands(row.get("selected_bands") or row.get("selected_band_set"))
    active = _bands(
        row.get("active_bands")
        or row.get("ca_pool")
        or row.get("current_bands")
    )
    primary = _positive_int(row.get("primary_band") or row.get("current_pband"))
    if not active and primary > 0:
        active = (primary,)
    return {
        "schema": STATE_SCHEMA,
        "owner": OWNER,
        "selected_bands": list(selected),
        "active_bands": list(active),
        "primary_band": primary,
        "cell_lock_active": _optional_bool(
            row.get("cell_lock_active", row.get("cell_locked"))
        ),
        "cell_lock_pci": _optional_pci(row.get("cell_lock_pci")),
        "cell_lock_earfcn": _positive_int(row.get("cell_lock_earfcn")),
        "serving_pci": _optional_pci(row.get("serving_pci", row.get("pci"))),
        "serving_earfcn": _positive_int(
            row.get("serving_earfcn") or row.get("earfcn")
        ),
        "traffic_presence_active": _optional_bool(
            row.get("traffic_presence_active")
        ),
        "traffic_presence_interval_s": _optional_float(
            row.get("traffic_presence_interval_s")
        ),
        "nr_lock_active": _optional_bool(row.get("nr_lock_active")),
        "nr_locked_bands": list(
            _bands(row.get("nr_locked_bands") or row.get("current_nr_bands"))
        ),
    }


def evaluate_action_applicability(
    lever: LeverType | str,
    params: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
    *,
    action: str = "",
) -> ActionApplicability:
    """Return false only when accepted current truth proves no effect."""

    lever_value = _lever(lever)
    if lever_value is None or not isinstance(params, Mapping):
        return _applicable()
    state = canonical_applicability_state(current)
    row = dict(params)
    if lever_value in {LeverType.BAND_SELECTION, LeverType.BAND_LOCK}:
        return _band_applicability(row, state)
    if lever_value is LeverType.PBAND:
        return _pband_applicability(row, state)
    if lever_value is LeverType.CELL_LOCK:
        return _cell_applicability(row, state, action=action)
    if lever_value is LeverType.TRAFFIC_PRESENCE:
        return _traffic_presence_applicability(row, state)
    if lever_value is LeverType.NR_BAND_LOCK:
        return _nr_band_applicability(row, state)
    return _applicable()


def _band_applicability(
    params: Mapping[str, Any], state: Mapping[str, Any]
) -> ActionApplicability:
    if params.get("auto") is True:
        return _applicable()
    target = _bands(params.get("resolved_target_bands") or params.get("bands"))
    selected = _bands(state.get("selected_bands"))
    active = _bands(state.get("active_bands"))
    current = selected or active
    if target and current and target == current:
        basis = "selected_pool" if selected else "active_pool"
        return _no_effect(
            "target already selected" if selected else "target already active",
            basis=basis,
            target=target,
            current=current,
        )
    return _applicable()


def _pband_applicability(
    params: Mapping[str, Any], state: Mapping[str, Any]
) -> ActionApplicability:
    pband = _non_negative_int(params.get("pband"))
    if pband <= 0:
        return _applicable()
    requested = _bands(params.get("ca_pool"))
    if not requested:
        requested = _bands((pband, *_bands(params.get("dl_bands"))))
    if pband not in requested:
        requested = _bands((pband, *requested))
    selected = _bands(state.get("selected_bands"))
    active = _bands(state.get("active_bands"))
    current = selected or active
    anchor = _positive_int(state.get("primary_band"))
    if anchor == pband and current and requested == current:
        basis = "selected_pool" if selected else "active_pool"
        return _no_effect(
            "PBAND already active in current selected pool"
            if selected
            else "PBAND already active",
            basis=basis,
            target=requested,
            current=current,
        )
    return _applicable()


def _cell_applicability(
    params: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    action: str,
) -> ActionApplicability:
    locked = state.get("cell_lock_active")
    unlock = params.get("unlock") is True
    if unlock:
        if locked is False:
            return _no_effect("cell lock already cleared", basis="lock_state")
        return _applicable()
    if locked is not True:
        return _applicable()
    target = (
        _optional_pci(params.get("pci")),
        _positive_int(params.get("earfcn")),
    )
    current = (
        _optional_pci(state.get("cell_lock_pci")),
        _positive_int(state.get("cell_lock_earfcn")),
    )
    if target != current or target[0] is None or target[1] <= 0:
        return _applicable()
    renew = "renew" in str(action or "").strip().lower() or params.get("renew") is True
    serving = (
        _optional_pci(state.get("serving_pci")),
        _positive_int(state.get("serving_earfcn")),
    )
    if renew and (serving[0] is None or serving[1] <= 0 or serving != target):
        return _applicable()
    return _no_effect(
        "cell lock already active",
        basis="lock_state",
        target=target,
        current=current,
    )


def _traffic_presence_applicability(
    params: Mapping[str, Any], state: Mapping[str, Any]
) -> ActionApplicability:
    target = _optional_float(params.get("keepalive_interval_s"))
    active = state.get("traffic_presence_active")
    current = state.get("traffic_presence_interval_s")
    if target is None:
        return _applicable()
    if target <= 0.0:
        if active is False:
            return _no_effect(
                "traffic presence already inactive", basis="runtime_keepalive"
            )
        return _applicable()
    if active is True and isinstance(current, (int, float)):
        if abs(float(current) - target) < 0.01:
            return _no_effect(
                "traffic presence keepalive already active",
                basis="runtime_keepalive",
            )
    return _applicable()


def _nr_band_applicability(
    params: Mapping[str, Any], state: Mapping[str, Any]
) -> ActionApplicability:
    target = _bands(params.get("nr_bands"))
    active = state.get("nr_lock_active")
    current = _bands(state.get("nr_locked_bands"))
    if not target:
        if active is False:
            return _no_effect("NR band lock already cleared", basis="nr_lock_state")
        return _applicable()
    if active is True and current and target == current:
        return _no_effect(
            "NR band lock already active",
            basis="nr_lock_state",
            target=target,
            current=current,
        )
    return _applicable()


def _applicable() -> ActionApplicability:
    return ActionApplicability(applicable=True)


def _no_effect(
    reason: str,
    *,
    basis: str,
    target: tuple[int, ...] = (),
    current: tuple[int, ...] = (),
) -> ActionApplicability:
    return ActionApplicability(
        applicable=False,
        code="NO_EFFECT",
        reason=reason,
        comparison_basis=basis,
        target_values=target,
        current_values=current,
    )


def _lever(value: LeverType | str) -> LeverType | None:
    if isinstance(value, LeverType):
        return value
    try:
        return LeverType[str(value or "").strip().upper()]
    except KeyError:
        return None


def _bands(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(sorted({_positive_int(item) for item in value if _positive_int(item)}))


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _optional_pci(value: Any) -> int | None:
    """Boundary adapter: unknown or invalid state never proves no effect."""
    if value is None:
        return None
    try:
        return core_lte_pci(value)
    except (TypeError, ValueError):
        return None


def _non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


__all__ = [
    "ActionApplicability",
    "canonical_applicability_state",
    "evaluate_action_applicability",
]
