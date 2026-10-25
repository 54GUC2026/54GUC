"""
Central modem backend factory.

Uses the registry in arch4/modem/registry.py. To add a new backend,
register it there — do not add elif branches here.
See ADDING_NEW_BACKEND.md for the full guide.
"""

from __future__ import annotations

import logging

from arch4.config_views import (
    build_backend_connection_config,
    build_huawei_connection_config,
    build_huawei_runtime_config,
    build_model_connection_config,
    build_openwrt_connection_config,
    build_signal_contract_config,
    build_zte_connection_config,
    build_zyxel_connection_config,
)
from arch4.interfaces import Arch4Config
from arch4.modem.catalog import supported_backend_help
from arch4.modem.registry import register_backend, create_from_registry

log = logging.getLogger("arch4.modem.factory")


# ── Register all built-in backends ───────────────────────────────────────────

@register_backend("huawei")
@register_backend("h173")
@register_backend("h155")
@register_backend("h122")
@register_backend("h112")
@register_backend("b530")
@register_backend("b535")
@register_backend("b311")
@register_backend("b818")
def _build_huawei(config: Arch4Config):
    from arch4.modem.huawei_b818 import HuaweiHiLinkModem
    huawei_cfg = build_huawei_connection_config(config)
    runtime_cfg = build_huawei_runtime_config(config)
    signal_cfg = build_signal_contract_config(config)
    backend = str(config.modem_backend or "").strip().lower()
    profile_id = str(config.modem_profile_id or "").strip()
    if backend == "h173":
        from arch4.modem_live.profile_catalog import modem_profile_selection
        selected = modem_profile_selection(profile_id)
        if selected is None or selected.backend != backend or selected.family != "huawei_hilink_like":
            raise ValueError("h173_exact_selected_profile_required")
    names = {
        "b818": "B818",
        "b535": "B535",
        "b530": "B530 4G CPE 3",
        "b311": "B311",
        "h112": "5G CPE Pro",
        "h122": "5G CPE Pro 2",
        "h155": "5G CPE 5",
        "huawei": "HiLink-like",
    }
    log.info("Modem backend: Huawei %s (HTTP/XML API)", names.get(backend, "HiLink"))
    return HuaweiHiLinkModem(
        host=huawei_cfg.host,
        username=huawei_cfg.user,
        password=huawei_cfg.password,
        ping_interface=huawei_cfg.ping_interface or None,
        backend_name=backend or "huawei",
        profile_id=profile_id,
        expected_model=huawei_cfg.expected_model,
        expected_serial=huawei_cfg.expected_serial,
        expected_firmware=huawei_cfg.expected_firmware,
        expected_imei=huawei_cfg.expected_imei,
        netmode_submit_timeout_s=runtime_cfg.netmode_submit_timeout_s,
        netmode_continue_on_read_timeout_for_verification=(
            runtime_cfg.netmode_continue_on_read_timeout_for_verification
        ),
        sequential_step_wait_s=runtime_cfg.sequential_step_wait_s,
        pband_wait_s=runtime_cfg.pband_wait_s,
        signal_sinr_scale=signal_cfg.sinr_scale,
        signal_rsrp_scale=signal_cfg.rsrp_scale,
        signal_rsrq_scale=signal_cfg.rsrq_scale,
        signal_rssi_scale=signal_cfg.rssi_scale,
        signal_contract_warn=signal_cfg.contract_warn,
    )

@register_backend("mikrotik", "chateau_5g")
def _build_mikrotik(config: Arch4Config):
    from arch4.modem.mikrotik import MikroTikModem
    log.info("Modem backend: MikroTik (RouterOS API + AT)")
    return MikroTikModem(config)


