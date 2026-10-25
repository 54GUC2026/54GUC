"""Optional Intelligence command inputs outside the permanent CGF root."""

from __future__ import annotations

from typing import Any, Optional

from arch4.action_admission import selected_action_context_from_action
from arch4.controller_frames import CellularIntelligenceGeneration
from arch4.interfaces import LeverAction


def reject_direct_tick_cellular_action(
    engine: Any,
    requested_action: Optional[LeverAction],
    *,
    accepted_frame: Optional[CellularIntelligenceGeneration] = None,
    extra: Optional[dict[str, Any]] = None,
) -> Optional[LeverAction]:
    """Hard-fail the deleted tick-era command path."""

    del engine, accepted_frame, extra
    if requested_action is not None:
        selected_action_context_from_action(requested_action)
        raise RuntimeError(
            "direct_tick_cellular_action_deleted_use_core_command_gateway"
        )
    return None


def resolve_calibration_command(engine: Any) -> Optional[str]:
    """Read the optional Intelligence calibration queue only."""

    if not bool(engine.enable_control_queue):
        return None
    return engine.calibration_queue.pop()


__all__ = ("reject_direct_tick_cellular_action", "resolve_calibration_command")
