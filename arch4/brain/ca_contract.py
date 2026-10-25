"""Canonical Brain CA contract helpers."""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames, QualityFrame


def _quality(source: DerivedSignalFrames | QualityFrame) -> QualityFrame:
    if isinstance(source, DerivedSignalFrames):
        return source.quality
    if isinstance(source, QualityFrame):
        return source
    raise TypeError("ca_contract_requires_derived_or_quality_frame")


def cart_ld_valid(computed: DerivedSignalFrames | QualityFrame) -> bool:
    """True when this tick has paired CART + DL-load evidence."""
    return bool(_quality(computed).cart_ld_valid)


def cart_ld_active(computed: DerivedSignalFrames | QualityFrame) -> bool:
    """Current paired CART_LD event. Invalid ticks are treated as absent."""
    if not cart_ld_valid(computed):
        return False
    return bool(int(_quality(computed).cart_ld or 0))


def cast_ld_valid(computed: DerivedSignalFrames | QualityFrame) -> bool:
    """True when this scope has at least one paired CART + DL-load sample."""
    return bool(_quality(computed).cast_ld_valid)


def cast_ld(computed: DerivedSignalFrames | QualityFrame) -> float:
    """Load-qualified CA stability over paired samples."""
    if not cast_ld_valid(computed):
        return 0.0
    return float(_quality(computed).cast_ld or 0.0)


def cart_ld_activity(computed: DerivedSignalFrames | QualityFrame) -> float:
    """Load-qualified CART churn over paired samples."""
    if not cast_ld_valid(computed):
        return 0.0
    return float(_quality(computed).cart_ld_activity or 0.0)


def ca_ld_stable(
    computed: DerivedSignalFrames | QualityFrame,
    *,
    min_cast_ld: float = 75.0,
    max_activity: float = 12.0,
) -> bool:
    """Decision-grade LD CA health from aggregate stability, not current tick."""
    if not cast_ld_valid(computed):
        return False
    return cast_ld(computed) >= float(min_cast_ld) and cart_ld_activity(computed) <= float(max_activity)
