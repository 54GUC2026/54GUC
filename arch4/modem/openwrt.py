"""
arch4/modem/openwrt.py
═══════════════════════════════════════════════════════════════
ARCH4 v1 — OpenWrt Modem Adapter (remote rpcd/ModemManager)

Monitor-first telemetry through authenticated OpenWrt HTTP ubus.  The optional
`modemmanager-rpcd` package supplies the ModemManager dump.  No command is
executed on the controller computer.
"""

import logging
import re
import time
from typing import Optional, List, Dict, Any, Mapping

from arch4.modem.base import (
    LiveAcquisitionContract,
    ModemInterface,
    TargetGuardError,
    build_public_modem_status,
    evaluate_target_guard,
)
from arch4.modem.backend_acquisition import (
    BackendAcquisitionReceipt,
    BackendAcquisitionRequest,
    matrix_cells_for_canonical_fields,
)
from arch4.modem.utils import RttProbeResult, measure_host_ping_rtt
from arch4.modem.lock_confirmation import build_lock_confirmation, parse_qnwlock_readback
from arch4.lte_mcs import estimate_qm_from_sinr, representative_cqi_for_qm
from arch4.interfaces import (
    ModemInput, LeverAction, LeverResult, LeverType,
    NeighborCell, CarrierInfo,
)
from arch4.core_contracts.physical_commands import PhysicalCommandDispatchEvidence
from arch4.modem_live.physical_command import (
    ModemLivePhysicalStagePlan,
    ModemLivePhysicalStageResult,
)
from arch4.modem.openwrt_rpcd import OpenWrtRpcdTransport
from arch4.modem.live_lane_contract import modem_live_access_profile_for
from arch4.modem_live.physical_request_authority import (
    ModemLivePhysicalRequestAuthority,
)

log = logging.getLogger("arch4.modem.openwrt")

def _canonical_network_type(value: Any, *, radio_connected: bool, ca_active: bool) -> str:
    text = str(value or "").strip()
    compact = re.sub(r"[^a-z0-9]+", "", text.lower())
    if not radio_connected:
        return "No Service"
    if compact in {"nrsa", "5gsa", "sa"}:
        return "NR SA (5G)"
    if compact in {"nrnsa", "5gnsa", "nsa", "endc", "lte5g", "4g5g"}:
        return "NR NSA (5G)"
    if "nr" in compact or "5g" in compact:
        return "NR NSA (5G)"
    return "LTE (4G)"


def _uci_bool(val) -> bool:
    """Interpret a UCI boolean value (0/1 or string)."""
    if val is None:
        return False
    s = str(val).strip().lower()
    return s in ("1", "true", "yes", "on")


