"""
Incident-aware recovery policy helpers.

This layer lets the runtime incident tracker bias recovery choice in narrowly bounded
places without taking over the state machine.
"""

from __future__ import annotations

from arch4.controller_frames import DerivedSignalFrames

from arch4.controller_frames import CellularIntelligenceGeneration

import time
from typing import Optional, Tuple

from arch4.interfaces import LeverAction
from arch4.policy.incident_action_policy import incident_class_name, incident_next_step


def _routeros_restore_ca_suppressed(*, engine, modem: CellularIntelligenceGeneration) -> bool:
    suppress = getattr(engine, "_restore_ca_routeros_passive_suppressed", None)
    if callable(suppress):
        return bool(
            suppress(
                modem,
                source="incident_recovery",
                reason="RouterOS band writes drop LTE link; incident recovery stays passive",
            )
        )
    if not getattr(engine, "_is_routeros_family_profile", lambda: False)():
        return False
    current_bands = sorted(c.band for c in modem.cellular.carriers)
    if len(current_bands) <= 1:
        return False
    now = time.time()
    band_sig = "+".join(str(b) for b in current_bands)
    cooldowns = getattr(engine, "cooldowns", None)
    blocked = bool(
        cooldowns
        and cooldowns.is_blocked_keyed("restore_ca_routeros_suppressed", band_sig, now=now)
    )
    if not blocked:
        if cooldowns:
            cooldowns.set_keyed(
                "restore_ca_routeros_suppressed", band_sig, hold_s=300.0, now=now
            )
        engine._emit_runtime_event(
            "restore_ca_routeros_suppressed",
            band_sig=band_sig,
            active_band_sig=band_sig,
            reason="RouterOS band writes drop LTE link; incident recovery stays passive",
        )
    return True


def run_degraded_incident_policy(
    *,
    engine,
    modem: CellularIntelligenceGeneration,
    computed: DerivedSignalFrames,
) -> Tuple[bool, Optional[LeverAction]]:
    raise RuntimeError(
        "direct_degraded_incident_action_deleted_use_normal_behavior_opportunity"
    )


def run_congested_incident_policy(
    *,
    engine,
    modem: CellularIntelligenceGeneration,
    computed: DerivedSignalFrames,
) -> Tuple[bool, Optional[LeverAction], bool]:
    raise RuntimeError(
        "direct_congested_incident_action_deleted_use_normal_behavior_opportunity"
    )


def run_emergency_incident_policy(
    *,
    engine,
    modem: CellularIntelligenceGeneration,
    computed: DerivedSignalFrames,
    emergency_class: str,
    step: int,
    timeout_reached: bool = False,
) -> Tuple[bool, Optional[LeverAction], bool]:
    del computed
    emergency_class = str(emergency_class or "")
    # Hard fault classes stay owned by the existing emergency ladder.
    if emergency_class in {"E_RADIO_DOWN", "E_SESSION_RESET", "E_RF_COLLAPSE"}:
        return False, None, False

    assessment = getattr(engine, "_incident_assessment", {}) or {}
    incident_class = incident_class_name(assessment)
    next_step = incident_next_step(assessment)

    if incident_class == "post_action_instability":
        engine._emit_runtime_event(
            "incident_recovery_hold",
            state=str(getattr(engine.posture_authority.state, "name", "FAULT") or "FAULT"),
            emergency_class=emergency_class,
            incident_class=incident_class,
            next_step=next_step,
            reason="emergency recovery held because incident points to self-induced instability and settle/backoff is preferred",
        )
        return True, None, False

    if incident_class == "radio_ca_loss":
        soft_emergency = emergency_class in {"", "E_CONGESTION_STORM"}
        if soft_emergency and (step == 0 or timeout_reached):
            action = engine._make_restore_ca_action(
                modem,
                reason="Incident recovery: restore known-good CA pool before emergency escalation",
                source="emergency_incident_radio_ca_loss",
            )
            if action is not None:
                return True, action, True
            if _routeros_restore_ca_suppressed(engine=engine, modem=modem):
                return True, None, False

    return False, None, False
