"""Accepted signal-tuning facts exported by the signal owner."""

from __future__ import annotations

from typing import Any, Mapping

from arch4.signal_owner import runtime_signal_owner


def signal_tuning_surface_from_owner(signals: Any) -> dict[str, Any]:
    """Read tuning state from the already-resolved canonical signal owner."""

    if signals is None:
        raise RuntimeError("signal tuning owner unavailable")
    status = getattr(signals, "get_dl_jitter_formula_status", None)
    weights_status = getattr(signals, "get_congestion_weights_status", None)
    out: dict[str, Any] = {}
    if callable(status):
        formula = dict(status() or {})
        if formula:
            out["jitter_formula"] = formula
    if callable(weights_status):
        weights = dict(weights_status() or {})
        if weights:
            out["congestion_weights"] = weights
    out["signal_tuning_status"] = {
        "valid": bool(callable(status) and callable(weights_status)),
        "source": "signal_compute_facade",
    }
    return out


def signal_tuning_runtime_surface(engine: Any) -> dict[str, Any]:
    """Return accepted tuning state from the canonical signal owner."""

    return signal_tuning_surface_from_owner(runtime_signal_owner(engine))


def publish_signal_tuning_runtime_surface(
    engine: Any,
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Attach accepted signal tuning facts for runtime-register publication."""

    out = dict(extra or {})
    surface = signal_tuning_runtime_surface(engine)
    if not surface:
        return out

    out.update(surface)
    return out


__all__ = [
    "publish_signal_tuning_runtime_surface",
    "signal_tuning_runtime_surface",
    "signal_tuning_surface_from_owner",
]
