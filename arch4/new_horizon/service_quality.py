"""Sole Intelligence capacity and grade formulas, selected by CGF quality_model.

No modem/History access, profile guessing, policy or live-RAT classification.
Recorded evidence and accepted CGF inputs use these same two formula families.
"""
from __future__ import annotations
import math
from typing import Any, Mapping
from arch4.core_contracts.quality_model import (
    LTE_QUALITY_MODEL, NR_QUALITY_MODEL, UNKNOWN_QUALITY_MODEL, require_quality_model,
)

def _number(value: Any) -> float | None:
    if isinstance(value, bool) or type(value) not in {int, float}:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None

SERVICE_REWARD_WEIGHTS = {
    "performance_capacity": 0.40,
    "performance_communication_quality": 0.30,
    "performance_latency": 0.20,
    "performance_jitter": 0.10,
}

# The sole Intelligence formula owner supplies this bounded customer scale.
ABSOLUTE_SERVICE_GRADE_ANCHORS = {
    "performance_capacity": (
        (0.0, -3.0),
        (15.0, 0.0),
        (22.5, 1.5),
        (30.0, 3.0),
    ),
    "performance_latency": (
        (20.0, 3.0),
        (35.0, 2.0),
        (55.0, 1.0),
        (85.0, 0.0),
        (130.0, -1.0),
        (250.0, -2.0),
        (370.0, -3.0),
    ),
    "performance_jitter": (
        (2.0, 3.0),
        (5.0, 2.0),
        (10.0, 1.0),
        (20.0, 0.0),
        (35.0, -1.0),
        (60.0, -2.0),
        (100.0, -3.0),
    ),
    "performance_communication_quality": (
        (0.0, -3.0),
        (0.25, -3.0),
        (0.50, 0.0),
        (0.75, 3.0),
        (1.0, 3.0),
    ),
}

def continuous_grade_from_anchors(
    value: Any,
    anchors: tuple[tuple[float, float], ...],
) -> float | None:
    """Interpolate one finite measurement on a bounded scale."""

    measured = _number(value)
    if measured is None:
        return None
    if len(anchors) < 2:
        raise ValueError("new_horizon_continuous_grade_anchors_required")
    prior_measurement = -math.inf
    for measurement, grade in anchors:
        if (
            not math.isfinite(float(measurement))
            or not math.isfinite(float(grade))
            or float(measurement) <= prior_measurement
            or not -3.0 <= float(grade) <= 3.0
        ):
            raise ValueError("new_horizon_continuous_grade_anchor_invalid")
        prior_measurement = float(measurement)
    if measured <= anchors[0][0]:
        return round(float(anchors[0][1]), 2)
    if measured >= anchors[-1][0]:
        return round(float(anchors[-1][1]), 2)
    for (lower_value, lower_grade), (upper_value, upper_grade) in zip(
        anchors,
        anchors[1:],
    ):
        if measured <= upper_value:
            fraction = (measured - lower_value) / (upper_value - lower_value)
            grade = lower_grade + fraction * (upper_grade - lower_grade)
            return round(max(-3.0, min(3.0, float(grade))), 2)
    raise RuntimeError("new_horizon_continuous_grade_interval_missing")

def continuous_absolute_component_grade(
    dimension: str,
    value: Any,
) -> float | None:
    """Grade one customer result against the defined anchors."""

    measured = _number(value)
    if measured is None:
        return None
    if dimension == "performance_capacity":
        bandwidth_mhz = measured / 1.5
        if bandwidth_mhz < 10.0:
            return -3.0
        return round(max(0.0, min(3.0, (bandwidth_mhz - 10.0) * 0.3)), 2)
    if dimension == "performance_communication_quality":
        return round(
            max(-3.0, min(3.0, (measured * 100.0 - 50.0) * 0.12)),
            2,
        )
    anchors = ABSOLUTE_SERVICE_GRADE_ANCHORS.get(str(dimension))
    if anchors is None:
        raise ValueError("new_horizon_absolute_grade_dimension_invalid")
    return continuous_grade_from_anchors(value, anchors)