@register_backend("openwrt", "cudy_p5")
def _build_openwrt(config: Arch4Config):
    from arch4.modem.openwrt import OpenWrtModemAdapter
    log.info("Modem backend: OpenWrt (ModemManager)")
    openwrt_cfg = build_openwrt_connection_config(config)
    return OpenWrtModemAdapter(
        host=openwrt_cfg.host,
        username=openwrt_cfg.user,
        password=openwrt_cfg.password,
        modem_id=openwrt_cfg.modem_id or None,
        ping_interface=openwrt_cfg.ping_interface or None,
        target_guard_model=openwrt_cfg.expected_model,
        target_guard_serial=openwrt_cfg.expected_serial,
        target_guard_firmware=openwrt_cfg.expected_firmware,
        target_guard_imei=openwrt_cfg.expected_imei,
    )


@register_backend("model")
def _build_model(config: Arch4Config):
    from arch4.modem.model_backend import ModemModelBackend
    model_cfg = build_model_connection_config(config)
    log.info(
        "Modem backend: Universal modem model profile (%s)",
        model_cfg.profile_path or model_cfg.profile_name or "built-in",
    )
    return ModemModelBackend(
        profile_name=model_cfg.profile_name,
        profile_path=model_cfg.profile_path,
    )


@register_backend("mf296r")
def _build_zte_goform(config: Arch4Config):
    from arch4.modem.zte_goform import ZteGoformModem
    zte_cfg = build_zte_connection_config(config)
    backend = str(config.modem_backend or "").strip().lower()
    log.info("Modem backend: ZTE %s (goform passive-first)", backend.upper())
    return ZteGoformModem(
        host=zte_cfg.host,
        username=zte_cfg.user,
        password=zte_cfg.password,
        ping_interface=zte_cfg.ping_interface or None,
        backend_name=backend,
        profile_id=config.modem_profile_id,
        expected_model=zte_cfg.expected_model,
        expected_serial=zte_cfg.expected_serial,
        expected_firmware=zte_cfg.expected_firmware,
        expected_imei=zte_cfg.expected_imei,
    )


@register_backend("mc888")
def _build_zte_mc888(config: Arch4Config):
    from arch4.modem.zte_goform import ZteGoformModem
    zte_cfg = build_zte_connection_config(config)
    log.info("Modem backend: ZTE MC888 5G (goform, NR band lock capable)")
    return ZteGoformModem(
        host=zte_cfg.host,
        username=zte_cfg.user,
        password=zte_cfg.password,
        ping_interface=zte_cfg.ping_interface or None,
        backend_name="mc888",
        profile_id=config.modem_profile_id,
        expected_model=zte_cfg.expected_model,
        expected_serial=zte_cfg.expected_serial,
        expected_firmware=zte_cfg.expected_firmware,
        expected_imei=zte_cfg.expected_imei,
    )


@register_backend("g5b")
def _build_zte_g5b(config: Arch4Config):
    from arch4.modem.zte_goform import ZteGoformModem
    zte_cfg = build_zte_connection_config(config)
    log.info("Modem backend: selected ZTE G5B GoForm profile")
    return ZteGoformModem(
        host=zte_cfg.host,
        username=zte_cfg.user,
        password=zte_cfg.password,
        ping_interface=zte_cfg.ping_interface or None,
        backend_name="g5b",
        profile_id=config.modem_profile_id,
        expected_model=zte_cfg.expected_model,
        expected_serial=zte_cfg.expected_serial,
        expected_firmware=zte_cfg.expected_firmware,
        expected_imei=zte_cfg.expected_imei,
    )


@register_backend("zte_zwrt", "g5tc")
def _build_zte_zwrt(config: Arch4Config):
    from arch4.modem.zte_zwrt import ZteZwrtUbusModem
    zte_cfg = build_zte_connection_config(config)
    backend = str(config.modem_backend or "").strip().lower()
    log.info("Modem backend: ZTE zwrt/ubus NG (%s)", backend)
    return ZteZwrtUbusModem(
        host=zte_cfg.host,
        username=zte_cfg.user,
        password=zte_cfg.password,
        ping_interface=zte_cfg.ping_interface or None,
        backend_name=str(config.modem_backend or "").strip().lower(),
        profile_id=config.modem_profile_id,
        expected_model=zte_cfg.expected_model,
        expected_serial=zte_cfg.expected_serial,
        expected_firmware=zte_cfg.expected_firmware,
        expected_imei=zte_cfg.expected_imei,
    )


