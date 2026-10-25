"""
arch4/startup/checklist.py
═══════════════════════════════════════════════════════════════
ARCH4 v1 — Startup Checklist (Read-Only Pre-Flight)

Startup reads and reports. Every mutation enters the typed Core command
gateway after startup; this checklist is never a modem writer.

Maps to Foundation Doc Chapter 5.
═══════════════════════════════════════════════════════════════
"""

import time
import logging
import inspect
from types import SimpleNamespace
from typing import Any, List, Tuple

from arch4.interfaces import (
    Constraints,
    Arch4Config,
    ARCH_SW_LABEL,
    PROVIDER_PLMN,
    DEPLOYED_BANDS,
)
from arch4.config_views import build_signal_contract_config, build_startup_display_config
from arch4.coerce import safe_bool
from arch4.modem.base import AuthenticationError, TargetGuardError
from arch4.architecture_contracts.runtime_generation import ModemLiveReadyReceipt

log = logging.getLogger("arch4.startup")


def _has_meaningful_ue_capability_data(caps: dict | None) -> bool:
    if not isinstance(caps, dict) or not caps:
        return False
    try:
        category = int(str(caps.get("ue_category", "0") or "0"))
    except Exception:
        category = 0
    dl_256 = str(caps.get("dl_256qam", "") or "").strip().lower()
    ul_64 = str(caps.get("ul_64qam", "") or "").strip().lower()
    return bool(
        category > 0
        or safe_bool(caps.get("pci_lock_available"), False)
        or dl_256 not in {"", "0", "unknown", "not_supported"}
        or ul_64 not in {"", "0", "unknown", "not_supported"}
    )


def _static_evidence_keys(constraints: Constraints) -> set[str]:
    current = getattr(constraints, "_capability_static_evidence_keys", None)
    if isinstance(current, (set, frozenset, list, tuple)):
        return {str(key) for key in current if str(key or "").strip()}
    return set()


def _mark_static_evidence(constraints: Constraints, keys) -> None:
    evidence = _static_evidence_keys(constraints)
    for key in keys:
        text = str(key or "").strip()
        if text:
            evidence.add(text)
    setattr(constraints, "_capability_static_evidence_keys", frozenset(evidence))


