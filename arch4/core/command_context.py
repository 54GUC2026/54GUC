"""Core-owned projection of the latest legal command ingress context."""

from __future__ import annotations

from arch4.core_contracts.accepted_runtime_view import (
    CoreAcceptedRuntimeView,
)
from arch4.core_contracts.commands import CoreCommandIngressContext
from arch4.core.accepted_root_outbox import CoreAcceptedRootOutbox


def build_core_command_ingress_context(
    view: CoreAcceptedRuntimeView,
) -> CoreCommandIngressContext:
    if not isinstance(view, CoreAcceptedRuntimeView):
        raise TypeError("core_command_context_accepted_view_required")
    return view.command_ingress_context()


def require_core_command_ingress_context(
    engine: object,
) -> CoreCommandIngressContext:
    """Read command ingress only from the installed accepted Core root."""

    fabric = getattr(engine, "modem_sample_publication", None)
    outbox = getattr(fabric, "accepted_root_outbox", None)
    if not isinstance(outbox, CoreAcceptedRootOutbox):
        raise RuntimeError("core_accepted_generation_root_unavailable")
    root = outbox.current()
    if root is None:
        raise RuntimeError("core_accepted_generation_root_unavailable")
    return build_core_command_ingress_context(root.runtime_view)


__all__ = (
    "build_core_command_ingress_context",
    "require_core_command_ingress_context",
)
