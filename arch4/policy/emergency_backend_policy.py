"""
Backend-specific emergency action helpers.

These helpers keep modem/backend-specific restart/recover action construction
out of the generic emergency state logic. RouterOS-class routers own their own
radio recovery; 54GUC observes and holds instead of poking the modem during
RouterOS emergency recovery.
"""

from __future__ import annotations

from typing import Any, Optional

from arch4.control.manual_queue import build_authority_lever_action
from arch4.interfaces import LeverAction, LeverType


def emergency_backend(engine: Any) -> str:
    return str(getattr(getattr(engine, "policy_profile", None), "modem_backend", "") or "").lower()


def _emergency_backend_family(engine: Any) -> str:
    """Resolve the backend family for emergency action selection."""
    from arch4.runtime_identity import runtime_backend_family_for_engine

    return runtime_backend_family_for_engine(engine, backend=emergency_backend(engine))


def make_emergency_backend_action(
    engine: Any,
    *,
    emergency_class: str,
) -> Optional[LeverAction]:
    backend = emergency_backend(engine)
    family = _emergency_backend_family(engine)
    is_mikrotik = family == "routeros_at_like"
    is_hilink = family == "huawei_hilink_like"

    if is_mikrotik:
        return None

    if emergency_class == "E_RADIO_DOWN":
        if is_hilink:
            return build_authority_lever_action(
                lever=LeverType.RESTART,
                params={"emergency_fast": True},
                reason="FAULT radio down: fast HiLink restart",
                intent="emergency_fast_restart",
                objective_id="emergency_radio_down:hilink_fast_restart",
                source="emergency_backend_policy",
            )
        return build_authority_lever_action(
            lever=LeverType.UE_CAPABILITY,
            params={"restart": True},
            reason="FAULT radio down: modem restart",
            intent="emergency_restart",
            objective_id="emergency_radio_down:restart",
            source="emergency_backend_policy",
        )

    if emergency_class in {"E_SESSION_RESET_STORM", "E_SESSION_RESET"}:
        if is_hilink:
            return None
        return build_authority_lever_action(
            lever=LeverType.UE_CAPABILITY,
            params={"restart": True},
            reason="SESSION RESET: modem restart",
            intent="emergency_restart",
            objective_id="emergency_session_reset:restart",
            source="emergency_backend_policy",
        )

    if emergency_class == "E_RF_COLLAPSE":
        if is_hilink:
            return build_authority_lever_action(
                lever=LeverType.RESTART,
                params={"emergency_fast": True},
                reason="RF COLLAPSE: fast B818 restart",
                intent="emergency_fast_restart",
                objective_id="emergency_rf_collapse:hilink_fast_restart",
                source="emergency_backend_policy",
            )
        return build_authority_lever_action(
            lever=LeverType.UE_CAPABILITY,
            params={"restart": True},
            reason="RF COLLAPSE: modem restart",
            intent="emergency_restart",
            objective_id="emergency_rf_collapse:restart",
            source="emergency_backend_policy",
        )

    if emergency_class == "E_EFFECTIVE_OUTAGE":
        if is_hilink:
            return build_authority_lever_action(
                lever=LeverType.RESTART,
                params={},
                reason="FAULT effective outage: HiLink restart as last step",
                intent="emergency_restart",
                objective_id="emergency_effective_outage:hilink_restart",
                source="emergency_backend_policy",
            )
        return build_authority_lever_action(
            lever=LeverType.UE_CAPABILITY,
            params={"restart": True},
            reason="FAULT effective outage: modem restart",
            intent="emergency_restart",
            objective_id="emergency_effective_outage:restart",
            source="emergency_backend_policy",
        )

    return build_authority_lever_action(
        lever=LeverType.UE_CAPABILITY,
        params={"restart": True},
        reason="FAULT timeout fallback: modem restart",
        intent="emergency_restart",
        objective_id="emergency_timeout:restart",
        source="emergency_backend_policy",
    )
