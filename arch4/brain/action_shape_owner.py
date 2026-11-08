"""Canonical action-shape owner for learned radio actions.

This owner normalizes persisted action records into one band-mutation shape
contract. ROI, proof, and learning consumers must read this surface instead of
guessing from private action-parameter dialects.
"""

from __future__ import annotations

from typing import Any, Mapping

from arch4.interfaces import LeverType
from arch4.runtime_band_scope import normalize_runtime_band_values

OWNER = "arch4.brain.action_shape_owner"
SCHEMA = "brain_action_shape.v1"


def action_shape_from_record(record: Any) -> dict[str, Any]:
    """Return the canonical action shape for one persisted action record."""

    lever = getattr(record, "lever", None)
    params = getattr(record, "action_params", {}) or {}
    return action_shape_from_params(params, lever=lever)


def action_shape_from_params(params: Mapping[str, Any] | None, *, lever: Any) -> dict[str, Any]:
    row = dict(params or {}) if isinstance(params, Mapping) else {}
    lever_name = _lever_name(lever)
    if lever_name == "PBAND":
        return _pband_shape(row, lever_name=lever_name)
    if lever_name == "BAND_SELECTION":
        return _band_selection_shape(row, lever_name=lever_name)
    return _invalid_shape(lever_name=lever_name, reason="unsupported_lever")


def pband_ca_pool_action_key(*, pband: int, ca_pool: list[int]) -> str:
    anchor = _positive_int(pband)
    pool = _bands([*list(ca_pool or []), anchor] if anchor > 0 else ca_pool)
    if anchor <= 0 or len(pool) <= 1:
        return ""
    return f"pband_ca:B{anchor}:pool:{_band_sig(pool)}"


def ca_pool_action_key(ca_pool: list[int]) -> str:
    pool = _bands(ca_pool)
    if len(pool) <= 1:
        return ""
    return f"ca_pool:{_band_sig(pool)}"


def ca_pool_delta_key(*, anchor: int, direction: str, band: int) -> str:
    anchor_i = _positive_int(anchor)
    band_i = _positive_int(band)
    direction_s = str(direction or "").strip().lower()
    if anchor_i <= 0 or band_i <= 0 or direction_s not in {"add", "remove"}:
        return ""
    return f"ca_pool_delta:B{anchor_i}:{direction_s}:B{band_i}"


def _pband_shape(params: dict[str, Any], *, lever_name: str) -> dict[str, Any]:
    pband = _positive_int(params.get("pband"))
    target_pool = _first_bands(
        params,
        "ca_pool",
        "resolved_target_bands",
        "bands",
        "dl_bands",
        extra=[pband],
    )
    previous_pband = _positive_int(params.get("_pband_previous_band"))
    key = str(params.get("_pband_action_key") or pband_ca_pool_action_key(pband=pband, ca_pool=target_pool))
    valid = pband > 0 and bool(key)
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "valid": valid,
        "invalid_reason": "" if valid else "missing_pband_or_ca_pool",
        "lever": lever_name,
        "family": "pband_ca_pool" if valid else "",
        "pband": pband,
        "previous_pband": previous_pband,
        "ca_pool": target_pool,
        "target_pool": target_pool,
        "previous_pool": _first_bands(params, "_ca_pool_previous_bands", "resolved_current_bands"),
        "added_bands": [],
        "removed_bands": [],
        "anchor_band": pband,
        "pband_action_key": key,
        "ca_pool_key": ca_pool_action_key(target_pool),
        "shape_source": "pband_action_contract",
    }


