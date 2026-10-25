"""SmartLoop opportunity-space owner.

This module enumerates what SmartLoop should consider. It does not score final
actions, open command envelopes, touch Memory, or talk to the modem.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

SCHEMA = "smart_loop.opportunity_matrix.v1"
OWNER = "arch4.smart_loop.opportunity_matrix"
RADIO_CONFIGURATION_ACTION = "apply_radio_configuration"


def build_opportunity_matrix(*, context: Any, facts: Any, state: Any) -> dict[str, Any]:
    current = _int(getattr(context, "current_pband", 0), 0)
    authority = _band_tuple(getattr(context, "authority_bands", ()))
    ca_pool = _band_tuple(getattr(context, "ca_pool", ()))
    rows: list[dict[str, Any]] = []

    if current > 0:
        rows.append(_row("current_anchor", "stay_current_anchor", current, (current,), "observe_current_anchor"))

    for band in sorted(_mapping(getattr(facts, "band_scores", {}))):
        target = _int(band, 0)
        if target > 0:
            rows.append(_row("learned_band_score", RADIO_CONFIGURATION_ACTION, target, _target_bands(authority, target), "learned_candidate"))

    for target in _prediction_bands(_mapping(getattr(facts, "prediction_context", {}))):
        rows.append(_row("crystal_ball_future", RADIO_CONFIGURATION_ACTION, target, _target_bands(authority, target), "future_candidate"))

    for target in _recent_window_bands(getattr(state, "observations", ())):
        rows.append(_row("recent_window", RADIO_CONFIGURATION_ACTION, target, _target_bands(authority, target), "recent_candidate"))

    capacity = _mapping(getattr(facts, "measured_capacity_fact", {}))
    if _capacity_says_current_weak(capacity, current):
        for target in authority:
            if target != current:
                rows.append(_row("measured_capacity_gap", RADIO_CONFIGURATION_ACTION, target, _target_bands(authority, target), "capacity_recovery_candidate"))

    if current > 0 and authority and ca_pool and current in authority and not set(authority).issubset(set(ca_pool)):
        rows.append(_row("configuration_space", RADIO_CONFIGURATION_ACTION, current, authority, "wider_ca_pool_candidate"))

    deduped = _dedupe_rows(rows)
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "context_key": str(
            getattr(context, "learning_context_key", "") or ""
        ),
        "identity_code": str(getattr(context, "identity_code", "") or ""),
        "rows": deduped,
        "target_bands": opportunity_target_bands(deduped),
        "nothing_left_on_table_basis": "candidate_space_enumerated",
    }


def opportunity_target_bands(matrix_or_rows: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> tuple[int, ...]:
    rows = matrix_or_rows.get("rows", ()) if isinstance(matrix_or_rows, Mapping) else matrix_or_rows
    out: list[int] = []
    for row in rows if isinstance(rows, Sequence) else ():
        target = _int(_mapping(row).get("target_band"), 0)
        if target > 0 and target not in out:
            out.append(target)
    return tuple(out)


def opportunity_for_target(matrix: Mapping[str, Any], target_band: int) -> dict[str, Any]:
    target = int(target_band or 0)
    rows = matrix.get("rows") if isinstance(matrix, Mapping) else ()
    for row in rows if isinstance(rows, Sequence) else ():
        current = _mapping(row)
        if _int(current.get("target_band"), 0) == target:
            return dict(current)
    return {}


def _row(source: str, action: str, target_band: int, target_bands: Sequence[int], reason_code: str) -> dict[str, Any]:
    return {
        "source": source,
        "action": action,
        "target_band": int(target_band),
        "target_bands": list(_band_tuple(target_bands)),
        "reason_code": reason_code,
    }


def _dedupe_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, int, tuple[int, ...]]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        current = dict(row)
        key = (
            str(current.get("action") or ""),
            str(current.get("source") or ""),
            _int(current.get("target_band"), 0),
            _band_tuple(current.get("target_bands", ())),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(current)
    return out


def _prediction_bands(prediction: Mapping[str, Any]) -> tuple[int, ...]:
    if prediction.get("schema") != "crystal_ball_horizon_context.v1":
        return ()
    if not bool(prediction.get("advisory_only", False)) or bool(prediction.get("direct_modem_actuation", True)):
        return ()
    out: list[int] = []
    for horizon in _mapping(prediction.get("horizons")).values():
        row = _mapping(horizon)
        for key in ("best_alternate", "best_candidate"):
            band = _int(_mapping(row.get(key)).get("band"), 0)
            if band > 0 and band not in out:
                out.append(band)
    return tuple(out)


def _recent_window_bands(observations: Any) -> tuple[int, ...]:
    out: list[int] = []
    if not isinstance(observations, Sequence):
        return ()
    for item in observations:
        band = _int(getattr(item, "pband", 0), 0)
        if band > 0 and band not in out:
            out.append(band)
    return tuple(out)


def _capacity_says_current_weak(capacity: Mapping[str, Any], current: int) -> bool:
    return (
        capacity.get("schema") == "measured_capacity_fact.v1"
        and capacity.get("owner") == "arch4.smart_loop.capacity_probe"
        and bool(capacity.get("measured", False))
        and _int(capacity.get("serving_band"), 0) == int(current or 0)
        and _float(capacity.get("dl_mbps"), 0.0) <= 5.0
        and _float(capacity.get("confidence"), 0.0) > 0.0
    )


def _target_bands(authority: Sequence[int], target: int) -> tuple[int, ...]:
    bands = list(_band_tuple(authority))
    if int(target or 0) > 0 and int(target) not in bands:
        bands.append(int(target))
    return _band_tuple(bands)


def _band_tuple(value: Any) -> tuple[int, ...]:
    if isinstance(value, str):
        value = value.replace("+", ",").split(",")
    if not isinstance(value, (list, tuple, set)):
        return ()
    out: list[int] = []
    for item in value:
        band = _int(str(item).lstrip("Bb"), 0)
        if band > 0 and band not in out:
            out.append(band)
    return tuple(sorted(out))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


__all__ = ["OWNER", "SCHEMA", "build_opportunity_matrix", "opportunity_for_target", "opportunity_target_bands"]