def weighted_service_reward(
    grades: Mapping[str, int | float | None],
) -> float | None:
    """Return Referee's weighted customer-result grade."""

    available: list[tuple[float, float]] = []
    for key, weight in SERVICE_REWARD_WEIGHTS.items():
        value = grades.get(key)
        if value is None:
            continue
        grade = _number(value)
        if grade is None or not -3.0 <= grade <= 3.0:
            raise ValueError("new_horizon_service_component_grade_invalid")
        available.append((grade, weight))
    total_weight = sum(weight for _grade, weight in available)
    if total_weight <= 0.0:
        return None
    reward = sum(grade * weight for grade, weight in available) / total_weight
    return round(max(-3.0, min(3.0, reward)), 2)

def context_quality_grades(
    observed: Mapping[str, Any],
) -> dict[str, int | float | None]:
    """Grade one ten-minute context against the user-experience scale."""

    if not isinstance(observed, Mapping):
        raise TypeError("new_horizon_context_quality_vector_required")
    model = require_quality_model(observed["quality_model"])
    grades = {
        "performance_capacity": None,
        "performance_latency": continuous_absolute_component_grade(
            "performance_latency",
            observed.get("latency_ms"),
        ),
        "performance_jitter": continuous_absolute_component_grade(
            "performance_jitter",
            observed.get("jitter_ms"),
        ),
        "performance_communication_quality": None,
        "performance_internet_availability": None,
        "performance_overall_outcome": None,
    }
    if model != LTE_QUALITY_MODEL:
        capacity = _number(observed.get("quality_capacity_fraction"))
        communication = _number(observed.get("cqb_cell"))
        if any(v is not None and not 0.0 <= v <= 1.0 for v in (capacity, communication)):
            raise ValueError("new_horizon_combined_quality_out_of_range")
        grades["performance_capacity"] = None if capacity is None else 6*capacity-3
        grades["performance_communication_quality"] = None if communication is None else 6*communication-3
        if model == NR_QUALITY_MODEL and capacity is not None and communication is not None:
            grades["performance_overall_outcome"] = weighted_service_reward(grades)
        return grades
    grades["performance_capacity"] = continuous_absolute_component_grade(
        "performance_capacity", observed.get("eci_points"),
    )
    grades["performance_communication_quality"] = continuous_absolute_component_grade(
        "performance_communication_quality", observed.get("cqb_cell"),
    )
    grades["performance_overall_outcome"] = weighted_service_reward(grades)
    return grades

def normalized_referee_service_value(
    observed: Mapping[str, Any],
) -> float | None:
    """Canonical AQ/3 coordinate in the explicitly declared model."""

    if not observed:
        return None
    grade = context_quality_grades(observed).get("performance_overall_outcome")
    value = _number(grade)
    return None if value is None else value / 3.0

def capacity_points(observed: Mapping[str, Any]) -> float | None:
    """ECI points use measured MHz; CGF fixed device class selects the carrier set."""
    model = require_quality_model(observed["quality_model"])
    if model == UNKNOWN_QUALITY_MODEL:
        return None
    bandwidth = _number(observed.get(
        "quality_total_bw_mhz" if model == NR_QUALITY_MODEL else "dl_bandwidth_mhz"
    ))
    if bandwidth is None or bandwidth < 0 or (model == LTE_QUALITY_MODEL and bandwidth == 0):
        return None
    return 1.5 * bandwidth


def project_service_vector(observed: Mapping[str, Any]) -> dict[str, Any]:
    """Permanent read projection; preserve archived payloads and recorded grades.

    LTE ECI remains its recorded fact. Historical 5G ECI is corrected only from
    recorded combined bandwidth, never reconstructed from live radio or a pool.
    """
    result = dict(observed)
    if not result:
        return result
    model = require_quality_model(result["quality_model"])
    if model != LTE_QUALITY_MODEL:
        result["eci_points"] = capacity_points(result)
    return result