class StartupChecklist:
    """
    Pre-flight verification sequence.
    Returns a Constraints object the brain carries at runtime.
    """

    def __init__(self, live_module: Any, config: Arch4Config,
                 verify_only: bool = True,
                 live_modem_reader=None):
        self.cfg = config
        self.startup_cfg = build_startup_display_config(config)
        self.signal_cfg = build_signal_contract_config(config)
        if verify_only is not True:
            raise RuntimeError(
                "startup_apply_mode_illegal_use_core_command_gateway"
            )
        self.verify_only = True
        self.live_modem_reader = live_modem_reader
        if live_module is None:
            raise TypeError("startup_checklist_modem_live_port_required")
        self.live_module = live_module
        self._issues: List[Tuple[str, str]] = []
        self._bootstrap_static_config = {}
        self._bootstrap_ue_capability = {}

    def _require_live_module(self, method: str):
        live_module = self.live_module
        if live_module is None or not hasattr(live_module, method):
            raise RuntimeError(f"Startup requires modem live module method: {method}")
        return live_module

    def _read_band_priority(self) -> List[int]:
        try:
            return [
                int(b)
                for b in list(self._require_live_module("read_band_priority").read_band_priority() or [])
                if int(b or 0) > 0
            ]
        except Exception:
            return []

    def _read_provider_plmn_from_live_module(self) -> str:
        read_plmn = getattr(self.live_module, "read_plmn", None)
        if not callable(read_plmn):
            return ""
        try:
            return str(read_plmn() or "").strip()
        except Exception:
            return ""

    def _run_initial_live_phase(self, constraints: Constraints, *, fast: bool = False):
        """Run the minimum startup work required for first live truth."""
        caps = None
        live_phase_used = False

        host_display = self.startup_cfg.host_display
        log.info("[1/5] Connecting to router %s ...", host_display)
        try:
            live_phase_used = True
            run_live_phase = self._require_live_module("run_startup_live_phase").run_startup_live_phase
            try:
                signature = inspect.signature(run_live_phase)
                supports_fast = "fast" in signature.parameters
            except Exception:
                supports_fast = True
            live_phase_kwargs = {"constraints": constraints}
            if supports_fast:
                live_phase_kwargs["fast"] = fast
            live_phase_result = run_live_phase(**live_phase_kwargs)
            if not isinstance(live_phase_result, ModemLiveReadyReceipt):
                raise TypeError(
                    "startup_modem_live_ready_receipt_required"
                )
            reader = self._require_live_module(
                "committed_live_generation"
            ).committed_live_generation().matrix_reader()
            identity = reader.line_values("identity")
            radio = reader.line_values("radio")
            rf = reader.line_values("rf")
            health = reader.line_values("health")
            connected = bool(
                live_phase_result.ready
                and health.get("radio_connected") is True
            )
            info = dict(identity)
            plmn = str(identity.get("provider_plmn") or "")
            provider = str(identity.get("provider_name") or "")
            modem_input = SimpleNamespace(
                radio_connected=bool(health.get("radio_connected")),
                sinr_db=float(rf.get("sinr_db") or 0.0),
                rsrp_dbm=float(rf.get("rsrp_dbm") or 0.0),
                pci=int(rf.get("pci") or 0),
                primary_band=int(radio.get("serving_band") or 0),
                ca_active=bool(radio.get("ca_active")),
                carrier_count=int(radio.get("carrier_count") or 0),
                ca_band_signature=str(
                    radio.get("ca_signature")
                    or radio.get("ca_band_signature")
                    or ""
                ),
                serving_band_signature=str(
                    radio.get("serving_band_signature") or ""
                ),
            )
            self._bootstrap_static_config = {}
            self._bootstrap_ue_capability = {}
        except AuthenticationError:
            raise
        except TargetGuardError as exc:
            log.error(
                "FATAL: Connected to router but target identity check failed: %s",
                exc.reason,
            )
            raise
        if not connected:
            log.error("FATAL: Cannot connect to router")
            raise ConnectionError(f"Cannot connect to {self.startup_cfg.connect_error_host}")
        log.info("  ✓ Connected")
        self._apply_modem_capability_constraints(constraints, caps)

        log.info("[2/5] Reading modem info ...")
        log.info("  Model:    %s", info.get("model", "unknown"))
        log.info("  IMEI:     %s", info.get("imei", "unknown"))
        log.info("  Firmware: %s", info.get("firmware", "unknown"))

        if plmn:
            constraints.provider_plmn = plmn
            constraints.provider_name = provider
            log.info("  PLMN:     %s (%s)", plmn, provider)
            deployed = list(self.startup_cfg.deployed_bands or sorted(DEPLOYED_BANDS))
            log.info("  Deployed: %s", ", ".join(f"B{b}" for b in sorted(deployed)))
        else:
            if live_phase_used:
                log.info("  PLMN:     deferred until RouterOS AT enrichment is stable")
            else:
                log.warning("  ⚠ Could not read PLMN from SIM")

        log.info("")
        log.info("Reading early live radio state ...")
        if modem_input is not None:
            pass
        elif callable(self.live_modem_reader):
            modem_input = self.live_modem_reader()
        if modem_input and modem_input.radio_connected:
            log.info("  ✓ Radio connected")
            log.info("    SINR = %.1f dB", modem_input.sinr_db)
            log.info("    RSRP = %.1f dBm", modem_input.rsrp_dbm)
            log.info("    PCI  = %d", modem_input.pci)
            log.info("    Band = B%d", modem_input.primary_band)
            if modem_input.ca_active and modem_input.carrier_count <= 1:
                log.info(
                    "    CA   = %s (exact carriers unavailable)",
                    modem_input.ca_band_signature or modem_input.serving_band_signature,
                )
            else:
                log.info("    CA   = %s (%d carriers)",
                         modem_input.ca_band_signature, modem_input.carrier_count)
        else:
            log.warning("  ⚠ Radio not connected or unreadable")

        return constraints, caps, info, modem_input

    def run_fast(self) -> Constraints:
        """Run only the connect/identity/first-live startup phase."""
        constraints = Constraints()
        log.info("=" * 60)
        log.info("%s — Startup Checklist [VERIFY ONLY FAST]", ARCH_SW_LABEL)
        log.info("=" * 60)

        self._run_initial_live_phase(constraints, fast=True)
        return constraints

    def run_deferred_verify(self, constraints: Constraints) -> Constraints:
        """Run slower startup verification after first live truth is visible."""
        log.info("[3/5] Deferred static configuration verification ...")
        constraints = self._check_static(constraints)

        log.info("[4/5] Deferred UE capability verification ...")
        constraints = self._check_ue_capability(constraints)

        current_prio = self._read_band_priority()
        desired_prio = [3, 1, 20]
        if current_prio:
            log.info("Band priority: B%s", " > B".join(str(b) for b in current_prio))
            if current_prio != desired_prio:
                log.warning(
                    "  ⚠ Priority should be B3 > B1 > B20 (currently B%s)",
                    " > B".join(str(b) for b in current_prio),
                )
            else:
                log.info("  ✓ Band priority correct")

        return constraints

    def run(self) -> Constraints:
        """
        Execute startup checklist.
        
        Connect, read everything, report, and never write.
        """
        constraints = Constraints()
        # Capability matrix (refreshed again after modem connect).
        caps = None
        log.info("=" * 60)
        log.info("%s — Startup Checklist [VERIFY ONLY]", ARCH_SW_LABEL)
        log.info("=" * 60)

        constraints, caps, info, modem_input = self._run_initial_live_phase(constraints)

        # Step 3: Check static config (READ ONLY — firewall, scheduler, dns)
        constraints = self.run_deferred_verify(constraints)

        # Capability summary (if available)
        if caps:
            log.info("")
            log.info("CAPABILITIES:")
            log.info("  band_select   = %s", "yes" if caps.supports_band_selection else "no")
            log.info("  cell_lock     = %s", "yes" if caps.supports_cell_lock else "no")
            log.info("  traffic_pres  = %s", "yes" if caps.supports_traffic_presence else "no")
            log.info("  traffic_vol   = %s", "yes" if caps.supports_traffic_volume else "no")
            log.info("  ue_caps       = %s", "yes" if caps.supports_ue_capability else "no")
            log.info("  restart       = %s", "yes" if caps.supports_restart else "no")
            log.info("  neighbors     = %s", "yes" if caps.supports_neighbors else "no")
            log.info("  ca_details    = %s", "yes" if caps.supports_ca_details else "no")
            log.info("  band_priority = %s", "yes" if caps.supports_band_priority else "no")
            log.info("  plmn          = %s", "yes" if caps.supports_plmn else "no")

        log.info("")
        log.info("SIGNAL NORMALIZATION PROFILE:")
        log.info(
            "  scales       = sinr x%.3f, rsrp x%.3f, rsrq x%.3f, rssi x%.3f",
            float(self.signal_cfg.sinr_scale),
            float(self.signal_cfg.rsrp_scale),
            float(self.signal_cfg.rsrq_scale),
            float(self.signal_cfg.rssi_scale),
        )
        log.info(
            "  contract_warn= %s  units=sinr[dB] rsrp[dBm] rsrq[dB] rssi[dBm]",
            "yes" if bool(self.signal_cfg.contract_warn) else "no",
        )

        log.info("")
        log.info("Final radio state snapshot ...")
        if modem_input and modem_input.radio_connected:
            log.info("  ✓ Radio connected")
            log.info("    SINR = %.1f dB", modem_input.sinr_db)
            log.info("    RSRP = %.1f dBm", modem_input.rsrp_dbm)
            log.info("    PCI  = %d", modem_input.pci)
            log.info("    Band = B%d", modem_input.primary_band)
            if modem_input.ca_active and modem_input.carrier_count <= 1:
                log.info("    CA   = %s (exact carriers unavailable)",
                         modem_input.ca_band_signature or modem_input.serving_band_signature)
            else:
                log.info("    CA   = %s (%d carriers)",
                         modem_input.ca_band_signature, modem_input.carrier_count)
        else:
            log.warning("  ⚠ Radio not connected or unreadable")

        # Summary
        log.info("")
        log.info("=" * 60)
        log.info("CONSTRAINT MAP:")
        log.info("-" * 60)
        for field in [
            "ttl_ipv4", "ttl_ipv6", "mss_ipv4", "mss_ipv6",
            "keepalive",
            "dl_256qam", "ul_64qam", "imei_phone",
            "pci_lock_available", "band_select_available",
            "band_lock_available", "band_priority_available", "ue_category",
        ]:
            val = getattr(constraints, field)
            status = "✓" if val else "✗"
            log.info("  %s %-22s = %s", status, field, val)

        if self._issues:
            log.info("")
            log.info("MISSING:")
            for item, status in self._issues:
                log.info("  → %s: %s", item, status)

        log.info("")
        log.info("Mode: VERIFY ONLY — nothing was changed.")

        log.info("=" * 60)
        return constraints

    def _apply_modem_capability_constraints(self, constraints: Constraints, caps) -> None:
        """Copy backend/profile write capability flags into the brain constraint map."""
        if not caps:
            return
        constraints.band_select_available = safe_bool(getattr(caps, "supports_band_selection", False), False)
        constraints.band_lock_available = safe_bool(getattr(caps, "supports_band_lock", False), False)
        constraints.band_priority_available = safe_bool(getattr(caps, "supports_band_priority", False), False)

    def _check_static(self, constraints: Constraints) -> Constraints:
        """Check static configuration. Only READS from router."""
        config_status = dict(self._bootstrap_static_config or {})
        if not config_status:
            config_status = self._require_live_module("read_static_config").read_static_config()

        static_items = [
            ("ttl_ipv4", "TTL hiding IPv4"),
            ("ttl_ipv6", "TTL hiding IPv6"),
            ("mss_ipv4", "MSS clamping IPv4"),
            ("mss_ipv6", "MSS clamping IPv6"),
            ("keepalive", "RRC keepalive"),
        ]
        _mark_static_evidence(
            constraints,
            (key for key, _label in static_items if key in config_status),
        )

        for key, label in static_items:
            present = config_status.get(key, False)
            if present:
                log.info("  ✓ %s", label)
                setattr(constraints, key, True)
            else:
                log.warning("  ✗ %s — MISSING", label)
                setattr(constraints, key, False)
                self._issues.append((key, "Missing"))

        return constraints

    def _check_ue_capability(self, constraints: Constraints) -> Constraints:
        """Check UE capability flags using correct EG120K-EA AT commands."""
        caps = dict(self._bootstrap_ue_capability or {})
        try:
            modem_caps = self._require_live_module("get_modem_capabilities").get_modem_capabilities()
        except Exception:
            modem_caps = None
        if modem_caps and not modem_caps.supports_ue_capability:
            if not caps:
                caps = self._require_live_module("read_ue_capability").read_ue_capability()
            if not _has_meaningful_ue_capability_data(caps):
                constraints.dl_256qam = False
                constraints.ul_64qam = False
                constraints.pci_lock_available = False
                constraints.ue_category = 0
                log.info("  ℹ UE capability read not supported on this modem")
                return constraints
            log.warning(
                "  ⚠ Capability contract mismatch: backend flag says UE info unsupported, "
                "but live UE capability data is available; trusting live data"
            )

        caps = caps or self._require_live_module("read_ue_capability").read_ue_capability()
        _mark_static_evidence(
            constraints,
            (
                key
                for key in (
                    "dl_256qam",
                    "ul_64qam",
                    "pci_lock_available",
                    "ue_category",
                )
                if key in caps or (key == "pci_lock_available" and "pci_lock_status" in caps)
            )
        )

        # 256QAM DL (via AT+QCFG="dl_256qam")
        dl_256 = str(caps.get("dl_256qam", ""))
        if dl_256 == "not_supported":
            constraints.dl_256qam = False
            log.info("  ℹ 256QAM DL — not available on this modem")
        elif dl_256 == "1":
            constraints.dl_256qam = True
            log.info("  ✓ 256QAM DL enabled")
        else:
            constraints.dl_256qam = False
            log.warning("  ✗ 256QAM DL — NOT enabled")
            self._issues.append(("dl_256qam", "Not enabled"))

        # 64QAM UL — Cat 12 implies 64QAM UL support
        ul_64 = str(caps.get("ul_64qam", ""))
        if ul_64 == "cat12_implied":
            constraints.ul_64qam = True
            log.info("  ✓ 64QAM UL (Cat 12 hardware)")
        elif "1" in ul_64:
            constraints.ul_64qam = True
            log.info("  ✓ 64QAM UL enabled")
        else:
            constraints.ul_64qam = False
            log.info("  ℹ 64QAM UL — status unknown")

        # PCI / cell lock availability (via AT+QNWLOCK)
        pci_avail = caps.get("pci_lock_available", False)
        constraints.pci_lock_available = pci_avail
        if pci_avail:
            status = caps.get("pci_lock_status", "")
            locked = ",0" not in status if status else False
            if locked:
                log.info("  ✓ Cell lock available (ACTIVE)")
            else:
                log.info("  ✓ Cell lock available (unlocked)")
        else:
            log.info("  ℹ Cell lock not available")

        # UE category — backend/model specific.
        cat_raw = str(caps.get("ue_category", "0"))
        try:
            constraints.ue_category = int(cat_raw)
        except ValueError:
            constraints.ue_category = 0
        if constraints.ue_category > 0:
            log.info("  ✓ UE Category %d", constraints.ue_category)
        else:
            log.info("  ℹ UE Category unknown")

        # Band priority
        band_prio = caps.get("band_priority", "")
        if band_prio and band_prio != "not_supported":
            log.info("  ℹ Band priority: %s", band_prio)

        return constraints
