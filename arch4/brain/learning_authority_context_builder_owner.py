"""LearningAuthority context construction owner."""

from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from arch4.interfaces import PolicyProfile, SystemState

OWNER = "arch4.brain.learning_authority_context_builder_owner"


@dataclass
class LearningContext:
    provider_plmn: str
    provider_name: str
    modem_backend: str
    calibration_marker: str
    calibration_signature: str
    hour: int
    rf_regime: str
    traffic_regime: str
    state_class: str
    baseline_state: str = ""
    session_state: str = ""
    provider_time_window: str = ""
    provider_time_explanation: str = ""
    nsa_active: bool = False

    def key(self) -> str:
        return "|".join(
            [
                f"plmn:{self.provider_plmn or '-'}",
                f"provider:{self.provider_name or '-'}",
                f"modem:{self.modem_backend or '-'}",
                f"cal:{self.calibration_marker or 'C0'}",
                f"sig:{self.calibration_signature or '-'}",
                f"h:{int(self.hour):02d}",
                f"rf:{self.rf_regime}",
                f"traffic:{self.traffic_regime}",
                f"state:{self.state_class or '-'}",
                f"base:{self.baseline_state or '-'}",
                f"session:{self.session_state or '-'}",
                f"pwin:{self.provider_time_window or '-'}",
                f"nsa:{'1' if self.nsa_active else '0'}",
            ]
        )


def build_learning_context(
    *,
    modem: CellularIntelligenceGeneration,
    state: Optional[SystemState],
    policy_profile: Optional[PolicyProfile],
    calibration_handoff: Optional[Dict[str, Any]],
    memory: Any,
    learning_cfg: Any,
    current_provider_plmn: str,
    current_provider_name: str,
) -> LearningContext:
    now = time.time()
    provider_plmn = str(
        (policy_profile.provider_plmn if policy_profile else "") or current_provider_plmn or ""
    )
    provider_name = str(
        (policy_profile.provider_name if policy_profile else "") or current_provider_name or ""
    )
    modem_backend = str((policy_profile.modem_backend if policy_profile else "") or "")
    marker = str(getattr(memory, "current_calibration_marker", "C0") or "C0")
    signature = _calibration_signature_text(getattr(memory, "current_calibration_signature", {}) or {})
    rf_regime = _rf_regime(
        learning_cfg,
        modem.cellular.sinr_db,
        rsrp_dbm=getattr(modem.cellular, 'rsrp_dbm', None),
        rsrq_db=getattr(modem.cellular, 'rsrq_db', None),
    )
    traffic_regime = "traffic" if bool(modem.traffic.has_traffic) else "idle"
    state_class = state.name if state else ""
    handoff = calibration_handoff if isinstance(calibration_handoff, dict) else {}
    hour = int(time.localtime(now).tm_hour)
    provider_window, provider_window_explanation = _provider_time_window(
        provider_plmn=provider_plmn,
        provider_name=provider_name,
        hour=hour,
    )
    return LearningContext(
        provider_plmn=provider_plmn,
        provider_name=provider_name,
        modem_backend=modem_backend,
        calibration_marker=marker,
        calibration_signature=signature,
        hour=hour,
        rf_regime=rf_regime,
        traffic_regime=traffic_regime,
        state_class=state_class,
        baseline_state=str(
            handoff.get("baseline_state")
            or handoff.get("trust_level", "")
            or ""
        ).strip().lower(),
        session_state=str(
            handoff.get("session_state")
            or handoff.get("setup_state", "")
            or ""
        ).strip().lower(),
        provider_time_window=provider_window,
        provider_time_explanation=provider_window_explanation,
        nsa_active=bool(getattr(modem.cellular, 'nsa_active', False)),
    )


def learning_context_payload(
    *,
    ctx: LearningContext,
    handoff: Dict[str, Any],
    learning_update_allowed: bool,
    learning_block_reason: str,
    compact_window: Dict[str, Any],
    calibration_fact_confidence: float,
) -> Dict[str, Any]:
    fact_package = (
        dict(handoff.get("calibration_fact_package"))
        if isinstance(handoff.get("calibration_fact_package"), dict)
        else {}
    )
    return {
        "key": ctx.key(),
        "provider_plmn": ctx.provider_plmn,
        "provider_name": ctx.provider_name,
        "modem_backend": ctx.modem_backend,
        "calibration_marker": ctx.calibration_marker,
        "calibration_signature": ctx.calibration_signature,
        "hour": int(ctx.hour),
        "rf_regime": ctx.rf_regime,
        "traffic_regime": ctx.traffic_regime,
        "state_class": ctx.state_class,
        "baseline_state": ctx.baseline_state,
        "session_state": ctx.session_state,
        "provider_time_window": ctx.provider_time_window,
        "provider_time_explanation": ctx.provider_time_explanation,
        "learning_update_allowed": bool(learning_update_allowed),
        "learning_block_reason": str(learning_block_reason or ""),
        "radio_mutation_window": dict(compact_window or {}),
        "calibration_fact_confidence": float(calibration_fact_confidence),
        "calibration_fact_level": str(
            handoff.get("calibration_fact_level", "") or ""
        ).strip().lower(),
        "calibration_fact_package": fact_package,
        "calibration_self_learning_input_strength": float(
            fact_package.get("self_learning_input_strength", 0.0) or 0.0
        ),
        "calibration_crystal_ball_input_strength": float(
            fact_package.get("crystal_ball_input_strength", 0.0) or 0.0
        ),
        "nsa_active": ctx.nsa_active,
    }


def _calibration_signature_text(value: Any) -> str:
    if isinstance(value, dict):
        items = []
        for key in sorted(value.keys()):
            items.append(f"{key}={value[key]}")
        return ";".join(items)[:180]
    return str(value)[:180]


def _provider_time_window(*, provider_plmn: str, provider_name: str, hour: int) -> tuple[str, str]:
    provider_text = f"{provider_plmn} {provider_name}".strip().lower()
    is_a1 = "22005" in provider_text or provider_text == "a1" or " a1" in f" {provider_text} "
    safe_hour = int(hour) % 24
    if is_a1 and 0 <= safe_hour < 6:
        return (
            "a1_overnight",
            "A1 overnight/early-morning samples are learned as a separate provider window.",
        )
    if is_a1 and 6 <= safe_hour < 9:
        return (
            "a1_morning_recovery",
            "A1 morning recovery samples are learned separately from normal daytime behavior.",
        )
    if is_a1:
        return (
            "a1_daytime",
            "A1 daytime samples are learned separately from A1 night behavior.",
        )
    if 0 <= safe_hour < 6:
        return (
            "overnight",
            "Overnight samples are learned separately from daytime behavior.",
        )
    return (
        "daytime",
        "Daytime samples are learned in the normal provider window.",
    )


def _rf_regime(
    learning_cfg: Any,
    sinr_db: float,
    *,
    rsrp_dbm: float | None = None,
    rsrq_db: float | None = None,
) -> str:
    severe_floor = float(learning_cfg.emergency_sinr_floor)
    weak_floor = float(learning_cfg.v5_low_rf_sinr_gate_db)
    if (
        float(sinr_db) <= severe_floor
        and rsrp_dbm is not None
        and rsrq_db is not None
        and float(rsrp_dbm) <= -88.0
        and float(rsrq_db) <= -12.0
    ):
        return "severe"
    if float(sinr_db) <= weak_floor:
        return "weak"
    return "good"


__all__ = [
    "OWNER",
    "LearningContext",
    "build_learning_context",
    "learning_context_payload",
]
