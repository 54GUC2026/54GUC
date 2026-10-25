"""
arch4/modem/zyxel_webui.py
═══════════════════════════════════════════════════════════════
Zyxel NR5101 / NR7101 / FWA510 5G Router — Web Configurator Backend

Communicates with Zyxel 5G routers via their HTTP/JSON DAL API.
Stock firmware is OpenWrt-based; the web UI uses CGI-based DAL endpoints.

Endpoints and field names were checked against public API examples and real
NR7101 hardware output.

Modem module: Quectel RG502Q-EA (USB-attached to MT7621 SoC).
Default management IP: 192.168.1.1 (HTTPS, self-signed cert).

Confirmed DAL OIDs:
  cellwan_status  — full cellular signal, cell identity, NSA state
  Traffic_Status  — per-interface traffic counters (br0, wwan0)

Confirmed field names:
  INTF_RSRP, INTF_RSRQ, INTF_SINR, INTF_RSSI, INTF_CQI, INTF_MCS, INTF_RI
  INTF_PhyCell_ID, INTF_RFCN, INTF_Cell_ID, INTF_TAC
  INTF_Current_Band, INTF_Current_Access_Technology, INTF_Status
  NSA_PCI, NSA_RFCN, NSA_Band, NSA_RSRP, NSA_RSRQ, NSA_SINR
  NSA_MCC, NSA_MNC, NSA_Enable, SCC_Info
"""

import base64
import json
import logging
import re
import time
from typing import Optional, List, Dict, Any
from urllib.parse import urljoin

import requests

from arch4.modem_live.startup_trace import traced_http

from arch4.modem.base import (
    AuthenticationError,
    LiveAcquisitionContract,
    ModemInterface,
    build_public_modem_status,
    evaluate_target_guard,
)
from arch4.modem.backend_acquisition import (
    BackendAcquisitionReceipt,
    BackendAcquisitionRequest,
    DECLARED_CANONICAL_SOURCE_FIELDS,
    matrix_cells_for_canonical_fields,
)
from arch4.modem.utils import RttProbeResult, measure_host_ping_rtt
from arch4.interfaces import (
    ModemInput, LeverAction, LeverResult, LeverType,
    NeighborCell, CarrierInfo,
)
from arch4.core_contracts.command_targets import CoreRestartTarget
from arch4.core_contracts.physical_commands import (
    PhysicalCommandDispatchEvidence,
)
from arch4.modem_live.physical_command import (
    ModemLivePhysicalStagePlan,
    ModemLivePhysicalStageResult,
)
from arch4.modem.live_lane_contract import modem_live_access_profile_for
from arch4.modem_live.physical_request_authority import (
    ModemLivePhysicalRequestAuthority,
)

log = logging.getLogger("arch4.modem.zyxel")

def _band_name_to_number(band_str: str) -> int:
    """Convert Zyxel band string like 'LTE_BC28' or 'n78' to integer."""
    s = str(band_str or "").strip().upper()
    if s.startswith("LTE_BC"):
        try:
            return int(s[6:])
        except ValueError:
            pass
    if s.startswith("LTE_B"):
        try:
            return int(s[5:])
        except ValueError:
            pass
    if s.startswith("N"):
        try:
            return int(s[1:])
        except ValueError:
            pass
    try:
        return int(s)
    except ValueError:
        return 0


def _safe_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_int(val, default: int = 0) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


