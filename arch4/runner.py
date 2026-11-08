"""
arch4/runner.py
═══════════════════════════════════════════════════════════════
CGF Headless Launcher

Three modes:
  bash start.sh              → verify config (read-only), then run CGF monitor
  bash start.sh --check      → verify config (read-only), print report, EXIT
  bash start.sh --apply      → verify + fix missing config, then run CGF monitor

Default is SAFE: never writes to router without --apply.
═══════════════════════════════════════════════════════════════
"""

import sys
import logging
import os

from arch4.cgf_runner import CGFRunner
from arch4.interfaces import Arch4Config, ARCH_SW_LABEL
from arch4.foundation.runtime_artifacts import (
    foundation_runtime_mode,
    foundation_write_authority,
)

log = logging.getLogger("arch4.runner")


def _runner_runtime_mode(*, probe_mode: bool, observe_only: bool, check_only: bool) -> str:
    return foundation_runtime_mode(
        probe_mode=probe_mode,
        observe_only=observe_only,
        check_only=check_only,
    )


def _runner_write_authority(*, probe_mode: bool, observe_only: bool, check_only: bool) -> tuple[str, str]:
    return foundation_write_authority(
        probe_mode=probe_mode,
        observe_only=observe_only,
        check_only=check_only,
    )


def _startup_apply_authorized(*, apply_requested: bool, startup_verify_only: bool) -> bool:
    """Startup writes are legal only for an explicit startup apply request."""
    return bool(apply_requested) and not bool(startup_verify_only)


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description=ARCH_SW_LABEL)
    parser.add_argument("--host", default="", help="Router IP (MikroTik)")
    parser.add_argument("--user", default="admin", help="Router username (MikroTik)")
    parser.add_argument("--password", default="", help="Router password (MikroTik)")
    parser.add_argument(
        "--sample-interval",
        dest="sample_interval_s",
        type=float,
        default=2.0,
        help="Runtime sample interval (seconds)",
    )
    parser.add_argument("--modem", default="", choices=["", "mikrotik", "b818", "b535", "b530", "h112", "h122", "huawei", "openwrt", "mf296r", "mc888", "g5b", "zte_zwrt", "nr5101", "fwa510", "model"],
                        help="Modem backend (required for headless; launcher scripts provide this)")
    parser.add_argument("--huawei-host", "--b818-host", dest="huawei_host", default="192.168.8.1",
                        help="Huawei HiLink IP address (default 192.168.8.1)")
    parser.add_argument("--huawei-user", "--b818-user", dest="huawei_user", default="admin",
                        help="Huawei HiLink web UI username (default admin)")
    parser.add_argument("--huawei-password", "--b818-password", dest="huawei_password", default="",
                        help="Huawei HiLink web UI password")
    parser.add_argument("--huawei-ping-iface", "--b818-ping-iface", dest="huawei_ping_iface", default="",
                        help="OS network interface for Huawei HiLink RTT ping")
    parser.add_argument("--zte-host", dest="zte_host", default="192.168.1.1",
                        help="ZTE goform router IP address (default 192.168.1.1)")
    parser.add_argument("--tplink-host", default="192.168.1.1", help="Qualified TP-Link device address")
    parser.add_argument("--tplink-user", default="admin", help="TP-Link device user")
    parser.add_argument("--tplink-password", default="", help="TP-Link device password")
    parser.add_argument("--tplink-ping-iface", default="", help="TP-Link RTT interface")
    parser.add_argument("--zte-user", dest="zte_user", default="admin",
                        help="ZTE WebUI username (default admin)")
    parser.add_argument("--zte-password", dest="zte_password", default="",
                        help="ZTE WebUI password")
    parser.add_argument("--zte-ping-iface", dest="zte_ping_iface", default="",
                        help="OS network interface for ZTE RTT ping")
    parser.add_argument("--openwrt-host", dest="openwrt_host", default="192.168.8.1",
                        help="OpenWrt rpcd HTTP host address")
    parser.add_argument("--openwrt-user", dest="openwrt_user", default="root",
                        help="OpenWrt host username")
    parser.add_argument("--openwrt-password", dest="openwrt_password", default="",
                        help="OpenWrt host password")
    parser.add_argument("--openwrt-modem-id", dest="openwrt_modem_id", default="",
                        help="ModemManager modem id override")
    parser.add_argument("--openwrt-ping-iface", dest="openwrt_ping_iface", default="",
                        help="OS network interface for OpenWrt RTT ping")
    parser.add_argument("--zyxel-host", dest="zyxel_host", default="192.168.1.1",
                        help="Zyxel web configurator host address")
    parser.add_argument("--zyxel-user", dest="zyxel_user", default="admin",
                        help="Zyxel web configurator username")
    parser.add_argument("--zyxel-password", dest="zyxel_password", default="",
                        help="Zyxel web configurator password")
    parser.add_argument("--zyxel-ping-iface", dest="zyxel_ping_iface", default="",
                        help="OS network interface for Zyxel RTT ping")
    parser.add_argument("--target-guard-model", dest="target_guard_model", default="",
                        help="Optional expected router model; refuse wrong-target web UI actions if it mismatches")
    parser.add_argument("--target-guard-serial", dest="target_guard_serial", default="",
                        help="Optional expected router serial; refuse wrong-target web UI actions if it mismatches")
    parser.add_argument("--target-guard-firmware", dest="target_guard_firmware", default="",
                        help="Optional secondary firmware match when router serial is unavailable")
    parser.add_argument("--target-guard-imei", dest="target_guard_imei", default="",
                        help="Optional expected router IMEI; refuse wrong-target web UI actions if it mismatches")
    parser.add_argument("--model-profile-name", dest="model_profile_name", default="",
                        help="Optional local model name; default is the built-in synthetic model")
    parser.add_argument("--model-profile-path", dest="model_profile_path", default="",
                        help="Optional custom model JSON file; overrides the name")
    parser.add_argument("--signal-sinr-scale", type=float, default=1.0,
                        help="Backend signal normalization scale for SINR")
    parser.add_argument("--signal-rsrp-scale", type=float, default=1.0,
                        help="Backend signal normalization scale for RSRP")
    parser.add_argument("--signal-rsrq-scale", type=float, default=1.0,
                        help="Backend signal normalization scale for RSRQ")
    parser.add_argument("--signal-rssi-scale", type=float, default=1.0,
                        help="Backend signal normalization scale for RSSI")
    parser.add_argument("--emergency-sinr-floor", type=float, default=3.0,
                        help="Emergency SINR floor used by runtime incident and state logic")
    parser.add_argument("--signal-contract-warn", action="store_true",
                        help="Warn when normalized values are outside LTE contract ranges")
    parser.add_argument("--no-signal-contract-warn", action="store_true",
                        help="Disable out-of-range signal contract warnings")
    parser.add_argument("--router-api-timeout", type=float, default=30.0,
                        help="RouterOS API socket timeout (seconds)")
    parser.add_argument("--mikrotik-rf-monitor-interval", type=float, default=3.0,
                        help="Minimum interval between modem-facing RF monitor reads (seconds)")
    parser.add_argument("--mikrotik-qcainfo-interval", type=float, default=8.0,
                        help="Minimum interval between AT+QCAINFO reads (seconds)")
    parser.add_argument("--mikrotik-qnwcfg-mcs-interval", type=float, default=6.0,
                        help='Minimum interval between AT+QNWCFG="mcs" reads (seconds)')
    parser.add_argument("--mikrotik-qeng-mcs-interval", type=float, default=15.0,
                        help="Minimum interval between AT+QENG servingcell reads (seconds)")
    parser.add_argument("--mikrotik-at-live-polling", action="store_true",
                        help="Enable continuous MikroTik at-chat enrichment polling. Default is RouterOS-only live telemetry.")
    parser.add_argument("--mikrotik-runtime-capability-probe", action="store_true",
                        help="Enable runtime MikroTik AT capability probes. Default avoids at-chat during live startup/maintenance.")
    parser.add_argument("--mikrotik-at-read-delay", type=float, default=1.0,
                        help="Post-delay after read-only AT commands (seconds)")
    parser.add_argument("--mikrotik-at-write-delay", type=float, default=2.5,
                        help="Post-delay after mutating AT commands (seconds)")
    parser.add_argument("--mikrotik-at-bus-guard", type=float, default=0.3,
                        help="Minimum gap between consecutive AT commands (seconds)")
    parser.add_argument("--mikrotik-action-min-session-uptime", type=float, default=60.0,
                        help="Block mutating radio actions until LTE session uptime reaches this value (seconds)")
    parser.add_argument("--mikrotik-post-write-blackout", type=float, default=10.0,
                        help="Pause AT read polling after mutating actions (seconds)")
    parser.add_argument("--mikrotik-at-latency-warn-ms", type=float, default=1500.0,
                        help="Warn when AT call latency exceeds this threshold (milliseconds)")
    parser.add_argument("--mikrotik-at-bus-guard-max", type=float, default=0.8,
                        help="Maximum adaptive AT bus guard (seconds)")
    parser.add_argument("--mikrotik-at-bus-guard-step", type=float, default=0.1,
                        help="Adaptive AT bus guard increment step (seconds)")
    parser.add_argument("--check", action="store_true",
                        help="Verify config only — read everything, change nothing, exit")
    parser.add_argument("--apply", action="store_true",
                        help="Allow startup to fix missing config (writes to router)")
    parser.add_argument("--live", action="store_true",
                        help="Enable runtime actuation (band changes, levers). "
                             "Without this flag, brain observes only and logs "
                             "'WOULD ACT' for every decision.")
    parser.add_argument(
        "--startup-verify-only",
        action="store_true",
        help="Force startup checklist to run verify-only even in --live mode (safety gate).",
    )
    args = parser.parse_args()

    # Headless modem backend resolution:
    # - Explicit --modem from launcher script -> use it.
    # - No --modem -> fail loudly. NGUI exact-profile selection is a UI hint
    #   only and never headless startup authority.
    if not args.modem:
        log.error(
            "No --modem specified. Headless runner requires an explicit modem target; "
            "NGUI exact-profile selection is a UI hint only."
        )
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        from arch4.runtime_paths import resolve_config_dir, resolve_runtime_output_dir
        from arch4.foundation.external_settings_import import (
            import_foundation_external_settings_once,
        )

        import_foundation_external_settings_once(
            source_root=resolve_config_dir(),
            storage_root=resolve_runtime_output_dir(),
        )
    except Exception as exc:
        # Headless Core remains observable with compiled defaults.  The
        # rejected external input is reported and never treated as authority.
        log.error("External settings import rejected: %s", exc)

    # Safety: --live implies runtime actuation; startup safety gate can still force verify-only.
    observe_only = not args.live
    startup_verify_only_env = str(os.getenv("ARCH6_STARTUP_VERIFY_ONLY", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    startup_verify_only = bool(args.startup_verify_only or startup_verify_only_env)
    if startup_verify_only and args.live:
        log.warning(
            "Startup safety gate active: startup checklist forced to VERIFY ONLY (no auto-apply)."
        )
    startup_apply_authorized = _startup_apply_authorized(
        apply_requested=bool(args.apply),
        startup_verify_only=bool(startup_verify_only),
    )

    config = Arch4Config(
        modem_backend=args.modem,
        router_host=args.host,
        router_user=args.user,
        router_password=args.password,
        sample_interval_s=args.sample_interval_s,
        b818_host=args.huawei_host,
        b818_user=args.huawei_user,
        b818_password=args.huawei_password,
        b818_ping_interface=args.huawei_ping_iface,
        zte_host=args.zte_host,
        tplink_host=args.tplink_host,
        tplink_user=args.tplink_user,
        tplink_password=args.tplink_password,
        tplink_ping_interface=args.tplink_ping_iface,
        zte_user=args.zte_user,
        zte_password=args.zte_password,
        zte_ping_interface=args.zte_ping_iface,
        openwrt_host=args.openwrt_host,
        openwrt_user=args.openwrt_user,
        openwrt_password=args.openwrt_password,
        openwrt_modem_id=args.openwrt_modem_id,
        openwrt_ping_interface=args.openwrt_ping_iface,
        target_guard_model=args.target_guard_model,
        target_guard_serial=args.target_guard_serial,
        target_guard_firmware=args.target_guard_firmware,
        target_guard_imei=args.target_guard_imei,
        model_profile_name=args.model_profile_name,
        model_profile_path=args.model_profile_path,
        signal_sinr_scale=args.signal_sinr_scale,
        signal_rsrp_scale=args.signal_rsrp_scale,
        signal_rsrq_scale=args.signal_rsrq_scale,
        signal_rssi_scale=args.signal_rssi_scale,
        emergency_sinr_floor=args.emergency_sinr_floor,
        router_api_timeout_s=args.router_api_timeout,
        mikrotik_rf_monitor_interval_s=args.mikrotik_rf_monitor_interval,
        mikrotik_qcainfo_interval_s=args.mikrotik_qcainfo_interval,
        mikrotik_qnwcfg_mcs_interval_s=args.mikrotik_qnwcfg_mcs_interval,
        mikrotik_qeng_mcs_interval_s=args.mikrotik_qeng_mcs_interval,
        mikrotik_at_live_polling_enabled=bool(args.mikrotik_at_live_polling),
        mikrotik_runtime_capability_probe_enabled=bool(args.mikrotik_runtime_capability_probe),
        mikrotik_at_read_delay_s=args.mikrotik_at_read_delay,
        mikrotik_at_write_delay_s=args.mikrotik_at_write_delay,
        mikrotik_at_bus_guard_s=args.mikrotik_at_bus_guard,
        mikrotik_action_min_session_uptime_s=args.mikrotik_action_min_session_uptime,
        mikrotik_post_write_blackout_s=args.mikrotik_post_write_blackout,
        mikrotik_at_latency_warn_ms=args.mikrotik_at_latency_warn_ms,
        mikrotik_at_bus_guard_max_s=args.mikrotik_at_bus_guard_max,
        mikrotik_at_bus_guard_step_s=args.mikrotik_at_bus_guard_step,
        signal_contract_warn=(
            True if args.signal_contract_warn
            else (False if args.no_signal_contract_warn else True)
        ),
    )
    config.zyxel_host = args.zyxel_host
    config.zyxel_user = args.zyxel_user
    config.zyxel_password = args.zyxel_password
    config.zyxel_ping_interface = args.zyxel_ping_iface

    runner = CGFRunner(
        config,
        verify_only=not startup_apply_authorized,
        check_only=args.check,
        observe_only=observe_only,
    )
    runner.run()


if __name__ == "__main__":
    main()
