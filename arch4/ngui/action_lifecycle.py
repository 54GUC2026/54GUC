"""Correlate NGUI requests with Engine action transactions."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any

from arch4.interfaces import LeverAction, LeverType


def lever_name(lever: Any) -> str:
    if isinstance(lever, LeverType):
        return lever.name
    name = getattr(lever, "name", None)
    return str(name or lever or "").strip().upper()


def infer_action_source(action: LeverAction) -> str:
    source = str(getattr(action, "_gui_action_source", "") or "").strip()
    return source or "ngui"


@dataclass(frozen=True, slots=True)
class NGUIActionCorrelation:
    correlation_id: str
    source: str
    lever: str
    requested_ts: float
    requested_monotonic: float


class NGUIActionCorrelationFactory:
    """Generate request correlation IDs without owning lifecycle state."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._sequence = 0

    def bind(
        self,
        action: LeverAction,
        *,
        requested_ts: float,
        requested_monotonic: float,
        source: str = "",
    ) -> NGUIActionCorrelation:
        if not isinstance(action, LeverAction):
            raise TypeError("ngui_action_correlation_requires_lever_action")
        action_source = str(source or infer_action_source(action)).strip()
        correlation = self.bind_semantic(
            lever=lever_name(action.lever),
            requested_ts=requested_ts,
            requested_monotonic=requested_monotonic,
            source=action_source,
        )
        correlation_id = correlation.correlation_id
        setattr(action, "_gui_action_id", correlation_id)
        setattr(action, "_gui_action_source", action_source)
        return correlation

    def bind_semantic(
        self,
        *,
        lever: str,
        requested_ts: float,
        requested_monotonic: float,
        source: str,
    ) -> NGUIActionCorrelation:
        """Bind a UI intent without adding a physical LeverAction target."""

        if requested_ts <= 0.0 or requested_monotonic <= 0.0:
            raise ValueError("ngui_action_correlation_requires_timestamps")
        lever_value = lever_name(lever)
        if not lever_value:
            raise ValueError("ngui_action_correlation_requires_lever")
        source_value = str(source or "").strip()
        if not source_value:
            raise ValueError("ngui_action_correlation_requires_source")
        with self._lock:
            self._sequence += 1
            correlation_id = f"gui-action-{self._sequence}"
        return NGUIActionCorrelation(
            correlation_id=correlation_id,
            source=source_value,
            lever=lever_value,
            requested_ts=float(requested_ts),
            requested_monotonic=float(requested_monotonic),
        )


__all__ = (
    "NGUIActionCorrelation",
    "NGUIActionCorrelationFactory",
    "infer_action_source",
    "lever_name",
)