def _band_selection_shape(params: dict[str, Any], *, lever_name: str) -> dict[str, Any]:
    target_pool = _first_bands(params, "resolved_target_bands", "ca_pool", "bands", "dl_bands")
    previous_pool = _first_bands(params, "resolved_current_bands", "_ca_pool_previous_bands")
    added_bands = _first_bands(params, "_ca_pool_added_bands")
    removed_bands = _first_bands(params, "_ca_pool_removed_bands")
    removed_single = _positive_int(params.get("_ca_pool_removed_band"))
    if removed_single > 0 and removed_single not in removed_bands:
        removed_bands.append(removed_single)
        removed_bands.sort()
    if not added_bands and previous_pool and target_pool:
        added_bands = sorted(set(target_pool) - set(previous_pool))
    if not removed_bands and previous_pool and target_pool:
        removed_bands = sorted(set(previous_pool) - set(target_pool))
    anchor = _first_positive(
        params.get("_tsar_candidate_band"),
        params.get("_ca_pool_anchor_band"),
        params.get("_ca_pool_kept_band"),
        params.get("pband"),
    )
    if anchor <= 0 and previous_pool:
        shared = sorted(set(previous_pool) & set(target_pool))
        anchor = int(shared[0] if shared else previous_pool[0])
    if anchor <= 0 and target_pool:
        anchor = int(target_pool[0])
    ca_pool_key = ca_pool_action_key(target_pool)
    valid = bool(ca_pool_key)
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "valid": valid,
        "invalid_reason": "" if valid else "missing_band_selection_pool",
        "lever": lever_name,
        "family": "ca_pool" if valid else "",
        "pband": 0,
        "previous_pband": 0,
        "ca_pool": target_pool,
        "target_pool": target_pool,
        "previous_pool": previous_pool,
        "added_bands": added_bands,
        "removed_bands": removed_bands,
        "anchor_band": anchor,
        "pband_action_key": "",
        "ca_pool_key": ca_pool_key,
        "shape_source": _band_selection_source(params, previous_pool=previous_pool, target_pool=target_pool),
    }


def _band_selection_source(params: Mapping[str, Any], *, previous_pool: list[int], target_pool: list[int]) -> str:
    if bool(params.get("_ca_pool_expansion_trial")):
        return "tsar_ca_pool_expansion_trial"
    if bool(params.get("_ca_pool_reduction")):
        return "tsar_ca_pool_reduction"
    if params.get("band_sequence_steps"):
        return "band_sequence"
    if bool(params.get("sequential")) or bool(params.get("forced_sequence")):
        return "sequential_band_selection"
    if previous_pool and target_pool:
        return "resolved_band_selection"
    return "band_selection"


def _invalid_shape(*, lever_name: str, reason: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "owner": OWNER,
        "valid": False,
        "invalid_reason": reason,
        "lever": lever_name,
        "family": "",
        "pband": 0,
        "previous_pband": 0,
        "ca_pool": [],
        "target_pool": [],
        "previous_pool": [],
        "added_bands": [],
        "removed_bands": [],
        "anchor_band": 0,
        "pband_action_key": "",
        "ca_pool_key": "",
        "shape_source": "",
    }


def _lever_name(lever: Any) -> str:
    if isinstance(lever, LeverType):
        return str(lever.name)
    text = str(lever or "").strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.upper()


def _first_bands(params: Mapping[str, Any], *keys: str, extra: list[int] | None = None) -> list[int]:
    for key in keys:
        bands = _bands(params.get(key))
        if bands:
            if extra:
                bands = _bands([*bands, *extra])
            return bands
    return _bands(extra or [])


def _bands(values: Any) -> list[int]:
    return [int(band) for band in normalize_runtime_band_values(values)]


def _band_sig(values: list[int]) -> str:
    return "+".join(f"B{int(band)}" for band in _bands(values))


def _positive_int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _first_positive(*values: Any) -> int:
    for value in values:
        number = _positive_int(value)
        if number > 0:
            return number
    return 0


__all__ = [
    "OWNER",
    "SCHEMA",
    "action_shape_from_params",
    "action_shape_from_record",
    "ca_pool_action_key",
    "ca_pool_delta_key",
    "pband_ca_pool_action_key",
]
