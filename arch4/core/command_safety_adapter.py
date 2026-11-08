"""Stateless runtime-to-Core mechanical-safety evidence adapter."""

from __future__ import annotations

import math
from typing import Any

from arch4.core_contracts.accepted_runtime_view import (
    CoreAcceptedRuntimeView,
)
from arch4.core_contracts.command_safety import CoreCommandSafetyEvidence
from arch4.core_contracts.commands import CoreCommandRequest
from arch4.core_contracts.current_health import (
    MODEM_LIVE_CURRENT_HEALTH_OWNER,
)
from arch4.interfaces import LeverAction


def build_core_command_safety_evidence(
    runtime_owner: Any,
    *,
    request: CoreCommandRequest,
    action: LeverAction,
    core_runtime_view: CoreAcceptedRuntimeView,
    identity_key: str,
    now: float,
) -> CoreCommandSafetyEvidence:
    """Project existing Core-readable truth for exactly one request."""

    if not isinstance(request, CoreCommandRequest):
        raise TypeError("core_command_safety_typed_request_required")
    if not isinstance(action, LeverAction):
        raise TypeError("core_command_safety_lever_action_required")
    if not isinstance(core_runtime_view, CoreAcceptedRuntimeView):
        raise TypeError("core_command_safety_runtime_view_required")
    matrix_identity = core_runtime_view.matrix_identity
    if (
        request.matrix_identity != matrix_identity
        or request.identity_code != core_runtime_view.identity_code
    ):
        raise ValueError("core_command_safety_request_view_mismatch")
    health_view = runtime_owner.current_health_view()
    if health_view.namespace != matrix_identity.namespace:
        raise RuntimeError("core_command_safety_health_namespace_mismatch")
    health_facts = health_view.facts
    if health_facts is None:
        raise RuntimeError("core_command_safety_health_pending")
    if (
        not isinstance(identity_key, str)
        or not identity_key
        or identity_key != identity_key.strip()
    ):
        raise ValueError("core_command_safety_identity_key_required")
    if type(now) is not float or not math.isfinite(now) or now <= 0.0:
        raise ValueError("core_command_safety_observed_at_invalid")
    return CoreCommandSafetyEvidence(
        request_id=request.request_id,
        matrix_identity=request.matrix_identity,
        identity_code=request.identity_code,
        identity_key=identity_key,
        modem_health_owner=MODEM_LIVE_CURRENT_HEALTH_OWNER,
        modem_health_state=health_facts.state.value,
        observed_at=now,
    )


__all__ = (
    "build_core_command_safety_evidence",
)
