"""Global Modem Live lane contract.

The backend profile may choose modem-specific group names, but each group must
belong to exactly one global lane purpose. This keeps B818, RouterOS, ZTE,
OpenWrt, and future modem profiles aligned without forcing every backend to use
the same raw endpoint names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from arch4.core_contracts.emergency_reachability import RecoveryTimingProfile


@dataclass(frozen=True)
class GlobalLiveLaneDefinition:
    lane: str
    contract_field: str
    purpose: str
    allowed_groups: frozenset[str]
    modem_contact: str = "read"

    @property
    def contacts_modem(self) -> bool:
        return self.modem_contact in {"read", "write", "read_write"}

    @property
    def writes_modem(self) -> bool:
        return self.modem_contact in {"write", "read_write"}

    @property
    def snapshot_only(self) -> bool:
        return self.modem_contact == "none"


@dataclass(frozen=True)
class ModemLiveAccessProfile:
    """Clock/register-facing modem access profile.

    This is intentionally higher level than a backend implementation.  It says
    which global lanes a modem family uses and how cautious the live module must
    be before writes, readbacks, and future discovery.
    """

    profile_id: str
    backend: str
    backend_family: str
    access_type: str
    lane_groups: Mapping[str, tuple[str, ...]]
    boot_static_refresh_policy: str = "identity_or_operator_refresh_only"
    fast_live_interval_s: float = 2.0
    fast_live_stale_s: float = 6.0
    slow_live_interval_s: float = 30.0
    event_write_min_quiet_s: float = 30.0
    post_write_readback_delay_s: float = 2.0
    post_write_readback_deadline_s: float = 30.0
    fast_read_slo_s: float = 2.0
    fast_sample_slo_warmup_samples: int = 15
    fast_signal_connect_timeout_s: float = 0.25
    fast_signal_read_timeout_s: float = 1.05
    maintenance_connect_timeout_s: float = 0.25
    maintenance_read_timeout_s: float = 0.75
    min_inter_request_gap_s: float = 0.5
    routeros_state_read_gap_s: float | None = None
    dev2_state_read_gap_s: float | None = None
    fast_traffic_cache_s: float = 0.75
    fast_traffic_connect_timeout_s: float = 0.12
    fast_traffic_min_timeout_s: float = 0.12
    fast_traffic_max_timeout_s: float = 0.35
    proactive_session_refresh_enabled: bool = True
    proactive_session_refresh_interval_s: float = 180.0
    proactive_session_refresh_min_gap_s: float = 60.0
    session_refresh_method: str = "none"
    session_connect_attempt_limit: int = 1
    session_connect_retry_gap_s: float = 0.0
    http_connection_mode: str = "close"
    read_retry_limit: int = 1
    write_retry_limit: int = 1
    readback_required: bool = True
    effective_match_required: bool = True
    direct_modem_writes_allowed: bool = True
    discovery_mode: str = "known_profile"
    recovery_timing: RecoveryTimingProfile = RecoveryTimingProfile()

    def __post_init__(self) -> None:
        if not isinstance(self.recovery_timing, RecoveryTimingProfile):
            raise TypeError("selected_recovery_timing_required")
        if (
            type(self.session_connect_attempt_limit) is not int
            or self.session_connect_attempt_limit < 1
        ):
            raise ValueError("modem_live_session_connect_attempt_limit_invalid")
        retry_gap = float(self.session_connect_retry_gap_s)
        if retry_gap < 0.0:
            raise ValueError("modem_live_session_connect_retry_gap_invalid")

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "profile_id": self.profile_id,
            "backend": self.backend,
            "backend_family": self.backend_family,
            "access_type": self.access_type,
            "lane_groups": {str(k): list(v) for k, v in dict(self.lane_groups or {}).items()},
            "boot_static_refresh_policy": self.boot_static_refresh_policy,
            "fast_live_interval_s": float(self.fast_live_interval_s),
            "fast_live_stale_s": float(self.fast_live_stale_s),
            "slow_live_interval_s": float(self.slow_live_interval_s),
            "event_write_min_quiet_s": float(self.event_write_min_quiet_s),
            "post_write_readback_delay_s": float(self.post_write_readback_delay_s),
            "post_write_readback_deadline_s": float(self.post_write_readback_deadline_s),
            "fast_read_slo_s": float(self.fast_read_slo_s),
            "fast_sample_slo_warmup_samples": int(self.fast_sample_slo_warmup_samples),
            "fast_signal_connect_timeout_s": float(self.fast_signal_connect_timeout_s),
            "fast_signal_read_timeout_s": float(self.fast_signal_read_timeout_s),
            "maintenance_connect_timeout_s": float(self.maintenance_connect_timeout_s),
            "maintenance_read_timeout_s": float(self.maintenance_read_timeout_s),
            "min_inter_request_gap_s": float(self.min_inter_request_gap_s),
            "routeros_state_read_gap_s": self.routeros_state_read_gap_s,
            "fast_traffic_cache_s": float(self.fast_traffic_cache_s),
            "fast_traffic_connect_timeout_s": float(self.fast_traffic_connect_timeout_s),
            "fast_traffic_min_timeout_s": float(self.fast_traffic_min_timeout_s),
            "fast_traffic_max_timeout_s": float(self.fast_traffic_max_timeout_s),
            "proactive_session_refresh_enabled": bool(self.proactive_session_refresh_enabled),
            "proactive_session_refresh_interval_s": float(self.proactive_session_refresh_interval_s),
            "proactive_session_refresh_min_gap_s": float(self.proactive_session_refresh_min_gap_s),
            "session_refresh_method": self.session_refresh_method,
            "session_connect_attempt_limit": int(
                self.session_connect_attempt_limit
            ),
            "session_connect_retry_gap_s": float(
                self.session_connect_retry_gap_s
            ),
            "read_retry_limit": int(self.read_retry_limit),
            "write_retry_limit": int(self.write_retry_limit),
            "readback_required": bool(self.readback_required),
            "effective_match_required": bool(self.effective_match_required),
            "direct_modem_writes_allowed": bool(self.direct_modem_writes_allowed),
            "discovery_mode": self.discovery_mode,
            "recovery_timing": self.recovery_timing.as_dict(),
        }
        # ``close`` was the reviewed generation-2 contract before transport
        # mode became an explicit profile field.  Keep that canonical default
        # byte-stable; only a non-default transport becomes new catalog
        # meaning and therefore requires a new reviewed Access Type.
        if self.http_connection_mode != "close":
            payload["http_connection_mode"] = self.http_connection_mode
        if self.dev2_state_read_gap_s is not None:
            payload["dev2_state_read_gap_s"] = self.dev2_state_read_gap_s
        return payload

    def fast_sample_expected_interval_s(self, *, clock_ms: int = 100) -> float:
        """Profile-owned diagnostic warning interval for completed fast samples.

        This value does not schedule modem access. Scheduling is owned by the
        profile cadence/cooldown fields; this value is only used to mark
        abnormal sample gaps after a read should have completed. 54GUC is not a
        real-time controller, so this warning interval deliberately allows OS
        scheduling jitter, GUI load, and occasional slow modem responses before
        raising noise in logs.
        """
        interval_s = max(0.0, float(self.fast_live_interval_s or 0.0))
        read_slo_s = max(0.0, float(self.fast_read_slo_s or 0.0))
        clock_s = max(1, int(clock_ms or 100)) / 1000.0
        diagnostic_margin_s = max(2.0, interval_s * 0.5, read_slo_s * 2.0, clock_s * 10.0)
        return max(
            interval_s + read_slo_s + diagnostic_margin_s,
            interval_s + 3.0,
        )


BOOT_STATIC_GROUPS: frozenset[str] = frozenset(
    {
        "device_identity",
        "identity_probe",
        "router_identity",
        "routerboard_identity",
        "network_identity",
        "modem_identity",
        "firmware",
        "protocol_fingerprint",
        "supported_bands",
        "antenna_capability",
        "antenna_status",
        "qam_capability",
        "sms_capability",
        "wifi_capability",
        "ue_capability",
    }
)

FAST_LIVE_GROUPS: frozenset[str] = frozenset(
    {
        "tcp_probe",
        "http_probe",
        "read_only_reachability",
        "radio_status",
        "lte_monitor",
        "modemmanager_signal",
        "ubus_radio_status",
        "dal_radio_status",
        "rf_quality",
        "traffic_presence",
        "traffic_volume",
        "connection_state",
        "rtt_probe",
        "nr_status",
    }
)

SLOW_LIVE_GROUPS: frozenset[str] = frozenset(
    {
        "compatibility_evidence",
        "discovery_enrichment",
        "status",
        "traffic",
        "uptime",
        "wifi_status",
        "netmode",
        "network_mode",
        "routeros_lte_state",
        "ca_info",
        "ca_details",
        "serving_cell_extra",
        "neighbors",
        "mcs",
        "quota_stats",
        "router_status",
        "modemmanager_location",
    }
)

EVENT_WRITE_GROUPS: frozenset[str] = frozenset(
    {
        "probe_only_no_write",
        "net_mode_write",
        "routeros_lte_band_mask",
        "band_selection",
        "band_lock",
        "cell_lock",
        "pband",
        "nr_band_lock",
        "antenna_write",
        "restart",
        "wifi_toggle",
        "login_token",
        "session_key",
        "at_chat",
        "mmcli_control",
        "ubus_rpc",
    }
)

EXPORT_GROUPS: frozenset[str] = frozenset(
    {
        "sidecars",
        "support_bundle",
        "history",
        "reporting",
        "logs",
        "runtime_sidecar",
    }
)

GLOBAL_LIVE_LANE_DEFINITIONS: tuple[GlobalLiveLaneDefinition, ...] = (
    GlobalLiveLaneDefinition(
        lane="boot_static",
        contract_field="static_capability_groups",
        purpose=(
            "Read identity, immutable capabilities, and operator-explicit static "
            "configuration once at connect/startup or after modem identity reset."
        ),
        allowed_groups=BOOT_STATIC_GROUPS,
        modem_contact="read",
    ),
    GlobalLiveLaneDefinition(
        lane="fast_live",
        contract_field="fast_modem_read_groups",
        purpose=(
            "Protected fresh modem read path for connection, serving RF, "
            "traffic presence, latency/health, and minimum action-safety truth."
        ),
        allowed_groups=FAST_LIVE_GROUPS,
        modem_contact="read",
    ),
    GlobalLiveLaneDefinition(
        lane="slow_live",
        contract_field="slow_modem_read_groups",
        purpose=(
            "Best-effort enrichment and maintenance reads that may lag, cache, "
            "or skip under pressure and must not delay fast_live."
        ),
        allowed_groups=SLOW_LIVE_GROUPS,
        modem_contact="read",
    ),
    GlobalLiveLaneDefinition(
        lane="event_write",
        contract_field="control_write_groups",
        purpose=(
            "Serialized commands, session/token refresh, mutation quiet windows, "
            "and post-write readback/effective-match verification."
        ),
        allowed_groups=EVENT_WRITE_GROUPS,
        modem_contact="read_write",
    ),
    GlobalLiveLaneDefinition(
        lane="export",
        contract_field="export_groups",
        purpose=(
            "File/export/report/history work. This lane consumes registers and "
            "snapshots only; it never contacts the modem."
        ),
        allowed_groups=EXPORT_GROUPS,
        modem_contact="none",
    ),
)

GLOBAL_LIVE_LANE_BY_FIELD: Mapping[str, GlobalLiveLaneDefinition] = {
    item.contract_field: item for item in GLOBAL_LIVE_LANE_DEFINITIONS
}

BETA_FAMILY_LIVE_LANE_GROUPS: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "xiaomi_miwifi_like": {
        "fast_live": ("radio_status", "rf_quality", "nr_status"),
        "slow_live": ("network_mode",),
        "boot_static": ("device_identity", "firmware", "supported_bands"),
        "event_write": ("net_mode_write", "login_token"),
        "export": ("sidecars", "support_bundle", "history"),
    },
    "tplink_dev2_like": {
        "fast_live": ("radio_status", "rf_quality", "nr_status", "traffic_presence", "traffic_volume"),
        "slow_live": ("network_mode",),
        "boot_static": ("device_identity", "firmware", "supported_bands"),
        "event_write": ("net_mode_write", "login_token"),
        "export": ("sidecars", "support_bundle", "history"),
    },
    "huawei_hilink_like": {
        "fast_live": ("radio_status", "rf_quality", "traffic_presence", "traffic_volume"),
        "slow_live": ("status", "traffic", "uptime"),
        "boot_static": (
            "device_identity",
            "firmware",
            "supported_bands",
            "antenna_capability",
            "antenna_status",
            "qam_capability",
            "sms_capability",
            "wifi_capability",
        ),
        "event_write": ("net_mode_write", "antenna_write", "restart", "login_token"),
        "export": ("sidecars", "support_bundle", "history"),
    },
    "routeros_at_like": {
        "fast_live": ("lte_monitor", "traffic_presence", "traffic_volume", "rtt_probe"),
        "slow_live": ("routeros_lte_state", "wifi_status", "ca_info", "serving_cell_extra", "mcs"),
        "boot_static": (
            "router_identity",
            "routerboard_identity",
            "network_identity",
            "modem_identity",
            "firmware",
            "supported_bands",
            "antenna_capability",
            "antenna_status",
            "qam_capability",
        ),
        "event_write": ("routeros_lte_band_mask", "cell_lock", "restart", "wifi_toggle", "at_chat"),
        "export": ("sidecars", "support_bundle", "history"),
    },
    "openwrt_modemmanager_like": {
        "fast_live": ("modemmanager_signal", "traffic_presence", "traffic_volume", "connection_state"),
        "slow_live": ("modemmanager_location", "wifi_status", "router_status", "network_mode"),
        "boot_static": (
            "device_identity",
            "firmware",
            "supported_bands",
            "antenna_capability",
            "qam_capability",
            "wifi_capability",
        ),
        "event_write": ("probe_only_no_write",),
        "export": ("sidecars", "support_bundle", "history"),
    },
    "zte_goform_like": {
        "fast_live": ("radio_status", "rf_quality", "traffic_presence", "traffic_volume", "nr_status"),
        "slow_live": ("ca_details", "wifi_status", "quota_stats", "network_mode"),
        "boot_static": (
            "device_identity",
            "firmware",
            "supported_bands",
            "antenna_capability",
            "qam_capability",
            "sms_capability",
            "wifi_capability",
        ),
        "event_write": ("band_selection", "cell_lock", "nr_band_lock", "restart", "wifi_toggle"),
        "export": ("sidecars", "support_bundle", "history"),
    },
    "zte_zwrt_ubus_like": {
        "fast_live": ("ubus_radio_status", "rf_quality", "traffic_presence", "traffic_volume", "nr_status"),
        "slow_live": ("ca_details", "wifi_status", "router_status", "network_mode"),
        "boot_static": (
            "device_identity",
            "firmware",
            "supported_bands",
            "antenna_capability",
            "qam_capability",
            "wifi_capability",
        ),
        "event_write": ("band_selection", "cell_lock", "nr_band_lock", "restart", "wifi_toggle", "ubus_rpc"),
        "export": ("sidecars", "support_bundle", "history"),
    },
    "zyxel_webui_like": {
        "fast_live": ("dal_radio_status", "rf_quality", "traffic_presence", "traffic_volume"),
        "slow_live": ("wifi_status", "router_status", "network_mode"),
        "boot_static": (
            "device_identity",
            "firmware",
            "supported_bands",
            "antenna_capability",
            "qam_capability",
            "wifi_capability",
        ),
        "event_write": ("band_selection", "cell_lock", "restart", "wifi_toggle", "session_key"),
        "export": ("sidecars", "support_bundle", "history"),
    },
    "unknown": {
        "fast_live": ("tcp_probe", "http_probe", "read_only_reachability"),
        "slow_live": ("compatibility_evidence", "discovery_enrichment"),
        "boot_static": ("identity_probe", "protocol_fingerprint"),
        "event_write": ("probe_only_no_write",),
        "export": ("sidecars", "support_bundle", "history"),
    },
}


def _default_access_type_for_family(backend_family: str) -> str:
    family = str(backend_family or "").strip().lower()
    if family == "huawei_hilink_like":
        return "huawei_http_single_lane"
    if family == "routeros_at_like":
        return "routeros_api_at_serial_bridge"
    if family == "openwrt_modemmanager_like":
        return "openwrt_modemmanager_single_lane"
    if family == "zte_goform_like":
        return "zte_goform_http_single_lane"
    if family == "zte_zwrt_ubus_like":
        return "zte_zwrt_ubus_single_lane"
    if family == "zyxel_webui_like":
        return "zyxel_webui_single_lane"
    return "probe_only_read_only"


def _profile(
    *,
    profile_id: str,
    backend: str,
    backend_family: str,
    access_type: str = "",
    fast_live_stale_s: float,
    slow_live_interval_s: float,
    event_write_min_quiet_s: float,
    post_write_readback_delay_s: float,
    fast_live_interval_s: float = 2.0,
    post_write_readback_deadline_s: float = 30.0,
    fast_read_slo_s: float = 2.0,
    fast_sample_slo_warmup_samples: int = 15,
    fast_signal_connect_timeout_s: float = 0.25,
    fast_signal_read_timeout_s: float = 1.05,
    maintenance_connect_timeout_s: float = 0.25,
    maintenance_read_timeout_s: float = 0.75,
    min_inter_request_gap_s: float = 0.5,
    routeros_state_read_gap_s: float | None = None,
    dev2_state_read_gap_s: float | None = None,
    fast_traffic_cache_s: float = 0.75,
    fast_traffic_connect_timeout_s: float = 0.12,
    fast_traffic_min_timeout_s: float = 0.12,
    fast_traffic_max_timeout_s: float = 0.35,
    proactive_session_refresh_enabled: bool = True,
    proactive_session_refresh_interval_s: float = 180.0,
    proactive_session_refresh_min_gap_s: float = 60.0,
    session_refresh_method: str = "none",
    session_connect_attempt_limit: int = 1,
    session_connect_retry_gap_s: float = 0.0,
    http_connection_mode: str = "close",
    read_retry_limit: int = 1,
    write_retry_limit: int = 1,
    readback_required: bool = True,
    effective_match_required: bool = True,
    direct_modem_writes_allowed: bool = True,
    discovery_mode: str = "known_profile",
    lane_groups_override: Mapping[str, tuple[str, ...]] | None = None,
    recovery_timing: RecoveryTimingProfile = RecoveryTimingProfile(),
) -> ModemLiveAccessProfile:
    lane_groups = {
        item.lane: tuple((BETA_FAMILY_LIVE_LANE_GROUPS.get(backend_family, {}) or {}).get(item.lane, ()) or ())
        for item in GLOBAL_LIVE_LANE_DEFINITIONS
    }
    if lane_groups_override:
        for lane, groups in lane_groups_override.items():
            lane_groups[str(lane)] = tuple(groups or ())
    return ModemLiveAccessProfile(
        profile_id=profile_id,
        backend=backend,
        backend_family=backend_family,
        access_type=access_type or _default_access_type_for_family(backend_family),
        recovery_timing=recovery_timing,
        lane_groups=lane_groups,
        fast_live_interval_s=fast_live_interval_s,
        fast_live_stale_s=fast_live_stale_s,
        slow_live_interval_s=slow_live_interval_s,
        event_write_min_quiet_s=event_write_min_quiet_s,
        post_write_readback_delay_s=post_write_readback_delay_s,
        post_write_readback_deadline_s=post_write_readback_deadline_s,
        fast_read_slo_s=fast_read_slo_s,
        fast_sample_slo_warmup_samples=fast_sample_slo_warmup_samples,
        fast_signal_connect_timeout_s=fast_signal_connect_timeout_s,
        fast_signal_read_timeout_s=fast_signal_read_timeout_s,
        maintenance_connect_timeout_s=maintenance_connect_timeout_s,
        maintenance_read_timeout_s=maintenance_read_timeout_s,
        min_inter_request_gap_s=min_inter_request_gap_s,
        routeros_state_read_gap_s=routeros_state_read_gap_s,
        dev2_state_read_gap_s=dev2_state_read_gap_s,
        fast_traffic_cache_s=fast_traffic_cache_s,
        fast_traffic_connect_timeout_s=fast_traffic_connect_timeout_s,
        fast_traffic_min_timeout_s=fast_traffic_min_timeout_s,
        fast_traffic_max_timeout_s=fast_traffic_max_timeout_s,
        proactive_session_refresh_enabled=proactive_session_refresh_enabled,
        proactive_session_refresh_interval_s=proactive_session_refresh_interval_s,
        proactive_session_refresh_min_gap_s=proactive_session_refresh_min_gap_s,
        session_refresh_method=session_refresh_method,
        session_connect_attempt_limit=session_connect_attempt_limit,
        session_connect_retry_gap_s=session_connect_retry_gap_s,
        http_connection_mode=http_connection_mode,
        read_retry_limit=read_retry_limit,
        write_retry_limit=write_retry_limit,
        readback_required=readback_required,
        effective_match_required=effective_match_required,
        direct_modem_writes_allowed=direct_modem_writes_allowed,
        discovery_mode=discovery_mode,
    )


BETA_MODEM_LIVE_ACCESS_PROFILES: Mapping[str, ModemLiveAccessProfile] = {
    "nx520-250902": _profile(
        profile_id="nx520_srba1_250902_dev2_access_v2", backend="nx520", backend_family="tplink_dev2_like",
        access_type="tplink_dev2_cbc512_single_lane", fast_live_stale_s=6.0, slow_live_interval_s=30.0,
        fast_live_interval_s=2.0, dev2_state_read_gap_s=0.0,
        fast_signal_read_timeout_s=5.0,
        recovery_timing=RecoveryTimingProfile(rescue_window_s=300.0, restart_verification_s=300.0),
        event_write_min_quiet_s=3.0, post_write_readback_delay_s=5.0,
        proactive_session_refresh_enabled=False, session_refresh_method="none",
    ),
    "nx520": _profile(
        profile_id="nx520_srba1_20250915_dev2_access_v1", backend="nx520", backend_family="tplink_dev2_like",
        access_type="tplink_dev2_cbc512_single_lane", fast_live_stale_s=6.0, slow_live_interval_s=30.0,
        event_write_min_quiet_s=3.0, post_write_readback_delay_s=3.0,
        proactive_session_refresh_enabled=False, session_refresh_method="none",
    ),
    "xiaomi_cb0401v2": _profile(
        profile_id="xiaomi_cb0401v2_3_0_88_stock_access_v1", backend="xiaomi_cb0401v2", backend_family="xiaomi_miwifi_like",
        access_type="xiaomi_stock_luci_single_lane", fast_live_stale_s=6.0, slow_live_interval_s=30.0,
        # Stock login negotiates TLS; public clients use multi-second requests.
        # These are ceilings, not a new polling interval or freshness allowance.
        fast_signal_connect_timeout_s=2.0, fast_signal_read_timeout_s=8.0,
        maintenance_connect_timeout_s=2.0, maintenance_read_timeout_s=8.0,
        event_write_min_quiet_s=3.0, post_write_readback_delay_s=3.0,
        proactive_session_refresh_enabled=False, session_refresh_method="none",
    ),
    "nx520v": _profile(
        profile_id="nx520v_eu1_260408_dev2_access_v1",
        backend="nx520v", backend_family="tplink_dev2_like",
        access_type="tplink_dev2_cbc512_single_lane",
        fast_live_stale_s=6.0, slow_live_interval_s=30.0,
        event_write_min_quiet_s=3.0, post_write_readback_delay_s=3.0,
        proactive_session_refresh_enabled=False,
        session_refresh_method="none",
    ),
    "b818": _profile(
        profile_id="b818_hilink_access_v1",
        backend="b818",
        backend_family="huawei_hilink_like",
        access_type="huawei_http_single_lane",
        fast_live_interval_s=2.1,
        fast_live_stale_s=15.0,
        slow_live_interval_s=30.0,
        event_write_min_quiet_s=30.0,
        # The B818 accepts the PBAND anchor write before its radio/session
        # is ready for the CA-pool write. Modem Live therefore owns this full
        # protected inter-stage and final-readback interval.
        post_write_readback_delay_s=5.0,
        post_write_readback_deadline_s=6.0,
        fast_read_slo_s=1.65,
        fast_signal_read_timeout_s=1.25,
        fast_traffic_max_timeout_s=0.25,
        proactive_session_refresh_interval_s=150.0,
        proactive_session_refresh_min_gap_s=45.0,
        session_refresh_method="ses_tok_info_state_login",
        lane_groups_override={
            "fast_live": ("radio_status", "rf_quality", "traffic_presence"),
            "slow_live": ("status", "traffic", "uptime", "wifi_status", "netmode", "quota_stats"),
        },
    ),
    "b535": _profile(
        profile_id="b535_hilink_access_v1",
        backend="b535",
        backend_family="huawei_hilink_like",
        fast_live_interval_s=2.0,
        fast_live_stale_s=6.0,
        slow_live_interval_s=30.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=5.0,
        session_refresh_method="ses_tok_info_state_login",
        lane_groups_override={
            "slow_live": ("status", "traffic", "uptime", "wifi_status", "quota_stats"),
        },
    ),
    "b535-232": _profile(
        profile_id="b535_232_hilink_access_v1",
        backend="b535-232",
        backend_family="huawei_hilink_like",
        fast_live_interval_s=2.0,
        fast_live_stale_s=6.0,
        slow_live_interval_s=30.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=5.0,
        session_refresh_method="ses_tok_info_state_login",
        lane_groups_override={
            "slow_live": ("status", "traffic", "uptime", "wifi_status", "quota_stats"),
        },
    ),
    "b311": _profile(
        profile_id="b311_hilink_access_v1",
        backend="b311",
        backend_family="huawei_hilink_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=10.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=2.0,
        session_refresh_method="ses_tok_info_state_login",
    ),
    "b530": _profile(
        profile_id="b530_hilink_access_v1",
        backend="b530",
        backend_family="huawei_hilink_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=10.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=2.0,
        session_refresh_method="ses_tok_info_state_login",
    ),
    "h112": _profile(
        profile_id="h112_hilink_access_v1",
        backend="h112",
        backend_family="huawei_hilink_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=10.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=2.0,
        session_refresh_method="ses_tok_info_state_login",
    ),
    "h122": _profile(
        profile_id="h122_hilink_access_v1",
        backend="h122",
        backend_family="huawei_hilink_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=10.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=2.0,
        session_refresh_method="ses_tok_info_state_login",
    ),
    "h155": _profile(
        profile_id="h155_hilink_access_v1",
        backend="h155",
        backend_family="huawei_hilink_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=10.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=2.0,
        session_refresh_method="ses_tok_info_state_login",
    ),
    "h173": _profile(
        profile_id="h173_c983_scram_hilink_access_v1", backend="h173",
        backend_family="huawei_hilink_like", fast_live_stale_s=6.0,
        slow_live_interval_s=10.0, event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=2.0, session_refresh_method="ses_tok_info_scram_state_login",
    ),
    "mikrotik": _profile(
        profile_id="mikrotik_routeros_at_access_v1",
        routeros_state_read_gap_s=0.0,
        backend="mikrotik",
        backend_family="routeros_at_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=30.0,
        event_write_min_quiet_s=45.0,
        post_write_readback_delay_s=5.0,
        proactive_session_refresh_enabled=False,
        session_refresh_method="routeros_api_session",
        session_connect_attempt_limit=3,
        session_connect_retry_gap_s=1.5,
    ),
    "routeros": _profile(
        profile_id="routeros_at_access_v1",
        routeros_state_read_gap_s=0.0,
        backend="routeros",
        backend_family="routeros_at_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=30.0,
        event_write_min_quiet_s=45.0,
        post_write_readback_delay_s=5.0,
        proactive_session_refresh_enabled=False,
        session_refresh_method="routeros_api_session",
        session_connect_attempt_limit=3,
        session_connect_retry_gap_s=1.5,
    ),
    "mf286r": _profile(
        profile_id="zte_mf286r_goform_access_v1",
        backend="mf286r",
        backend_family="zte_goform_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=30.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=3.0,
        session_refresh_method="zte_goform_ad",
    ),
    "mf296r": _profile(
        profile_id="zte_mf296r_goform_access_v1",
        backend="mf296r",
        backend_family="zte_goform_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=30.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=3.0,
        proactive_session_refresh_enabled=True,
        proactive_session_refresh_interval_s=60.0,
        proactive_session_refresh_min_gap_s=60.0,
        session_refresh_method="zte_mf296r_ld_sha256_session",
        http_connection_mode="persistent",
    ),
    "mc801a": _profile(
        profile_id="zte_mc801a_goform_access_v1",
        backend="mc801a",
        backend_family="zte_goform_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=30.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=3.0,
        session_refresh_method="zte_goform_ad",
    ),
    "g5b": _profile(
        profile_id="g5b_b03_md5_goform_access_v1", backend="g5b",
        backend_family="zte_goform_like", fast_live_stale_s=6.0,
        slow_live_interval_s=30.0, event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=3.0, session_refresh_method="zte_goform_ad_md5_b03",
    ),
    "mc888": _profile(
        profile_id="zte_mc888_goform_access_v1",
        backend="mc888",
        backend_family="zte_goform_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=30.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=3.0,
        session_refresh_method="zte_goform_ad",
    ),
    "unknown": _profile(
        profile_id="unknown_probe_only_access_v1",
        backend="unknown",
        backend_family="unknown",
        fast_live_interval_s=5.0,
        fast_live_stale_s=15.0,
        slow_live_interval_s=60.0,
        event_write_min_quiet_s=0.0,
        post_write_readback_delay_s=0.0,
        post_write_readback_deadline_s=0.0,
        proactive_session_refresh_enabled=False,
        readback_required=False,
        effective_match_required=False,
        direct_modem_writes_allowed=False,
        discovery_mode="probe_only_no_write",
    ),
}

FAMILY_RUNTIME_PROBE_LIVE_ACCESS_PROFILES: Mapping[str, ModemLiveAccessProfile] = {
    "huawei_hilink_like": _profile(
        profile_id="huawei_hilink_family_runtime_probe_access_v1",
        backend="huawei_hilink_like",
        backend_family="huawei_hilink_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=10.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=2.0,
        session_refresh_method="ses_tok_info_state_login",
        discovery_mode="family_runtime_probe",
    ),
    "routeros_at_like": _profile(
        profile_id="routeros_at_family_runtime_probe_access_v1",
        backend="routeros_at_like",
        backend_family="routeros_at_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=30.0,
        event_write_min_quiet_s=45.0,
        post_write_readback_delay_s=5.0,
        proactive_session_refresh_enabled=False,
        session_refresh_method="routeros_api_session",
        discovery_mode="family_runtime_probe",
    ),
    "zte_goform_like": _profile(
        profile_id="zte_goform_family_runtime_probe_access_v1",
        backend="zte_goform_like",
        backend_family="zte_goform_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=30.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=3.0,
        session_refresh_method="zte_goform_ad",
        discovery_mode="family_runtime_probe",
    ),
    "zte_zwrt_ubus_like": _profile(
        profile_id="zte_zwrt_ubus_family_runtime_probe_access_v1",
        backend="zte_zwrt_ubus_like",
        backend_family="zte_zwrt_ubus_like",
        # Shared UBUS reply allowance; polling cadence and freshness stay separate.
        fast_signal_read_timeout_s=8.0,
        fast_live_stale_s=6.0,
        slow_live_interval_s=30.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=3.0,
        session_refresh_method="zte_zwrt_ubus_session",
        discovery_mode="family_runtime_probe",
    ),
    "openwrt_modemmanager_like": _profile(
        profile_id="openwrt_modemmanager_rpcd_read_only_access_v1",
        backend="openwrt_modemmanager_like",
        backend_family="openwrt_modemmanager_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=30.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=3.0,
        proactive_session_refresh_enabled=False,
        session_refresh_method="modemmanager_session",
        readback_required=False,
        effective_match_required=False,
        direct_modem_writes_allowed=False,
        discovery_mode="family_runtime_probe",
    ),
    "zyxel_webui_like": _profile(
        profile_id="zyxel_webui_family_runtime_probe_access_v1",
        backend="zyxel_webui_like",
        backend_family="zyxel_webui_like",
        fast_live_stale_s=6.0,
        slow_live_interval_s=30.0,
        event_write_min_quiet_s=30.0,
        post_write_readback_delay_s=3.0,
        session_refresh_method="zyxel_webui_session_key",
        discovery_mode="family_runtime_probe",
    ),
}

FAMILY_LIVE_ACCESS_PROFILE_DEFAULTS: Mapping[str, ModemLiveAccessProfile] = {
    **FAMILY_RUNTIME_PROBE_LIVE_ACCESS_PROFILES,
    "unknown": BETA_MODEM_LIVE_ACCESS_PROFILES["unknown"],
}

PROFILE_ID_LIVE_ACCESS_BACKENDS: Mapping[str, str] = {
    "tplink_nx520_srba1_250902_dev2": "nx520-250902",
    "huawei_b535_333_h470sp6c9831": "b535",
    "huawei_b535_232_h191sp64c983": "b535-232",
    "huawei_b535_232_model": "b535-232",
}


def global_live_lane_vocab() -> frozenset[str]:
    groups: set[str] = set()
    for item in GLOBAL_LIVE_LANE_DEFINITIONS:
        groups.update(item.allowed_groups)
    return frozenset(groups)


def global_live_lane_purpose_map() -> dict[str, dict[str, Any]]:
    return {
        item.lane: {
            "contract_field": item.contract_field,
            "purpose": item.purpose,
            "allowed_groups": sorted(item.allowed_groups),
            "modem_contact": item.modem_contact,
            "contacts_modem": item.contacts_modem,
            "writes_modem": item.writes_modem,
            "snapshot_only": item.snapshot_only,
        }
        for item in GLOBAL_LIVE_LANE_DEFINITIONS
    }


def live_lane_groups_for_family(family: str) -> dict[str, tuple[str, ...]]:
    profile = BETA_FAMILY_LIVE_LANE_GROUPS.get(str(family or "").strip().lower(), {})
    return {item.lane: tuple(profile.get(item.lane, ()) or ()) for item in GLOBAL_LIVE_LANE_DEFINITIONS}


def modem_live_access_backend_for_identity(
    *,
    backend: str = "",
    profile_id: str = "",
    model: str = "",
    firmware: str = "",
    vendor: str = "",
) -> str:
    """Resolve the exact access backend without contacting the modem.

    CLI backend is only a coarse transport hint. Exact identity/profile evidence
    owns a distinct access contract only when its physical cadence differs.
    """

    backend_key = str(backend or "").strip().lower()
    profile_key = str(profile_id or "").strip().lower()
    if profile_key in PROFILE_ID_LIVE_ACCESS_BACKENDS:
        return PROFILE_ID_LIVE_ACCESS_BACKENDS[profile_key]
    model_key = str(model or "").strip().lower()
    firmware_key = str(firmware or "").strip().lower()
    del vendor
    if backend_key in {"b535", "huawei"}:
        if "b535-232" in model_key or "h191sp64c983" in firmware_key:
            return "b535-232"
    return backend_key


def modem_live_access_profile_for(
    *,
    backend: str = "",
    backend_family: str = "",
    profile_id: str = "",
    model: str = "",
    firmware: str = "",
    vendor: str = "",
) -> ModemLiveAccessProfile:
    if str(profile_id or '').strip():
        # An exact selection has one portable authority. Generic discovery
        # contracts below are never a fallback for a missing selected file.
        from arch4.modem_live.profile_definition.registry import startup_registry
        data = startup_registry().select(str(profile_id).strip()).access
        recovery = RecoveryTimingProfile(**data.pop('recovery_timing'))
        data['lane_groups'] = {name:tuple(groups) for name,groups in data['lane_groups'].items()}
        return ModemLiveAccessProfile(**data,recovery_timing=recovery)
    backend_key = modem_live_access_backend_for_identity(
        backend=backend,
        profile_id=profile_id,
        model=model,
        firmware=firmware,
        vendor=vendor,
    )
    family_key = str(backend_family or "").strip().lower()
    if backend_key in BETA_MODEM_LIVE_ACCESS_PROFILES:
        return BETA_MODEM_LIVE_ACCESS_PROFILES[backend_key]
    if family_key in FAMILY_LIVE_ACCESS_PROFILE_DEFAULTS:
        return FAMILY_LIVE_ACCESS_PROFILE_DEFAULTS[family_key]
    return BETA_MODEM_LIVE_ACCESS_PROFILES["unknown"]


def validate_global_live_lane_contract(contract: Any, *, require_populated_lanes: bool = False) -> tuple[str, ...]:
    """Return lane contract violations for a backend profile.

    The check is deliberately semantic, not transport-specific. A RouterOS group
    can be named ``lte_monitor`` and a Huawei group can be named
    ``radio_status`` as long as both live in the same global lane.
    """

    issues: list[str] = []
    lane_map = dict(getattr(contract, "lane_group_map", lambda: {})() or {})

    for definition in GLOBAL_LIVE_LANE_DEFINITIONS:
        values = tuple(lane_map.get(definition.contract_field, ()) or ())
        if require_populated_lanes and not values:
            issues.append(f"{definition.contract_field}:empty")
            continue
        unknown = sorted(set(values).difference(definition.allowed_groups))
        if unknown:
            issues.append(f"{definition.contract_field}:unknown:{','.join(unknown)}")

    overlaps = getattr(contract, "lane_group_overlaps", lambda: {})() or {}
    for name, values in sorted(overlaps.items()):
        issues.append(f"{name}:overlap:{','.join(values)}")

    static_overlaps = getattr(contract, "static_capability_runtime_overlaps", lambda: {})() or {}
    for name, values in sorted(static_overlaps.items()):
        issues.append(f"{name}:static_runtime_overlap:{','.join(values)}")

    return tuple(issues)
