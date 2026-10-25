"""Scope and expiry guards shared by typed Core operational services."""

from __future__ import annotations

from time import time
from typing import Any

from arch4.core.command_context import (
    require_core_command_ingress_context,
)
from arch4.operational_service_contracts import (
    CoreOperationalFreshness,
    CoreOperatorRequestContext,
)


def core_operational_rejection(
    engine: Any,
    context: CoreOperatorRequestContext,
    *,
    now: float | None = None,
) -> str:
    if not isinstance(context, CoreOperatorRequestContext):
        raise TypeError("core_operational_typed_context_required")
    current = float(time() if now is None else now)
    if current > context.expires_at:
        return "core_operational_request_expired"
    try:
        active = require_core_command_ingress_context(engine)
    except RuntimeError:
        return "core_operational_accepted_generation_unavailable"
    requested = context.ingress
    if requested.session_id != active.session_id:
        return "core_operational_session_mismatch"
    if requested.identity_code != active.identity_code:
        return "core_operational_identity_mismatch"
    if requested.source_epoch != active.source_epoch:
        return "core_operational_source_epoch_mismatch"
    if (
        context.freshness
        in {
            CoreOperationalFreshness.OBSERVATION_DEPENDENT,
            CoreOperationalFreshness.CURRENT_OBSERVATION_DIAGNOSTIC,
        }
        and requested.source_sequence != active.source_sequence
    ):
        return "core_operational_source_sequence_stale"
    return ""


__all__ = ("core_operational_rejection",)
