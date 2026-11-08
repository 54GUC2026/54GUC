from __future__ import annotations

from arch4.controller_frames import CellularIntelligenceGeneration

from typing import Any, Optional

from arch4.interfaces import LeverAction
from arch4.operator_profiles import resolve_action_policy_registry
from arch4.runtime_identity import runtime_backend_family_for_engine
from arch4.policy.family_radio_drop_policy import decide_family_radio_drop_recovery as decide_family_radio_drop_recovery_impl
from arch4.policy.family_hilink_policy import (
    decide_hilink_morning_sequence,
    decide_hilink_overnight_recovery,
)
from arch4.policy.family_daytime_policy import filter_family_daytime_band_action as filter_family_daytime_band_action_impl


def resolve_runtime_provider_plmn(engine: Any) -> str:
    provider = getattr(engine, "_policy_provider_plmn", None)
    if callable(provider):
        try:
            return str(provider() or "").strip()
        except Exception:
            return ""
    return ""


def resolve_runtime_backend(engine: Any) -> str:
    cfg = getattr(engine, "cfg", None)
    return str(getattr(cfg, "modem_backend", "") or "").strip().lower()


def resolve_runtime_backend_family(engine: Any) -> str:
    return runtime_backend_family_for_engine(engine, backend=resolve_runtime_backend(engine))


def resolve_runtime_action_policy(engine: Any) -> dict[str, str]:
    provider = resolve_runtime_provider_plmn(engine)
    backend = resolve_runtime_backend(engine)
    family = resolve_runtime_backend_family(engine)
    registry = resolve_action_policy_registry(provider, backend)
    if registry:
        return registry
    return resolve_action_policy_registry(provider, family)


def decide_family_overnight_recovery(engine: Any, modem: CellularIntelligenceGeneration, now: float) -> tuple[Optional[LeverAction], bool]:
    action_id = resolve_runtime_action_policy(engine).get("overnight_recovery", "")
    if action_id == "a1_huawei_overnight_recovery":
        return decide_hilink_overnight_recovery(engine, modem, now)
    return None, False


def decide_family_morning_sequence(engine: Any, modem: CellularIntelligenceGeneration, now: float) -> Optional[LeverAction]:
    action_id = resolve_runtime_action_policy(engine).get("morning_sequence", "")
    if action_id == "a1_huawei_morning_sequence":
        return decide_hilink_morning_sequence(engine, modem, now)
    return None


def decide_family_radio_drop_recovery(engine: Any, modem: CellularIntelligenceGeneration, now: float) -> tuple[Optional[LeverAction], bool]:
    action_id = resolve_runtime_action_policy(engine).get("radio_drop_recovery", "")
    if action_id == "a1_huawei_radio_drop_recovery":
        return decide_family_radio_drop_recovery_impl(engine, modem, now)
    return None, False


def filter_family_daytime_band_action(engine: Any, action: Optional[LeverAction]) -> Optional[LeverAction]:
    action_id = resolve_runtime_action_policy(engine).get("daytime_band_filter", "")
    if action_id == "a1_huawei_daytime_band_filter":
        return filter_family_daytime_band_action_impl(engine, action, family="huawei_hilink_like")
    return action
