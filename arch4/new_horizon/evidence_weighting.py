"""One age policy for rebuildable Intelligence advice, never stored facts."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


EVIDENCE_RETENTION_S = 14.0 * 86400.0


def evidence_influence(observed_at: float, *, now: float) -> float:
    return max(0.0, 1.0 - (now - observed_at) / EVIDENCE_RETENTION_S) if observed_at <= now else 0.0


@dataclass(frozen=True, slots=True)
class EvidenceWeights:
    weights: tuple[float, ...]
    effective_count: float

    def mean(self, values: Iterable[float | None]) -> float | None:
        pairs = tuple(zip(self.weights, values, strict=True))
        usable = [
            (weight, value)
            for weight, value in pairs
            if value is not None and weight > 0.0
        ]
        if any(not math.isfinite(value) for _, value in usable):
            raise ValueError("new_horizon_evidence_value_invalid")
        total = math.fsum(weight for weight, _ in usable)
        if not total:
            return None
        result = math.fsum(weight * value for weight, value in usable) / total
        # A convex mean cannot leave the input range due to floating-point noise.
        return min(
            max(value for _, value in usable),
            max(min(value for _, value in usable), result),
        )


def evidence_weights(closed_at: Iterable[float], *, now: float) -> EvidenceWeights:
    """Apply uniform linear fourteen-day influence, never change saved grades.

    Future rows have no influence. The caller owns retention, identity and
    qualification. Real counts and measured values are not changed here.
    """

    if type(now) not in {int, float} or not math.isfinite(now) or now <= 0:
        raise ValueError("new_horizon_evidence_clock_invalid")
    times = tuple(closed_at)
    if any(
        type(value) not in {int, float} or not math.isfinite(value) or value <= 0
        for value in times
    ):
        raise ValueError("new_horizon_evidence_timestamp_invalid")
    aged = tuple(
        evidence_influence(value, now=now)
        for value in times
    )
    mass = math.fsum(aged)
    if not mass:
        return EvidenceWeights(tuple(0.0 for _ in times), 0.0)
    weights = tuple(value / mass for value in aged)
    return EvidenceWeights(weights, mass)
