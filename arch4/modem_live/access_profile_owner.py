"""Modem Live interface for access-profile classification and publication."""

from __future__ import annotations

from typing import Any

from arch4.modem.live_lane_contract import (
    ModemLiveAccessProfile,
    modem_live_access_profile_for,
)
from arch4.operator_profiles import infer_backend_family


OWNER = "arch4.modem_live.access_profile_owner"


_ACCESS_METHOD_LABELS = {
    ("huawei_http_single_lane", None): "HiLink API",
    (None, "hilink_http_xml"): "HiLink API",
    ("routeros_api_at_serial_bridge", None): "API + AT",
    ("zte_goform_http_single_lane", None): "GoForm API",
    ("zte_zwrt_ubus_single_lane", None): "UBUS API",
    ("openwrt_modemmanager_single_lane", None): "ModemManager",
    ("tplink_dev2_cbc512_single_lane", None): "Encrypted API",
    ("xiaomi_stock_luci_single_lane", None): "LuCI API",
    ("zyxel_webui_single_lane", None): "Web API",
}


def access_type_display_label(contract, *, semantics):
    """Publish short reviewed-method text; detailed contracts stay unchanged."""
    if contract.get("status") in {"unknown", "provisional"}:
        if contract.get("access_type_id") != "0x0000000000000000" or semantics is not None:
            raise ValueError("access_label_unverified_contract_invalid")
        return "Unverified"
    if contract.get("status") != "exact" or not isinstance(semantics, dict):
        raise ValueError("access_label_reviewed_semantics_required")
    method = (semantics.get("access_contract"), semantics.get("transport_encoding"))
    try:
        return _ACCESS_METHOD_LABELS[method]
    except (KeyError, TypeError) as exc:
        raise ValueError("access_label_method_not_reviewed") from exc


def resolve_modem_live_access_profile(
    *,
    backend: str = "",
    backend_family: str = "",
    profile_id: str = "",
    model: str = "",
    firmware: str = "",
    vendor: str = "",
):
    """Classify access inside Modem Live."""

    family = str(backend_family or "").strip() or infer_backend_family(backend)
    return modem_live_access_profile_for(
        backend=backend,
        backend_family=family,
        profile_id=profile_id,
        model=model,
        firmware=firmware,
        vendor=vendor,
    )


def modem_live_bootstrap_clock_plan(config: Any) -> dict[str, object]:
    """Return a scheduling hint before connection without publishing AccessType.v1."""

    backend = str(getattr(config, "modem_backend", "") or "")
    profile = resolve_modem_live_access_profile(backend=backend,
        profile_id=str(getattr(config, "modem_profile_id", "") or ""))
    fast_interval_s = float(getattr(profile, "fast_live_interval_s", 0.0) or 0.0)
    fast_read_slo_s = float(getattr(profile, "fast_read_slo_s", 0.0) or 0.0)
    expected_interval_s = (
        float(profile.fast_sample_expected_interval_s(clock_ms=100))
        if hasattr(profile, "fast_sample_expected_interval_s")
        else 0.0
    )
    return {
        "modem_live_access_profile": str(getattr(profile, "profile_id", "") or ""),
        "modem_live_fast_interval_s": fast_interval_s,
        "modem_live_fast_read_slo_s": fast_read_slo_s,
        "modem_live_slow_interval_s": float(
            getattr(profile, "slow_live_interval_s", 0.0) or 0.0
        ),
        "modem_live_stale_after_s": float(
            getattr(profile, "fast_live_stale_s", 0.0) or 0.0
        ),
        "modem_live_modem_sample_expected_interval_s": round(expected_interval_s, 3),
        "classification_role": "bootstrap_schedule_hint_only",
        "access_type_authoritative": False,
    }


def require_selected_modem_live_access_profile(live_module: Any) -> ModemLiveAccessProfile:
    """Read the immutable profile already classified by Modem Live."""

    reader = getattr(live_module, "modem_live_access_profile", None)
    if not callable(reader):
        raise RuntimeError("Modem Live selected access-profile port unavailable")
    profile = reader()
    if not isinstance(profile, ModemLiveAccessProfile):
        raise RuntimeError("Modem Live selected access-profile publication invalid")
    if not str(profile.profile_id or "").strip():
        raise RuntimeError("Modem Live selected access-profile id unavailable")
    return profile


__all__ = [
    "OWNER",
    "modem_live_bootstrap_clock_plan",
    "require_selected_modem_live_access_profile",
    "resolve_modem_live_access_profile",
]
