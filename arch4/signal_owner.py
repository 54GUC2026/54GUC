"""Canonical runtime signal adapter resolver.

SignalComputer applies already accepted congestion, jitter, and derived-signal
configuration to the live processors. It is not the policy owner for runtime
tuning requests. Policy owners resolve this adapter only after accepting and
publishing their own owner-stamped runtime policy proposal.
"""

from __future__ import annotations

from typing import Any


def runtime_signal_owner(core: Any) -> Any:
    """Return the direct Core-owned non-advancing signal facade."""

    signals = getattr(core, "signals", None)
    if signals is None:
        raise RuntimeError("core_signal_runtime_facade_unavailable")
    if callable(getattr(signals, "update", None)):
        raise RuntimeError("state-advancing SignalComputer escaped composition")
    return signals


__all__ = ["runtime_signal_owner"]
