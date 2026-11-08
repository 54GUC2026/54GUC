from __future__ import annotations

from typing import Any, Optional

from arch4.interfaces import LeverAction
from arch4.operator_profiles import infer_backend_family
from arch4.policy.a1_provider_policy import suppress_a1_provider_daytime_band_action


HILINK_FAMILY = "huawei_hilink_like"
suppress_b818_daytime_band_action = suppress_a1_provider_daytime_band_action


def resolve_runtime_backend_family(engine: Any) -> str:
    profile = getattr(engine, "policy_profile", None)
    family = str(getattr(profile, "backend_family", "") or "").strip().lower()
    if family:
        return family
    cfg = getattr(engine, "cfg", None)
    backend = str(getattr(cfg, "modem_backend", "") or "").strip().lower()
    return infer_backend_family(backend)


def filter_family_daytime_band_action(
    engine: Any,
    action: Optional[LeverAction],
    *,
    family: str | None = None,
) -> Optional[LeverAction]:
    family_name = str(family or "").strip().lower() or resolve_runtime_backend_family(engine)
    if family_name == HILINK_FAMILY:
        return suppress_b818_daytime_band_action(engine, action)
    return action