class OpenWrtModemAdapter(ModemInterface):
    """OpenWrt adapter (ModemManager-first)."""

    physical_request_authority_required = True

    def __init__(
        self,
        host: str = "192.168.8.1",
        username: str = "root",
        password: str = "",
        modem_id: Optional[str] = None,
        timeout_s: float = 4.0,
        ping_interface: Optional[str] = None,
        target_guard_model: Optional[str] = None,
        target_guard_serial: Optional[str] = None,
        target_guard_firmware: Optional[str] = None,
        target_guard_imei: Optional[str] = None,
        transport: Optional[OpenWrtRpcdTransport] = None,
    ):
        self._host = str(host or "").strip()
        self._username = str(username or "root")
        self._modem_id = str(modem_id or "").strip() or None
        self._modem_id_explicit = self._modem_id is not None
        self._timeout_s = timeout_s
        self._ping_iface = str(ping_interface or "").strip() or None
        self._target_guard_model = str(target_guard_model or "").strip() or None
        self._target_guard_serial = str(target_guard_serial or "").strip() or None
        self._target_guard_firmware = str(target_guard_firmware or "").strip() or None
        self._target_guard_imei = str(target_guard_imei or "").strip() or None
        self._access_profile = modem_live_access_profile_for(
            backend="openwrt",
            backend_family="openwrt_modemmanager_like",
        )
        self._physical_request_authority = ModemLivePhysicalRequestAuthority(
            self._access_profile
        )
        self._transport = transport or OpenWrtRpcdTransport(
            host=self._host,
            username=self._username,
            password=str(password or ""),
            timeout_s=timeout_s,
            physical_request_authority=self._physical_request_authority,
        )
        if isinstance(self._transport, OpenWrtRpcdTransport):
            self._transport.bind_physical_request_authority(
                self._physical_request_authority
            )
        self._connected = False
        self._prev_rx: Optional[int] = None
        self._prev_tx: Optional[int] = None
        self._prev_ts: Optional[float] = None
        self._net_iface: Optional[str] = None
        self._last_status: Dict[str, Any] = {}
        self._board_cache: Dict[str, Any] = {}
        self._board_cache_ts: float = 0.0
        self._iface_dump_cache: Dict[str, Any] = {}
        self._iface_dump_cache_ts: float = 0.0
        self._wan_cache: Dict[str, Any] = {}
        self._wan_cache_ts: float = 0.0
        self._wan_logical_if: Optional[str] = None
        # Wi-Fi state cache
        self._wifi_cache: Dict[str, Any] = {}
        self._wifi_cache_ts: float = 0.0
        self._modem_cache: Dict[str, Any] = {}
        self._modem_cache_ts: float = 0.0
        self._traffic_rate_cache: tuple[float, float] = (0.0, 0.0)
        self._last_openwrt_acquired_fields: frozenset[str] = frozenset()

    def physical_request_lane_scope(self, lane: str):
        return self._physical_request_authority.lane_scope(lane)

    def physical_request_authority_status(self) -> Dict[str, Any]:
        return self._physical_request_authority.snapshot()

    def connect(self) -> bool:
        if not self._transport.connect():
            log.error("OpenWrt rpcd authentication failed at %s", self._host)
            self._connected = False
            return False
        modem = self._fetch_modemmanager_snapshot(force=True)
        if not modem:
            log.error(
                "OpenWrt modemmanager-rpcd returned no modem at %s",
                self._host,
            )
            self._connected = False
            return False
        self._net_iface = self._detect_net_iface(modem)
        self._connected = True
        # Target guard: verify device identity if expectations are configured
        if self._target_guard_enabled():
            info = self.get_modem_info()
            self._apply_target_guard(info)  # raises TargetGuardError on mismatch
        log.info(
            "Connected to OpenWrt rpcd/ModemManager host=%s modem=%s",
            self._host,
            self._modem_id,
        )
        return True

    def disconnect(self) -> None:
        self._connected = False
        self._transport.close()

    def is_connected(self) -> bool:
        if not self._connected:
            return False
        probe = self._transport.call("system", "board")
        if probe.access_denied:
            self._connected = False
            return False
        if not probe.success:
            raise ConnectionError("openwrt_session_status_unavailable")
        return bool(probe.success)

    def _read_modem_input_impl(self, *, actionable_only: bool) -> Optional[ModemInput]:
        if not self._connected:
            return None

        before_traffic_ts = float(self._prev_ts or 0.0)
        before_board_ts = float(self._board_cache_ts or 0.0)
        before_wan_ts = float(self._wan_cache_ts or 0.0)
        before_wifi_ts = float(self._wifi_cache_ts or 0.0)
        modem_snapshot = self._fetch_modemmanager_snapshot(force=True)
        if not modem_snapshot:
            return None
        base = {"modem": modem_snapshot}
        signal = {"signal": dict(modem_snapshot.get("signal") or {})}
        if not actionable_only and not self._net_iface:
            self._net_iface = self._detect_net_iface(modem_snapshot)

        sinr = self._get_first_float(signal, [
            ("signal", "lte", "snr"),
            ("signal", "lte", "sinr"),
        ], default=0.0)
        rsrp = self._get_first_float(signal, [
            ("signal", "lte", "rsrp"),
        ], default=0.0)
        rsrq = self._get_first_float(signal, [
            ("signal", "lte", "rsrq"),
        ], default=0.0)
        rssi = self._get_first_float(signal, [
            ("signal", "lte", "rssi"),
        ], default=0.0)

        # NR5G signal — confirmed field paths from ModemManager source
        # (mmcli-modem-signal.c: nr5g_rsrp, nr5g_rsrq, nr5g_snr).
        # JSON: {"signal": {"nr5g": {"rsrp": ..., "rsrq": ..., "snr": ...}}}
        nr_rsrp = self._get_first_float(signal, [
            ("signal", "nr5g", "rsrp"),
            ("signal", "5g", "rsrp"),
        ], default=0.0)
        nr_sinr = self._get_first_float(signal, [
            ("signal", "nr5g", "snr"),
            ("signal", "5g", "snr"),
        ], default=0.0)
        nr_rsrq = self._get_first_float(signal, [
            ("signal", "nr5g", "rsrq"),
            ("signal", "5g", "rsrq"),
        ], default=0.0)

        primary_band = int(self._get_first_int(base, [
            ("modem", "3gpp", "lte", "band"),
            ("modem", "3gpp", "lte", "band-number"),
        ], default=0))
        pci = int(self._get_first_int(base, [
            ("modem", "3gpp", "lte", "pci"),
        ], default=0))
        earfcn = int(self._get_first_int(base, [
            ("modem", "3gpp", "lte", "earfcn"),
        ], default=0))
        enb_id = 0

        modem_registered = self._is_radio_connected(base)
        session_uptime_s = 0.0
        ts = time.time()

        rx_bps, tx_bps = self._read_traffic(cached_only=actionable_only)
        modem_root = base.get("modem", {}) or {}
        generic = modem_root.get("generic", {}) or {}
        system = modem_root.get("system", {}) or {}
        board = self._get_board_metadata(cached_only=actionable_only)
        wan_status = self._get_wan_status(cached_only=actionable_only)
        wan_connected = self._is_wan_connected(wan_status)
        radio_connected = modem_registered if wan_connected is None else bool(modem_registered and wan_connected)
        provider_name = str(
            self._get_path(base, ("modem", "3gpp", "operator-name"))
            or generic.get("operator-name")
            or ""
        )
        provider_plmn = str(
            self._get_path(base, ("modem", "3gpp", "operator-code"))
            or generic.get("operator-code")
            or ""
        )
        raw_network_type = str(
            self._get_path(base, ("modem", "access-technologies"))
            or self._get_path(base, ("modem", "generic", "access-technologies"))
            or ""
        )
        firmware = str(
            generic.get("revision")
            or modem_root.get("revision")
            or ""
        )
        mm_plugin = str(system.get("plugin") or "")
        mm_primary_port = str(system.get("primary-port") or "")
        transport = self._infer_transport(base)
        drivers = self._normalize_driver_list(system.get("drivers"))
        model = str(generic.get("model", "") or "")
        manufacturer = str(generic.get("manufacturer", "") or "")
        board_model = str(
            self._get_path(board, ("model",))
            or self._get_path(board, ("board_name",))
            or ""
        )
        distro = str(self._get_path(board, ("release", "distribution")) or "OpenWrt")
        version = str(
            self._get_path(board, ("release", "version"))
            or self._get_path(board, ("release", "revision"))
            or ""
        )
        imei = str(
            modem_root.get("equipment-identifier")
            or generic.get("equipment-identifier")
            or self._get_path(modem_root, ("3gpp", "imei"))
            or ""
        )
        serial = str(
            modem_root.get("serial")
            or generic.get("device-identifier")
            or ""
        )
        tac = int(self._get_first_int(base, [
            ("modem", "3gpp", "lte", "tac"),
            ("modem", "3gpp", "tac"),
        ], default=0))
        wan_ip = str(
            self._get_path(wan_status, ("ipv4-address", 0, "address"))
            or self._get_path(wan_status, ("ipv6-address", 0, "address"))
            or ""
        )
        wan_logical_if = str(self._wan_logical_if or "")

        carriers = []
        if primary_band:
            carriers.append(CarrierInfo(
                band=primary_band,
                earfcn=earfcn,
                pci=pci,
                bandwidth_mhz=20.0,
                sinr_db=sinr,
                rsrp_dbm=rsrp,
                state=1,
                is_pcell=True,
            ))

        ca_active = False
        ca_signature = f"B{primary_band}" if primary_band else ""
        network_type = _canonical_network_type(
            raw_network_type,
            radio_connected=bool(radio_connected),
            ca_active=ca_active,
        )

        # Wi-Fi status (non-critical — failure yields None fields)
        wifi_on, wifi_24, wifi_5 = self._read_wifi_status(cached_only=actionable_only)

        self._last_status = {
            "ts": ts,
            "provider_name": provider_name or None,
            "provider_plmn": provider_plmn or None,
            "provider": provider_name or None,
            "plmn": provider_plmn or None,
            "network_type": network_type,
            "radio_connected": bool(radio_connected),
            "radio_state_text": self._describe_radio_state(
                modem_registered=modem_registered,
                radio_connected=bool(radio_connected),
                wan_connected=wan_connected,
            ),
            "ca_active": False,
            "ca_state_text": "INACTIVE",
            "band": primary_band or None,
            "serving_band": primary_band or None,
            "serving_band_signature": f"B{primary_band}" if primary_band else "",
            "selected_band_set": [],
            "selected_band_set_signature": "",
            "active_band_set": [primary_band] if primary_band else [],
            "active_band_set_signature": f"B{primary_band}" if primary_band else "",
            "band_set": [primary_band] if primary_band else [],
            "band_set_signature": f"B{primary_band}" if primary_band else "",
            "pci": pci or None,
            "earfcn_dl": earfcn or None,
            "earfcn_ul": None,
            "enb_id": enb_id or None,
            "tac": tac or None,
            "carrier_count": 1 if primary_band else 0,
            "mcs_dl0": 0,
            "mcs_dl1": 0,
            "mcs_ul": 0,
            "cqi": 0,
            "cqi0": 0,
            "cqi1": 0,
            "ri": 1,
            "model": model or None,
            "router_model": board_model or manufacturer or None,
            "firmware": firmware or None,
            "software": distro or None,
            "software_version": version or None,
            "serial": serial or None,
            "imei": imei or None,
            "wan_ip": wan_ip or None,
            "wan_connected": wan_connected,
            "packet_session_connected": wan_connected,
            "wan_iface_name": self._net_iface or None,
            "wan_logical_if": wan_logical_if or None,
            "modem_transport": transport or None,
            "modemmanager_plugin": mm_plugin or None,
            "modemmanager_primary_port": mm_primary_port or None,
            "modemmanager_drivers": drivers or None,
            "neighbors": [],
            "session_uptime_s": session_uptime_s,
            "rtt_ms": None,
            "wifi_enabled": wifi_on,
            "wifi_24ghz_enabled": wifi_24,
            "wifi_5ghz_enabled": wifi_5,
        }
        self._last_openwrt_acquired_fields = (
            self._openwrt_fields_proven_by_sources(
                base=base,
                signal=signal,
                board=board,
                wan_status=wan_status,
                traffic_refreshed=float(self._prev_ts or 0.0) > before_traffic_ts,
                board_refreshed=float(self._board_cache_ts or 0.0) > before_board_ts,
                wan_refreshed=float(self._wan_cache_ts or 0.0) > before_wan_ts,
                wifi_refreshed=float(self._wifi_cache_ts or 0.0) > before_wifi_ts,
                wifi_values=(wifi_on, wifi_24, wifi_5),
            )
        )

        # Target guard: verify device identity during runtime reads
        if self._target_guard_enabled():
            try:
                self._apply_target_guard(self._last_status)
            except TargetGuardError:
                return None

        # RSRQ provenance: reported if modem gave it, else estimate or unavailable
        rsrq_source = "reported" if abs(rsrq) > 0.001 else "unavailable"
        if rsrq_source == "unavailable" and abs(rsrp) > 0.001 and abs(rssi) > 0.001:
            import math
            n_rb = 100  # assume 20 MHz (most common LTE bandwidth)
            rsrq = 10.0 * math.log10(n_rb) + rsrp - rssi
            rsrq = max(-19.5, min(-3.0, round(rsrq, 1)))
            rsrq_source = "estimated"

        # Synthetic QAM/CQI path for sparse modem exports.
        qam_dl_primary = estimate_qm_from_sinr(sinr)
        qam_source = "inferred" if qam_dl_primary else "unknown"
        qam_confidence = 0.6 if qam_dl_primary else 0.0
        cqi_val = 0
        cqi_source = "unavailable"
        if qam_dl_primary:
            cqi_val = representative_cqi_for_qm(qam_dl_primary)
            cqi_source = "estimated"

        return ModemInput(
            sinr_db=sinr,
            rsrp_dbm=rsrp,
            rsrq_db=rsrq,
            rssi_dbm=rssi,
            mcs_dl=0,
            mcs_ul=0,
            cqi=cqi_val,
            ri=1,
            carriers=carriers,
            ca_active=ca_active,
            ca_band_signature=ca_signature,
            primary_band=primary_band,
            pci=pci,
            earfcn=earfcn,
            enb_id=enb_id,
            radio_connected=radio_connected,
            packet_session_connected=wan_connected,
            session_uptime_s=session_uptime_s,
            rx_bytes_per_sec=rx_bps,
            tx_bytes_per_sec=tx_bps,
            selected_band_set=[],
            neighbors=[],
            timestamp=ts,
            mod0=qam_dl_primary,
            qam_dl_primary=qam_dl_primary,
            qam_dl_secondary=0,
            qam_ul=0,
            qam_source=qam_source,
            qam_confidence=qam_confidence,
            nr_rsrp_dbm=nr_rsrp,
            nr_sinr_db=nr_sinr,
            nr_rsrq_db=nr_rsrq,
            wifi_enabled=wifi_on,
            wifi_24ghz_enabled=wifi_24,
            wifi_5ghz_enabled=wifi_5,
            rsrq_source=rsrq_source,
            cqi_source=cqi_source,
        )

    def read_modem_input(self) -> Optional[ModemInput]:
        return self._read_modem_input_impl(actionable_only=False)

    def read_actionable_modem_input(self) -> Optional[ModemInput]:
        return self._read_modem_input_impl(actionable_only=True)

    @staticmethod
    def _source_path_observed(
        payload: Dict[str, Any],
        path: tuple[Any, ...],
    ) -> bool:
        current: Any = payload
        for part in path:
            if isinstance(part, int):
                if not isinstance(current, (list, tuple)) or part >= len(current):
                    return False
                current = current[part]
            else:
                if not isinstance(current, dict) or part not in current:
                    return False
                current = current[part]
        if current is None:
            return False
        if isinstance(current, str):
            return bool(current.strip())
        return True

    @classmethod
    def _openwrt_fields_proven_by_sources(
        cls,
        *,
        base: Dict[str, Any],
        signal: Dict[str, Any],
        board: Dict[str, Any],
        wan_status: Dict[str, Any],
        traffic_refreshed: bool,
        board_refreshed: bool,
        wan_refreshed: bool,
        wifi_refreshed: bool,
        wifi_values: tuple[Any, Any, Any],
    ) -> frozenset[str]:
        fields: set[str] = set()
        signal_groups = (
            (("signal", "lte", "snr"), {"sinr_db"}),
            (("signal", "lte", "sinr"), {"sinr_db"}),
            (("signal", "lte", "rsrp"), {"rsrp_dbm"}),
            (("signal", "lte", "rsrq"), {"rsrq_db"}),
            (("signal", "lte", "rssi"), {"rssi_dbm"}),
            (("signal", "nr5g", "rsrp"), {"nr5g_active", "nr5g_rsrp_dbm"}),
            (("signal", "nr5g", "rsrq"), {"nr5g_active", "nr5g_rsrq_db"}),
            (("signal", "nr5g", "snr"), {"nr5g_active", "nr5g_sinr_db"}),
            (("signal", "5g", "rsrp"), {"nr5g_active", "nr5g_rsrp_dbm"}),
            (("signal", "5g", "rsrq"), {"nr5g_active", "nr5g_rsrq_db"}),
            (("signal", "5g", "snr"), {"nr5g_active", "nr5g_sinr_db"}),
        )
        for path, canonical_fields in signal_groups:
            if cls._source_path_observed(signal, path):
                fields.update(canonical_fields)
        base_groups = (
            (("modem", "3gpp", "lte", "band"), {"band", "serving_band", "serving_band_signature", "active_band_set", "active_band_set_signature", "band_set", "band_set_signature", "carrier_count"}),
            (("modem", "3gpp", "lte", "band-number"), {"band", "serving_band", "serving_band_signature", "active_band_set", "active_band_set_signature", "band_set", "band_set_signature", "carrier_count"}),
            (("modem", "3gpp", "lte", "pci"), {"pci"}),
            (("modem", "3gpp", "lte", "earfcn"), {"earfcn", "earfcn_dl"}),
            (("modem", "3gpp", "lte", "tac"), {"tac"}),
            (("modem", "3gpp", "tac"), {"tac"}),
            (("modem", "3gpp", "operator-name"), {"provider_name"}),
            (("modem", "3gpp", "operator-code"), {"provider_plmn"}),
            (("modem", "generic", "operator-name"), {"provider_name"}),
            (("modem", "generic", "operator-code"), {"provider_plmn"}),
            (("modem", "access-technologies"), {"network_type"}),
            (("modem", "generic", "access-technologies"), {"network_type"}),
            (("modem", "generic", "state"), {"radio_connected", "radio_state_text"}),
            (("modem", "generic", "model"), {"model"}),
            (("modem", "generic", "manufacturer"), {"manufacturer"}),
            (("modem", "generic", "revision"), {"firmware"}),
            (("modem", "equipment-identifier"), {"imei"}),
            (("modem", "generic", "equipment-identifier"), {"imei"}),
            (("modem", "3gpp", "imei"), {"imei"}),
            (("modem", "serial"), {"serial"}),
            (("modem", "generic", "device-identifier"), {"serial"}),
        )
        for path, canonical_fields in base_groups:
            if cls._source_path_observed(base, path):
                fields.update(canonical_fields)
        if board_refreshed and board:
            for paths, field in (
                ((("model",), ("board_name",)), "router_model"),
                ((("release", "distribution"),), "software"),
                ((("release", "version"), ("release", "revision")), "software_version"),
            ):
                if any(cls._source_path_observed(board, path) for path in paths):
                    fields.add(field)
        if wan_refreshed and wan_status:
            if any(cls._source_path_observed(wan_status, (key, 0, "address"))
                   for key in ("ipv4-address", "ipv6-address")):
                fields.add("wan_ip")
            if cls._is_wan_connected(wan_status) is not None:
                fields.add("packet_session_connected")
        if traffic_refreshed:
            fields.update({"rx_bytes_per_sec", "tx_bytes_per_sec"})
        if wifi_refreshed:
            for value, field in zip(
                wifi_values,
                (
                    "wifi_enabled",
                    "wifi_24ghz_enabled",
                    "wifi_5ghz_enabled",
                ),
            ):
                if value is not None:
                    fields.add(field)
        return frozenset(fields)

    def acquire_modem_input(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "startup_full":
            raise ValueError(
                "openwrt_startup_full_acquisition_lane_required"
            )
        modem = self.read_modem_input()
        return self._capture_backend_acquisition_receipt(
            request=request,
            modem=modem,
            acquired_cells=(
                matrix_cells_for_canonical_fields(
                    self._last_openwrt_acquired_fields
                )
                if modem is not None
                else frozenset()
            ),
            failure_reason="openwrt_startup_acquisition_unavailable",
        )

    def _capture_openwrt_actionable_receipt(
        self,
        *,
        request: BackendAcquisitionRequest,
        modem: Optional[ModemInput],
    ) -> BackendAcquisitionReceipt:
        acquired_cells = (
            matrix_cells_for_canonical_fields(
                self._last_openwrt_acquired_fields
            )
            if modem is not None
            else frozenset()
        )
        return self._capture_backend_acquisition_receipt(
            request=request,
            modem=modem,
            acquired_cells=acquired_cells,
            failure_reason="openwrt_actionable_acquisition_unavailable",
        )

    def acquire_actionable_modem_input(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "fast_live":
            raise ValueError(
                "openwrt_fast_live_acquisition_lane_required"
            )
        return self._capture_openwrt_actionable_receipt(
            request=request,
            modem=self.read_actionable_modem_input(),
        )

    def acquire_post_write_modem_input(
        self,
        request: BackendAcquisitionRequest,
        stage: PhysicalCommandDispatchEvidence,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "post_write":
            raise ValueError(
                "openwrt_post_write_acquisition_lane_required"
            )
        if not isinstance(stage, PhysicalCommandDispatchEvidence):
            raise TypeError(
                "openwrt_post_write_stage_evidence_required"
            )
        if not stage.permits_target_verification:
            raise ValueError(
                "openwrt_post_write_accepted_stage_required"
            )
        return self._capture_openwrt_actionable_receipt(
            request=request,
            modem=self.read_actionable_modem_input(),
        )

    def refresh_live_maintenance(self) -> None:
        raise RuntimeError("openwrt_maintenance_requires_owned_steps")

    def acquire_live_maintenance(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        raise RuntimeError("openwrt_maintenance_requires_owned_steps")

    def iter_acquire_live_maintenance(self, request: BackendAcquisitionRequest):
        if request.lane != "slow_live":
            raise ValueError("openwrt_slow_live_acquisition_lane_required")
        if not self._connected:
            return
        yield self._capture_maintenance_sources(request, board=self._get_board_metadata(force=True))
        yield self._capture_maintenance_sources(request, wan_status=self._get_wan_status(force=True))
        yield self._capture_maintenance_sources(request, wifi_values=self._read_wifi_status(force=True))

    def _capture_maintenance_sources(
        self, request: BackendAcquisitionRequest, *,
        board: Optional[Dict[str, Any]] = None,
        wan_status: Optional[Dict[str, Any]] = None,
        wifi_values: tuple = (None, None, None),
    ) -> BackendAcquisitionReceipt:
        board, wan_status = board or {}, wan_status or {}
        wifi_on, wifi_24, wifi_5 = wifi_values
        last_status = dict(self._last_status or {})
        if board:
            last_status["router_model"] = str(
                self._get_path(board, ("model",))
                or self._get_path(board, ("board_name",))
                or last_status.get("router_model")
                or ""
            ) or None
            last_status["software"] = str(
                self._get_path(board, ("release", "distribution"))
                or last_status.get("software")
                or "OpenWrt"
            ) or None
            last_status["software_version"] = str(
                self._get_path(board, ("release", "version"))
                or self._get_path(board, ("release", "revision"))
                or last_status.get("software_version")
                or ""
            ) or None
        if wan_status:
            last_status["wan_ip"] = str(
                self._get_path(wan_status, ("ipv4-address", 0, "address"))
                or self._get_path(wan_status, ("ipv6-address", 0, "address"))
                or last_status.get("wan_ip")
                or ""
            ) or None
            last_status["wan_connected"] = self._is_wan_connected(wan_status)
            last_status["packet_session_connected"] = last_status["wan_connected"]
            last_status["wan_logical_if"] = str(self._wan_logical_if or "") or None
            last_status["wan_iface_name"] = self._net_iface or last_status.get("wan_iface_name")
        for field, value in zip(("wifi_enabled", "wifi_24ghz_enabled", "wifi_5ghz_enabled"), wifi_values):
            if value is not None:
                last_status[field] = value
        if last_status:
            last_status["ts"] = time.time()
            self._last_status = last_status

        acquired_fields = self._openwrt_fields_proven_by_sources(
            base={}, signal={}, board=board, wan_status=wan_status,
            traffic_refreshed=False, board_refreshed=True, wan_refreshed=True,
            wifi_refreshed=True, wifi_values=(wifi_on, wifi_24, wifi_5),
        )
        acquired_cells = matrix_cells_for_canonical_fields(
            acquired_fields
        )
        return self._capture_backend_acquisition_receipt(
            request=request,
            modem=None,
            acquired_cells=acquired_cells,
            failure_reason=(
                ""
                if acquired_cells
                else "openwrt_maintenance_acquired_no_matrix_cells"
            ),
        )

    def get_live_acquisition_contract(self) -> LiveAcquisitionContract:
        return LiveAcquisitionContract(
            acquisition_class="aligned_split",
            clocked_register_read=True,
            live_register_interval_s=2.0,
            live_register_stale_s=6.0,
            profile_name="openwrt_modemmanager_status",
            has_explicit_actionable_snapshot=True,
            has_explicit_maintenance_refresh=True,
            maintenance_stepwise=True,
            backend_io_parallel_safe=False,
            io_serial_required=True,
            startup_parallel_allowed=False,
            runtime_action_requires_io_lock=True,
            rtt_holds_io_lock=False,
            rtt_min_interval_s=1.0,
            maintenance_holds_io_lock=False,
            neighbor_reads_hold_io_lock=False,
            fast_read_groups=("modemmanager_signal", "traffic_presence", "traffic_volume", "connection_state"),
            slow_read_groups=("modemmanager_location", "wifi_status", "router_status", "network_mode"),
            static_capability_groups=(
                "device_identity",
                "firmware",
                "supported_bands",
                "antenna_capability",
                "qam_capability",
                "wifi_capability",
            ),
            control_write_groups=("probe_only_no_write",),
            export_groups=("sidecars", "support_bundle", "history"),
            static_capability_refresh_policy="identity_or_operator_refresh_only",
        )

    def read_neighbors(self) -> List[NeighborCell]:
        return []

    def measure_rtt(
        self,
        target: str = "8.8.8.8",
        count: int = 3,
        *,
        timeout_s: float,
    ) -> RttProbeResult:
        return measure_host_ping_rtt(
            target=target,
            count=count,
            ping_interface=self._ping_iface,
            timeout_s=timeout_s,
        )

    def execute_physical_stage(
        self,
        plan: ModemLivePhysicalStagePlan,
    ) -> ModemLivePhysicalStageResult:
        if not isinstance(plan, ModemLivePhysicalStagePlan):
            raise TypeError("openwrt_physical_stage_plan_required")
        del plan
        raise RuntimeError("openwrt_remote_physical_control_unsupported")

    def execute_lever(self, action: LeverAction) -> LeverResult:
        del action
        raise RuntimeError("openwrt_generic_execute_lever_forbidden")

    def _exec_cell_lock(self, action: LeverAction) -> LeverResult:
        del action
        raise RuntimeError(
            "openwrt_direct_cell_lock_write_forbidden"
        )

    def verify_static_config(self) -> Dict[str, bool]:
        return {}

    def apply_static_config(self, item: str) -> bool:
        del item
        raise RuntimeError("openwrt_direct_static_config_write_forbidden")

    def verify_ue_capability(self) -> Dict[str, Any]:
        return {}

    def set_ue_capability(self, key: str, value: Any) -> bool:
        del key, value
        raise RuntimeError("openwrt_direct_ue_capability_write_forbidden")

    def restart_modem(self) -> bool:
        raise RuntimeError("openwrt_direct_restart_write_forbidden")

    def _target_guard_enabled(self) -> bool:
        return bool(
            self._target_guard_model
            or self._target_guard_serial
            or self._target_guard_firmware
            or self._target_guard_imei
        )

    def _apply_target_guard(self, observed: Optional[Dict[str, Any]]) -> bool:
        if not self._target_guard_enabled():
            return True
        ok, reason = evaluate_target_guard(
            expected_model=self._target_guard_model or "",
            expected_serial=self._target_guard_serial or "",
            expected_firmware=self._target_guard_firmware or "",
            expected_imei=self._target_guard_imei or "",
            observed=observed,
        )
        if not ok:
            log.warning("OpenWrt target guard rejected: %s", reason)
            if isinstance(observed, dict):
                observed["target_guard_ok"] = False
                observed["target_guard_reason"] = str(reason or "")
            raise TargetGuardError(
                f"Target guard failed for OpenWrt modem: {reason}",
                reason=str(reason or ""),
            )
        return True

    def read_fresh_device_identity(self) -> Dict[str, str]:
        self._get_board_metadata(force=True)
        return self.get_modem_info()

    def get_modem_info(self) -> Dict[str, str]:
        modem = self._fetch_modemmanager_snapshot(force=True)
        if not modem:
            return {}
        base = {"modem": modem}
        generic = modem.get("generic", {}) or {}
        system = modem.get("system", {}) or {}
        board = self._get_board_metadata()
        return {
            "model": str(generic.get("model", "")),
            "manufacturer": str(generic.get("manufacturer", "")),
            "router_model": str(
                self._get_path(board, ("model",))
                or self._get_path(board, ("board_name",))
                or generic.get("manufacturer", "")
            ),
            "software": str(self._get_path(board, ("release", "distribution")) or "OpenWrt"),
            "software_version": str(
                self._get_path(board, ("release", "version"))
                or self._get_path(board, ("release", "revision"))
                or ""
            ),
            "serial": str(
                modem.get("serial")
                or generic.get("device-identifier")
                or ""
            ),
            "firmware": str(generic.get("revision") or modem.get("revision") or ""),
            "imei": str(
                modem.get("equipment-identifier")
                or generic.get("equipment-identifier")
                or self._get_path(modem, ("3gpp", "imei"))
                or ""
            ),
            "modem_transport": self._infer_transport(base),
            "modemmanager_plugin": str(system.get("plugin", "") or ""),
            "modemmanager_primary_port": str(system.get("primary-port", "") or ""),
        }

    def get_current_bands(self) -> List[int]:
        modem = self._fetch_modemmanager_snapshot(force=True)
        base = {"modem": modem} if modem else {}
        band = self._get_first_int(base, [
            ("modem", "3gpp", "lte", "band"),
            ("modem", "3gpp", "lte", "band-number"),
        ], default=0)
        return [int(band)] if band else []

    def get_capabilities(self):
        from arch4.interfaces import get_capabilities
        return get_capabilities("openwrt")

    def get_last_status(self) -> Dict[str, Any]:
        return build_public_modem_status(self._last_status)

    def scan_neighbors(self) -> List[Dict[str, Any]]:
        return []

    def read_band_priority(self) -> List[int]:
        """Band priority not available on OpenWrt/ModemManager."""
        return []

    def set_band_priority(self, bands: List[int]) -> bool:
        del bands
        raise RuntimeError("openwrt_direct_band_priority_write_forbidden")

    def read_serving_cell(self) -> Dict[str, Any]:
        return {}

    # ─── Wi-Fi status and control ──────────────────────────

    def _read_wifi_status(self, *, force: bool = False, cached_only: bool = False) -> tuple:
        """Read Wi-Fi per-band enable state from OpenWrt.

        Uses `ubus call network.wireless status` which returns per-radio
        status including the `up` field and config details (hwmode/band).

        Returns (wifi_enabled, wifi_24ghz_enabled, wifi_5ghz_enabled).
        All Optional[bool] — None if Wi-Fi is unavailable.
        """
        ws = self._get_wifi_status(force=force, cached_only=cached_only)
        if not ws:
            return None, None, None

        wifi_24: Optional[bool] = None
        wifi_5: Optional[bool] = None

        for radio_name, radio_data in ws.items():
            if not isinstance(radio_data, dict):
                continue
            band = self._classify_openwrt_radio_band(radio_name, radio_data)
            if band is None:
                continue
            up = bool(radio_data.get("up", False))
            # Also check UCI disabled flag — radio can be "up" in
            # netifd but marked disabled in config.
            config = radio_data.get("config", {}) or {}
            disabled = config.get("disabled")
            if disabled is not None:
                # UCI: disabled=1 means off, disabled=0 means on
                enabled = not _uci_bool(disabled)
            else:
                enabled = up
            if band == "2.4":
                wifi_24 = enabled
            elif band == "5":
                wifi_5 = enabled

        if wifi_24 is None and wifi_5 is None:
            return None, None, None

        overall = bool(wifi_24 or wifi_5)
        return overall, wifi_24, wifi_5

    @staticmethod
    def _classify_openwrt_radio_band(radio_name: str, radio_data: dict) -> Optional[str]:
        """Classify an OpenWrt radio as '2.4' or '5' GHz.

        Heuristics (checked in order):
          1. config.band field: '2g' or '5g'
          2. config.hwmode: '11g'/'11b' → 2.4, '11a' → 5
          3. config.channel: 1-14 → 2.4, 36+ → 5
          4. Radio name containing '0' → 2.4, '1' → 5 (OpenWrt convention)
        """
        config = radio_data.get("config", {}) or {}

        # 1. Explicit band field (OpenWrt 22.03+)
        band = str(config.get("band", "")).lower()
        if band in ("2g", "2.4g", "2.4ghz"):
            return "2.4"
        if band in ("5g", "5ghz"):
            return "5"

        # 2. hwmode
        hwmode = str(config.get("hwmode", "")).lower()
        if hwmode in ("11g", "11b", "11ng"):
            return "2.4"
        if hwmode in ("11a", "11na", "11ac", "11ax"):
            return "5"

        # 3. Channel number
        channel = config.get("channel")
        if channel is not None and str(channel).isdigit():
            ch = int(channel)
            if 1 <= ch <= 14:
                return "2.4"
            if ch >= 36:
                return "5"

        # 4. Radio naming convention (radio0 = 2.4, radio1 = 5)
        name = str(radio_name).lower()
        if name == "radio0":
            return "2.4"
        if name == "radio1":
            return "5"

        return None

    def wifi_set_radio(self, radio: str, enabled: bool) -> bool:
        del radio, enabled
        raise RuntimeError("openwrt_direct_wifi_write_forbidden")

    def _get_wifi_status(self, force: bool = False, cached_only: bool = False) -> Dict[str, Any]:
        """Read Wi-Fi status from remote ubus, cached for 5s."""
        now = time.time()
        if self._wifi_cache and (cached_only or (not force and (now - self._wifi_cache_ts) < 5.0)):
            return self._wifi_cache
        if cached_only:
            return self._wifi_cache
        data = self._rpc_data("network.wireless", "status")
        if data:
            self._wifi_cache = data
            self._wifi_cache_ts = now
            return data
        self._wifi_cache = {}
        self._wifi_cache_ts = now
        return {}

    # ─── Internal helpers ───────────────────────────────────

    def _rpc_data(
        self,
        object_name: str,
        method: str,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        result = self._transport.call(object_name, method, params or {})
        if not result.success:
            if result.code in {-1, 6}:
                self._connected = False
            log.debug(
                "OpenWrt rpcd call %s.%s unavailable: %s",
                object_name,
                method,
                result.reason or result.code,
            )
            return {}
        return dict(result.data) if isinstance(result.data, Mapping) else {}

    @staticmethod
    def _modem_record_id(record: Mapping[str, Any], index: int) -> str:
        candidates = (
            record.get("id"),
            record.get("index"),
            record.get("path"),
            (record.get("generic") or {}).get("dbus-path")
            if isinstance(record.get("generic"), Mapping)
            else None,
        )
        for candidate in candidates:
            text = str(candidate or "").strip()
            match = re.search(r"/Modem/(\d+)$", text)
            if match:
                return match.group(1)
            if text.isdigit():
                return text
        return str(index)

    def _fetch_modemmanager_snapshot(self, *, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        if self._modem_cache and not force and (now - self._modem_cache_ts) < 1.0:
            return dict(self._modem_cache)
        payload = self._rpc_data("modemmanager", "dump")
        records = payload.get("modem", []) if isinstance(payload, dict) else []
        if isinstance(records, Mapping):
            records = [records]
        if not isinstance(records, list):
            records = []
        selected: Dict[str, Any] = {}
        selected_id = ""
        for index, record in enumerate(records):
            if not isinstance(record, Mapping):
                continue
            record_id = self._modem_record_id(record, index)
            if self._modem_id is None or record_id == self._modem_id:
                selected = dict(record)
                selected_id = record_id
                break
        if not selected:
            self._modem_cache = {}
            self._modem_cache_ts = now
            return {}
        if not self._modem_id_explicit:
            if self._modem_id is not None and self._modem_id != selected_id:
                self._net_iface = None
                self._wan_logical_if = None
            self._modem_id = selected_id
        self._modem_cache = selected
        self._modem_cache_ts = now
        return dict(selected)

    def _detect_net_iface(
        self,
        modem_snapshot: Optional[Mapping[str, Any]] = None,
    ) -> Optional[str]:
        modem = dict(modem_snapshot or self._fetch_modemmanager_snapshot())
        generic = modem.get("generic", {}) or {}
        ports = modem.get("ports") or generic.get("ports") or []
        for p in ports:
            if isinstance(p, dict) and p.get("type") == "net":
                name = str(p.get("name") or "").strip()
                if name:
                    return name
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                if str(p[1] or "").strip().lower() == "net":
                    name = str(p[0] or "").strip()
                    if name:
                        return name
        dump = self._get_interface_dump()
        interfaces = dump.get("interface", []) if isinstance(dump, dict) else []
        for entry in interfaces if isinstance(interfaces, list) else []:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("proto") or "").strip().lower() != "modemmanager":
                continue
            name = str(entry.get("l3_device") or entry.get("device") or "").strip()
            if name:
                return name
        return None

    def _read_traffic(self, *, cached_only: bool = False) -> tuple:
        if cached_only:
            return self._traffic_rate_cache
        if not self._net_iface:
            self._net_iface = self._detect_net_iface()
        if not self._net_iface:
            return 0.0, 0.0
        status = self._rpc_data(
            "network.device",
            "status",
            {"name": self._net_iface},
        )
        statistics = status.get("statistics", {}) if isinstance(status, dict) else {}
        try:
            rx = int(
                statistics.get("rx_bytes")
                if isinstance(statistics, Mapping)
                else status.get("rx_bytes")
            )
            tx = int(
                statistics.get("tx_bytes")
                if isinstance(statistics, Mapping)
                else status.get("tx_bytes")
            )
        except (TypeError, ValueError):
            return 0.0, 0.0
        now = time.time()
        if self._prev_rx is None or self._prev_tx is None or self._prev_ts is None:
            self._prev_rx, self._prev_tx, self._prev_ts = rx, tx, now
            self._traffic_rate_cache = (0.0, 0.0)
            return 0.0, 0.0
        dt = max(1e-3, now - self._prev_ts)
        rx_bps = max(0.0, (rx - self._prev_rx) / dt)
        tx_bps = max(0.0, (tx - self._prev_tx) / dt)
        self._prev_rx, self._prev_tx, self._prev_ts = rx, tx, now
        self._traffic_rate_cache = (rx_bps, tx_bps)
        return self._traffic_rate_cache

    def _get_board_metadata(self, *, force: bool = False, cached_only: bool = False) -> Dict[str, Any]:
        now = time.time()
        if self._board_cache and (cached_only or (not force and (now - self._board_cache_ts) < 60.0)):
            return self._board_cache
        if cached_only:
            return self._board_cache
        data = self._rpc_data("system", "board")
        if data:
            self._board_cache = data
            self._board_cache_ts = now
            return data
        self._board_cache = {}
        self._board_cache_ts = now
        return {}

    def _get_wan_status(self, *, force: bool = False, cached_only: bool = False) -> Dict[str, Any]:
        now = time.time()
        if self._wan_cache and (cached_only or (not force and (now - self._wan_cache_ts) < 15.0)):
            return self._wan_cache
        if cached_only:
            return self._wan_cache
        logical_if = self._resolve_wan_logical_interface(force=force)
        if logical_if:
            data = self._rpc_data(
                f"network.interface.{logical_if}",
                "status",
            )
            if data:
                self._wan_logical_if = str(logical_if)
                self._wan_cache = data
                self._wan_cache_ts = now
                return data
        self._wan_cache = {}
        self._wan_cache_ts = now
        return {}

    def _get_interface_dump(self, *, force: bool = False, cached_only: bool = False) -> Dict[str, Any]:
        now = time.time()
        if self._iface_dump_cache and (cached_only or (not force and (now - self._iface_dump_cache_ts) < 15.0)):
            return self._iface_dump_cache
        if cached_only:
            return self._iface_dump_cache
        data = self._rpc_data("network.interface", "dump")
        if data:
            self._iface_dump_cache = data
            self._iface_dump_cache_ts = now
            return data
        self._iface_dump_cache = {}
        self._iface_dump_cache_ts = now
        return {}

    def _resolve_wan_logical_interface(self, *, force: bool = False) -> Optional[str]:
        if self._wan_logical_if and not force:
            return self._wan_logical_if
        dump = self._get_interface_dump(force=force)
        interfaces = dump.get("interface", []) if isinstance(dump, dict) else []
        if not isinstance(interfaces, list):
            interfaces = []
        target_net = str(self._net_iface or "").strip()
        proto_candidate: Optional[str] = None
        device_candidate: Optional[str] = None
        for entry in interfaces:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("interface") or "").strip()
            proto = str(entry.get("proto") or "").strip().lower()
            l3_device = str(entry.get("l3_device") or "").strip()
            device = str(entry.get("device") or "").strip()
            if proto == "modemmanager" and not proto_candidate and name:
                proto_candidate = name
            if target_net and name and target_net in {l3_device, device}:
                self._wan_logical_if = name
                return name
            if not device_candidate and name and (l3_device or device):
                if target_net and target_net in {l3_device, device}:
                    device_candidate = name
        self._wan_logical_if = proto_candidate or device_candidate or "wan"
        return self._wan_logical_if

    @staticmethod
    def _is_wan_connected(wan_status: Dict[str, Any]) -> Optional[bool]:
        if not wan_status:
            return None
        if any(wan_status.get(key) for key in ("ipv4-address", "ipv6-address")):
            return True
        if "up" in wan_status:
            return bool(wan_status.get("up"))
        if "pending" in wan_status:
            return not bool(wan_status.get("pending"))
        return None

    def _is_radio_connected(self, base: Dict[str, Any]) -> bool:
        modem = base.get("modem", {}) or {}
        generic = modem.get("generic", {}) or {}
        state = str(modem.get("state") or generic.get("state") or "").lower()
        return any(s in state for s in ("connected", "registered"))

    @staticmethod
    def _normalize_driver_list(value: Any) -> str:
        if isinstance(value, list):
            return ", ".join(str(v).strip() for v in value if str(v).strip())
        return str(value or "").strip()

    def _infer_transport(self, base: Dict[str, Any]) -> str:
        system = (base.get("modem", {}) or {}).get("system", {}) or {}
        primary_port = str(system.get("primary-port") or "").strip().lower()
        drivers = self._normalize_driver_list(system.get("drivers")).lower()
        ports = (base.get("modem", {}) or {}).get("ports", []) or []
        port_types = {
            str((p or {}).get("type", "")).strip().lower()
            for p in ports
            if isinstance(p, dict)
        }
        port_names = " ".join(
            str((p or {}).get("name", "")).strip().lower()
            for p in ports
            if isinstance(p, dict)
        )
        if "qmi" in drivers or "qmi_wwan" in drivers or "qmi" in port_types:
            return "qmi"
        if "mbim" in drivers or "mbim" in port_types:
            return "mbim"
        if primary_port.startswith("cdc-wdm") or "cdc-wdm" in port_names:
            return "mbim_or_qmi"
        if primary_port.startswith("ttyusb") or any(isinstance(p, dict) and p.get("type") == "at" for p in ports):
            return "at"
        return ""

    @staticmethod
    def _describe_radio_state(*, modem_registered: bool, radio_connected: bool, wan_connected: Optional[bool]) -> str:
        if radio_connected:
            return "Connected"
        if modem_registered and wan_connected is False:
            return "Disconnected"
        return "Disconnected"

    def _get_first_float(self, data: Dict[str, Any], paths: List[tuple], default: float) -> float:
        for path in paths:
            val = self._get_path(data, path)
            if val is None:
                continue
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
        return default

    def _get_first_int(self, data: Dict[str, Any], paths: List[tuple], default: int) -> int:
        for path in paths:
            val = self._get_path(data, path)
            if val is None:
                continue
            try:
                return int(float(val))
            except (TypeError, ValueError):
                continue
        return default

    def _get_path(self, data: Dict[str, Any], path: tuple) -> Any:
        cur: Any = data
        for key in path:
            if isinstance(cur, dict):
                cur = cur.get(key)
                continue
            if isinstance(cur, list) and isinstance(key, int):
                if 0 <= key < len(cur):
                    cur = cur[key]
                    continue
                return None
            return None
        return cur