class ZyxelWebuiModem(ModemInterface):
    """Zyxel NR5101/NR7101/FWA510 DAL API backend.

    Uses the confirmed /cgi-bin/DAL?oid= endpoints with JSON responses.
    Login requires base64-encoded password posted to /UserLogin.
    Session maintained via cookies. Reboot requires sessionkey from login.
    """

    _CELL_LOCK_MODELS = {"NR5101", "NR7101", "NR7102", "FWA710"}
    _NO_CELL_LOCK_MODELS = {"NR5101-NEBULA", "FWA505", "FWA510"}
    physical_request_authority_required = True

    def __init__(
        self,
        host: str = "192.168.1.1",
        username: str = "admin",
        password: str = "",
        ping_interface: Optional[str] = None,
        backend_name: str = "nr5101",
        expected_model: str = "",
        expected_serial: str = "",
        expected_firmware: str = "",
        expected_imei: str = "",
    ):
        self._host = str(host or "192.168.1.1").strip()
        self._username = str(username or "admin").strip()
        self._password = str(password or "").strip()
        self._ping_iface = str(ping_interface or "").strip() or None
        self._backend_name = str(backend_name or "nr5101").strip().lower()
        self._access_profile = modem_live_access_profile_for(
            backend=self._backend_name,
            backend_family="zyxel_webui_like",
        )
        self._physical_request_authority = ModemLivePhysicalRequestAuthority(
            self._access_profile
        )
        self._expected_model = str(expected_model or "").strip()
        # Zyxel hardware does not expose a serial number.
        # Zyxel does not provide a serial-number comparison surface; the
        # shared factory argument is intentionally ignored at this boundary.
        self._expected_firmware = str(expected_firmware or "").strip()
        self._expected_imei = str(expected_imei or "").strip()

        # Use HTTPS by default (Zyxel uses self-signed certs)
        self._base_url = f"https://{self._host}"
        self._session = None  # requests.Session
        self._connected = False
        self._sessionkey: Optional[str] = None
        self._last_status: Dict[str, Any] = {}
        self._model_detected: str = ""
        self._firmware_detected: str = ""
        self._imei_detected: str = ""
        self._cell_lock_capable: bool = False
        self._nr_capable: bool = True
        self._prev_rx: Optional[int] = None
        self._prev_tx: Optional[int] = None
        self._prev_ts: Optional[float] = None
        self._last_traffic_status: Dict[str, Any] = {}
        self._zyxel_api_timing: Dict[str, Dict[str, Any]] = {}
        self._last_zyxel_cellular_fields: frozenset[str] = frozenset()

    def physical_request_lane_scope(self, lane: str):
        return self._physical_request_authority.lane_scope(lane)

    def physical_request_authority_status(self) -> Dict[str, Any]:
        return self._physical_request_authority.snapshot()

    def _zyxel_http_get(
        self,
        url: str,
        *,
        operation: str,
        timeout: float,
        lane: str = "slow_live",
        **kwargs: Any,
    ):
        if self._session is None:
            raise RuntimeError("zyxel_http_session_unavailable")
        physical_lane = self._physical_request_authority.current_lane(
            default=lane
        )
        with self._physical_request_authority.request(
            lane=physical_lane,
            operation=f"zyxel:http:get:{operation}",
        ):
            return traced_http(self._session.get,
                url,
                timeout=timeout,
                **kwargs,
            )

    def _zyxel_http_post(
        self,
        url: str,
        *,
        operation: str,
        timeout: float,
        lane: str = "event_write",
        **kwargs: Any,
    ):
        if self._session is None:
            raise RuntimeError("zyxel_http_session_unavailable")
        physical_lane = self._physical_request_authority.current_lane(
            default=lane
        )
        with self._physical_request_authority.request(
            lane=physical_lane,
            operation=f"zyxel:http:post:{operation}",
        ):
            return traced_http(self._session.post,
                url,
                timeout=timeout,
                **kwargs,
            )

    # ── Connection lifecycle ─────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            import urllib3
            urllib3.disable_warnings()
            self._session = requests.Session()
            self._session.verify = False  # self-signed certs
            self._session.headers.update({
                "User-Agent": "LUC/6.6 ZyxelBackend",
            })
        except ImportError:
            log.error("'requests' library not available")
            return False

        # Test basic connectivity.
        try:
            r = self._zyxel_http_get(
                f"{self._base_url}/getBasicInformation",
                operation="basic_information",
                lane="startup",
                timeout=8,
            )
            if r.status_code != 200:
                log.error("Zyxel not reachable at %s (HTTP %d)", self._base_url, r.status_code)
                return False
        except requests.RequestException as exc:
            log.error("Zyxel connect failed: %s", exc)
            return False
        try:
            data = r.json()
        except requests.exceptions.JSONDecodeError as exc:
            log.error("Zyxel basic information returned invalid JSON: %s", exc)
            return False
        if data.get("result") != "ZCFG_SUCCESS":
            log.warning("Zyxel getBasicInformation: %s", data.get("result"))

        # Login
        if not self._login():
            raise AuthenticationError(
                f"Authentication failed for Zyxel at {self._base_url} — check password"
            )

        # Read initial status to detect model
        cellular = self._dal_get("cellwan_status")
        if cellular:
            self._imei_detected = str(cellular.get("INTF_IMEI", "") or "")
            self._firmware_detected = str(
                cellular.get("INTF_Module_Software_Version", "") or ""
            )
            # Model detection from supported bands pattern or backend name
            self._model_detected = self._backend_name.upper()
            self._cell_lock_capable = (
                self._model_detected in self._CELL_LOCK_MODELS
            )
            log.info(
                "Zyxel connected: backend=%s imei=%s module_fw=%s",
                self._backend_name, self._imei_detected,
                self._firmware_detected,
            )

        self._connected = True
        return True

    def disconnect(self) -> None:
        if self._session and self._sessionkey:
            try:
                self._zyxel_http_get(
                    f"{self._base_url}/cgi-bin/UserLogout"
                    f"?sessionkey={self._sessionkey}",
                    operation="logout",
                    lane="disconnect",
                    timeout=5,
                )
            except Exception as exc:
                log.debug("disconnect: suppressed %s", exc)
                pass
        self._connected = False
        self._sessionkey = None
        if self._session:
            try:
                self._session.close()
            except Exception as exc:
                log.debug("disconnect: suppressed %s", exc)
                pass
            self._session = None

    def is_connected(self) -> bool:
        return self._connected

    # ── Zyxel DAL API ────────────────────────────────────────────────────────

    def _login(self) -> bool:
        """POST /UserLogin with base64-encoded password."""
        if not self._session:
            return False
        password_b64 = base64.b64encode(
            self._password.encode("utf-8")
        ).decode("utf-8")
        payload = {
            "Input_Account": self._username,
            "Input_Passwd": password_b64,
            "currLang": "en",
            "RememberPassword": 0,
            "SHA512_password": False,
        }
        try:
            r = self._zyxel_http_post(
                f"{self._base_url}/UserLogin",
                operation="login",
                lane="session",
                data=json.dumps(payload),
                timeout=10,
            )
            if r.status_code == 200:
                try:
                    data = r.json()
                except requests.exceptions.JSONDecodeError as exc:
                    log.error("Zyxel login returned invalid JSON: %s", exc)
                    return False
                self._sessionkey = data.get("sessionkey")
                # Update session cookies from response
                self._session.cookies.update(r.cookies)
                if self._sessionkey:
                    log.info("Zyxel login OK (sessionkey acquired)")
                    return True
                log.warning("Zyxel login: no sessionkey in response")
                return bool(data.get("result") == "ZCFG_SUCCESS")
            log.warning("Zyxel login failed: HTTP %d", r.status_code)
            return False
        except requests.RequestException as exc:
            log.error("Zyxel login error: %s", exc)
            return False

    def _dal_get(self, oid: str) -> Optional[Dict[str, Any]]:
        """GET /cgi-bin/DAL?oid=<oid> → Object[0] dict."""
        if not self._session:
            return None
        try:
            r = self._zyxel_http_get(
                f"{self._base_url}/cgi-bin/DAL?oid={oid}",
                operation=f"dal:{oid}",
                timeout=8,
            )
            if r.status_code == 401:
                self._connected = False
                self._sessionkey = ""
                log.info("Zyxel session expired; Connectivity repair required")
                return None
            if r.status_code == 500:
                log.warning("Zyxel DAL %s: 500 Server Error (stale session?)", oid)
                return None
            r.raise_for_status()
        except requests.RequestException as exc:
            log.debug("Zyxel DAL %s transport error: %s", oid, exc)
            return None
        try:
            data = r.json()
        except requests.exceptions.JSONDecodeError as exc:
            log.debug("Zyxel DAL %s invalid JSON: %s", oid, exc)
            return None
        if data.get("result") != "ZCFG_SUCCESS":
            log.debug("Zyxel DAL %s: result=%s", oid, data.get("result"))
            return None
        objects = data.get("Object")
        if isinstance(objects, list) and objects:
            return objects[0]
        return data

    def _record_zyxel_api_timing(
        self,
        lane_key: str,
        *,
        oid: str,
        elapsed_s: float,
        ok: bool,
    ) -> None:
        elapsed_ms = round(max(0.0, float(elapsed_s or 0.0)) * 1000.0, 1)
        self._zyxel_api_timing[str(lane_key or "unknown")] = {
            "oid": str(oid or ""),
            "elapsed_ms": elapsed_ms,
            "last_ok": bool(ok),
        }

    def _inject_zyxel_lane_debug_status(self, status: Dict[str, Any], *, lane: str) -> None:
        status["zyxel_fast_read"] = {
            "lane": "protected_fast_read",
            "groups": ["cellwan_status"],
            "endpoint_count": 1,
            "active_lane": str(lane or ""),
            "uses_cached_traffic": True,
        }
        status["zyxel_api_timing"] = {
            key: dict(value) for key, value in self._zyxel_api_timing.items()
        }

    def _read_dal_oid_for_lane(self, oid: str, *, lane_key: str) -> Optional[Dict[str, Any]]:
        started = time.perf_counter()
        out = self._dal_get(oid)
        self._record_zyxel_api_timing(
            lane_key,
            oid=oid,
            elapsed_s=time.perf_counter() - started,
            ok=isinstance(out, dict) and bool(out),
        )
        return out

    def _read_cellular_status(self, *, lane_key: str) -> Optional[Dict[str, Any]]:
        return self._read_dal_oid_for_lane("cellwan_status", lane_key=lane_key)

    def _parse_traffic_status(self, traffic: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if not isinstance(traffic, dict) or not traffic:
            return out
        wwan_ifaces = traffic.get("ipIfaceSt") or []
        iface_names = traffic.get("ipIface") or []
        for iface_meta, iface_st in zip(iface_names, wwan_ifaces):
            if not isinstance(iface_meta, dict) or not isinstance(iface_st, dict):
                continue
            ifname = str(iface_meta.get("X_ZYXEL_IfName", "") or "")
            if not ifname.startswith("wwan"):
                continue
            raw_counters = (iface_st.get("BytesReceived"), iface_st.get("BytesSent"))
            if not all(type(value) is int or (
                isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip())
            ) for value in raw_counters):
                return {}
            rx, tx = (int(value) for value in raw_counters)
            if rx < 0 or tx < 0:
                return {}
            now = time.time()
            rx_bps, tx_bps = 0.0, 0.0
            if self._prev_rx is not None and self._prev_tx is not None and self._prev_ts is not None:
                dt = max(0.1, now - self._prev_ts)
                rx_bps = max(0.0, (rx - self._prev_rx)) / dt
                tx_bps = max(0.0, (tx - self._prev_tx)) / dt
            self._prev_rx = rx
            self._prev_tx = tx
            self._prev_ts = now
            out = {
                "traffic_current_download_rate_bps": rx_bps,
                "traffic_current_upload_rate_bps": tx_bps,
                "traffic_total_download_bytes": rx,
                "traffic_total_upload_bytes": tx,
                "wan_iface_name": ifname,
                "traffic_rate_source": "traffic_status",
                "traffic_presence_active": True,
            }
            self._last_traffic_status = dict(out)
            return out
        return out

    def _read_traffic_status(self, *, lane_key: str) -> Dict[str, Any]:
        traffic = self._read_dal_oid_for_lane("Traffic_Status", lane_key=lane_key)
        return self._parse_traffic_status(traffic)

    def _build_modem_input_from_cellular(
        self,
        cellular: Dict[str, Any],
        *,
        lane_key: str,
        live_traffic: Optional[Dict[str, Any]] = None,
    ) -> ModemInput:
        traffic_state = dict(self._last_traffic_status)
        if isinstance(live_traffic, dict) and live_traffic:
            traffic_state.update(live_traffic)

        ts = time.time()

        # LTE signal values use the confirmed DAL field names.
        sinr = _safe_float(cellular.get("INTF_SINR"), 0.0)
        rsrp = _safe_float(cellular.get("INTF_RSRP"), 0.0)
        rsrq = _safe_float(cellular.get("INTF_RSRQ"), 0.0)
        rssi = _safe_float(cellular.get("INTF_RSSI"), 0.0)

        # Scheduler data — Zyxel actually exposes CQI, MCS, RI!
        cqi = _safe_int(cellular.get("INTF_CQI"), 0)
        mcs = _safe_int(cellular.get("INTF_MCS"), 0)
        ri = _safe_int(cellular.get("INTF_RI"), 0)
        pmi = _safe_int(cellular.get("INTF_PMI"), 0)

        # Cell identity
        pci = _safe_int(cellular.get("INTF_PhyCell_ID"), 0)
        earfcn = _safe_int(cellular.get("INTF_RFCN"), 0)
        cell_id = _safe_int(cellular.get("INTF_Cell_ID"), 0)
        tac = _safe_int(cellular.get("INTF_TAC"), 0)
        enb_id = cell_id >> 8 if cell_id > 0 else 0

        # Band
        band_str = str(cellular.get("INTF_Current_Band", "") or "")
        band = _band_name_to_number(band_str)

        # Bandwidth
        dl_bw = _safe_int(cellular.get("INTF_Downlink_Bandwidth"), 0)
        ul_bw = _safe_int(cellular.get("INTF_Uplink_Bandwidth"), 0)
        # Zyxel reports BW as RB count index (3=10MHz, 5=20MHz, etc.)
        bw_map = {0: 0, 1: 1.4, 2: 3, 3: 5, 4: 10, 5: 15, 6: 20}
        dl_bw_mhz = bw_map.get(dl_bw, dl_bw)
        ul_bw_mhz = bw_map.get(ul_bw, ul_bw)

        # Connection status
        intf_status = str(cellular.get("INTF_Status", "") or "")
        access_tech = str(
            cellular.get("INTF_Current_Access_Technology", "") or ""
        )
        radio_connected = intf_status.lower() == "up"

        # NSA (5G) state
        nsa_enable = bool(cellular.get("NSA_Enable", False))
        nsa_pci = _safe_int(cellular.get("NSA_PCI"), 0)
        nsa_rfcn = _safe_int(cellular.get("NSA_RFCN"), 0)
        nsa_band_str = str(cellular.get("NSA_Band", "") or "")
        nsa_band = _band_name_to_number(nsa_band_str)
        nsa_rsrp = _safe_float(cellular.get("NSA_RSRP"), 0.0)
        nsa_rsrq = _safe_float(cellular.get("NSA_RSRQ"), 0.0)
        nsa_sinr = _safe_float(cellular.get("NSA_SINR"), 0.0)
        nsa_rssi = _safe_float(cellular.get("NSA_RSSI"), 0.0)
        nsa_mcc = str(cellular.get("NSA_MCC", "") or "")
        nsa_mnc = str(cellular.get("NSA_MNC", "") or "")
        nsa_active = bool(
            nsa_enable and ("NR" in access_tech.upper() or nsa_pci > 0)
        )

        # Provider info from network_in_use string
        network_in_use = str(
            cellular.get("INTF_Network_In_Use", "") or ""
        )
        # Format: "Current_DNA DNA_NR-NSA EN-DC_24412"
        # Parse provider name from it
        provider_name = ""
        provider_plmn = ""
        if network_in_use:
            parts = network_in_use.split("_")
            if len(parts) >= 2:
                provider_name = parts[1]  # e.g. "DNA"
            # PLMN might be the last numeric segment
            for p in reversed(parts):
                if p.isdigit() and len(p) >= 5:
                    provider_plmn = p
                    break

        # Network type
        if nsa_active:
            network_type = "NR NSA (5G)"
        elif "LTE" in access_tech.upper():
            network_type = "LTE (4G)"
        else:
            network_type = "No Service" if not access_tech else ""

        # Supported bands
        supported_bands_str = str(
            cellular.get("INTF_Supported_Bands", "") or ""
        )
        supported_bands = [
            _band_name_to_number(b)
            for b in supported_bands_str.split(",")
            if _band_name_to_number(b) > 0
        ]
        supported_bands_observed_at = float(
            self._last_status.get("supported_lte_bands_observed_at", 0.0)
            or ts
        )

        # Carrier aggregation (SCC_Info)
        scc_info = cellular.get("SCC_Info") or []
        carriers = []
        if band > 0:
            carriers.append(CarrierInfo(
                band=band,
                earfcn=earfcn,
                pci=pci,
                bandwidth_mhz=float(dl_bw_mhz),
                sinr_db=sinr,
                rsrp_dbm=rsrp,
                state=1,
                is_pcell=True,
            ))
        ca_active = bool(scc_info)
        for scc in scc_info:
            if not isinstance(scc, dict):
                continue
            scc_band = _band_name_to_number(scc.get("Band", ""))
            if scc_band > 0:
                carriers.append(CarrierInfo(
                    band=scc_band,
                    earfcn=_safe_int(scc.get("RFCN"), 0),
                    pci=_safe_int(scc.get("PhysCellID") or scc.get("PCI"), 0),
                    bandwidth_mhz=20.0,
                    sinr_db=0.0,
                    rsrp_dbm=0.0,
                    state=1,
                    is_pcell=False,
                ))

        if ca_active:
            all_bands = sorted({c.band for c in carriers if c.band > 0})
            ca_signature = "+".join(f"B{b}" for b in all_bands)
        else:
            ca_signature = f"B{band}" if band > 0 else ""

        # IMEI / module FW (update on each read)
        imei = str(cellular.get("INTF_IMEI", "") or "")
        if imei and imei != "---":
            self._imei_detected = imei
        module_fw = str(
            cellular.get("INTF_Module_Software_Version", "") or ""
        )
        if module_fw:
            self._firmware_detected = module_fw

        # Build extra dict
        extra = {
            "ts": ts,
            "provider_name": provider_name or None,
            "provider_plmn": provider_plmn or None,
            "provider": provider_name or None,
            "plmn": provider_plmn or None,
            "network_type": network_type,
            "radio_connected": radio_connected,
            "radio_state_text": "Connected" if radio_connected else "Disconnected",
            "ca_active": ca_active,
            "ca_state_text": (
                "ACTIVE" if ca_active else "INACTIVE"
            ),
            "band": band or None,
            "serving_band": band or None,
            "serving_band_signature": f"B{band}" if band else "",
            "active_band_set": sorted({c.band for c in carriers}),
            "active_band_set_signature": ca_signature,
            "band_set": sorted({c.band for c in carriers}),
            "band_set_signature": ca_signature,
            "supported_lte_bands": supported_bands,
            "supported_lte_bands_source": "zyxel.INTF_Supported_Bands",
            "supported_lte_bands_observed_at": supported_bands_observed_at,
            "pci": pci or None,
            "cell_id": cell_id or None,
            "enb_id": enb_id or None,
            "earfcn_dl": earfcn or None,
            "tac": tac or None,
            "rrc": "Connected" if radio_connected else "Idle",
            "ri": ri or None,
            "dl_bandwidth": f"{dl_bw_mhz}MHz" if dl_bw_mhz else None,
            "ul_bandwidth": f"{ul_bw_mhz}MHz" if ul_bw_mhz else None,
            "trans_mode": f"TM[{ri}]" if ri else None,
            # Scheduler — Zyxel exposes CQI and MCS!
            "cqi0": cqi if cqi > 0 else None,
            "mcs_dl0": mcs if mcs > 0 else None,
            "intf_pmi": pmi if pmi > 0 else None,
            # NR fields
            "nr5g_active": nsa_active,
            "nr5g_band": nsa_band if nsa_active else 0,
            "nr5g_pci": nsa_pci if nsa_active else 0,
            "nr5g_arfcn": nsa_rfcn if nsa_active else 0,
            "nr5g_rsrp": nsa_rsrp if nsa_active else 0.0,
            "nr5g_rsrq": nsa_rsrq if nsa_active else 0.0,
            "nr5g_sinr": nsa_sinr if nsa_active else 0.0,
            # USIM
            "usim_status": str(cellular.get("USIM_Status", "") or ""),
            # Device
            "model": self._model_detected,
            "firmware": self._firmware_detected,
            "imei": self._imei_detected,
            "access_technology": access_tech,
        }
        extra.update(traffic_state)
        self._inject_zyxel_lane_debug_status(extra, lane=lane_key)
        self._last_status = extra
        self._last_zyxel_cellular_fields = (
            self._canonical_fields_proven_by_cellular(cellular)
        )

        return ModemInput(
            timestamp=ts,
            sinr_db=sinr,
            rsrp_dbm=rsrp,
            rsrq_db=rsrq,
            rssi_dbm=rssi,
            mcs_dl=mcs,
            mcs_ul=0,
            cqi=cqi,
            ri=ri,
            primary_band=band,
            pci=pci,
            earfcn=earfcn,
            enb_id=enb_id,
            cell_id=cell_id,
            radio_connected=radio_connected,
            session_uptime_s=0.0,
            rx_bytes_per_sec=_safe_float(traffic_state.get("traffic_current_download_rate_bps"), 0.0),
            tx_bytes_per_sec=_safe_float(traffic_state.get("traffic_current_upload_rate_bps"), 0.0),
            ca_active=ca_active,
            ca_band_signature=ca_signature,
            carriers=carriers,
            selected_band_set=[],
            dl_bandwidth=f"{dl_bw_mhz}MHz" if dl_bw_mhz else "",
            ul_bandwidth=f"{ul_bw_mhz}MHz" if ul_bw_mhz else "",
            tac=tac,
            rrc="Connected" if radio_connected else "Idle",
            trans_mode=f"TM[{ri}]" if ri else "",
            network_type=network_type,
            provider_name=provider_name,
            provider_plmn=provider_plmn,
            cqi0=cqi,
            mcs_dl0=mcs,
            nr_band=nsa_band if nsa_active else 0,
            nr_pci=nsa_pci if nsa_active else 0,
            nr_arfcn=nsa_rfcn if nsa_active else 0,
            nsa_active=nsa_active,
        )

    # ── Signal reading ───────────────────────────────────────────────────────

    def read_modem_input(self) -> Optional[ModemInput]:
        if not self._connected:
            return None

        cellular = self._read_cellular_status(lane_key="full_read")
        if not cellular:
            return None
        traffic = self._read_traffic_status(lane_key="traffic_read")
        return self._build_modem_input_from_cellular(
            cellular,
            lane_key="full_read",
            live_traffic=traffic,
        )

    def read_actionable_modem_input(self) -> Optional[ModemInput]:
        if not self._connected:
            return None

        cellular = self._read_cellular_status(lane_key="actionable_read")
        if not cellular:
            return None
        return self._build_modem_input_from_cellular(
            cellular,
            lane_key="actionable_read",
        )

    @staticmethod
    def _canonical_fields_proven_by_cellular(
        cellular: Dict[str, Any],
    ) -> frozenset[str]:
        groups = (
            (("INTF_SINR",), {"sinr_db"}),
            (("INTF_RSRP",), {"rsrp_dbm"}),
            (("INTF_RSRQ",), {"rsrq_db"}),
            (("INTF_RSSI",), {"rssi_dbm"}),
            (("INTF_CQI",), {"cqi", "cqi0"}),
            (("INTF_MCS",), {"mcs_dl", "mcs_dl0"}),
            (("INTF_RI",), {"ri", "ri_valid", "ri_source", "trans_mode"}),
            (("INTF_PhyCell_ID",), {"pci"}),
            (("INTF_RFCN",), {"earfcn", "earfcn_dl"}),
            (("INTF_Cell_ID",), {"cell_id", "enb_id"}),
            (("INTF_TAC",), {"tac"}),
            (("INTF_Current_Band",), {"band", "serving_band", "serving_band_signature", "active_band_set", "active_band_set_signature", "band_set", "band_set_signature", "carrier_count"}),
            (("INTF_Downlink_Bandwidth",), {"dl_bandwidth", "dl_bandwidth_mhz"}),
            (("INTF_Uplink_Bandwidth",), {"ul_bandwidth", "ul_bandwidth_mhz"}),
            (("INTF_Status",), {"radio_connected", "radio_state_text", "rrc"}),
            (("INTF_Current_Access_Technology",), {"network_type"}),
            (("INTF_MCC", "INTF_MNC"), {"provider_name", "provider_plmn"}),
            (("INTF_Supported_Bands",), {"supported_lte_bands", "supported_lte_bands_source", "supported_lte_bands_observed_at"}),
            (("SCC_Info",), {"ca_active", "ca_state_text", "ca_signature", "carrier_count", "active_band_set", "active_band_set_signature", "band_set", "band_set_signature"}),
            (("NSA_Enable",), {"nr5g_active", "nsa_active"}),
            (("NSA_Band",), {"nr5g_band"}),
            (("NSA_PCI",), {"nr5g_pci"}),
            (("NSA_RFCN",), {"nr5g_arfcn"}),
            (("NSA_RSRP",), {"nr5g_rsrp_dbm"}),
            (("NSA_RSRQ",), {"nr5g_rsrq_db"}),
            (("NSA_SINR",), {"nr5g_sinr_db"}),
            (("INTF_IMEI",), {"imei"}),
            (("INTF_Module_Software_Version",), {"firmware"}),
        )
        fields: set[str] = set()
        for source_names, canonical_fields in groups:
            if any(
                source in cellular
                and cellular[source] is not None
                and (
                    not isinstance(cellular[source], str)
                    or bool(cellular[source].strip())
                )
                for source in source_names
            ):
                fields.update(canonical_fields)
        return frozenset(fields)

    @staticmethod
    def _canonical_fields_proven_by_traffic(
        traffic: Dict[str, Any],
    ) -> frozenset[str]:
        return frozenset(
            str(field)
            for field, value in traffic.items()
            if field in DECLARED_CANONICAL_SOURCE_FIELDS
            and value is not None
            and (not isinstance(value, str) or bool(value.strip()))
        )

    def acquire_modem_input(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "startup_full":
            raise ValueError(
                "zyxel_startup_full_acquisition_lane_required"
            )
        modem = self.read_modem_input()
        acquired_fields = (
            self._last_zyxel_cellular_fields
            | self._canonical_fields_proven_by_traffic(
                dict(self._last_traffic_status or {})
            )
        )
        acquired_cells = (
            matrix_cells_for_canonical_fields(acquired_fields)
            if modem is not None
            else frozenset()
        )
        return self._capture_backend_acquisition_receipt(
            request=request,
            modem=modem,
            acquired_cells=acquired_cells,
            failure_reason="zyxel_startup_acquisition_unavailable",
        )

    def acquire_actionable_modem_input(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "fast_live":
            raise ValueError(
                "zyxel_fast_live_acquisition_lane_required"
            )
        modem = self.read_actionable_modem_input()
        acquired_cells = (
            matrix_cells_for_canonical_fields(
                self._last_zyxel_cellular_fields
            )
            if modem is not None
            else frozenset()
        )
        return self._capture_backend_acquisition_receipt(
            request=request,
            modem=modem,
            acquired_cells=acquired_cells,
            failure_reason="zyxel_actionable_acquisition_unavailable",
        )

    def acquire_post_write_modem_input(
        self,
        request: BackendAcquisitionRequest,
        stage: PhysicalCommandDispatchEvidence,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "post_write":
            raise ValueError(
                "zyxel_post_write_acquisition_lane_required"
            )
        if not isinstance(stage, PhysicalCommandDispatchEvidence):
            raise TypeError(
                "zyxel_post_write_stage_evidence_required"
            )
        if not stage.permits_target_verification:
            raise ValueError(
                "zyxel_post_write_accepted_stage_required"
            )
        modem = self.read_modem_input()
        acquired_fields = (
            self._last_zyxel_cellular_fields
            | self._canonical_fields_proven_by_traffic(
                dict(self._last_traffic_status or {})
            )
        )
        acquired_cells = (
            matrix_cells_for_canonical_fields(acquired_fields)
            if modem is not None
            else frozenset()
        )
        return self._capture_backend_acquisition_receipt(
            request=request,
            modem=modem,
            acquired_cells=acquired_cells,
            failure_reason="zyxel_post_write_acquisition_unavailable",
        )

    def refresh_live_maintenance(self) -> None:
        raise RuntimeError("zyxel_typed_maintenance_required")

    def acquire_live_maintenance(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "slow_live":
            raise ValueError(
                "zyxel_slow_live_acquisition_lane_required"
            )
        if not self._connected:
            traffic = {}
        else:
            traffic = self._read_traffic_status(
                lane_key="maintenance_read"
            )
        status = dict(self._last_status or {})
        status.update(traffic)
        self._inject_zyxel_lane_debug_status(
            status,
            lane="maintenance_read",
        )
        self._last_status = status
        acquired_fields = self._canonical_fields_proven_by_traffic(traffic)
        acquired_cells = matrix_cells_for_canonical_fields(acquired_fields)
        return self._capture_backend_acquisition_receipt(
            request=request,
            modem=None,
            acquired_cells=acquired_cells,
            failure_reason="zyxel_maintenance_acquisition_unavailable",
        )

    def get_live_acquisition_contract(self) -> LiveAcquisitionContract:
        return LiveAcquisitionContract(
            acquisition_class="aligned_split",
            clocked_register_read=True,
            live_register_interval_s=2.0,
            live_register_stale_s=6.0,
            profile_name="zyxel_webui_status",
            has_explicit_actionable_snapshot=True,
            has_explicit_maintenance_refresh=True,
            backend_io_parallel_safe=False,
            io_serial_required=True,
            startup_parallel_allowed=False,
            runtime_action_requires_io_lock=True,
            rtt_holds_io_lock=False,
            rtt_min_interval_s=1.0,
            maintenance_holds_io_lock=False,
            generic_neighbor_scan_enabled=False,
            fast_read_groups=("dal_radio_status", "rf_quality", "traffic_presence", "traffic_volume"),
            slow_read_groups=("wifi_status", "router_status", "network_mode"),
            static_capability_groups=(
                "device_identity",
                "firmware",
                "supported_bands",
                "antenna_capability",
                "qam_capability",
                "wifi_capability",
            ),
            control_write_groups=("band_selection", "cell_lock", "restart", "wifi_toggle", "session_key"),
            export_groups=("sidecars", "support_bundle", "history"),
            static_capability_refresh_policy="identity_or_operator_refresh_only",
        )

    def read_neighbors(self) -> List[NeighborCell]:
        # Zyxel does not expose neighbor cells through the DAL API
        # (only available via AT commands if SSH access is available)
        return []

    def measure_rtt(
        self,
        target: str = "8.8.8.8",
        count: int = 3,
        *,
        timeout_s: float,
    ) -> RttProbeResult:
        return measure_host_ping_rtt(
            target,
            count=count,
            ping_interface=self._ping_iface,
            timeout_s=timeout_s,
        )

    # ── Target guard ──────────────────────────────────────────────────────

    def _target_guard_enabled(self) -> bool:
        return bool(self._expected_model or self._expected_imei or self._expected_firmware)

    def _check_target_guard(self) -> tuple[bool, str]:
        if not self._target_guard_enabled():
            return True, ""
        return evaluate_target_guard(
            expected_model=self._expected_model,
            expected_firmware=self._expected_firmware,
            expected_imei=self._expected_imei,
            observed={
                "model": self._model_detected,
                "firmware": self._firmware_detected,
                "imei": self._imei_detected,
            },
        )

    # ── Lever execution ──────────────────────────────────────────────────────

    def execute_physical_stage(
        self,
        plan: ModemLivePhysicalStagePlan,
    ) -> ModemLivePhysicalStageResult:
        if not isinstance(plan, ModemLivePhysicalStagePlan):
            raise TypeError("zyxel_physical_stage_plan_required")
        target = plan.target
        if self._target_guard_enabled():
            ok, reason = self._check_target_guard()
            if not ok:
                raise RuntimeError(str(reason or "zyxel_target_guard_rejected"))
        if not isinstance(target, CoreRestartTarget):
            raise RuntimeError("zyxel_typed_target_not_implemented")
        if not self._sessionkey or self._session is None:
            raise RuntimeError("zyxel_session_unavailable")
        started = time.time()
        accepted = False
        reason = "zyxel_restart_failed"
        try:
            response = self._zyxel_http_post(
                f"{self._base_url}/cgi-bin/Reboot"
                f"?sessionkey={self._sessionkey}",
                operation="reboot",
                timeout=10,
            )
            returned = time.time()
            response.raise_for_status()
        except requests.RequestException as exc:
            returned = time.time()
            reason = f"zyxel_restart_transport_error:{type(exc).__name__}"
        else:
            try:
                payload = response.json()
            except requests.exceptions.JSONDecodeError:
                reason = "zyxel_restart_protocol_error:invalid_json"
            else:
                accepted = bool(payload.get("result") == "ZCFG_SUCCESS")
                reason = "accepted" if accepted else "zyxel_restart_rejected"
        return ModemLivePhysicalStageResult(
            plan=plan,
            request_dispatched_at=started,
            transport_returned_at=returned,
            outcome="accepted" if accepted else "rejected",
            reason=reason,
            radio_drop_detected=False,
        )

    def execute_lever(self, action: LeverAction) -> LeverResult:
        del action
        raise RuntimeError("zyxel_generic_execute_lever_forbidden")

    def _execute_restart(self, reason: str) -> LeverResult:
        del reason
        raise RuntimeError("zyxel_hidden_restart_write_forbidden")

    def _execute_band_selection(self, params: dict, reason: str) -> LeverResult:
        del params, reason
        raise RuntimeError("zyxel_hidden_band_write_forbidden")

    def _execute_cell_lock(self, params: dict, reason: str) -> LeverResult:
        del params, reason
        raise RuntimeError("zyxel_hidden_cell_lock_write_forbidden")

    # ── Capability / info stubs ──────────────────────────────────────────────

    def verify_static_config(self) -> Dict[str, bool]:
        return {}

    def apply_static_config(self, item: str) -> bool:
        del item
        raise RuntimeError("zyxel_direct_static_config_write_forbidden")

    def verify_ue_capability(self) -> Dict[str, Any]:
        return {
            "dl_256qam": "1",
            "ul_64qam": "1",
            "ue_category": 20,
            "pci_lock_available": self._cell_lock_capable,
            "dl_mimo_layers": 4,
            "nsa_supported": True,
            "nr_bands": [1, 3, 7, 8, 20, 28, 38, 41, 77, 78],
        }

    def set_ue_capability(self, key: str, value: Any) -> bool:
        del key, value
        raise RuntimeError("zyxel_direct_ue_capability_write_forbidden")

    def restart_modem(self) -> bool:
        raise RuntimeError("zyxel_direct_restart_write_forbidden")

    def read_fresh_device_identity(self) -> Dict[str, str]:
        cellular = self._dal_get("cellwan_status") or {}
        imei = str(cellular.get("INTF_IMEI") or "").strip()
        firmware = str(cellular.get("INTF_Module_Software_Version") or "").strip()
        if imei:
            self._imei_detected = imei
        if firmware:
            self._firmware_detected = firmware
        return {"imei": imei, **({"firmware": firmware} if firmware else {})}

    def get_modem_info(self) -> Dict[str, str]:
        return {
            "model": self._model_detected or self._backend_name.upper(),
            "firmware": self._firmware_detected,
            "imei": self._imei_detected,
            "backend": "zyxel_webui",
            "module": "Quectel RG502Q-EA",
        }

    def get_current_bands(self) -> List[int]:
        cellular = self._dal_get("cellwan_status")
        if not cellular:
            return []
        supported = str(cellular.get("INTF_Supported_Bands", "") or "")
        return [
            _band_name_to_number(b)
            for b in supported.split(",")
            if _band_name_to_number(b) > 0
        ]

    def get_capabilities(self):
        from arch4.modem.catalog import get_capabilities
        return get_capabilities(self._backend_name)

    def get_last_status(self) -> Dict[str, Any]:
        return build_public_modem_status(self._last_status)

    def scan_neighbors(self) -> List[Dict[str, Any]]:
        return []

    def read_serving_cell(self) -> Dict[str, Any]:
        return dict(self._last_status)

    def read_band_priority(self) -> List[int]:
        return []

    def set_band_priority(self, bands: List[int]) -> bool:
        del bands
        raise RuntimeError("zyxel_direct_band_write_forbidden")
