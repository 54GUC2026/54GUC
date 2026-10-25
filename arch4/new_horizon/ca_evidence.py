"""Observed CA evidence arithmetic. No modem access, grading or storage owner."""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping

_COUNT = "ca_observed_samples"
_SECONDARIES = "ca_observed_secondary_band_count"
_BAND = re.compile(r"ca_observed_(primary|secondary)_b([1-9][0-9]*)_samples\Z")


def is_ca_counter(name: str) -> bool:
    return name in {_COUNT, _SECONDARIES} or _BAND.fullmatch(name) is not None


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _require_observation(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("new_horizon_ca_observation_required")
    row = dict(value)
    bands = row.get("secondary_bands")
    if (
        type(row.get("sample_id")) is not str or not row["sample_id"]
        or type(row.get("observed_at")) not in {int, float}
        or not math.isfinite(row["observed_at"]) or row["observed_at"] <= 0
        or not _positive_int(row.get("primary_band"))
        or not isinstance(bands, (list, tuple)) or not bands
        or any(not _positive_int(band) for band in bands)
        or len(set(bands)) != len(bands)
        or not _positive_int(row.get("secondary_count"))
        or row["secondary_count"] != len(bands)
        or (row.get("composition") is not None and type(row["composition"]) is not str)
    ):
        raise ValueError("new_horizon_ca_observation_invalid")
    row["secondary_bands"] = sorted(bands)
    return row


def capture_ca_observation(
    value: Mapping[str, Any], *, primary_band: Any, accepted_at: float,
) -> dict[str, Any] | None:
    """Adapt an already fresh Core CA surface; never infer from the allowed pool."""
    if (value.get("ca_active") is not True
            or value.get("ca_composition_state") != "available"
            or value.get("ca_composition_capability") != "observed"):
        return None
    epoch, generation, sequence = (value.get(name) for name in
                                  ("source_epoch", "lifecycle_generation", "sample_sequence"))
    if (type(epoch) is not str or not epoch or not _positive_int(generation)
            or not _positive_int(sequence)):
        raise ValueError("new_horizon_ca_sample_identity_required")
    row = _require_observation(dict(
        sample_id=f"{epoch}:{generation}:{sequence}", observed_at=value.get("sample_ts"),
        primary_band=primary_band, secondary_bands=value.get("ca_component_band_set"),
        secondary_count=value.get("ca_component_count"), composition=value.get("ca_composition_signature"),
    ))
    if row["observed_at"] > accepted_at:
        raise ValueError("new_horizon_ca_future_observation")
    return row


def summarize_ca_observations(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Count physical samples once; primary and secondary can share a band.

    Core's component count is a distinct secondary-band count, not the number
    of carriers. Do not infer carrier multiplicity from it.
    """
    samples: dict[str, dict[str, Any]] = {}
    for value in rows:
        observation = value.get("ca_observation")
        if observation is None:
            continue
        observation = _require_observation(observation)
        key = observation["sample_id"]
        if key in samples and samples[key] != observation:
            raise ValueError("new_horizon_ca_sample_identity_reused")
        samples[key] = observation
    if not samples:
        return {}
    result = {_COUNT: len(samples), _SECONDARIES: 0}
    for observation in samples.values():
        result[_SECONDARIES] += observation["secondary_count"]
        for role, bands in (("primary", [observation["primary_band"]]),
                            ("secondary", observation["secondary_bands"])):
            for band in bands:
                key = f"ca_observed_{role}_b{band}_samples"
                result[key] = result.get(key, 0) + 1
    return result


def observed_ca_support(vector: Mapping[str, Any]) -> dict[str, Any]:
    """Missing optional measurements are unknown; malformed present ones fail."""
    counts = {name: value for name, value in vector.items() if name.startswith("ca_observed_")}
    if not counts:
        return dict(state="unavailable", sample_count=0, primary_bands=[], secondary_bands=[])
    for name, value in counts.items():
        if (not is_ca_counter(name) or type(value) not in {int, float}
                or not math.isfinite(value) or value <= 0 or value != int(value)):
            raise ValueError("new_horizon_ca_counter_invalid:" + name)
    count, secondaries = counts.get(_COUNT), counts.get(_SECONDARIES)
    if count is None or secondaries is None or secondaries < count:
        raise ValueError("new_horizon_ca_counter_totals_required")
    roles: dict[str, list[int]] = {"primary": [], "secondary": []}
    primary_count = secondary_count = 0
    for name, value in counts.items():
        match = _BAND.fullmatch(name)
        if match is None:
            continue
        if value > count:
            raise ValueError("new_horizon_ca_counter_exceeds_samples")
        roles[match[1]].append(int(match[2]))
        if match[1] == "primary":
            primary_count += value
        else:
            secondary_count += value
    if primary_count != count or secondary_count != secondaries:
        raise ValueError("new_horizon_ca_counter_roles_required")
    return dict(state="observed", sample_count=int(count),
                primary_bands=sorted(roles["primary"]), secondary_bands=sorted(roles["secondary"]))


def observed_ca_bands(vector: Mapping[str, Any]) -> tuple[int, ...]:
    evidence = observed_ca_support(vector)
    return tuple(sorted(set(evidence["primary_bands"] + evidence["secondary_bands"])))


def ca_pool_compatible(vector: Mapping[str, Any], pool: Iterable[int]) -> bool:
    """Measured outcomes cannot promise service from a carrier the request excludes."""
    return set(observed_ca_bands(vector)).issubset(pool)


def forecast_ca_support(vector: Mapping[str, Any], pool: Iterable[int]) -> dict[str, Any]:
    """Explain measured support without claiming unobserved bands are absent."""
    support = observed_ca_support(vector)
    observed = set(support["primary_bands"] + support["secondary_bands"])
    return {**support, "unobserved_requested_bands": sorted(set(pool) - observed)}