@register_backend("nr5101", "fwa510", "nr5103e")
def _build_zyxel(config: Arch4Config):
    from arch4.modem.zyxel_webui import ZyxelWebuiModem
    zyxel_cfg = build_zyxel_connection_config(config)
    backend = str(config.modem_backend or "").strip().lower()
    names = {"nr5101": "NR5101", "fwa510": "FWA510"}
    log.info("Modem backend: Zyxel %s (HTTP/JSON web configurator)", names.get(backend, backend.upper()))
    return ZyxelWebuiModem(
        host=zyxel_cfg.host,
        username=zyxel_cfg.user,
        password=zyxel_cfg.password,
        ping_interface=zyxel_cfg.ping_interface or None,
        backend_name=backend,
        expected_model=zyxel_cfg.expected_model,
        expected_serial=zyxel_cfg.expected_serial,
        expected_firmware=zyxel_cfg.expected_firmware,
        expected_imei=zyxel_cfg.expected_imei,
    )


@register_backend("nx520v", "nx520")
def _build_tplink_dev2(config: Arch4Config):
    from arch4.modem.tplink_dev2 import TplinkDev2Modem
    from arch4.modem_live.profile_catalog import modem_profile_selections, modem_profile_selection
    backend = str(config.modem_backend).strip().lower()
    profile_id = str(config.modem_profile_id or '').strip()
    if not profile_id:
        candidates = [row.profile_id for row in modem_profile_selections() if row.backend == backend]
        if len(candidates) != 1:
            raise ValueError('tplink_explicit_device_profile_required')
        profile_id = candidates[0]
    selected = modem_profile_selection(profile_id)
    if selected is None or selected.backend != backend or selected.family != 'tplink_dev2_like':
        raise ValueError('tplink_profile_backend_mismatch')
    conn = build_backend_connection_config(config)
    return TplinkDev2Modem(profile_id=profile_id, host=conn.host, username=conn.user, password=conn.password,
        ping_interface=conn.ping_interface or None, expected_model=conn.expected_model,
        expected_serial=conn.expected_serial, expected_firmware=conn.expected_firmware,
        expected_imei=conn.expected_imei)


@register_backend("xiaomi_cb0401v2")
def _build_xiaomi_cb0401v2(config: Arch4Config):
    from arch4.modem.xiaomi_miwifi import XiaomiMiWiFiModem
    conn = build_backend_connection_config(config)
    return XiaomiMiWiFiModem(host=conn.host, username=conn.user, password=conn.password,
        profile_id=config.modem_profile_id,
        ping_interface=conn.ping_interface or None, expected_model=conn.expected_model,
        expected_serial=conn.expected_serial, expected_firmware=conn.expected_firmware,
        expected_imei=conn.expected_imei)


# ── Public factory function ───────────────────────────────────────────────────

def create_modem_backend(config: Arch4Config):
    """Create the appropriate modem backend for config.modem_backend.

    Uses the registry — no if/elif chain. To add a new backend,
    use @register_backend in arch4/modem/registry.py or any module
    that imports registry and registers before this function is called.
    """
    selected_id=str(getattr(config,'modem_profile_id','')or'')
    result = create_from_registry(config)
    if result is None:
        backend = str(config.modem_backend or "").strip().lower()
        raise ValueError(
            f"Unknown modem_backend: {backend!r}. "
            f"Supported backends: {supported_backend_help()}."
        )
    if selected_id:
        from arch4.modem_live.profile_catalog import modem_profile_definition
        result._portable_definition=modem_profile_definition(selected_id)
    return result
