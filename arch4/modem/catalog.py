"""
Modem backend catalog and family-level onboarding metadata.

This file avoids importing backend implementations so onboarding data
can be reused by interfaces, device monitors, and future black-box
recognition without circular imports.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Dict, List, Optional


FAMILY_HUAWEI_HILINK = "huawei_hilink_like"
FAMILY_ROUTEROS_AT = "routeros_at_like"
FAMILY_OPENWRT_MM = "openwrt_modemmanager_like"
FAMILY_ZTE_WEBUI = "zte_webui_like"
FAMILY_ZTE_ZWRT = "zte_zwrt_ubus_like"
FAMILY_MODEL_EMULATOR = "modem_model_like"
FAMILY_ZYXEL_WEBUI = "zyxel_webui_like"
FAMILY_TPLINK_DEV2 = "tplink_dev2_like"
FAMILY_XIAOMI_MI_WIFI = "xiaomi_miwifi_like"
FAMILY_UNKNOWN = "unknown"

MINIMUM_REQUIRED_OVERLAP = (
    "auth_login",
    "identity_model_fw",
    "serving_cell_rf_read",
    "traffic_session_read",
    "safe_capability_detection",
)


HUAWEI_HILINK_ROUTER_MANAGED_STATIC_CONFIG: Dict[str, bool] = {
    "ttl_ipv4": True,
    "ttl_ipv6": True,
    "mss_ipv4": True,
    "mss_ipv6": True,
    "keepalive": True,
}

ZTE_GOFORM_ROUTER_MANAGED_STATIC_CONFIG: Dict[str, bool] = {
    "ttl_ipv4": True,
    "ttl_ipv6": True,
    "mss_ipv4": True,
    "mss_ipv6": True,
    "keepalive": True,
}

HUAWEI_B535_232_UE_CAPABILITY: Dict[str, Any] = {
    "dl_256qam": False,
    "ul_64qam": True,
    "ue_category": 7,
}

HUAWEI_B535_333_UE_CAPABILITY: Dict[str, Any] = {
    "dl_256qam": True,
    "ul_64qam": True,
    "ue_category": 13,
}

B535_333_UNSUPPORTED_SCHEDULER_FIELD_TRUST: Dict[str, str] = {
    field: "unsupported"
    for field in (
        "cqi",
        "cqi0",
        "cqi1",
        "mcs_dl0",
        "mcs_dl1",
        "mcs_ul",
        "qam_dl0",
        "qam_dl1",
        "qam_ul",
        "ri",
        "trans_mode",
    )
}

MIKROTIK_ROUTEROS_PROFILE_STATIC_CONFIG: Dict[str, bool] = {
    "ttl_ipv4": True,
    "ttl_ipv6": True,
    "mss_ipv4": True,
    "mss_ipv6": True,
    "keepalive": True,
}

MIKROTIK_EG120K_UE_CAPABILITY: Dict[str, Any] = {
    "dl_256qam": True,
    "ul_64qam": "cat12_implied",
    "ue_category": 12,
}


SUPPORT_LEVEL_MONITOR = "monitor_only"
SUPPORT_LEVEL_SAFE_CONTROL = "safe_control"
SUPPORT_LEVEL_ADVANCED_CONTROL = "advanced_control"
SUPPORT_LEVEL_FULL_EXPERT = "full_expert_control"

BAND_CAPABILITY_DECLARED = "declared"
BAND_CAPABILITY_UNSUPPORTED = "unsupported"
BAND_CAPABILITY_LIVE_REQUIRED = "live_required"
BAND_CAPABILITY_STATUSES = frozenset(
    {
        BAND_CAPABILITY_DECLARED,
        BAND_CAPABILITY_UNSUPPORTED,
        BAND_CAPABILITY_LIVE_REQUIRED,
    }
)

# Evidence identifiers are part of the protected profile contract.  A profile
# points to one reviewed source; consumers never interpret these strings as a
# second capability owner.
BAND_REF_QUECTEL_RG650E_EU = (
    "https://www.quectel.com/content/uploads/2024/03/"
    "Quectel_RG650ERG650V_Series_5G_Specification_V1.2.pdf"
)
BAND_REF_QUECTEL_RG520F_EU = (
    "https://www.quectel.com/content/uploads/2026/01/"
    "Quectel_Product_Brochure_V8.5_2026.pdf#RG520F-EU"
)
BAND_REF_QUECTEL_RG502Q_EA = (
    "https://forums.quectel.com/uploads/short-url/"
    "gZbwan0OT4teshliYw6xhFmI1yh.pdf#table-35"
)
BAND_REF_QUECTEL_EG18_EA = (
    "https://quectel.com/content/uploads/2021/03/"
    "Quectel_EG18_Hardware_Design_V1.0.pdf#table-1"
)
BAND_REF_QUECTEL_EG120K_EA = (
    "https://www.quectel.com/content/uploads/2024/03/"
    "Quectel_EG120K_Series_LTE-A_Specification_V1.3-1.pdf"
)
BAND_REF_HUAWEI_B818_263 = (
    "https://www.eurodk.com/files/catalogue/2891/"
    "huaweib8182632-5f22cd7757a9a.pdf#working-band"
)
BAND_REF_HUAWEI_B535_232 = (
    "https://www.brommeland.no/file/standardmappe-uni/"
    "huawei-b535-232-product-description-v100r001_03english.pdf"
    "#working-band"
)
BAND_REF_HUAWEI_B535_333 = (
    "https://www.getic.de/files/catalogue/3697/"
    "huaweib535333ltecpeproductdescriptionv100r001_02english-"
    "61385e265f32a.pdf#working-band"
)
BAND_REF_HUAWEI_H122_373 = (
    "https://www.communica.se/huawei/H122-373.pdf#working-band"
)
BAND_REF_HUAWEI_H155_381 = (
    "https://device.report/m/"
    "73399e555e3c17e40b3c16be3c553e3437f94ebc5aab9650aeae026339860ced"
    "#working-band"
)
BAND_REF_HUAWEI_H153_381 = (
    "https://device.report/m/"
    "fda550a04222bf63623b961ad3e754ee47d9dd907b15537848964944076bee76"
    "#working-band"
)
BAND_REF_HUAWEI_H112 = (
    "docs/CGF_MUST_READ/VB5_3_CODE_COMMENTS_COMPENDIUM_2026-07-15.md"
    "#h112-370-h112-372-hardware-split"
)
BAND_REF_ZTE_MC888_EU = (
    "https://oss.ztedevices.com/prod/cn/direct/mc888/"
    "MC888%20Quick%20Start%20Guide%20V1.0_RED-0407.pdf"
    "#eu-frequency-bands"
)
BAND_REF_ZTE_MC801A_EU = (
    "https://www.t-mobile.cz/dcpublic/zte_mc801a_en_rychly_pruvodce.pdf"
    "#eu-frequency-bands"
)
BAND_REF_LIVE_REQUIRED = (
    "docs/CGF_MUST_READ/"
    "CGF_MODEM_LIVE_EFFECTIVE_DEVICE_MATRIX_REPLACEMENT_2026-07-23.md"
    "#production-profile-completeness-replacement"
)


@dataclass(frozen=True)
class BackendActionSemantics:
    """Vendor-neutral control semantics exposed to higher layers.

    Boolean capability flags remain supported for backward compatibility, but
    they are too shallow for a universal 4G/5G controller. This structure
    describes *how* a backend supports an action so policy/UI can reason about
    partial support without vendor-specific branching.

    Relationship to ModemCapabilities boolean flags:

    - ``traffic_visibility`` describes what the backend can *observe*
      (none / presence / counters / full). Traffic capability flags now
      default optimistic-true by policy, so real backends should advertise
      at least presence/counters visibility unless they are explicitly proven
      traffic-blind.

    - ``ca_visibility`` describes the depth of carrier aggregation info the
      backend can report (none / basic / detailed). ``supports_ca_details``
      in capabilities means the backend provides per-component SCC info
      (band, earfcn, PCI). If ``supports_ca_details`` is True, then
      ``ca_visibility`` should be ``detailed``.

    - ``nr_control_mode`` describes the granularity of NR control
      (none / nsa_band / sa_band / rat_mode / mixed). ``supports_nr_band_lock``
      in capabilities means the backend can lock to specific NR band numbers.
      ``rat_mode`` means the backend can toggle 4G/5G mode selection only —
      it should NOT pair with ``supports_nr_band_lock = True``. Use ``mixed``
      when both RAT mode and NR band lock are available.
    """

    band_control_mode: str = "none"          # none / allowlist / lock_single / lock_combo
    pband_control_mode: str = "none"         # none / hilink_netmode_two_step / routeros_band_mask_stack
    ca_pool_control_mode: str = "none"       # none / hilink_netmode_allowlist / routeros_band_mask_allowlist
    cell_control_mode: str = "none"          # none / pci / pci_earfcn / full_cell
    cell_lock_control_mode: str = "none"     # none / qnwlock_earfcn_pci / gtcelllock_earfcn_pci
    nr_control_mode: str = "none"            # none / nsa_band / sa_band / rat_mode / mixed
    restart_mode: str = "none"               # none / interface / radio / soft_reboot / full_reboot
    action_persistence: str = "temporary"    # temporary / session / persistent_nvram
    traffic_presence_mode: str = "runtime_54guc"  # none / runtime_54guc / modem_native
    traffic_visibility: str = "none"         # none / presence / counters / full
    ca_visibility: str = "none"              # none / basic / detailed
    safety_grade: str = "unknown"            # read_only / constrained / safe / expert
    notes: str = ""


BACKEND_SUPPORT_LEVELS: Dict[str, str] = {
    "xiaomi_cb0401v2": SUPPORT_LEVEL_SAFE_CONTROL,
    "nx520": SUPPORT_LEVEL_SAFE_CONTROL,
    "nx520v": SUPPORT_LEVEL_SAFE_CONTROL,
    "mikrotik": SUPPORT_LEVEL_FULL_EXPERT,
    "b818": SUPPORT_LEVEL_ADVANCED_CONTROL,
    "b535": SUPPORT_LEVEL_ADVANCED_CONTROL,
    "b530": SUPPORT_LEVEL_ADVANCED_CONTROL,
    "huawei": SUPPORT_LEVEL_SAFE_CONTROL,
    "h112": SUPPORT_LEVEL_ADVANCED_CONTROL,
    "h122": SUPPORT_LEVEL_ADVANCED_CONTROL,
    "openwrt": SUPPORT_LEVEL_MONITOR,
    "mf296r": SUPPORT_LEVEL_SAFE_CONTROL,
    "zte_zwrt": SUPPORT_LEVEL_ADVANCED_CONTROL,
    "g5tc": SUPPORT_LEVEL_ADVANCED_CONTROL,
    "h155": SUPPORT_LEVEL_ADVANCED_CONTROL,
    "nr5103e": SUPPORT_LEVEL_SAFE_CONTROL,
    "chateau_5g": SUPPORT_LEVEL_FULL_EXPERT,
    "cudy_p5": SUPPORT_LEVEL_MONITOR,
    "deco_x50_5g": SUPPORT_LEVEL_MONITOR,
    "mc888": SUPPORT_LEVEL_ADVANCED_CONTROL,
    "g5b": SUPPORT_LEVEL_ADVANCED_CONTROL,
    "model": SUPPORT_LEVEL_FULL_EXPERT,
    "nr5101": SUPPORT_LEVEL_SAFE_CONTROL,
    "fwa510": SUPPORT_LEVEL_SAFE_CONTROL,
}


BACKEND_ACTION_SEMANTICS: Dict[str, BackendActionSemantics] = {
    "h173": BackendActionSemantics(band_control_mode="allowlist", pband_control_mode="none",
        ca_pool_control_mode="hilink_partial_lte_allowlist_assumption", nr_control_mode="none",
        traffic_visibility="counters", ca_visibility="basic", safety_grade="constrained",
        notes="C983 software reference. Partial LTE mask preserves native RAT/NR choice; other control dialects are not admitted."),
    "nx520": BackendActionSemantics(band_control_mode="allowlist", pband_control_mode="tplink_dev2_single_band_then_pool",
        ca_pool_control_mode="tplink_dev2_band_allowlist", nr_control_mode="nsa_band",
        restart_mode="soft_reboot",
        traffic_visibility="presence", ca_visibility="detailed", safety_grade="constrained",
        notes="DEV2 LTE allowlist encoding; exact250902 profile alone admits best-effort single-band then pool. Profile capability gates remain mandatory; no native primary-cell control claim."),
    "xiaomi_cb0401v2": BackendActionSemantics(band_control_mode="allowlist", pband_control_mode="none",
        ca_pool_control_mode="xiaomi_stock_band_allowlist", nr_control_mode="mixed",
        traffic_visibility="none", ca_visibility="none", safety_grade="constrained",
        notes="Conditional 3.0.88 stock LuCI, preceding 3.0.85 reference. LTE/NR allowlists and read diagnostics; no APN, cell, reboot or Wi-Fi writes."),
    "nx520v": BackendActionSemantics(band_control_mode="allowlist", pband_control_mode="none",
        ca_pool_control_mode="tplink_dev2_band_allowlist", nr_control_mode="mixed",
          traffic_visibility="presence", ca_visibility="detailed", safety_grade="constrained",
        notes="Exact EU1 260408 encrypted DEV2, software/simulator qualified only. No forced anchor, cell lock, Wi-Fi or restart qualification."),
    "mikrotik": BackendActionSemantics(
        band_control_mode="allowlist",
        pband_control_mode="routeros_band_mask_stack",
        ca_pool_control_mode="routeros_band_mask_allowlist",
        cell_control_mode="none",
        cell_lock_control_mode="routeros_at_chat_qnwlock_guarded",
        nr_control_mode="rat_mode",
        restart_mode="radio",
        action_persistence="session",
        traffic_presence_mode="runtime_54guc",
        traffic_visibility="full",
        ca_visibility="detailed",
        safety_grade="expert",
        notes=(
            "RouterOS is authority for LTE band allowlist control. Runtime keepalive is owned by "
            "54GUC so missing RouterOS scheduler entries do not become failed modem actions. PBAND anchoring is attempted by "
            "a RouterOS single-band mask followed by CA-pool restore; PCI cell lock uses guarded "
            "RouterOS at-chat QNWLOCK and should be treated as an expert action."
        ),
    ),
    "b818": BackendActionSemantics(
        band_control_mode="allowlist",
        pband_control_mode="hilink_netmode_two_step",
        ca_pool_control_mode="hilink_netmode_allowlist",
        cell_control_mode="none",
        nr_control_mode="none",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="basic",
        safety_grade="safe",
        notes="Huawei HiLink class supports family-safe radio control, but not full cell locking.",
    ),
    "b535": BackendActionSemantics(
        band_control_mode="allowlist",
        pband_control_mode="hilink_netmode_two_step",
        ca_pool_control_mode="hilink_netmode_allowlist",
        cell_control_mode="none",
        nr_control_mode="none",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="basic",
        safety_grade="safe",
        notes="Huawei HiLink 4G family control surface.",
    ),
    "b530": BackendActionSemantics(
        band_control_mode="allowlist",
        pband_control_mode="hilink_netmode_two_step",
        ca_pool_control_mode="hilink_netmode_allowlist",
        cell_control_mode="none",
        nr_control_mode="none",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="basic",
        safety_grade="safe",
        notes="Legacy HiLink 4G family control surface.",
    ),
    "h112": BackendActionSemantics(
        band_control_mode="allowlist",
        pband_control_mode="hilink_netmode_two_step",
        ca_pool_control_mode="hilink_netmode_allowlist",
        cell_control_mode="none",
        nr_control_mode="rat_mode",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="basic",
        safety_grade="safe",
        notes="Huawei 5G CPE family with radio-mode control but limited cell-level authority.",
    ),
    "h122": BackendActionSemantics(
        band_control_mode="allowlist",
        pband_control_mode="hilink_netmode_two_step",
        ca_pool_control_mode="hilink_netmode_allowlist",
        cell_control_mode="none",
        nr_control_mode="rat_mode",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="basic",
        safety_grade="safe",
        notes="Huawei 5G CPE family with radio-mode control but limited cell-level authority.",
    ),
    "openwrt": BackendActionSemantics(
        band_control_mode="none",
        pband_control_mode="none",
        ca_pool_control_mode="none",
        cell_control_mode="none",
        cell_lock_control_mode="none",
        nr_control_mode="none",
        restart_mode="none",
        action_persistence="temporary",
        traffic_visibility="counters",
        ca_visibility="basic",
        safety_grade="read_only",
        notes=(
            "Standard OpenWrt rpcd/ModemManager remote monitor. The controller "
            "does not execute local mmcli, raw serial or UCI writes. Physical "
            "controls stay unavailable until a separately proven remote API owns them."
        ),
    ),
    "mf296r": BackendActionSemantics(
        band_control_mode="allowlist",
        pband_control_mode="zte_goform_single_band_then_pool",
        ca_pool_control_mode="zte_goform_band_mask_allowlist",
        cell_control_mode="none",
        nr_control_mode="none",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="detailed",
        safety_grade="constrained",
        notes="ZTE MF296R goform backend. Rich telemetry and LTE band-mask control via authenticated goform API. Cell and NR control remain unavailable on A1SRB B04.",
    ),
    "zte_zwrt": BackendActionSemantics(
        band_control_mode="allowlist",
        cell_control_mode="pci_earfcn",
        nr_control_mode="mixed",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="detailed",
        safety_grade="safe",
        notes="ZTE NG ubus API (G5TC and later). Band lock, cell lock, NR band lock via zte_nwinfo_api. Same data model as goform, different transport.",
    ),
    "g5tc": BackendActionSemantics(
        band_control_mode="allowlist",
        cell_control_mode="pci_earfcn",
        nr_control_mode="mixed",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="detailed",
        safety_grade="safe",
        notes="ZTE G5TC (Drei, Snapdragon X72). Uses zte_zwrt ubus backend. Full band/cell/NR control via zte_nwinfo_api.",
    ),
    "h155": BackendActionSemantics(
        band_control_mode="allowlist",
        cell_control_mode="none",
        nr_control_mode="rat_mode",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="detailed",
        safety_grade="safe",
        notes="Huawei 5G CPE 5 (H155-381, Brovi). HiLink API at 192.168.8.1. Same family as B818/H112/H122. Balong 5000 chipset, 5G NSA/SA.",
    ),
    "huawei": BackendActionSemantics(
        band_control_mode="allowlist",
        pband_control_mode="hilink_netmode_two_step",
        ca_pool_control_mode="hilink_netmode_allowlist",
        cell_control_mode="none",
        nr_control_mode="rat_mode",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="basic",
        safety_grade="constrained",
        notes="Generic Huawei HiLink-like lane. Exact rows must override tighter firmware facts when known.",
    ),
    "nr5103e": BackendActionSemantics(
        band_control_mode="allowlist",
        cell_control_mode="pci_earfcn",
        nr_control_mode="none",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="detailed",
        safety_grade="constrained",
        notes="Zyxel NR5103E (SD X62). Same web UI family as NR5101/FWA510 at 192.168.1.1. Band lock available, cell lock carrier-dependent.",
    ),
    "chateau_5g": BackendActionSemantics(
        band_control_mode="lock_combo",
        cell_control_mode="none",
        nr_control_mode="mixed",
        restart_mode="radio",
        action_persistence="session",
        traffic_visibility="full",
        ca_visibility="detailed",
        safety_grade="expert",
        notes="MikroTik Chateau 5G ax (SD X62, Quectel RG502Q-EA). RouterOS v7 owns the safe band/NR surface; deterministic PCI cell lock is not exposed.",
    ),
    "cudy_p5": BackendActionSemantics(
        band_control_mode="allowlist",
        cell_control_mode="none",
        nr_control_mode="none",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="basic",
        safety_grade="constrained",
        notes="Cudy P5 (SD X62 Quectel RM520N, IPQ5018). OpenWrt/LuCI based. Band lock in web UI. mmcli/AT over USB internally. SSH locked on stock firmware.",
    ),
    "deco_x50_5g": BackendActionSemantics(
        band_control_mode="none",
        cell_control_mode="none",
        nr_control_mode="none",
        restart_mode="none",
        action_persistence="temporary",
        traffic_visibility="counters",
        ca_visibility="none",
        safety_grade="read_only",
        notes="TP-Link Deco X50-5G (SD X62). Proprietary Deco app management. Very limited web UI. Band lock buggy per community reports. No public API discovered.",
    ),
    "mc888": BackendActionSemantics(
        band_control_mode="allowlist",
        cell_control_mode="pci_earfcn",
        nr_control_mode="mixed",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="detailed",
        safety_grade="safe",
        notes="ZTE 5G goform family supports LTE band selection, PCI/EARFCN cell locking, reboot, and NR band control on compatible firmware.",
    ),
    "g5b": BackendActionSemantics(
        band_control_mode="allowlist",
        cell_control_mode="pci_earfcn",
        nr_control_mode="mixed",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="detailed",
        safety_grade="safe",
        notes="ZTE 5G goform family supports LTE band selection, PCI/EARFCN cell locking, reboot, and NR band control on compatible firmware.",
    ),
    "model": BackendActionSemantics(
        band_control_mode="lock_combo",
        cell_control_mode="full_cell",
        nr_control_mode="mixed",
        restart_mode="radio",
        action_persistence="session",
        traffic_visibility="full",
        ca_visibility="detailed",
        safety_grade="expert",
        notes="Synthetic backend used for full control-path validation and replay.",
    ),
    "nr5101": BackendActionSemantics(
        band_control_mode="none",
        cell_control_mode="none",
        nr_control_mode="none",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="basic",
        safety_grade="constrained",
        notes="Zyxel backend is live but still awaiting API discovery for band/cell operations.",
    ),
    "fwa510": BackendActionSemantics(
        band_control_mode="none",
        cell_control_mode="none",
        nr_control_mode="none",
        restart_mode="soft_reboot",
        action_persistence="persistent_nvram",
        traffic_visibility="counters",
        ca_visibility="basic",
        safety_grade="constrained",
        notes="Zyxel backend is live but still awaiting API discovery for band/cell operations.",
    ),
}

_PROFILE_ACTION_NAMES: tuple[str, ...] = (
    "radio_mode",
    "band_selection",
    "band_lock",
    "cell_lock",
    "antenna_mode",
    "wifi_toggle",
    "network_settings",
    "mobile_data",
    "restart",
    "nr_band_lock",
    "pband",
)

_PROFILE_EMPTY_BACKEND_OVERRIDES: Dict[str, str] = {
    "huawei_h122_373_three_uk": "h122",
    "huawei_h122_373_three_uk_c1217": "h122",
    "huawei_h122_373_global_c00": "h122",
    "huawei_h122_373_cpe_pro2": "h122",
    "huawei_h155_381_cpe5": "h155",
    "huawei_h153_381_cpe5s": "h155",
    "huawei_hilink_generic_baseline": "huawei",
    "zte_webui_goform_baseline": "mc888",
    "zte_zwrt_ubus_baseline": "zte_zwrt",
}


def profile_launch_backend(profile_id: Any, backend: Any) -> str:
    """Return the concrete transport backend for one exact profile row."""
    direct = str(backend or "").strip().lower()
    if direct:
        return direct
    return _PROFILE_EMPTY_BACKEND_OVERRIDES.get(str(profile_id or "").strip(), "")


def _action_supported_by_contract(
    action: str,
    *,
    capabilities: Dict[str, bool],
    semantics: BackendActionSemantics,
    action_trust: str,
) -> bool:
    trust = str(action_trust or "").strip().lower()
    if trust == "unsupported":
        return False
    if action == "radio_mode":
        return bool(capabilities.get("supports_radio_mode"))
    if action == "band_selection":
        return bool(capabilities.get("supports_band_selection")) and semantics.band_control_mode != "none"
    if action == "band_lock":
        return bool(capabilities.get("supports_band_lock") or capabilities.get("supports_band_selection")) and semantics.band_control_mode != "none"
    if action == "cell_lock":
        return bool(capabilities.get("supports_cell_lock")) and (
            semantics.cell_control_mode != "none" or semantics.cell_lock_control_mode != "none"
        )
    if action == "mobile_data":
        return bool(capabilities.get("supports_mobile_data"))
    if action == "network_settings":
        return bool(capabilities.get("supports_network_settings"))
    if action == "wifi_toggle":
        return bool(capabilities.get("supports_wifi_control"))
    if action == "restart":
        return bool(capabilities.get("supports_restart")) and semantics.restart_mode != "none"
    if action == "nr_band_lock":
        return bool(capabilities.get("supports_nr_band_lock")) and semantics.nr_control_mode in {
            "nsa_band",
            "sa_band",
            "mixed",
        }
    if action == "pband":
        return bool(capabilities.get("supports_pband")) and semantics.pband_control_mode != "none"
    if action == "antenna_mode":
        # Antenna status is readable; no typed Core switch exists.
        return False
    return False


def _default_action_trust(
    action: str,
    *,
    capabilities: Dict[str, bool],
    semantics: BackendActionSemantics,
) -> str:
    return (
        "supported"
        if _action_supported_by_contract(
            action,
            capabilities=capabilities,
            semantics=semantics,
            action_trust="supported",
        )
        else "unsupported"
    )


CONSERVATIVE_CAPABILITY_FLAGS: Dict[str, bool] = {
    "supports_radio_mode": False,
    "supports_band_selection": False,
    "supports_band_lock": False,
    "supports_cell_lock": False,
    "supports_traffic_presence": True,
    "supports_traffic_volume": True,
    "supports_ue_capability": False,
    "supports_restart": False,
    "supports_neighbors": False,
    "supports_sms": False,
    "supports_ca_details": False,
    "supports_band_priority": False,
    "supports_plmn": False,
    "supports_pband": False,
    "supports_wifi_control": False,
    "supports_network_settings": False,
    "supports_mobile_data": False,
    "supports_nr_band_lock": False,
}


@dataclass(frozen=True)
class BackendCatalogEntry:
    backend: str
    family: str
    vendor: str
    display_name: str
    transports: tuple[str, ...]
    implemented: bool
    capabilities: Dict[str, bool]

    def __post_init__(self) -> None:
        # Conservative defaults should remain explicitly false for lane-agnostic
        # backends. PBAND anchor must never outrun the backend's actual
        # band-selection support.
        raw_caps = dict(self.capabilities or {})
        caps = dict(CONSERVATIVE_CAPABILITY_FLAGS)
        caps.update(raw_caps)
        if "supports_sms" not in raw_caps:
            caps["supports_sms"] = bool(
                self.implemented
                and self.family in {FAMILY_HUAWEI_HILINK, FAMILY_ZTE_WEBUI}
            )
        else:
            caps["supports_sms"] = bool(caps["supports_sms"] and self.implemented)
        explicit = raw_caps.get("supports_pband", None)
        pband_supported = (
            bool(self.implemented and caps.get("supports_band_selection", False))
            if explicit is None
            else bool(explicit and self.implemented and caps.get("supports_band_selection", False))
        )
        caps["supports_pband"] = pband_supported
        object.__setattr__(self, "capabilities", caps)


@dataclass(frozen=True)
class FirmwareProfileOverlay:
    """Model/firmware/operator-specific profile overlay.

    Backend-level capabilities stay conservative for control levers, while
    traffic support now defaults optimistic-true until disproven. This overlay
    records the narrower, observed contract for carrier firmware builds without
    leaking vendor command names above the modem boundary.

    Provenance records where the truth came from:
      "observed"    — confirmed on live hardware
      "documented"  — from vendor datasheet/spec, not live-tested
      "inferred"    — derived from family similarity, lower confidence
      "community"   — from community forums/research, not vendor-verified

    field_trust maps 54GUC field names to trust levels:
      "confirmed"   — live-tested, data known good
      "expected"    — from spec/family, should work but not live-verified
      "synthetic"   — 54GUC computes this, modem doesn't report it directly
      "untested"    — present in API but not verified for correctness
      "unsupported" — modem does not expose this field
      "non_useful"  — field is computed but not meaningful on this exact lane

    Optional selectors make the support matrix explicit when a family needs
    more than just model/firmware matching:
      vendor_aliases — alternate brand/OEM names for the same hardware lane
      provider_name_contains — carrier/operator text hints
      provider_plmn_contains — exact PLMN hints
      os_name / os_version_contains — host/router OS generation hints
    """

    profile_id: str
    backend: str
    vendor: str
    family: str
    model_contains: tuple[str, ...]
    firmware_contains: tuple[str, ...]
    display_name: str
    capabilities: Dict[str, bool]
    supported_fields: tuple[str, ...]
    unsupported_fields: tuple[str, ...]
    synthetic_fields: Dict[str, str]
    lte_band_capability_status: str
    nr_band_capability_status: str
    device_radio_class: str
    band_capability_reference: str
    notes: str
    provenance: str = "inferred"
    field_trust: Dict[str, str] = None  # type: ignore[assignment]
    action_trust: Dict[str, str] = None  # type: ignore[assignment]
    static_config: Dict[str, Any] = None  # type: ignore[assignment]
    ue_capability: Dict[str, Any] = None  # type: ignore[assignment]
    scheduler_signal_mode: str = "auto"  # auto | none | cqi | mcs | dual
    provider_name_contains: tuple[str, ...] = ()
    provider_plmn_contains: tuple[str, ...] = ()
    os_name: str = ""
    os_version_contains: tuple[str, ...] = ()
    vendor_aliases: tuple[str, ...] = ()
    target_guard_model: str = ""
    supported_lte_bands: tuple[int, ...] = ()
    supported_nr_bands: tuple[int, ...] = ()
    # Provider-defined pool when this exact firmware cannot report its pool.
    # This is not hardware band capability or observed secondary carriers.
    provider_band_pool: tuple[int, ...] = ()
    network_settings: str = ""  # Exact-profile protocol contract; never inherited.
    network_settings_writable: bool = True  # Read dialect alone does not grant writes.
    apn_profile_creation: bool = False  # Explicit native user-profile creation qualification.
    network_settings_firmware: tuple[str, ...] = ()
    apn_write_requires_explicit_password: bool = False
    firmware_exact: str = ""
    hardware_exact: str = ""
    firmware_pattern: str = ""
    authentication_contract: str = ""
    # A password-only WebUI may use a fixed, hidden wire account. This is an
    # exact-profile authentication adapter, never a sequence of login retries.
    login_wire_username: str = ""
    nr_cell_id_in_nsa: bool = True
    traffic_rate_mode: str = "reported"
    read_diagnostic_extra_oids: tuple[str, ...] = ()
    # Native implementation binding is separate from broad matching scope.
    # Explicit data lets copied profiles keep working under a new filename.
    driver: str = ""

    def native_firmware_qualified(self, firmware: str) -> bool:
        value = str(firmware or '').strip()
        if self.firmware_exact:
            return value == self.firmware_exact
        if self.firmware_pattern:
            from arch4.modem_live.profile_definition.bounded_regex import RegexBudget
            try:return len(value)<=256 and RegexBudget().call('fullmatch',self.firmware_pattern,value)is not None
            except ValueError:return False
        return True

    def network_settings_qualified(self, firmware: str) -> bool:
        return bool(self.network_settings) and self.native_firmware_qualified(firmware) and (not self.network_settings_firmware or firmware in self.network_settings_firmware)

    def __post_init__(self) -> None:
        if self.traffic_rate_mode not in {"reported", "cellular_interface_counter_delta"}:
            raise ValueError(f'{self.profile_id}: invalid traffic rate source')
        if self.firmware_exact and self.firmware_pattern:
            raise ValueError(f'{self.profile_id}: conflicting firmware identity selectors')
        if self.firmware_pattern:
            re.compile(self.firmware_pattern)
        # frozen=True prevents assignment, use object.__setattr__
        raw_caps = dict(self.capabilities or {})
        backend_entry = BACKEND_CATALOG.get(str(self.backend or "").strip().lower())
        caps = dict(CONSERVATIVE_CAPABILITY_FLAGS)
        if backend_entry is not None:
            caps.update(backend_entry.capabilities)
        caps.update(raw_caps)
        caps["supports_network_settings"] = bool(self.network_settings) and self.network_settings_writable
        # PBAND is a 54GUC-local lever built from ordinary band
        # selection. The backend family decides the exact safe sequence.
        explicit_pband = raw_caps.get("supports_pband", None)
        if explicit_pband is not None:
            caps["supports_pband"] = bool(
                explicit_pband and caps.get("supports_band_selection", False)
            )
        object.__setattr__(self, "capabilities", caps)
        if self.device_radio_class not in {"4g_only", "5g", "unclassified"}:
            raise ValueError(f"{self.profile_id}: invalid device radio class")
        if self.device_radio_class == "4g_only" and self.nr_band_capability_status != "unsupported":
            raise ValueError(f"{self.profile_id}: 4G-only profile cannot declare NR")
        if any(type(band) is not int or band <= 0 for band in self.provider_band_pool):
            raise ValueError(f"{self.profile_id}: invalid provider band pool")
        for field_name in ("supported_lte_bands", "supported_nr_bands", "provider_band_pool"):
            raw_bands = tuple(getattr(self, field_name) or ())
            normalized = tuple(sorted({int(value) for value in raw_bands if int(value) > 0}))
            object.__setattr__(self, field_name, normalized)
        lte_status = str(self.lte_band_capability_status or "").strip().lower()
        nr_status = str(self.nr_band_capability_status or "").strip().lower()
        reference = str(self.band_capability_reference or "").strip()
        if lte_status not in BAND_CAPABILITY_STATUSES:
            raise ValueError(
                f"{self.profile_id}: invalid LTE band capability status"
            )
        if nr_status not in BAND_CAPABILITY_STATUSES:
            raise ValueError(
                f"{self.profile_id}: invalid NR band capability status"
            )
        if not reference:
            raise ValueError(
                f"{self.profile_id}: band capability evidence reference required"
            )
        for rat, status, bands in (
            ("LTE", lte_status, self.supported_lte_bands),
            ("NR", nr_status, self.supported_nr_bands),
        ):
            if status == BAND_CAPABILITY_DECLARED and not bands:
                raise ValueError(
                    f"{self.profile_id}: {rat} declared capability requires bands"
                )
            if status != BAND_CAPABILITY_DECLARED and bands:
                raise ValueError(
                    f"{self.profile_id}: {rat} {status} capability must not carry bands"
                )
        object.__setattr__(self, "lte_band_capability_status", lte_status)
        object.__setattr__(self, "nr_band_capability_status", nr_status)
        object.__setattr__(self, "band_capability_reference", reference)
        supported = {str(field).strip() for field in (self.supported_fields or ()) if str(field).strip()}
        unsupported = {str(field).strip() for field in (self.unsupported_fields or ()) if str(field).strip()}

        if nr_status == BAND_CAPABILITY_UNSUPPORTED:
            nr_fields = {
                "nsa_active", "sa_active", "nr5g_active", "nr_mode",
                "nr_state_text", "nr_band", "nr5g_band", "nr_pci",
                "nr5g_pci", "nr_arfcn", "nr5g_arfcn", "nr_cell_id",
                "nr5g_cell_id", "nr_rsrp_dbm", "nr5g_rsrp_dbm",
                "nr_rsrq_db", "nr5g_rsrq_db", "nr_sinr_db",
                "nr5g_sinr_db", "nr_cells", "nr5g_cells",
                "nr_bandwidth_mhz",
            }
            supported.difference_update(nr_fields)
            unsupported.update(nr_fields)

        def _promote_canonical(fields: tuple[str, ...]) -> None:
            for field in fields:
                unsupported.discard(field)
                supported.add(field)

        if caps.get("supports_traffic_presence"):
            _promote_canonical(("traffic_presence",))
        if caps.get("supports_traffic_volume"):
            _promote_canonical(("traffic_volume",))
        if caps.get("supports_band_selection") or caps.get("supports_band_lock"):
            _promote_canonical(
                (
                    "selected_band_set",
                    "selected_band_set_signature",
                    "configured_band_pool",
                    "configured_band_pool_signature",
                )
            )
        original_supported = set(self.supported_fields or ())
        original_unsupported = set(self.unsupported_fields or ())
        object.__setattr__(
            self,
            "supported_fields",
            tuple(field for field in self.supported_fields if field in supported)
            + tuple(sorted(supported - original_supported)),
        )
        object.__setattr__(
            self,
            "unsupported_fields",
            tuple(field for field in self.unsupported_fields if field in unsupported)
            + tuple(sorted(unsupported - original_unsupported)),
        )
        object.__setattr__(
            self,
            "provider_name_contains",
            tuple(str(token).strip() for token in (self.provider_name_contains or ()) if str(token).strip()),
        )
        object.__setattr__(
            self,
            "provider_plmn_contains",
            tuple(str(token).strip() for token in (self.provider_plmn_contains or ()) if str(token).strip()),
        )
        object.__setattr__(self, "os_name", str(self.os_name or "").strip())
        object.__setattr__(
            self,
            "os_version_contains",
            tuple(str(token).strip() for token in (self.os_version_contains or ()) if str(token).strip()),
        )
        object.__setattr__(
            self,
            "vendor_aliases",
            tuple(str(token).strip() for token in (self.vendor_aliases or ()) if str(token).strip()),
        )
        explicit_field_trust = {
            str(field).strip(): str(trust).strip()
            for field, trust in dict(self.field_trust or {}).items()
            if str(field).strip() and str(trust).strip()
        }
        field_trust: Dict[str, str] = {}
        default_supported_trust = "confirmed" if self.provenance == "observed" else "expected"
        for field in self.supported_fields or ():
            field_trust[str(field)] = default_supported_trust
        for field in self.unsupported_fields or ():
            field_trust[str(field)] = "unsupported"
        for field in (self.synthetic_fields or {}):
            field_trust[str(field)] = "synthetic"
        field_trust.update(explicit_field_trust)
        object.__setattr__(self, "field_trust", field_trust)
        if self.static_config is None:
            object.__setattr__(self, "static_config", {})
        if self.ue_capability is None:
            object.__setattr__(self, "ue_capability", {})
        resolved_backend = self.driver or profile_launch_backend(self.profile_id, self.backend)
        semantics = BACKEND_ACTION_SEMANTICS.get(resolved_backend, BackendActionSemantics())
        at = {
            str(action): str(trust).strip()
            for action, trust in dict(self.action_trust or {}).items()
            if str(action).strip() and str(trust).strip()
        }
        for action in _PROFILE_ACTION_NAMES:
            at.setdefault(
                action,
                _default_action_trust(
                    action,
                    capabilities=caps,
                    semantics=semantics,
                ),
            )
        if not bool(caps.get("supports_pband", False)):
            at["pband"] = "unsupported"
        else:
            at.setdefault("pband", "supported")
        object.__setattr__(self, "action_trust", at)

    def get_field_trust(self, field: str) -> str:
        """Return trust level for a field, with fallback logic.

        Priority: explicit field_trust > synthetic > unsupported > supported > 'untested'.
        Synthetic is checked before supported because a field can appear in both
        (e.g. session_uptime_s listed in supported_fields but synthesized by 54GUC).
        The explicit field_trust dict always wins over any list membership.
        """
        ft = self.field_trust or {}
        if field in ft:
            return ft[field]
        if field in (self.synthetic_fields or {}):
            return "synthetic"
        if field in self.unsupported_fields:
            return "unsupported"
        if field in self.supported_fields:
            return "confirmed" if self.provenance == "observed" else "expected"
        return "untested"

    def get_action_trust(self, action: str) -> str:
        """Return Layer 3 trust level for an action on this firmware profile.

        Action names use 54GUC internal action vocabulary (lowercase snake_case):
          band_selection, band_lock, cell_lock, antenna_mode, wifi_toggle,
          restart, nr_band_lock, pband, radio_mode

        Trust levels:
          "supported"   — action is known to work reliably on this firmware
          "degraded"    — action works but with documented caveats
                          (e.g. requires restart to take effect, intermittent,
                           firmware-version-specific quirks)
          "unsupported" — action is not available / known to fail
          "untested"    — no profile-specific data; fall back to backend-level truth

        Priority: explicit action_trust entry > 'untested'.
        """
        at = self.action_trust or {}
        return str(at.get(action) or "untested")

    def get_scheduler_signal_mode(self) -> str:
        """Return the scheduler signal authority for this runtime profile.

        Modes:
          none — no trustworthy native scheduler signal
          cqi  — use vendor/native CQI as scheduler truth
          mcs  — use native MCS as scheduler truth
          dual — both are reported; prefer MCS when valid, otherwise CQI
          auto — derive from supported_fields
        """
        mode = str(self.scheduler_signal_mode or "auto").strip().lower()
        if mode in {"none", "cqi", "mcs", "dual"}:
            return mode
        supported = set(self.supported_fields or ())
        has_cqi = any(field in supported for field in ("cqi", "cqi0", "cqi1"))
        has_mcs = any(field in supported for field in ("mcs_dl0", "mcs_dl1", "mcs_ul"))
        if has_cqi and has_mcs:
            return "dual"
        if has_cqi:
            return "cqi"
        if has_mcs:
            return "mcs"
        return "none"


@dataclass(frozen=True)
class CandidateRouterProfile:
    vendor: str
    model: str
    family: str
    reuse_backend_hint: str
    transports: tuple[str, ...]
    overlay_strategy: str
    overlay_risk: str
    required_overlap: tuple[str, ...]
    notes: str


BACKEND_CATALOG: Dict[str, BackendCatalogEntry] = {
    "h173": BackendCatalogEntry(backend="h173", family=FAMILY_HUAWEI_HILINK, vendor="Huawei",
        display_name="H173-383 C983 reference (conditional)", transports=("hilink_http_xml",), implemented=True,
        capabilities={"supports_band_selection":True,"supports_band_lock":True,"supports_pband":False,
            "supports_nr_band_lock":False,"supports_traffic_presence":True,"supports_traffic_volume":True}),
    "nx520": BackendCatalogEntry(backend="nx520", family=FAMILY_TPLINK_DEV2, vendor="TP-Link",
        display_name="NX520 SRBA1 20250915 (DEV2 assumption)", transports=("tplink_encrypted_dev2_cbc512",), implemented=True,
        capabilities={"supports_band_selection":True,"supports_band_lock":True,"supports_pband":False,
            "supports_nr_band_lock":False,"supports_traffic_presence":True,"supports_traffic_volume":True,"supports_ca_details":True}),
    "xiaomi_cb0401v2": BackendCatalogEntry(backend="xiaomi_cb0401v2", family=FAMILY_XIAOMI_MI_WIFI, vendor="Xiaomi",
        display_name="CB0401V2 3.0.88 (conditional)", transports=("xiaomi_stock_luci",), implemented=True,
        capabilities={"supports_band_selection":True,"supports_band_lock":True,"supports_pband":False,
            "supports_nr_band_lock":True,"supports_traffic_presence":False,"supports_traffic_volume":False}),
    "nx520v": BackendCatalogEntry(backend="nx520v", family=FAMILY_TPLINK_DEV2, vendor="TP-Link",
        display_name="NX520v EU1 260408 (conditional)", transports=("tplink_encrypted_dev2_cbc512",), implemented=True,
        capabilities={"supports_band_selection":True,"supports_band_lock":True,"supports_pband":False,
            "supports_nr_band_lock":True,"supports_plmn":True,"supports_ca_details":True,"supports_network_settings":True}),
    "mikrotik": BackendCatalogEntry(
        backend="mikrotik",
        family=FAMILY_ROUTEROS_AT,
        vendor="MikroTik",
        display_name="MikroTik RouterOS + AT modem",
        transports=("routeros_api", "at_chat"),
        implemented=True,
        capabilities={
            "supports_band_selection": False,
            "supports_band_lock": False,
            "supports_cell_lock": True,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": True,
            "supports_restart": True,
            "supports_neighbors": False,
            "supports_ca_details": True,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": True,
            "supports_nr_band_lock": False,
            "supports_pband": False,
        },
    ),
    "b818": BackendCatalogEntry(
        backend="b818",
        family=FAMILY_HUAWEI_HILINK,
        vendor="Huawei",
        display_name="Huawei B818 HiLink",
        transports=("http_xml_webui",),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": False,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": True,
            "supports_restart": True,
            "supports_neighbors": True,
            "supports_ca_details": False,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": True,
            "supports_pband": True,
            "supports_nr_band_lock": False,
        },
    ),
    "b535": BackendCatalogEntry(
        backend="b535",
        family=FAMILY_HUAWEI_HILINK,
        vendor="Huawei",
        display_name="Huawei B535 HiLink",
        transports=("http_xml_webui",),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": False,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": True,
            "supports_restart": True,
            "supports_neighbors": True,
            "supports_ca_details": False,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": True,
            "supports_pband": True,
            "supports_nr_band_lock": False,
        },
    ),
    "huawei": BackendCatalogEntry(
        backend="huawei",
        family=FAMILY_HUAWEI_HILINK,
        vendor="Huawei",
        display_name="Huawei HiLink-like",
        transports=("http_xml_webui",),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": False,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": True,
            "supports_restart": True,
            "supports_neighbors": True,
            "supports_ca_details": False,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": True,
            "supports_pband": True,
            "supports_nr_band_lock": False,
        },
    ),
    "b311": BackendCatalogEntry(
        backend="b311",
        family=FAMILY_HUAWEI_HILINK,
        vendor="Huawei",
        display_name="Huawei B311 HiLink",
        transports=("http_xml_webui",),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": False,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": False,
            "supports_restart": True,
            "supports_neighbors": False,
            "supports_ca_details": False,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": True,
            "supports_pband": True,
            "supports_nr_band_lock": False,
        },
    ),
    "h112": BackendCatalogEntry(
        backend="h112",
        family=FAMILY_HUAWEI_HILINK,
        vendor="Huawei",
        display_name="Huawei 5G CPE Pro (H112-370)",
        transports=("http_xml_webui",),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": False,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": True,
            "supports_restart": True,
            "supports_neighbors": True,
            "supports_ca_details": False,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": True,
            "supports_pband": True,
            "supports_nr_band_lock": False,
        },
    ),
    "h122": BackendCatalogEntry(
        backend="h122",
        family=FAMILY_HUAWEI_HILINK,
        vendor="Huawei",
        display_name="Huawei 5G CPE Pro 2 (H122-373)",
        transports=("http_xml_webui",),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": False,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": True,
            "supports_restart": True,
            "supports_neighbors": True,
            "supports_ca_details": False,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": True,
            "supports_pband": True,
            "supports_nr_band_lock": False,
        },
    ),
    "openwrt": BackendCatalogEntry(
        backend="openwrt",
        family=FAMILY_OPENWRT_MM,
        vendor="OpenWrt",
        display_name="OpenWrt ModemManager",
        transports=("http_ubus_modemmanager_rpcd",),
        implemented=True,
        capabilities={
            "supports_band_selection": False,
            "supports_band_lock": False,
            "supports_cell_lock": False,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": False,
            "supports_restart": False,
            "supports_neighbors": False,
            "supports_ca_details": False,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": False,
            "supports_pband": False,
            "supports_nr_band_lock": False,
        },
    ),
    "mf296r": BackendCatalogEntry(
        backend="mf296r",
        family=FAMILY_ZTE_WEBUI,
        vendor="ZTE",
        display_name="ZTE MF296R goform",
        transports=("http_goform", "browser_webui"),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": False,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": False,
            "supports_restart": True,
            "supports_neighbors": False,
            "supports_ca_details": True,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": False,
            "supports_nr_band_lock": False,
            "supports_pband": True,
        },
    ),
    "zte_zwrt": BackendCatalogEntry(
        backend="zte_zwrt",
        family=FAMILY_ZTE_ZWRT,
        vendor="ZTE",
        display_name="ZTE zwrt/ubus NG",
        transports=("http_ubus_json", "browser_webui"),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": True,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": False,
            "supports_restart": True,
            "supports_neighbors": False,
            "supports_ca_details": True,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": True,
            "supports_nr_band_lock": True,
            "supports_pband": True,
        },
    ),
    "g5tc": BackendCatalogEntry(
        backend="g5tc",
        family=FAMILY_ZTE_ZWRT,
        vendor="ZTE",
        display_name="ZTE G5TC 5G (Snapdragon X72, ubus NG)",
        transports=("http_ubus_json", "browser_webui"),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": True,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": False,
            "supports_restart": True,
            "supports_neighbors": False,
            "supports_ca_details": True,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": True,
            "supports_nr_band_lock": True,
            "supports_pband": True,
        },
    ),
    "h155": BackendCatalogEntry(
        backend="h155",
        family=FAMILY_HUAWEI_HILINK,
        vendor="Huawei",
        display_name="Huawei 5G CPE 5 (H155-381, Brovi)",
        transports=("http_hilink_xml", "browser_webui"),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": False,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": False,
            "supports_restart": True,
            "supports_neighbors": False,
            "supports_ca_details": True,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": True,
            "supports_nr_band_lock": False,
            "supports_pband": True,
        },
    ),
    "nr5103e": BackendCatalogEntry(
        backend="nr5103e",
        family=FAMILY_ZYXEL_WEBUI,
        vendor="Zyxel",
        display_name="Zyxel NR5103E 5G CPE (SD X62)",
        transports=("http_json", "browser_webui"),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": True,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": False,
            "supports_restart": True,
            "supports_neighbors": False,
            "supports_ca_details": True,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": False,
            "supports_nr_band_lock": False,
            "supports_pband": True,
        },
    ),
    "chateau_5g": BackendCatalogEntry(
        backend="chateau_5g",
        family=FAMILY_ROUTEROS_AT,
        vendor="MikroTik",
        display_name="MikroTik Chateau 5G ax (SD X62)",
        transports=("routeros_api", "at_command"),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": False,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": False,
            "supports_restart": True,
            "supports_neighbors": False,
            "supports_ca_details": True,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": True,
            "supports_nr_band_lock": True,
            "supports_pband": True,
        },
    ),
    "cudy_p5": BackendCatalogEntry(
        backend="cudy_p5",
        family=FAMILY_OPENWRT_MM,
        vendor="Cudy",
        display_name="Cudy P5 5G (SD X62, OpenWrt/LuCI)",
        transports=("http_ubus_modemmanager_rpcd",),
        implemented=True,
        capabilities={
            "supports_band_selection": False,
            "supports_band_lock": False,
            "supports_cell_lock": False,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": False,
            "supports_restart": False,
            "supports_neighbors": False,
            "supports_ca_details": False,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": False,
            "supports_nr_band_lock": False,
            "supports_pband": False,
        },
    ),
    "deco_x50_5g": BackendCatalogEntry(
        backend="deco_x50_5g",
        family=FAMILY_UNKNOWN,
        vendor="TP-Link",
        display_name="TP-Link Deco X50-5G (SD X62)",
        transports=("proprietary_app",),
        implemented=False,
        capabilities={
            "supports_band_selection": False,
            "supports_band_lock": False,
            "supports_cell_lock": False,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": False,
            "supports_restart": False,
            "supports_neighbors": False,
            "supports_ca_details": False,
            "supports_band_priority": False,
            "supports_plmn": False,
            "supports_wifi_control": False,
            "supports_nr_band_lock": False,
            "supports_pband": True,
        },
    ),
    "mc888": BackendCatalogEntry(
        backend="mc888",
        family=FAMILY_ZTE_WEBUI,
        vendor="ZTE",
        display_name="ZTE 5G goform (MC801A, MC888, MC889)",
        transports=("http_goform", "browser_webui"),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": True,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": False,
            "supports_restart": True,
            "supports_neighbors": False,
            "supports_ca_details": True,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": False,
            "supports_nr_band_lock": True,
            "supports_pband": True,
        },
    ),
    "g5b": BackendCatalogEntry(
        backend="g5b",
        family=FAMILY_ZTE_WEBUI,
        vendor="ZTE",
        display_name="ZTE G5B 5G goform (Qualcomm SDX62-0)",
        transports=("http_goform", "browser_webui"),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": True,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": False,
            "supports_restart": True,
            "supports_neighbors": False,
            "supports_ca_details": True,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": False,
            "supports_nr_band_lock": True,
            "supports_pband": True,
        },
    ),
    "b530": BackendCatalogEntry(
        backend="b530",
        family=FAMILY_HUAWEI_HILINK,
        vendor="Huawei",
        display_name="Huawei B530-336 4G CPE 3 (Cat 7)",
        transports=("http_xml_webui",),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": False,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": True,
            "supports_restart": True,
            "supports_neighbors": True,
            "supports_ca_details": False,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": True,
            "supports_pband": True,
            "supports_nr_band_lock": False,
        },
    ),
    "model": BackendCatalogEntry(
        backend="model",
        family=FAMILY_MODEL_EMULATOR,
        vendor="Synthetic",
        display_name="Universal modem model / emulator",
        transports=("profile_json",),
        implemented=True,
        capabilities={
            "supports_band_selection": True,
            "supports_band_lock": True,
            "supports_cell_lock": True,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": True,
            "supports_restart": True,
            "supports_neighbors": True,
            "supports_ca_details": True,
            "supports_band_priority": True,
            "supports_plmn": True,
            "supports_wifi_control": False,
            "supports_nr_band_lock": True,
            "supports_pband": True,
        },
    ),
    "nr5101": BackendCatalogEntry(
        backend="nr5101",
        family=FAMILY_ZYXEL_WEBUI,
        vendor="Zyxel",
        display_name="Zyxel NR5101 5G NR Indoor Router",
        transports=("http_json_webui",),
        implemented=True,
        capabilities={
            "supports_band_selection": False,
            "supports_band_lock": False,  # pending API discovery
            "supports_cell_lock": False,       # pending API discovery
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": False,
            "supports_restart": True,
            "supports_neighbors": True,
            "supports_ca_details": False,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": False,
            "supports_nr_band_lock": False,    # pending API discovery,
            "supports_pband": True,
        },
    ),
    "fwa510": BackendCatalogEntry(
        backend="fwa510",
        family=FAMILY_ZYXEL_WEBUI,
        vendor="Zyxel",
        display_name="Zyxel FWA510 5G NR Indoor Router",
        transports=("http_json_webui",),
        implemented=True,
        capabilities={
            "supports_band_selection": False,
            "supports_band_lock": False,
            "supports_cell_lock": False,
            "supports_traffic_presence": True,
            "supports_traffic_volume": True,
            "supports_ue_capability": False,
            "supports_restart": True,
            "supports_neighbors": True,
            "supports_ca_details": False,
            "supports_band_priority": False,
            "supports_plmn": True,
            "supports_wifi_control": False,
            "supports_nr_band_lock": False,
            "supports_pband": True,
        },
    ),
}


PROVIDER_BAND_POOL_FIELDS: tuple[str, ...] = (
    "selected_band_set", "selected_band_set_signature",
    "configured_band_pool", "configured_band_pool_signature",
    "configured_band_pool_source", "configured_band_pool_status",
    "configured_band_pool_age_s", "configured_band_pool_last_sample_ts",
    "configured_ca_pool_display", "ca_signature", "ca_band_signature",
    "carrier_count",
)


FIELD_SUPPORT_MF296R_BASE: tuple[str, ...] = (
    "radio_connected",
    "network_type",
    "sinr_db",
    "rsrp_dbm",
    "rsrq_db",
    "rssi_dbm",
    "ca_active",
    "ca_signature",
    "carrier_count",
    "serving_band",
    "active_band_set",
    "active_band_set_signature",
    "band_set",
    "band_set_signature",
    "pci",
    "earfcn_dl",
    "enb_id",
    "cell_id",
    "provider_name",
    "provider_plmn",
    "plmn",
    "wan_ip",
    "model",
    "firmware",
    "hardware_version",
    "config_version",
    "serial",
    "imei",
    "imsi",
    "tac",
    "session_uptime_s",
    "wifi_enabled",
    "wifi_24ghz_enabled",
    "wifi_5ghz_enabled",
)

FIELD_UNSUPPORTED_MF296R_BASE: tuple[str, ...] = (
    "mcs_dl0",
    "mcs_dl1",
    "mcs_ul",
    "cqi",
    "cqi0",
    "cqi1",
    "ri",
    "qam_dl0",
    "qam_dl1",
    "qam_ul",
    "tx_power",
    "lan_mac",
    "webui",
    "antenna",
    "nr5g_active",
    "nr5g_band",
    "nr5g_rsrp_dbm",
    "nr5g_rsrq_db",
    "nr5g_sinr_db",
    "nr5g_pci",
    "wifi_6ghz_enabled",
    "temperature_ambient_c",
    "temperature_modem_4g_c",
    "temperature_modem_5g_c",
    "temperature_pa1_c",
    "temperature_wifi_c",
)

FIELD_SUPPORT_MIKROTIK_ROUTEROS_BASE: tuple[str, ...] = (
    "radio_connected",
    "network_type",
    "sinr_db",
    "rsrp_dbm",
    "rsrq_db",
    "rssi_dbm",
    "ca_active",
    "ca_signature",
    "carrier_count",
    "serving_band",
    "selected_band_set",
    "active_band_set",
    "band_set",
    "pci",
    "earfcn_dl",
    "enb_id",
    "cell_id",
    "tac",
    "ri",
    "provider_name",
    "provider_plmn",
    "plmn",
    "wan_ip",
    "model",
    "router_model",
    "hardware_version",
    "software",
    "software_version",
    "firmware",
    "serial",
    "imei",
    "wifi_enabled",
    "wifi_24ghz_enabled",
    "wifi_5ghz_enabled",
    "traffic_presence",
    "traffic_volume",
    "rx_bytes_per_sec",
    "tx_bytes_per_sec",
    "traffic_valid",
    "traffic_state_detail",
    "traffic_invalid_reason",
    "packet_session_connected",
    "connection_status_code",
    "session_uptime_s",
    "traffic_total_download_bytes",
    "traffic_total_upload_bytes",
    "traffic_session_download_bytes",
    "traffic_session_upload_bytes",
    "traffic_device_download_counter_bytes",
    "traffic_device_upload_counter_bytes",
    "traffic_counter_period_started_at",
    "traffic_counter_interface_started_at",
    "traffic_accounting_source",
    "traffic_accounting_status",
    "traffic_volume_poll_interval_s",
    "traffic_volume_last_sample_ts",
    "traffic_volume_next_sample_ts",
    "traffic_volume_age_s",
    "traffic_rate_source",
)

FIELD_SUPPORT_MIKROTIK_ROUTEROS_5G: tuple[str, ...] = FIELD_SUPPORT_MIKROTIK_ROUTEROS_BASE + (
    "dl_bandwidth",
    "ul_bandwidth",
    "dl_bandwidth_mhz",
    "ul_bandwidth_mhz",
    "dl_freq_mhz",
    "ul_freq_mhz",
    "tx_power",
    "cqi0",
    "mcs_dl0",
    "mcs_dl1",
    "mcs_ul",
    "qam_dl0",
    "qam_dl1",
    "qam_ul",
    "nr5g_active",
    "nr5g_mode",
    "nr5g_cell_id",
    "nr5g_pci",
    "nr5g_arfcn",
    "nr5g_band",
    "nr5g_band_set",
    "nr5g_rsrp_dbm",
    "nr5g_rsrq_db",
    "nr5g_sinr_db",
    "nr5g_rx0_rsrp_dbm",
    "nr5g_rx1_rsrp_dbm",
)

FIELD_UNSUPPORTED_MIKROTIK_ROUTEROS_BASE: tuple[str, ...] = (
    "temperature_ambient_c",
    "temperature_modem_4g_c",
    "temperature_modem_5g_c",
    "temperature_pa1_c",
    "temperature_wifi_c",
)

FIELD_UNSUPPORTED_MIKROTIK_ROUTEROS_LTE_ONLY: tuple[str, ...] = FIELD_UNSUPPORTED_MIKROTIK_ROUTEROS_BASE + (
    "nr5g_active",
    "nr5g_mode",
    "nr5g_cell_id",
    "nr5g_pci",
    "nr5g_arfcn",
    "nr5g_band",
    "nr5g_band_set",
    "nr5g_rsrp_dbm",
    "nr5g_rsrq_db",
    "nr5g_sinr_db",
    "nr5g_rx0_rsrp_dbm",
    "nr5g_rx1_rsrp_dbm",
)


# Per-router data is owned by the portable folder. Protocol classes and generic
# capability/identity invariants remain here; no embedded profile fallback.
def _load_portable_profile_records() -> tuple[FirmwareProfileOverlay, ...]:
    from dataclasses import fields
    from arch4.modem_live.profile_definition.registry import startup_registry
    registry = startup_registry()
    tuple_fields = {field.name for field in fields(FirmwareProfileOverlay)
                    if str(field.type).startswith("tuple[")}
    records = []
    for identifier in registry.ids():
        metadata = registry.metadata(identifier)
        for name in tuple_fields:
            metadata[name] = tuple(metadata[name])
        records.append(FirmwareProfileOverlay(**metadata))
    return tuple(records)


FIRMWARE_PROFILE_OVERLAYS: tuple[FirmwareProfileOverlay, ...] = _load_portable_profile_records()


CANDIDATE_ROUTER_PROFILES: tuple[CandidateRouterProfile, ...] = (
    CandidateRouterProfile(
        vendor="ZTE",
        model="G5B",
        family=FAMILY_ZTE_WEBUI,
        reuse_backend_hint="g5b",
        transports=("browser_webui", "http_goform"),
        overlay_strategy="family_reuse_plus_model_firmware_overlay",
        overlay_risk="low",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "ZTE G5B is a Qualcomm SDX62-0 5G CPE using goform API (same family as MC888). "
            "Default IP 192.168.0.1 or 192.168.1.1. No external antenna ports. "
            "Supports NR band lock via goform. VOIP/VOLTE via RJ11 port."
        ),
    ),
    CandidateRouterProfile(
        vendor="ZTE",
        model="MC888 Pro",
        family=FAMILY_ZTE_WEBUI,
        reuse_backend_hint="mc888",
        transports=("browser_webui", "http_goform"),
        overlay_strategy="family_reuse_plus_model_firmware_overlay",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Public tooling still places MC888 Pro in the legacy goform family. "
            "Promote to the zwrt/ubus NG branch only if live transport probing "
            "proves a real /ubus overlap."
        ),
    ),
    CandidateRouterProfile(
        vendor="ZTE",
        model="MC888 Ultra",
        family=FAMILY_ZTE_WEBUI,
        reuse_backend_hint="mc888",
        transports=("browser_webui", "http_goform"),
        overlay_strategy="family_reuse_plus_model_firmware_overlay",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Public tooling still places MC888 Ultra in the legacy goform family. "
            "Promote to the zwrt/ubus NG branch only if live transport probing "
            "proves a real /ubus overlap."
        ),
    ),
    CandidateRouterProfile(
        vendor="ZTE",
        model="MC801A",
        family=FAMILY_ZTE_WEBUI,
        reuse_backend_hint="mc888",
        transports=("browser_webui", "http_goform"),
        overlay_strategy="family_reuse_plus_model_firmware_overlay",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "MC801A is a 5G goform device (SDX55, NSA+SA). Same goform API as MC888. "
            "Uses mc888 backend for 5G NR band lock capability."
        ),
    ),
    CandidateRouterProfile(
        vendor="ZTE",
        model="MC801",
        family=FAMILY_ZTE_WEBUI,
        reuse_backend_hint="",
        transports=("browser_webui", "http_goform"),
        overlay_strategy="family_probe_before_overlay_decision",
        overlay_risk="high",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Plain MC801 is an older Snapdragon X50 goform device with NSA-only 5G. "
            "Do not silently reuse the mc888 backend or inherit MC801A/MC888 NSA+SA "
            "truth until a dedicated MC801 support boundary/profile is implemented."
        ),
    ),
    CandidateRouterProfile(
        vendor="ZTE",
        model="MC888",
        family=FAMILY_ZTE_WEBUI,
        reuse_backend_hint="mc888",
        transports=("browser_webui", "http_goform"),
        overlay_strategy="family_reuse_plus_model_firmware_overlay",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Plain MC888 uses the GoForm boundary and its selected firmware contract. "
            "Exact protected AT B06 is a monitoring target with unqualified controls. "
            "Do not inherit older AT actuation or select another protocol as fallback."
        ),
    ),
    CandidateRouterProfile(
        vendor="Huawei",
        model="H112",
        family=FAMILY_HUAWEI_HILINK,
        reuse_backend_hint="h112",
        transports=("http_xml_webui", "browser_webui"),
        overlay_strategy="family_reuse_plus_model_firmware_overlay",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Huawei 5G CPE Pro — covers H112-370 (UK carrier SKU, n78 only, Wi-Fi 6 4x4 AX) "
            "and H112-372 (global unlocked, n41/n77/n78/n79, Wi-Fi 5 2x2 AC). "
            "Both SKUs use HiLink XML/HTTP at 192.168.8.1 and share hardware_version WL1H112M. "
            "Balong 5000 chipset. NR band lock and cell lock not available via HiLink API. "
            "Firmware/carrier overlay (huawei_h112_three_uk_c21 or huawei_h112_cpe_pro) "
            "refines band management truth at runtime."
        ),
    ),
    CandidateRouterProfile(
        vendor="Huawei",
        model="B311-221",
        family=FAMILY_HUAWEI_HILINK,
        reuse_backend_hint="huawei",
        transports=("http_xml_webui", "browser_webui"),
        overlay_strategy="family_reuse_first_then_capture_and_diff",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Older LTE-only Huawei HiLink router explicitly listed in public tested-device "
            "clients. Route through the generic Huawei HiLink backend first and let "
            "capture-and-diff determine whether a dedicated low-detail overlay is needed."
        ),
    ),
    CandidateRouterProfile(
        vendor="Huawei",
        model="B315s-22",
        family=FAMILY_HUAWEI_HILINK,
        reuse_backend_hint="huawei",
        transports=("http_xml_webui", "browser_webui"),
        overlay_strategy="family_reuse_first_then_capture_and_diff",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Source-backed HiLink LTE router candidate. Treat as the shared Huawei XML/API "
            "family first; do not invent B818/B535 feature parity until payload overlap is proven."
        ),
    ),
    CandidateRouterProfile(
        vendor="Huawei",
        model="B525s-23a",
        family=FAMILY_HUAWEI_HILINK,
        reuse_backend_hint="huawei",
        transports=("http_xml_webui", "browser_webui"),
        overlay_strategy="family_reuse_first_then_capture_and_diff",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Widely cited HiLink candidate with public tooling support. Use the generic Huawei "
            "backend first, then add model quirks only if live payloads diverge."
        ),
    ),
    CandidateRouterProfile(
        vendor="Huawei",
        model="B525s-65a",
        family=FAMILY_HUAWEI_HILINK,
        reuse_backend_hint="huawei",
        transports=("http_xml_webui", "browser_webui"),
        overlay_strategy="family_reuse_first_then_capture_and_diff",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Same public HiLink client evidence as other B525 variants, but exact suffixes are "
            "firmware-sensitive. Generic Huawei backend is the safest first-use lane."
        ),
    ),
    CandidateRouterProfile(
        vendor="Huawei",
        model="B528s",
        family=FAMILY_HUAWEI_HILINK,
        reuse_backend_hint="huawei",
        transports=("http_xml_webui", "browser_webui"),
        overlay_strategy="family_reuse_first_then_capture_and_diff",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Publicly tested as part of the broader Huawei HiLink router set. Reuse the generic "
            "family backend until a firmware-specific overlay is justified."
        ),
    ),
    CandidateRouterProfile(
        vendor="Huawei",
        model="B535-333",
        family=FAMILY_HUAWEI_HILINK,
        reuse_backend_hint="b535",
        transports=("http_xml_webui", "browser_webui"),
        overlay_strategy="hilink_profile_with_live_response_coverage",
        overlay_risk="low",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "B535-333 uses the shared Huawei HiLink transport and translator. Its exact "
            "profile owns model, firmware, capability and timing facts."
        ),
    ),
    CandidateRouterProfile(
        vendor="Huawei",
        model="B612-233",
        family=FAMILY_HUAWEI_HILINK,
        reuse_backend_hint="huawei",
        transports=("http_xml_webui", "browser_webui"),
        overlay_strategy="family_reuse_first_then_capture_and_diff",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Release-note-backed Huawei HiLink candidate. Bring it through the generic family "
            "backend first and only promote to a stronger model overlay after payload capture."
        ),
    ),
    CandidateRouterProfile(
        vendor="Huawei",
        model="B628-265",
        family=FAMILY_HUAWEI_HILINK,
        reuse_backend_hint="huawei",
        transports=("http_xml_webui", "browser_webui"),
        overlay_strategy="family_reuse_first_then_capture_and_diff",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Newer LTE HiLink CPE listed in public tested-device clients. Shares the family "
            "transport, but feature completeness still needs live payload confirmation."
        ),
    ),
    CandidateRouterProfile(
        vendor="ZTE",
        model="MF296R",
        family=FAMILY_ZTE_WEBUI,
        reuse_backend_hint="mf296r",
        transports=("browser_webui", "http_goform"),
        overlay_strategy="family_reuse_plus_firmware_operator_overlay",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "MF296R has an implemented backend (mf296r). Goform API provides "
            "rich telemetry. A1SRB B04 firmware has constrained band/cell control — "
            "other firmware builds may differ. Firmware-specific overlay refines truth."
        ),
    ),
    CandidateRouterProfile(
        vendor="Huawei",
        model="B818",
        family=FAMILY_HUAWEI_HILINK,
        reuse_backend_hint="b818",
        transports=("http_xml_webui", "browser_webui"),
        overlay_strategy="family_reuse_plus_firmware_operator_overlay",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Same broad family can still need a firmware/operator quirk overlay. "
            "Example: Vodafone-locked B818 variants."
        ),
    ),
    CandidateRouterProfile(
        vendor="Huawei",
        model="B715s-23c",
        family=FAMILY_HUAWEI_HILINK,
        reuse_backend_hint="huawei",
        transports=("http_xml_webui", "browser_webui"),
        overlay_strategy="family_reuse_first_then_capture_and_diff",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Public tooling treats B715 as a working HiLink-like member. Use the generic "
            "Huawei backend first rather than guessing B818-class scheduler completeness."
        ),
    ),
    CandidateRouterProfile(
        vendor="Huawei",
        model="B530",
        family=FAMILY_HUAWEI_HILINK,
        reuse_backend_hint="b530",
        transports=("http_xml_webui", "browser_webui"),
        overlay_strategy="family_reuse_plus_model_firmware_quirk_overlay",
        overlay_risk="low",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Huawei 4G CPE 3 (B530-336). Cat 7, max 300 Mbps. Same HiLink API "
            "as B818/B535 family. 4G only — no NR/5G fields. WiFi 5 AC1300."
        ),
    ),
    CandidateRouterProfile(
        vendor="TP-Link",
        model="Archer NX200",
        family=FAMILY_UNKNOWN,
        reuse_backend_hint="",
        transports=("browser_webui", "http_json_or_xml_unknown"),
        overlay_strategy="new_family_unless_minimum_overlap_with_existing_webui_family_is_proven",
        overlay_risk="high",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "TP-Link 5G AX1800 router. Web UI at 192.168.1.1. NSA+SA. "
            "NR bands: n1/n3/n5/n7/n8/n20/n28/n38/n40/n41/n77/n78. "
            "Proprietary TP-Link HTTP API — needs reverse engineering. "
            "Not yet implemented — registered as candidate for future support."
        ),
    ),
    CandidateRouterProfile(
        vendor="Zyxel",
        model="NR5101",
        family=FAMILY_ZYXEL_WEBUI,
        reuse_backend_hint="nr5101",
        transports=("http_json_webui", "browser_webui"),
        overlay_strategy="family_reuse_plus_model_firmware_overlay",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Zyxel NR5101 5G NR Indoor Router. Quectel RG502Q-EA module. "
            "Stock firmware is OpenWrt Barrier Breaker 14.x based. "
            "Web configurator API at 192.168.1.1. Generic variant supports "
            "cell lock; Nebula variant does not. WiFi 6 AX1800. "
            "Two SMA connectors for external antennas."
        ),
    ),
    CandidateRouterProfile(
        vendor="Zyxel",
        model="NR7101",
        family=FAMILY_ZYXEL_WEBUI,
        reuse_backend_hint="nr5101",
        transports=("http_json_webui", "browser_webui"),
        overlay_strategy="family_reuse_plus_model_firmware_overlay",
        overlay_risk="low",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Zyxel NR7101 5G NR Outdoor Router. Same Quectel module and "
            "API family as NR5101. PoE powered. IP67 rated. Community "
            "OpenWrt support available. Reuses nr5101 backend."
        ),
    ),
    CandidateRouterProfile(
        vendor="Zyxel",
        model="FWA510",
        family=FAMILY_ZYXEL_WEBUI,
        reuse_backend_hint="fwa510",
        transports=("http_json_webui", "browser_webui"),
        overlay_strategy="family_reuse_plus_model_firmware_overlay",
        overlay_risk="medium",
        required_overlap=MINIMUM_REQUIRED_OVERLAP,
        notes=(
            "Zyxel FWA510 5G NR Indoor Router. Tri-band WiFi. "
            "Nebula cloud managed. No cell lock on this model. "
            "Same web configurator API family as NR5101."
        ),
    ),
)


FAMILY_REQUIRED_OVERLAP: Dict[str, tuple[str, ...]] = {
    FAMILY_HUAWEI_HILINK: MINIMUM_REQUIRED_OVERLAP,
    FAMILY_ROUTEROS_AT: MINIMUM_REQUIRED_OVERLAP,
    FAMILY_OPENWRT_MM: MINIMUM_REQUIRED_OVERLAP,
    FAMILY_ZTE_WEBUI: MINIMUM_REQUIRED_OVERLAP,
    FAMILY_ZTE_ZWRT: MINIMUM_REQUIRED_OVERLAP,
    FAMILY_MODEL_EMULATOR: MINIMUM_REQUIRED_OVERLAP,
    FAMILY_ZYXEL_WEBUI: MINIMUM_REQUIRED_OVERLAP,
}

FAMILY_OVERLAY_RISK: Dict[str, str] = {
    FAMILY_HUAWEI_HILINK: "medium",
    FAMILY_ROUTEROS_AT: "low",
    FAMILY_OPENWRT_MM: "low",
    FAMILY_ZTE_WEBUI: "high",
    FAMILY_ZTE_ZWRT: "high",
    FAMILY_MODEL_EMULATOR: "low",
    FAMILY_ZYXEL_WEBUI: "medium",
    FAMILY_UNKNOWN: "high",
}


# ── Canonical Backend Identity ──────────────────────────────────────────
#
# Internal modules should consume this typed object instead of raw backend
# strings.  It wraps the backend key together with its canonical family,
# support level, and capability flags — all resolved from the catalog at
# construction time.  This keeps vendor-specific vocabulary at the catalog
# boundary and gives consumers a single validated object.


@dataclass(frozen=True)
class CanonicalBackend:
    """Validated, normalized backend identity for internal consumers.

    Constructed via ``resolve_canonical_backend()``.  Internal modules
    should pass this object rather than raw backend name strings so that
    the canonical family, support level, and capabilities are always
    consistent with the catalog.
    """

    key: str                              # normalized lowercase backend key
    family: str                           # canonical family constant
    vendor: str                           # display vendor name
    display_name: str                     # human-readable name
    support_level: str                    # monitor_only / safe_control / …
    capabilities: Dict[str, bool]         # frozen capability snapshot
    implemented: bool = True              # whether a real backend exists

    @property
    def is_known(self) -> bool:
        """True if this backend was found in the catalog."""
        return self.family != FAMILY_UNKNOWN

    @property
    def supports_band_selection(self) -> bool:
        return self.capabilities.get("supports_band_selection", False)

    @property
    def supports_cell_lock(self) -> bool:
        return self.capabilities.get("supports_cell_lock", False)

    @property
    def supports_restart(self) -> bool:
        return self.capabilities.get("supports_restart", False)

    @property
    def supports_wifi_control(self) -> bool:
        return self.capabilities.get("supports_wifi_control", False)


def resolve_canonical_backend(backend: str) -> CanonicalBackend:
    """Resolve a raw backend string into a CanonicalBackend.

    Known backends resolve to full catalog data.  Unknown backends
    return a safe fallback with ``is_known=False`` and optimistic traffic
    support only — never crashes.
    """
    key = str(backend or "").strip().lower()
    entry = BACKEND_CATALOG.get(key)
    if entry is None:
        return CanonicalBackend(
            key=key,
            family=FAMILY_UNKNOWN,
            vendor="Unknown",
            display_name=str(backend or "Unknown"),
            support_level=SUPPORT_LEVEL_MONITOR,
            capabilities=dict(CONSERVATIVE_CAPABILITY_FLAGS),
            implemented=False,
        )
    return CanonicalBackend(
        key=entry.backend,
        family=entry.family,
        vendor=entry.vendor,
        display_name=entry.display_name,
        support_level=get_backend_support_level(key),
        capabilities=dict(entry.capabilities),
        implemented=entry.implemented,
    )


def supported_backends() -> List[str]:
    return sorted(BACKEND_CATALOG.keys())


def supported_backend_help() -> str:
    return ", ".join(supported_backends())


def get_backend_entry(backend: str) -> Optional[BackendCatalogEntry]:
    return BACKEND_CATALOG.get(str(backend or "").strip().lower())


def get_backend_family(backend: str) -> str:
    entry = get_backend_entry(backend)
    return entry.family if entry else FAMILY_UNKNOWN


def get_backend_capability_flags(backend: str) -> Dict[str, bool]:
    entry = get_backend_entry(backend)
    if entry:
        caps = dict(entry.capabilities)
    else:
        caps = dict(CONSERVATIVE_CAPABILITY_FLAGS)
    if "supports_sms" not in caps:
        caps["supports_sms"] = bool(
            entry
            and entry.implemented
            and entry.family in {FAMILY_HUAWEI_HILINK, FAMILY_ZTE_WEBUI}
        )
    else:
        caps["supports_sms"] = bool(caps["supports_sms"] and entry and entry.implemented)
    explicit = caps.get("supports_pband", None)
    if explicit is None:
        caps["supports_pband"] = bool(
            entry
            and entry.implemented
            and caps.get("supports_band_selection", False)
        )
    else:
        caps["supports_pband"] = bool(
            explicit
            and entry
            and entry.implemented
            and caps.get("supports_band_selection", False)
        )
    return caps


def _complete_profile_capability_flags(profile: FirmwareProfileOverlay) -> Dict[str, bool]:
    """Return the one complete runtime capability map for an exact profile.

    Firmware overlays may state only the fields that differ from the backend
    family contract.  Consumers above the catalog must not independently
    repair missing capability keys.  The catalog completes the map once by
    applying the exact overlay over the canonical backend capability contract.
    """

    caps = get_backend_capability_flags(profile.backend)
    caps.update({str(key): bool(value) for key, value in profile.capabilities.items()})
    return caps


def _norm(text: Any) -> str:
    return " ".join(str(text or "").strip().lower().replace("_", " ").split())


def match_candidate_router_profile(vendor: Any, model: Any) -> Optional[CandidateRouterProfile]:
    vtxt = _norm(vendor)
    mtxt = _norm(model)
    if not mtxt:
        return None
    for profile in CANDIDATE_ROUTER_PROFILES:
        pv = _norm(profile.vendor)
        pm = _norm(profile.model)
        if vtxt and pv and pv not in vtxt:
            continue
        if pm and pm in mtxt:
            return profile
    return None


def list_family_transports(family: str) -> List[str]:
    fam = str(family or "").strip().lower()
    transports: List[str] = []
    for entry in BACKEND_CATALOG.values():
        if entry.family != fam:
            continue
        for transport in entry.transports:
            if transport not in transports:
                transports.append(transport)
    for profile in CANDIDATE_ROUTER_PROFILES:
        if profile.family != fam:
            continue
        for transport in profile.transports:
            if transport not in transports:
                transports.append(transport)
    return transports


def family_probe_order(family: str) -> List[str]:
    fam = str(family or "").strip().lower()
    if fam == FAMILY_HUAWEI_HILINK:
        return [
            "http_root",
            "webui_session_token",
            "webui_login_state",
            "serving_cell_endpoint",
            "traffic_stats_endpoint",
        ]
    if fam == FAMILY_ROUTEROS_AT:
        return [
            "routeros_api_login",
            "router_identity",
            "lte_monitor_snapshot",
            "at_servingcell_read",
            "traffic_counter_read",
        ]
    if fam == FAMILY_ZTE_WEBUI:
        return [
            "http_root",
            "webui_login_state",
            "identity_endpoint",
            "serving_cell_endpoint",
            "traffic_stats_endpoint",
        ]
    if fam == FAMILY_ZTE_ZWRT:
        return [
            "http_root",
            "ubus_login_info",
            "ubus_session_login",
            "ubus_netinfo_call",
            "ubus_traffic_call",
        ]
    if fam == FAMILY_OPENWRT_MM:
        return [
            "modemmanager_presence",
            "serving_cell_snapshot",
            "traffic_counter_read",
        ]
    if fam == FAMILY_ZYXEL_WEBUI:
        return [
            "http_root",
            "webui_login",
            "device_info_endpoint",
            "cell_signal_endpoint",
            "cell_status_endpoint",
        ]
    return [
        "http_root",
        "router_identity",
        "serving_cell_snapshot",
        "traffic_counter_read",
    ]


def family_reuse_policy(family: str) -> Dict[str, Any]:
    fam = str(family or "").strip().lower()
    return {
        "family": fam or FAMILY_UNKNOWN,
        "family_first": True,
        "new_backend_only_if_overlap_insufficient": True,
        "probe_order": family_probe_order(fam),
        "known_transports": list_family_transports(fam),
        "required_overlap": list(FAMILY_REQUIRED_OVERLAP.get(fam, MINIMUM_REQUIRED_OVERLAP)),
        "overlay_default_risk": str(FAMILY_OVERLAY_RISK.get(fam, "high")),
    }


def get_backend_support_level(backend: str) -> str:
    name = str(backend or "").strip().lower()
    return BACKEND_SUPPORT_LEVELS.get(name, SUPPORT_LEVEL_MONITOR)


def get_backend_action_semantics(backend: str) -> BackendActionSemantics:
    name = str(backend or "").strip().lower()
    entry = BACKEND_ACTION_SEMANTICS.get(name)
    if entry is not None:
        return entry
    return BackendActionSemantics()


def _profile_text(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().split())


def _profile_scope_tokens(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    return tuple(str(token).strip() for token in (values or ()) if str(token).strip())


def _profile_text_matches_any(text: str, tokens: tuple[str, ...]) -> bool:
    if not text:
        return False
    norm = _profile_text(text)
    return any(_profile_text(token) in norm for token in tokens if str(token or "").strip())


def _profile_vendor_matches(profile: FirmwareProfileOverlay, vendor_text: str) -> bool:
    if not profile.vendor or not vendor_text:
        return True
    allowed = (profile.vendor,) + tuple(profile.vendor_aliases or ())
    return _profile_text_matches_any(vendor_text, allowed)


def _routeros_family_context(*, backend_name: str, caller_family: str) -> bool:
    return backend_name == "mikrotik" or caller_family == FAMILY_ROUTEROS_AT


def _profile_os_name_matches(
    *,
    profile: FirmwareProfileOverlay,
    backend_name: str,
    caller_family: str,
    os_name_text: str,
) -> bool:
    if not profile.os_name:
        return True
    required = _profile_text(profile.os_name)
    if os_name_text and required in os_name_text:
        return True
    # MikroTik runtime can identify the LTE module before the RouterOS name is
    # present in the same owner payload. The backend/family itself is the OS
    # authority for this lane, so do not downgrade to the generic baseline.
    return required == "ROUTEROS" and _routeros_family_context(
        backend_name=backend_name,
        caller_family=caller_family,
    )


def _profile_os_version_matches(
    *,
    profile: FirmwareProfileOverlay,
    backend_name: str,
    caller_family: str,
    model_text: str,
    firmware_text: str,
    os_version_text: str,
) -> bool:
    if not profile.os_version_contains:
        return True
    if os_version_text:
        # The model fallback below is only for an OS version not yet sampled.
        # A fresh incompatible version must not select a different firmware lane.
        return all(_profile_text(token) in os_version_text for token in profile.os_version_contains)
    if not _routeros_family_context(backend_name=backend_name, caller_family=caller_family):
        return False
    # A RouterOS backend plus an exact modem-module selector is enough to keep
    # the profile lane stable until the slower OS version field arrives.
    return _profile_model_tokens_match(
        backend_name=backend_name,
        model_text=model_text,
        firmware_text=firmware_text,
        tokens=profile.model_contains,
    )


def _profile_model_tokens_match(
    *,
    backend_name: str,
    model_text: str,
    firmware_text: str,
    tokens: tuple[str, ...],
) -> bool:
    if not tokens:
        return True
    haystack = model_text
    # RouterOS often reports the board model and modem module firmware as
    # separate fields. Module selectors such as EG120K/RG502Q may therefore be
    # present in either field, but they still identify the same hardware lane.
    if backend_name == "mikrotik":
        haystack = f"{model_text} {firmware_text}".strip()
    return all(_profile_text(token) in haystack for token in tokens)


def profile_coverage_level(profile: FirmwareProfileOverlay) -> str:
    """Return a matrix-friendly coverage level for one overlay row."""
    if (
        profile.firmware_contains
        or profile.provider_name_contains
        or profile.provider_plmn_contains
        or profile.os_name
        or profile.os_version_contains
    ):
        return "exact"
    if profile.model_contains:
        return "model_specific"
    return "family_baseline"


def describe_profile_selector_scope(profile: FirmwareProfileOverlay) -> Dict[str, Any]:
    """Export the explicit selector dimensions for a firmware overlay."""
    return {
        "vendor": str(profile.vendor or "").strip(),
        "vendor_aliases": list(_profile_scope_tokens(profile.vendor_aliases)),
        "hw_sku_contains": list(_profile_scope_tokens(profile.model_contains)),
        "fw_sku_contains": list(_profile_scope_tokens(profile.firmware_contains)),
        "provider_name_contains": list(_profile_scope_tokens(profile.provider_name_contains)),
        "provider_plmn_contains": list(_profile_scope_tokens(profile.provider_plmn_contains)),
        "os_name": str(profile.os_name or "").strip(),
        "os_version_contains": list(_profile_scope_tokens(profile.os_version_contains)),
    }


def _profile_has_specific_selector(profile: FirmwareProfileOverlay) -> bool:
    return bool(
        profile.model_contains
        or profile.firmware_contains
        or profile.provider_name_contains
        or profile.provider_plmn_contains
        or profile.os_name
        or profile.os_version_contains
    )


def match_firmware_profile(
    *,
    backend: str = "",
    vendor: Any = "",
    model: Any = "",
    firmware: Any = "",
    provider_name: Any = "",
    provider_plmn: Any = "",
    os_name: Any = "",
    os_version: Any = "",
) -> Optional[FirmwareProfileOverlay]:
    backend_name = str(backend or "").strip().lower()
    vendor_text = _profile_text(vendor)
    model_text = _profile_text(model)
    firmware_text = _profile_text(firmware)
    provider_name_text = _profile_text(provider_name)
    provider_plmn_text = _profile_text(provider_plmn)
    os_name_text = _profile_text(os_name)
    os_version_text = _profile_text(os_version)
    backend_entry = get_backend_entry(backend_name) if backend_name else None
    candidate_profile = match_candidate_router_profile(vendor, model)
    candidate_backend = str(candidate_profile.reuse_backend_hint or "").strip().lower() if candidate_profile else ""
    candidate_family = str(candidate_profile.family or "").strip().lower() if candidate_profile else ""
    caller_family = get_backend_family(backend_name) if backend_entry else candidate_family
    for profile in FIRMWARE_PROFILE_OVERLAYS:
        profile_backend = str(profile.backend or "").strip().lower()
        if backend_name in {"h173", "g5b"} and profile_backend != backend_name:
            continue  # Selected challenge-only backend cannot inherit a classic family profile.
        if profile_backend:
            if backend_name and backend_name != profile_backend:
                candidate_may_redirect = bool(
                    candidate_backend == profile_backend
                    and (
                        not backend_entry
                        or (
                            caller_family
                            and candidate_family
                            and caller_family == candidate_family
                        )
                    )
                )
                if not candidate_may_redirect:
                    continue
            elif not backend_name and candidate_backend != profile_backend and not _profile_has_specific_selector(profile):
                continue
        # Family gate: when a generic overlay (no backend) declares a family,
        # the caller's backend must belong to that family to match.
        if profile.family and not profile.backend and backend_name:
            if caller_family != profile.family:
                continue
        if not _profile_vendor_matches(profile, vendor_text):
            continue
        if (profile.firmware_exact or profile.firmware_pattern) and profile.target_guard_model:
            if _profile_text(model) != _profile_text(profile.target_guard_model):
                continue
        if profile.model_contains and not _profile_model_tokens_match(
            backend_name=backend_name,
            model_text=model_text,
            firmware_text=firmware_text,
            tokens=profile.model_contains,
        ):
            continue
        if not profile.native_firmware_qualified(str(firmware or "")):
            continue
        if profile.firmware_contains and not all(_profile_text(token) in firmware_text for token in profile.firmware_contains):
            continue
        if profile.provider_name_contains:
            if not provider_name_text or not _profile_text_matches_any(provider_name_text, profile.provider_name_contains):
                continue
        if profile.provider_plmn_contains:
            if not provider_plmn_text or not _profile_text_matches_any(provider_plmn_text, profile.provider_plmn_contains):
                continue
        if not _profile_os_name_matches(
            profile=profile,
            backend_name=backend_name,
            caller_family=caller_family,
            os_name_text=os_name_text,
        ):
            continue
        if not _profile_os_version_matches(
            profile=profile,
            backend_name=backend_name,
            caller_family=caller_family,
            model_text=model_text,
            firmware_text=firmware_text,
            os_version_text=os_version_text,
        ):
            continue
        return profile
    return None


def firmware_profile_by_id(profile_id: Any) -> Optional[FirmwareProfileOverlay]:
    """Return the one catalog record selected by an exact profile id."""

    target = str(profile_id or "").strip()
    if not target:
        return None
    return next(
        (profile for profile in FIRMWARE_PROFILE_OVERLAYS if profile.profile_id == target),
        None,
    )


def export_field_trust_from_profile(
    profile: FirmwareProfileOverlay,
    *,
    fields: tuple[str, ...] = (),
) -> Dict[str, str]:
    """Export field trust without selecting a profile a second time."""

    if not isinstance(profile, FirmwareProfileOverlay):
        raise TypeError("exact_firmware_profile_required")
    target_fields = fields if fields else tuple(sorted(profile.field_trust or {}))
    return {field: profile.get_field_trust(field) for field in target_fields}


def export_action_trust_from_profile(
    profile: FirmwareProfileOverlay,
    *,
    actions: tuple[str, ...] = (),
) -> Dict[str, str]:
    """Export action trust without selecting a profile a second time."""

    if not isinstance(profile, FirmwareProfileOverlay):
        raise TypeError("exact_firmware_profile_required")
    target_actions = actions if actions else _PROFILE_ACTION_NAMES
    return {action: profile.get_action_trust(action) for action in target_actions}



def describe_runtime_profile(
    *,
    backend: str = "",
    vendor: Any = "",
    model: Any = "",
    firmware: Any = "",
    provider_name: Any = "",
    provider_plmn: Any = "",
    os_name: Any = "",
    os_version: Any = "",
) -> Dict[str, Any]:
    profile = match_firmware_profile(
        backend=backend,
        vendor=vendor,
        model=model,
        firmware=firmware,
        provider_name=provider_name,
        provider_plmn=provider_plmn,
        os_name=os_name,
        os_version=os_version,
    )
    if profile is None:
        return {}
    model_text = _profile_text(model)
    backend_name = str(backend or "").strip().lower()
    caller_family = get_backend_family(backend_name) if backend_name else ""
    has_model_specific_match = bool(profile.model_contains)
    has_firmware_specific_match = bool(
        profile.firmware_contains
        or profile.os_name
        or profile.os_version_contains
    )
    known_specific_model_variants = False
    backend_model_revalidation_required = False
    if model_text:
        candidate_profile = match_candidate_router_profile(vendor, model)
        if candidate_profile is not None:
            candidate_backend = str(candidate_profile.reuse_backend_hint or "").strip().lower()
            candidate_family = str(candidate_profile.family or "").strip().lower()
            if candidate_backend and backend_name and candidate_backend != backend_name:
                if not caller_family or not candidate_family or caller_family == candidate_family:
                    backend_model_revalidation_required = True
        for candidate in FIRMWARE_PROFILE_OVERLAYS:
            if backend_name and candidate.backend and backend_name != str(candidate.backend).lower():
                continue
            if candidate.family and not candidate.backend and backend_name:
                if caller_family != candidate.family:
                    continue
            if not _profile_vendor_matches(candidate, _profile_text(vendor)):
                continue
            if candidate.model_contains and all(_profile_text(token) in model_text for token in candidate.model_contains):
                if candidate.firmware_contains:
                    known_specific_model_variants = True
                    break
    match_quality = (
        "exact_firmware" if has_firmware_specific_match
        else "model_specific" if has_model_specific_match
        else "family_baseline"
    )
    coverage_level = profile_coverage_level(profile)
    firmware_revalidation_required = (
        match_quality == "family_baseline"
        and (
            (bool(_profile_text(firmware)) and known_specific_model_variants)
            or backend_model_revalidation_required
        )
    )
    return {
        "profile_id": profile.profile_id,
        "backend": profile.backend,
        "vendor": profile.vendor,
        "family": profile.family,
        "display_name": profile.display_name,
        "provenance": profile.provenance,
        "scheduler_signal_mode": profile.get_scheduler_signal_mode(),
        "match_quality": match_quality,
        "coverage_level": coverage_level,
        "firmware_revalidation_required": firmware_revalidation_required,
        "known_specific_model_variants": known_specific_model_variants,
        "backend_model_revalidation_required": backend_model_revalidation_required,
        "match": {
            "model_contains": list(profile.model_contains),
            "firmware_contains": list(profile.firmware_contains),
            "provider_name_contains": list(profile.provider_name_contains),
            "provider_plmn_contains": list(profile.provider_plmn_contains),
            "os_name": profile.os_name,
            "os_version_contains": list(profile.os_version_contains),
        },
        "selector_scope": describe_profile_selector_scope(profile),
        "capabilities": _complete_profile_capability_flags(profile),
        "nr_cell_id_in_nsa": profile.nr_cell_id_in_nsa,
        "network_settings_protocol": profile.network_settings,
        "network_settings_firmware": list(profile.network_settings_firmware),
        "static_config": dict(profile.static_config),
        "ue_capability": dict(profile.ue_capability),
        "lte_band_capability_status": profile.lte_band_capability_status,
        "nr_band_capability_status": profile.nr_band_capability_status,
        "band_capability_reference": profile.band_capability_reference,
        "supported_lte_bands": list(profile.supported_lte_bands),
        "supported_nr_bands": list(profile.supported_nr_bands),
        "provider_band_pool": list(profile.provider_band_pool),
        "field_support": {
            "supported": list(profile.supported_fields),
            "unsupported": list(profile.unsupported_fields),
            "synthetic": dict(profile.synthetic_fields),
        },
        "field_trust": export_field_trust_from_profile(profile),
        "action_support": {a: profile.get_action_trust(a) for a in _ALL_KNOWN_ACTIONS},
        "notes": profile.notes,
    }


def describe_runtime_profile_by_id(profile_id: Any) -> Dict[str, Any]:
    """Return the full profile truth for an already resolved profile id.

    This is used after the Modem Live Module has already resolved exact
    identity. In that path the profile id is the authority; GUI/reporting
    must not downgrade support just because a display field is unavailable in
    the same tick.
    """
    profile = firmware_profile_by_id(profile_id)
    if profile is None:
        return {}
    match_quality = (
        "exact_firmware"
        if profile.firmware_contains or profile.os_name or profile.os_version_contains
        else "model_specific"
        if profile.model_contains
        else "family_baseline"
    )
    return {
            "profile_id": profile.profile_id,
            "backend": profile.backend,
            "vendor": profile.vendor,
            "family": profile.family,
            "display_name": profile.display_name,
            "provenance": profile.provenance,
            "scheduler_signal_mode": profile.get_scheduler_signal_mode(),
            "match_quality": match_quality,
            "coverage_level": profile_coverage_level(profile),
            "firmware_revalidation_required": False,
            "known_specific_model_variants": False,
            "backend_model_revalidation_required": False,
            "match": {
                "model_contains": list(profile.model_contains),
                "firmware_contains": list(profile.firmware_contains),
                "provider_name_contains": list(profile.provider_name_contains),
                "provider_plmn_contains": list(profile.provider_plmn_contains),
                "os_name": profile.os_name,
                "os_version_contains": list(profile.os_version_contains),
            },
            "selector_scope": describe_profile_selector_scope(profile),
            "capabilities": _complete_profile_capability_flags(profile),
        "nr_cell_id_in_nsa": profile.nr_cell_id_in_nsa,
        "network_settings_protocol": profile.network_settings,
        "network_settings_firmware": list(profile.network_settings_firmware),
            "static_config": dict(profile.static_config),
            "ue_capability": dict(profile.ue_capability),
            "lte_band_capability_status": profile.lte_band_capability_status,
            "nr_band_capability_status": profile.nr_band_capability_status,
            "band_capability_reference": profile.band_capability_reference,
            "supported_lte_bands": list(profile.supported_lte_bands),
            "supported_nr_bands": list(profile.supported_nr_bands),
            "provider_band_pool": list(profile.provider_band_pool),
            "field_support": {
                "supported": list(profile.supported_fields),
                "unsupported": list(profile.unsupported_fields),
                "synthetic": dict(profile.synthetic_fields),
            },
            "field_trust": export_field_trust_from_profile(profile),
            "action_support": {a: profile.get_action_trust(a) for a in _ALL_KNOWN_ACTIONS},
            "notes": profile.notes,
        }


def scheduler_signal_mode_for_runtime_profile(
    *,
    backend: str = "",
    vendor: Any = "",
    model: Any = "",
    firmware: Any = "",
) -> str:
    """Return the scheduler source mode for the best-matching runtime profile."""
    profile = match_firmware_profile(
        backend=backend,
        vendor=vendor,
        model=model,
        firmware=firmware,
    )
    if profile is not None:
        return profile.get_scheduler_signal_mode()
    return "none"


def describe_backend_support(backend: str) -> Dict[str, Any]:
    entry = get_backend_entry(backend)
    semantics = get_backend_action_semantics(backend)
    return {
        "backend": str(backend or "").strip().lower(),
        "family": entry.family if entry else FAMILY_UNKNOWN,
        "vendor": entry.vendor if entry else "Unknown",
        "display_name": entry.display_name if entry else str(backend or "Unknown"),
        "implemented": bool(entry.implemented) if entry else False,
        "support_level": get_backend_support_level(backend),
        "capabilities": get_backend_capability_flags(backend),
        "action_semantics": asdict(semantics),
    }


def export_field_trust_from_overlay(
    *,
    backend: str = "",
    vendor: str = "",
    model: str = "",
    firmware: str = "",
    provider_name: str = "",
    provider_plmn: str = "",
    os_name: str = "",
    os_version: str = "",
    fields: tuple[str, ...] = (),
) -> Dict[str, str]:
    """Export per-field trust levels from the best-matching overlay.

    Returns a dict mapping field names to trust levels:
      "confirmed", "expected", "synthetic", "untested", "unsupported"

    If no overlay matches, all fields return "untested".
    If fields is empty, returns trust for all known fields in the overlay.

    This is the Layer 3 public contract — no vendor field names leak out.
    """
    profile = match_firmware_profile(
        backend=backend,
        vendor=vendor,
        model=model,
        firmware=firmware,
        provider_name=provider_name,
        provider_plmn=provider_plmn,
        os_name=os_name,
        os_version=os_version,
    )
    if profile is None:
        return {f: "untested" for f in fields} if fields else {}

    if fields:
        return {f: profile.get_field_trust(f) for f in fields}

    # No specific fields requested — export all known fields
    all_fields = set(profile.supported_fields)
    all_fields.update(profile.unsupported_fields)
    all_fields.update(profile.synthetic_fields or {})
    all_fields.update(profile.field_trust or {})
    return {f: profile.get_field_trust(f) for f in sorted(all_fields)}


# ── Layer 3 action-truth public contract ───────────────────────────────────

_ALL_KNOWN_ACTIONS: tuple[str, ...] = _PROFILE_ACTION_NAMES


def export_action_trust_from_overlay(
    *,
    backend: str = "",
    vendor: str = "",
    model: str = "",
    firmware: str = "",
    provider_name: str = "",
    provider_plmn: str = "",
    os_name: str = "",
    os_version: str = "",
    actions: tuple[str, ...] = (),
) -> Dict[str, str]:
    """Export per-action trust levels from the best-matching Layer 3 overlay.

    Returns a dict mapping action names to trust levels:
      "supported", "degraded", "unsupported", "untested"

    If no overlay matches, all actions return "untested".
    If actions is empty, returns trust for all known actions.

    Action names use 54GUC internal vocabulary (not vendor command names).
    This is the Layer 3 public contract — no vendor semantics leak out.
    """
    profile = match_firmware_profile(
        backend=backend,
        vendor=vendor,
        model=model,
        firmware=firmware,
        provider_name=provider_name,
        provider_plmn=provider_plmn,
        os_name=os_name,
        os_version=os_version,
    )
    target_actions = actions if actions else _ALL_KNOWN_ACTIONS
    if profile is None:
        return {a: "untested" for a in target_actions}
    return {a: profile.get_action_trust(a) for a in target_actions}
