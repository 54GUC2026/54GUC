"""External virtual appliance for the two supported OpenWrt API dialects.

The appliance owns synthetic router and modem behavior.  Production adapters
reach it through their normal HTTP ubus boundary; this module never publishes
Matrix truth and is never imported by production runtime code.
"""

from __future__ import annotations

import base64
import hashlib
import copy
import heapq
import json
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from arch4.simulators.zte_mc888 import TrafficEngine


SCENARIO_SCHEMA = "OpenWrtDualSimulatorScenario.v1"
CONTROL_HEADER = "X-CGF-Simulator-Control"
STANDARD_DIALECT = "openwrt_modemmanager_rpcd"
ZWRT_DIALECT = "zte_zwrt_ubus"
SUPPORTED_DIALECTS = frozenset({STANDARD_DIALECT, ZWRT_DIALECT})
ZERO_SESSION_ID = "0" * 32
ZWRT_NETWORK_METHODS = {
    ("zte_nwinfo_api", "nwinfo_manual_scan"): set(),
    ("zte_nwinfo_api", "nwinfo_m_netselect_status"): set(),
    ("zte_nwinfo_api", "nwinfo_m_netselect_contents"): set(),
    ("zte_nwinfo_api", "nwinfo_m_netselect_result"): set(),
    ("zte_nwinfo_api", "nwinfo_manual_register"): {"m_mcc_mnc", "m_rat"},
    ("zte_nwinfo_api", "nwinfo_set_netselect"): {"net_select_mode"},
    ("zwrt_data", "get_wwaniface"): {"cid"},
    ("zwrt_data", "set_wwaniface"): {"cid", "connect_mode", "enable", "roam_enable"},
    ("zwrt_apn_object", "get_apn_mode"): set(),
    ("zwrt_apn_object", "get_manu_apn_list"): set(),
    ("zwrt_apn_object", "get_auto_apn_list"): set(),
    ("zwrt_apn_object", "modify_manu_apn"): {"profileId", "profilename", "wanapn", "pdpType", "pppAuthMode", "username", "password"},
}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _bands(value: Any) -> list[int]:
    if isinstance(value, str):
        values = value.replace("B", "").replace("b", "").split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []
    result: list[int] = []
    for item in values:
        band = _as_int(item)
        if band > 0 and band not in result:
            result.append(band)
    return result


def _decode_lte_mask(value: Any) -> list[int]:
    text = str(value or "").strip().lower()
    if not text:
        return []
    try:
        mask = int(text, 16) if text.startswith("0x") else int(text)
    except ValueError:
        return []
    return [band for band in range(1, 89) if mask & (1 << (band - 1))]


@dataclass(frozen=True)
class HttpReply:
    status: int
    body: bytes
    content_type: str = "application/json"
    close_without_reply: bool = False
    headers: dict[str, str] = field(default_factory=dict)


class OpenWrtScenarioEngine:
    """The only owner of synthetic identity, radio, traffic and faults."""

    def __init__(
        self,
        scenario: Mapping[str, Any],
        *,
        password: str | None = None,
        control_token: str = "openwrt-lab",
        time_scale: float = 1.0,
        event_log: str | Path | None = None,
    ) -> None:
        self.scenario = validate_scenario(scenario)
        self.dialect = str(self.scenario["dialect"])
        self.password = str(
            password
            if password is not None
            else self.scenario["authentication"]["password"]
        )
        self.username = str(self.scenario["authentication"].get("username") or "root")
        self.control_token = str(control_token or "openwrt-lab")
        self.time_scale = max(0.001, float(time_scale))
        self.event_log = Path(event_log).expanduser() if event_log else None
        self.request_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.sessions: dict[str, float] = {}
        self.secured_sessions: set[str] = set()
        # Explicit synthetic firmware variants; not claims about physical B17.
        self.session_exchange_unsupported = str(self.scenario.get("session_exchange_unsupported") or "")
        if self.session_exchange_unsupported not in {"", "web_crt_get", "web_http_enstr_set"}:
            raise ValueError("simulator_session_exchange_variant_invalid")
        self.session_exchange_required = bool(self.scenario.get("session_exchange_required", True))
        self.bootstrap_cookie = secrets.token_hex(16)
        if self.dialect == ZWRT_DIALECT:
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.hazmat.primitives import serialization
            self.session_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            self.session_public_key = self.session_private_key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("ascii")
        self.events: deque[dict[str, Any]] = deque(maxlen=4000)
        self.faults: deque[dict[str, Any]] = deque()
        self.pending: list[tuple[float, int, str, dict[str, Any]]] = []
        self.pending_counter = 0
        self.request_counter = 0
        self.queued_requests = 0
        self.max_queued_requests = 0
        self.physical_inflight = 0
        self.max_physical_inflight = 0
        self.field_overrides: dict[str, Any] = {}
        self.last_protocol_payload: Any = None
        self.started = time.monotonic()
        self.traffic = TrafficEngine(
            self.scenario["traffic"], time_scale=self.time_scale
        )
        self._reset_state()

    def _reset_state(self) -> None:
        radio = self.scenario["radio"]
        self.started = time.monotonic()
        self.configured_lte_pool = _bands(radio.get("configured_lte_pool"))
        self.serving_band = _as_int(
            radio.get("serving_band"), self.configured_lte_pool[0]
        )
        self.active_lte_bands = _bands(
            radio.get("active_lte_bands") or [self.serving_band]
        )
        self.nr_bands = _bands(radio.get("nr_bands") or [78])
        self.nr_band = _as_int(radio.get("nr_band"), self.nr_bands[0])
        self.mode_choices = tuple(self.scenario.get("radio_mode", {}).get("choices", ()))
        self.configured_mode = self.scenario.get("radio_mode", {}).get("selected", "")
        self.cell_lock = {"pci": 0, "earfcn": 0}
        self.wifi_24_enabled = bool(radio.get("wifi_24ghz_enabled", True))
        self.wifi_5_enabled = bool(radio.get("wifi_5ghz_enabled", True))
        self.radio_available_at = self.started
        self.reboot_until = 0.0
        self.sessions.clear()
        self.secured_sessions.clear()
        self.pending.clear()
        self.field_overrides.clear()
        self.faults.clear()
        self.last_protocol_payload = None
        self.traffic.reset()
        self.network = copy.deepcopy(self.scenario.get("network", {}))
        self.sim_identity = {key: self.scenario["identity"][key] for key in ("imsi", "iccid")}
        self.network_behavior: dict[str, Any] = {}
        self.network_scan_ready_at = 0.0
        self.network_registration_generation = 0

    def reset(self) -> None:
        with self.state_lock:
            self._reset_state()
            self.record("reset")

    def scaled(self, seconds: Any) -> float:
        return max(0.0, _as_float(seconds)) * self.time_scale

    def response_delay(self, lane: str) -> float:
        key = {
            "homepage": "homepage_response_s",
            "login": "login_response_s",
            "read": "read_response_s",
            "write": "write_ack_s",
            "control": "control_response_s",
        }.get(lane, "read_response_s")
        return self.scaled(self.scenario["timing"].get(key, 0.0))

    def record(self, kind: str, **data: Any) -> None:
        event = {
            "ts": round(time.time(), 6),
            "kind": str(kind),
            "dialect": self.dialect,
            **data,
        }
        self.events.append(event)
        if self.event_log is not None:
            self.event_log.parent.mkdir(parents=True, exist_ok=True)
            with self.event_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")

    def new_session(self) -> str:
        token = secrets.token_hex(16)
        self.sessions[token] = time.monotonic()
        self.record("session_opened", token=token[:8])
        return token

    def authenticated(self, token: str) -> bool:
        session_id = str(token or "")
        opened = self.sessions.get(session_id)
        if opened is None:
            return False
        inactivity_s = _as_float(
            self.scenario["authentication"].get("session_inactivity_s"), 600.0
        )
        if inactivity_s > 0.0 and time.monotonic() - opened > self.scaled(inactivity_s):
            self.sessions.pop(session_id, None)
            return False
        self.sessions[session_id] = time.monotonic()
        return True

    def expire_sessions(self) -> None:
        self.sessions.clear()
        self.record("sessions_expired")

    def schedule(self, delay_s: float, label: str, changes: Mapping[str, Any]) -> None:
        self.pending_counter += 1
        heapq.heappush(
            self.pending,
            (
                time.monotonic() + self.scaled(delay_s),
                self.pending_counter,
                str(label),
                dict(changes),
            ),
        )

    def _apply_due(self) -> None:
        now = time.monotonic()
        while self.pending and self.pending[0][0] <= now:
            _when, _ordinal, label, changes = heapq.heappop(self.pending)
            if label == "network_registration":
                if changes["generation"] != self.network_registration_generation:
                    self.record("transition_superseded", label=label)
                    continue
                if self.network_behavior.get("require_connection_settings") and not self._network_prerequisites():
                    self.schedule(1.0, label, changes)
                    continue
                self.network["serving_plmn"] = changes["serving_plmn"]
            else:
                for key, value in changes.items():
                    setattr(self, key, value)
            self.record("transition_complete", label=label, changes=changes)

    def radio_connected(self) -> bool:
        self._apply_due()
        traffic = self.traffic.sample()
        return bool(
            time.monotonic() >= self.radio_available_at
            and time.monotonic() >= self.reboot_until
            and traffic.get("connected", True)
            and (not self.network or self.network["wwan"]["enable"] == 1)
            and (not self.network_behavior.get("require_connection_settings") or (
                self._network_prerequisites() and self.network["serving_plmn"] == self.network_behavior["require_connection_settings"]["plmn"]))
        )

    def _network_prerequisites(self) -> bool:
        required = self.network_behavior["require_connection_settings"]
        return bool(self.network["wwan"] == {"enable": 1, "connect_mode": 1, "roam_enable": 1}
                    and self.network["apn"]["wanapn"] == required["apn"]
                    and self.network["selection"] == "manual_select"
                    and self.network["target_plmn"] == required["plmn"])

    def schedule_radio_change(self, label: str, changes: Mapping[str, Any]) -> None:
        reattach_s = _as_float(self.scenario["timing"].get("lte_reattach_s"), 3.0)
        self.radio_available_at = time.monotonic() + self.scaled(reattach_s)
        self.traffic.block_until(self.radio_available_at)
        self.schedule(reattach_s, label, changes)
        self.record("radio_transition_started", label=label, changes=dict(changes))

    def modemmanager_dump(self) -> dict[str, Any]:
        self._apply_due()
        identity = self.scenario["identity"]
        radio = self.scenario["radio"]
        connected = self.radio_connected()
        nr_active = bool(connected and self.traffic.sample().get("nr_active", True))
        record: dict[str, Any] = {
            "id": str(identity.get("modem_id") or "0"),
            "path": f"/org/freedesktop/ModemManager1/Modem/{identity.get('modem_id', '0')}",
            "generic": {
                "state": "connected" if connected else "registered",
                "manufacturer": identity["manufacturer"],
                "model": identity["model"],
                "revision": identity["firmware"],
                "equipment-identifier": identity["imei"],
                "device-identifier": identity["serial"],
                "access-technologies": ["lte", "5gnr"] if nr_active else ["lte"],
                "ports": [
                    [identity.get("net_interface", "wwan0"), "net"],
                    [identity.get("primary_port", "cdc-wdm0"), "qmi"],
                ],
            },
            "system": {
                "plugin": identity.get("plugin", "quectel"),
                "primary-port": identity.get("primary_port", "cdc-wdm0"),
                "drivers": list(identity.get("drivers") or ["qmi_wwan"]),
            },
            "ports": [
                {"name": identity.get("net_interface", "wwan0"), "type": "net"},
                {"name": identity.get("primary_port", "cdc-wdm0"), "type": "qmi"},
            ],
            "3gpp": {
                "operator-name": radio["provider"],
                "operator-code": radio["plmn"],
                "imei": identity["imei"],
                "lte": {
                    "band": self.serving_band,
                    "pci": radio["lte_pci"],
                    "earfcn": radio["lte_earfcn"],
                    "tac": radio["tac"],
                },
            },
            "signal": {
                "lte": {
                    "rssi": radio["lte_rssi_dbm"],
                    "rsrp": radio["lte_rsrp_dbm"],
                    "rsrq": radio["lte_rsrq_db"],
                    "snr": radio["lte_sinr_db"],
                },
                "nr5g": {
                    "rsrp": radio["nr_rsrp_dbm"],
                    "rsrq": radio["nr_rsrq_db"],
                    "snr": radio["nr_sinr_db"],
                } if nr_active else {},
            },
        }
        return {"modem": [self._apply_overrides(record)]}

    def board_status(self) -> dict[str, Any]:
        identity = self.scenario["identity"]
        return self._apply_overrides(
            {
                "kernel": identity.get("kernel", "6.6.73"),
                "hostname": identity.get("hostname", "cgf-openwrt-sim"),
                "model": identity["router_model"],
                "board_name": identity.get("board_name", "virtual,openwrt-5g"),
                "release": {
                    "distribution": "OpenWrt",
                    "version": identity["openwrt_version"],
                    "revision": identity.get("openwrt_revision", "r0-sim"),
                },
            }
        )

    def interface_dump(self) -> dict[str, Any]:
        identity = self.scenario["identity"]
        connected = self.radio_connected()
        return {
            "interface": [
                {
                    "interface": "wan",
                    "proto": "modemmanager",
                    "up": connected,
                    "pending": not connected,
                    "l3_device": identity.get("net_interface", "wwan0"),
                    "device": identity.get("net_interface", "wwan0"),
                }
            ]
        }

    def wan_status(self) -> dict[str, Any]:
        identity = self.scenario["identity"]
        radio = self.scenario["radio"]
        connected = self.radio_connected()
        return {
            "up": connected,
            "pending": not connected,
            "proto": "modemmanager",
            "l3_device": identity.get("net_interface", "wwan0"),
            "device": identity.get("net_interface", "wwan0"),
            "ipv4-address": ([{"address": radio["wan_ip"], "mask": 30}] if connected else []),
        }

    def device_status(self, name: str) -> dict[str, Any]:
        expected = str(self.scenario["identity"].get("net_interface") or "wwan0")
        if str(name or "") != expected:
            return {}
        traffic = self.traffic.sample()
        return {
            "up": self.radio_connected(),
            "statistics": {
                "rx_bytes": traffic["rx_total"],
                "tx_bytes": traffic["tx_total"],
            },
        }

    def wireless_status(self) -> dict[str, Any]:
        return {
            "radio0": {
                "up": self.wifi_24_enabled,
                "config": {"band": "2g", "disabled": "0" if self.wifi_24_enabled else "1"},
            },
            "radio1": {
                "up": self.wifi_5_enabled,
                "config": {"band": "5g", "disabled": "0" if self.wifi_5_enabled else "1"},
            },
        }

    def zwrt_netinfo(self) -> dict[str, Any]:
        self._apply_due()
        identity = self.scenario["identity"]
        radio = self.scenario["radio"]
        connected = self.radio_connected()
        traffic = self.traffic.sample()
        active = list(self.active_lte_bands if connected else [])
        pband = active[0] if active else 0
        scells = active[1:]
        nr_active = bool(connected and self.configured_mode != "Only_LTE"
                         and traffic.get("nr_active", True))
        result: dict[str, Any] = {
            "network_type": "ENDC" if nr_active else ("LTE" if connected else "NO_SERVICE"),
            "modem_main_state": "modem_traffic" if connected else "modem_disconnected",
            "ppp_status": "ipv4_ipv6_connected" if connected else "disconnected",
            "network_provider": self.network["providers"].get(self.network["serving_plmn"], "Unknown") if self.network else radio["provider"],
            "network_provider_fullname": self.network["providers"].get(self.network["serving_plmn"], "Unknown") if self.network else radio["provider"],
            "mccmnc": self.network["serving_plmn"] if self.network else radio["plmn"],
            "net_select_mode": self.network.get("selection"),
            "lte_band": str(pband),
            "wan_active_band": ",".join(str(item) for item in active),
            "lte_ca_pcell_band": f"B{pband}" if pband else "",
            "lte_ca_scell_band": ",".join(f"B{item}" for item in scells),
            "lte_band_lock": hex(sum(1 << (band - 1) for band in self.configured_lte_pool)),
            "lte_pci": radio["lte_pci"] if connected else "",
            "lte_earfcn": radio["lte_earfcn"] if connected else "",
            "cell_id": radio["lte_cell_id"] if connected else "",
            "tac": radio["tac"] if connected else "",
            "lte_rssi": radio["lte_rssi_dbm"] if connected else "",
            "lte_rsrp": radio["lte_rsrp_dbm"] if connected else "",
            "lte_rsrq": radio["lte_rsrq_db"] if connected else "",
            "lte_snr": radio["lte_sinr_db"] if connected else "",
            "lte_ca_pcell_bandwidth": radio["lte_bandwidth_mhz"],
            "nr5g_band": self.nr_band if nr_active else "",
            "nr5g_action_nsa_band": self.nr_band if nr_active else "",
            "nr5g_pci": radio["nr_pci"] if nr_active else "",
            "nr5g_action_channel": radio["nr_arfcn"] if nr_active else "",
            "nr5g_cell_id": radio["nr_cell_id"] if nr_active else "",
            "nr5g_bandwidth": radio["nr_bandwidth_mhz"] if nr_active else "",
            "nr5g_nsa_bandwidth": radio["nr_bandwidth_mhz"] if nr_active else "",
            "nr5g_rsrp": radio["nr_rsrp_dbm"] if nr_active else "",
            "nr5g_rsrq": radio["nr_rsrq_db"] if nr_active else "",
            "nr5g_snr": radio["nr_sinr_db"] if nr_active else "",
            "model_name": identity["model"],
        }
        if self.mode_choices:
            # These are separate preserved native restrictions, not active RF
            # or inferred hardware capabilities. Only the current mode scenario
            # supplies them; historical fixtures retain their original shape.
            preserved = self.scenario["radio_mode"].get("preserved", {})
            result.update(net_select=self.configured_mode,
                lte_band=",".join(map(str, self.configured_lte_pool)),
                nr5g_nsa_band_lock=",".join(map(str, self.nr_bands)),
                nr5g_sa_band_lock=",".join(map(str, self.nr_bands)),
                lock_lte_cell=f"{self.cell_lock['pci']},{self.cell_lock['earfcn']}",
                lock_nr_cell=preserved.get("lock_nr_cell", ""),
                gw_band_lock=preserved.get("gw_band_lock", ""),
                nr5g_nrdc_band_lock=preserved.get("nr5g_nrdc_band_lock", ""))
        return self._apply_overrides(result)

    def zwrt_service(self, service: str, method: str, params: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self.scenario["identity"]
        radio = self.scenario["radio"]
        if (service, method) in ZWRT_NETWORK_METHODS:
            return self.zwrt_network_service(service, method, params)
        if service == "zte_nwinfo_api" and method == "nwinfo_get_netinfo":
            return 0, self.zwrt_netinfo()
        if service == "zwrt_bsp.thermal" and method == "get_cpu_temp":
            return 0, {"cpu_temp": 49, "modem_temp": 52, "nr_temp": 55}
        if service == "zwrt_mc.device.manager" and method == "get_device_info":
            device = {
                "model_name": identity["model"],
                "hardware_version": identity["hardware_version"],
                "software_version": identity["firmware"],
            }
            if self.mode_choices:
                device.update(wa_inner_version=identity["firmware"], modem_imei=identity["imei"])
            return 0, self._apply_overrides(device)
        if service == "zwrt_router.api" and method == "router_get_status":
            return 0, {"wan_ipaddr": radio["wan_ip"], "up_time": int((time.monotonic() - self.started) / self.time_scale)}
        if service == "zwrt_data" and method == "get_wwandst":
            traffic = self.traffic.sample()
            return 0, {
                "wan_rx_bytes": traffic["rx_total"],
                "wan_tx_bytes": traffic["tx_total"],
                "monthly_rx_bytes": traffic["monthly_rx"],
                "monthly_tx_bytes": traffic["monthly_tx"],
                "ppp_uptime": int(traffic["connected_seconds"]),
            }
        if service == "zwrt_zte_mdm.api" and method == "get_sim_info":
            if identity["model"] == "G5TC":
                # B17 field shape captured from the physical tester, independent
                # of the production normalizer and with synthetic values only.
                return 0, {"sim_imsi": self.sim_identity.get("imsi", ""),
                           "sim_iccid": self.sim_identity.get("iccid", ""),
                           "sim_states": self.sim_identity.get("sim_status", "sim ready"),
                           "current_sim_slot": "1" if self.mode_choices else 1}
            return 0, {"sim_status": "ready", **self.sim_identity}
        if service == "zwrt_zte_mdm.api" and method == "get_modem_msn":
            return 0, {"modem_msn": identity["serial"]}
        if service == "zwrt_zte_mdm.api" and method == "get_imei":
            return 0, {"imei": identity["imei"]}
        if service == "network.interface.zte_wan" and method == "status":
            return 0, self.wan_status()
        if service == "network.wireless" and method == "status":
            return 0, self.wireless_status()
        if service == "uci" and method == "get":
            config = str(params.get("config") or "")
            section = str(params.get("section") or "")
            if config == "zwrt_common_info":
                return 0, {"values": self._apply_overrides({
                    "model_name": identity["model"],
                    "hardware_version": identity["hardware_version"],
                    "wa_inner_version": identity["firmware"],
                })}
            if config == "zte_nwinfo" and section == "sys_info":
                return 0, {"values": self._apply_overrides({"operate_mode": "ONLINE"})}
            if config == "zwrt_zte_mdm" and section == "device_info":
                return 0, {"values": self._apply_overrides({
                    "modem_msn": identity["serial"],
                    "IMEI": identity["imei"],
                })}
            if config == "wireless" and section in {"radio0", "wifi0", "main_2g"}:
                return 0, {"values": {"band": "2g", "disabled": "0" if self.wifi_24_enabled else "1"}}
            if config == "wireless" and section in {"radio1", "wifi1", "main_5g"}:
                return 0, {"values": {"band": "5g", "disabled": "0" if self.wifi_5_enabled else "1"}}
            return 4, {}
        if service == "uci" and method == "set":
            section = str(params.get("section") or "")
            values = dict(params.get("values") or {})
            enabled = str(values.get("disabled") or "0") not in {"1", "true", "yes"}
            if section in {"radio0", "wifi0", "main_2g"}:
                self.wifi_24_enabled = enabled
                return 0, {}
            if section in {"radio1", "wifi1", "main_5g"}:
                self.wifi_5_enabled = enabled
                return 0, {}
        if service == "uci" and method == "commit":
            return 0, {}
        if service == "network" and method == "reload":
            return 0, {}
        if service == "zte_nwinfo_api" and method == "nwinfo_set_gwl_bandlock":
            pool = _decode_lte_mask(params.get("lte_band_mask"))
            if not pool:
                return 2, {}
            self.schedule_radio_change(
                "lte_band_lock",
                {"configured_lte_pool": pool, "serving_band": pool[0], "active_lte_bands": [pool[0]]},
            )
            return 0, {}
        if service == "zte_nwinfo_api" and method == "nwinfo_set_nrbandlock":
            bands = _bands(params.get("nr5g_band"))
            if not bands:
                return 2, {}
            self.schedule_radio_change("nr_band_lock", {"nr_bands": bands, "nr_band": bands[0]})
            return 0, {}
        if service == "zte_nwinfo_api" and method == "nwinfo_lock_lte_cell":
            self.cell_lock = {
                "pci": _as_int(params.get("lock_lte_pci")),
                "earfcn": _as_int(params.get("lock_lte_earfcn")),
            }
            return 0, {}
        if service == "system" and method == "reboot":
            downtime_s = _as_float(self.scenario["timing"].get("reboot_downtime_s"), 60.0)
            self.reboot_until = time.monotonic() + self.scaled(downtime_s)
            self.radio_available_at = self.reboot_until
            self.traffic.block_until(self.radio_available_at)
            self.sessions.clear()
            self.record("reboot_started", downtime_s=downtime_s)
            return 0, {}
        return 3, {}

    def zwrt_network_service(self, service: str, method: str, params: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Independent synthetic NG protocol; an ACK is not registration."""
        self._apply_due()
        safe_params = {key: "<redacted>" if key == "password" else value for key, value in params.items()}
        self.record("network_call", service=service, method=method, params=safe_params)
        if not self.network or method in self.network_behavior.get("deny_methods", []):
            return 6, {}
        expected = ZWRT_NETWORK_METHODS[(service, method)]
        if method == "nwinfo_set_netselect" and set(params) == {"net_select"}:
            native = params["net_select"]
            if (not self.mode_choices or type(native) is not str or native not in self.mode_choices
                    or self.network_behavior.get("reject_mode_write")):
                return 2, {}
            if not self.network_behavior.get("ignore_mode_write"):
                self.schedule_radio_change("configured_mode", {"configured_mode": native})
            return 0, {}
        if method == "set_wwaniface" and params.get("enable") == 0:
            expected = expected | {"connect_status"}
        if set(params) != expected:
            return 2, {}
        net = self.network
        if method == "nwinfo_manual_scan":
            self.network_scan_ready_at = time.monotonic() + self.scaled(self.network_behavior.get("scan_delay_s", 0.5))
            return 0, {}
        if method == "nwinfo_m_netselect_status":
            done = self.network_scan_ready_at and time.monotonic() >= self.network_scan_ready_at and not self.network_behavior.get("scan_stuck")
            return 0, {"m_netselect_status": "done" if done else "scanning"}
        if method == "nwinfo_m_netselect_contents":
            rows = self.network_behavior.get("scan_rows", net["scan_rows"])
            return 0, {"m_netselect_contents": copy.deepcopy(rows)}
        if method == "nwinfo_m_netselect_result":
            result = self.network_behavior.get("registration_result")
            if result is None:
                result = "success" if net["target_plmn"] and net["serving_plmn"] == net["target_plmn"] else "pending"
            return 0, {"m_netselect_result": result}
        if method == "nwinfo_manual_register":
            rows = self.network_behavior.get("scan_rows", net["scan_rows"])
            if not self.network_scan_ready_at or time.monotonic() < self.network_scan_ready_at:
                return 2, {}
            if not any(r["mcc_mnc"] == params["m_mcc_mnc"] and r["rat"] == params["m_rat"] and r["status"] in {"available", "current"} for r in rows):
                return 2, {}
            self.network_registration_generation += 1
            if self.network_behavior.get("ignore_operator_write"):
                return 0, {}
            net["selection"] = "manual_select"
            net["target_plmn"] = params["m_mcc_mnc"]
            if not self.network_behavior.get("registration_never"):
                plmn = self.network_behavior.get("registration_wrong_plmn", params["m_mcc_mnc"])
                delay = self.network_behavior.get("registration_delay_s", net["registration_delay_s"])
                self.schedule(delay, "network_registration", {"serving_plmn": plmn, "generation": self.network_registration_generation})
            return 0, {}
        if method == "nwinfo_set_netselect":
            if params["net_select_mode"] != "auto_select":
                return 2, {}
            self.network_registration_generation += 1
            net.update(selection="auto_select", target_plmn="", serving_plmn=net["automatic_plmn"])
            return 0, {}
        if method == "get_wwaniface":
            return (0, dict(net["wwan"], connect_status=(
                "ipv4_connected" if self.radio_connected() else "disconnected"
            ))) if params["cid"] == 1 else (2, {})
        if method == "set_wwaniface":
            if any(type(params[k]) is not int for k in params if k != "connect_status") or params["cid"] != 1 or any(params[k] not in (0, 1) for k in ("enable", "connect_mode", "roam_enable")):
                return 2, {}
            if params["enable"] == 0 and params.get("connect_status") != "disconnected":
                return 2, {}
            if self.network_behavior.get("ignore_data_hold" if params["enable"] == 0 else "ignore_data_start"):
                return 0, {}
            net["wwan"].update({key: params[key] for key in ("connect_mode", "enable")})
            if not self.network_behavior.get("ignore_roaming_write"):
                net["wwan"]["roam_enable"] = params["roam_enable"]
            return 0, {}
        if method == "get_apn_mode":
            return 0, {"apn_mode": "0" if self.network_behavior.get("apn_readonly") else "1"}
        if method in {"get_manu_apn_list", "get_auto_apn_list"}:
            return 0, {"apnListArray": [dict(net["apn"], password="********")]}
        if method == "modify_manu_apn":
            if self.network_behavior.get("apn_readonly") or params["profileId"] != net["apn"]["profileId"] or params["pdpType"] not in (1, 2, 3) or params["pppAuthMode"] not in (0, 1, 2):
                return 2, {}
            if not self.network_behavior.get("ignore_apn_write"):
                net["apn"].update(params)
            return 0, {}
        raise AssertionError("declared simulator network method has no handler")

    def standard_service(self, service: str, method: str, params: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if service == "modemmanager" and method == "dump":
            return 0, self.modemmanager_dump()
        if service == "system" and method == "board":
            return 0, self.board_status()
        if service == "network.interface" and method == "dump":
            return 0, self.interface_dump()
        if service == "network.interface.wan" and method == "status":
            return 0, self.wan_status()
        if service == "network.device" and method == "status":
            return 0, self.device_status(str(params.get("name") or ""))
        if service == "network.wireless" and method == "status":
            return 0, self.wireless_status()
        return 3, {}

    def _apply_overrides(self, value: dict[str, Any]) -> dict[str, Any]:
        result = json.loads(json.dumps(value))
        for path, replacement in self.field_overrides.items():
            parts = [part for part in str(path).split(".") if part]
            current: Any = result
            for part in parts[:-1]:
                if not isinstance(current, dict):
                    break
                current = current.setdefault(part, {})
            else:
                if isinstance(current, dict) and parts:
                    current[parts[-1]] = replacement
        return result

    def inject_fault(self, kind: str, *, count: int = 1, delay_s: float = 0.0) -> None:
        allowed = {
            "empty_json", "malformed_json", "http_500", "timeout",
            "drop_connection", "blank_rf", "invalid_sentinel", "stale_payload",
        }
        if kind not in allowed:
            raise ValueError(f"openwrt_simulator_unknown_fault:{kind}")
        for _ in range(max(1, int(count))):
            self.faults.append({"kind": kind, "delay_s": max(0.0, float(delay_s))})
        self.record("fault_queued", fault=kind, count=max(1, int(count)))

    def control(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or "").strip()
        if action == "set_traffic":
            self.traffic.select(str(payload.get("profile") or ""), _as_float(payload.get("duration_s")))
        elif action == "expire_session":
            self.expire_sessions()
        elif action == "fault":
            self.inject_fault(
                str(payload.get("kind") or ""),
                count=max(1, _as_int(payload.get("count"), 1)),
                delay_s=max(0.0, _as_float(payload.get("delay_s"))),
            )
        elif action == "set_fields":
            fields = payload.get("fields")
            if not isinstance(fields, Mapping):
                raise ValueError("openwrt_simulator_control_fields_mapping_required")
            self.field_overrides.update({str(key): value for key, value in fields.items()})
        elif action == "clear_fields":
            names = payload.get("fields")
            if names is None:
                self.field_overrides.clear()
            elif isinstance(names, list):
                for name in names:
                    self.field_overrides.pop(str(name), None)
            else:
                raise ValueError("openwrt_simulator_control_field_list_required")
        elif action == "reset":
            self.reset()
        elif action == "network_behavior":
            fields = payload.get("fields")
            allowed = {"deny_methods", "scan_stuck", "scan_rows", "scan_delay_s", "registration_never", "registration_wrong_plmn", "registration_delay_s", "registration_result", "ignore_roaming_write", "apn_readonly", "ignore_data_hold", "ignore_data_start", "ignore_apn_write", "ignore_operator_write", "require_connection_settings", "reject_mode_write", "ignore_mode_write"}
            if not isinstance(fields, Mapping) or set(fields) - allowed:
                raise ValueError("simulator_network_behavior_fields_invalid")
            self.network_behavior = dict(fields)
        elif action == "change_sim":
            if set(payload) != {"action", "imsi", "iccid"}:
                raise ValueError("simulator_sim_fields_invalid")
            self.sim_identity = {key: str(payload[key]) for key in ("imsi", "iccid")}
        else:
            raise ValueError(f"openwrt_simulator_unknown_control_action:{action or '<empty>'}")
        return self.public_state()

    def public_state(self) -> dict[str, Any]:
        self._apply_due()
        return {
            "scenario": self.scenario["name"],
            "dialect": self.dialect,
            "evidence": self.scenario["evidence"],
            "sessions": len(self.sessions),
            "radio_connected": self.radio_connected(),
            "serving_band": self.serving_band,
            "configured_lte_pool": list(self.configured_lte_pool),
            "active_lte_bands": list(self.active_lte_bands),
            "nr_band": self.nr_band,
            "cell_lock": dict(self.cell_lock),
            "wifi_24ghz_enabled": self.wifi_24_enabled,
            "wifi_5ghz_enabled": self.wifi_5_enabled,
            "traffic": self.traffic.sample(),
            "field_overrides": dict(self.field_overrides),
            "queued_faults": len(self.faults),
            "queued_requests": self.queued_requests,
            "max_queued_requests": self.max_queued_requests,
            "max_physical_inflight": self.max_physical_inflight,
            "pending_transitions": len(self.pending),
            "network": {key: value for key, value in self.network.items() if key != "apn"},
            "apn": {key: value for key, value in self.network.get("apn", {}).items() if key != "password"},
        }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenWrt"

    @property
    def engine(self) -> OpenWrtScenarioEngine:
        return self.server.engine  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _read_body(self) -> bytes:
        length = max(0, _as_int(self.headers.get("Content-Length")))
        return self.rfile.read(length) if length else b""

    @staticmethod
    def _json_reply(payload: Any, *, status: int = 200) -> HttpReply:
        return HttpReply(status, json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def _control_allowed(self) -> bool:
        return secrets.compare_digest(
            str(self.headers.get(CONTROL_HEADER) or ""), self.engine.control_token
        )

    def _ubus_result(self, request_id: Any, code: int, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": [int(code), dict(data or {})]}

    def _standard_call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        params = list(request.get("params") or [])
        if len(params) != 4:
            return self._ubus_result(request.get("id"), 2)
        session_id, service, method, args = params
        service_text = str(service or "")
        method_text = str(method or "")
        call_args = dict(args or {}) if isinstance(args, Mapping) else {}
        if service_text == "session" and method_text == "login":
            ok = (
                str(call_args.get("username") or "") == self.engine.username
                and str(call_args.get("password") or "") == self.engine.password
            )
            if not ok:
                return self._ubus_result(request.get("id"), 6)
            return self._ubus_result(
                request.get("id"), 0,
                {"ubus_rpc_session": self.engine.new_session(), "expires": 600},
            )
        if not self.engine.authenticated(str(session_id or "")):
            return self._ubus_result(request.get("id"), 6)
        code, data = self.engine.standard_service(service_text, method_text, call_args)
        return self._ubus_result(request.get("id"), code, data)

    def _zwrt_call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if request.get("method") == "list":
            if request.get("params") != ["zte_nwinfo_api"]:
                return self._ubus_result(request.get("id"), 2)
            methods = {method: {key: "String" for key in sorted(keys)}
                       for (service, method), keys in ZWRT_NETWORK_METHODS.items()
                       if service == "zte_nwinfo_api"}
            return {"jsonrpc": "2.0", "id": request.get("id"),
                    "result": {"zte_nwinfo_api": methods}}
        params = list(request.get("params") or [])
        if len(params) != 4:
            return self._ubus_result(request.get("id"), 2)
        session_id, service, method, args = params
        service_text = str(service or "")
        method_text = str(method or "")
        call_args = dict(args or {}) if isinstance(args, Mapping) else {}
        if service_text == "zwrt_web" and method_text == "web_login_info":
            return self._ubus_result(request.get("id"), 0, {"zte_web_sault": "CGF-SIM-SALT"})
        if service_text == "zwrt_web" and method_text in {"web_login", "web_developer_option_login"}:
            # Developer elevation is not an alternative anonymous login.
            if method_text == "web_developer_option_login" and not self.engine.authenticated(str(session_id or "")):
                return self._ubus_result(request.get("id"), 6)
            first = hashlib.sha256(self.engine.password.encode("utf-8")).hexdigest().upper()
            expected = hashlib.sha256((first + "CGF-SIM-SALT").encode("utf-8")).hexdigest().upper()
            if str(call_args.get("password") or "").upper() != expected:
                return self._ubus_result(request.get("id"), 0, {"result": 1})
            return self._ubus_result(
                request.get("id"), 0,
                {"result": 0, "ubus_rpc_session": self.engine.new_session()},
            )
        if not self.engine.authenticated(str(session_id or "")):
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32002, "message": "Access denied"},
            }
        if service_text == "zwrt_web" and method_text == self.engine.session_exchange_unsupported:
            return self._ubus_result(request.get("id"), 3)
        if service_text == "zwrt_web" and method_text == "web_crt_get":
            return self._ubus_result(request.get("id"), 0, {"result": self.engine.session_public_key})
        if service_text == "zwrt_web" and method_text == "web_http_enstr_set":
            from cryptography.hazmat.primitives.asymmetric import padding
            try:
                encrypted = base64.b64decode(call_args.get("web_enstr", ""), validate=True)
                key = self.engine.session_private_key.decrypt(encrypted, padding.PKCS1v15()).decode("ascii")
                if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
                    raise ValueError
            except (ValueError, UnicodeError):
                return self._ubus_result(request.get("id"), 2)
            self.engine.secured_sessions.add(session_id)
            return self._ubus_result(request.get("id"), 0, {"result": "success"})
        if session_id not in self.engine.secured_sessions and self.engine.session_exchange_required:
            return self._ubus_result(request.get("id"), 6)
        code, data = self.engine.zwrt_service(service_text, method_text, call_args)
        return self._ubus_result(request.get("id"), code, data)

    def _dispatch_ubus(self, body: bytes) -> HttpReply:
        try:
            value = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json_reply({"error": "invalid_json"}, status=400)
        requests_list = value if isinstance(value, list) else [value]
        if not all(isinstance(item, Mapping) for item in requests_list):
            return self._json_reply({"error": "invalid_request"}, status=400)
        call = self._standard_call if self.engine.dialect == STANDARD_DIALECT else self._zwrt_call
        response = [call(item) for item in requests_list]
        payload: Any = response if isinstance(value, list) else response[0]
        self.engine.last_protocol_payload = json.loads(json.dumps(payload))
        return self._json_reply(payload)

    def _fault_reply(self, fault: Mapping[str, Any]) -> HttpReply | None:
        kind = str(fault.get("kind") or "")
        if kind == "timeout":
            time.sleep(self.engine.scaled(fault.get("delay_s") or 2.0))
            return None
        if kind == "http_500":
            return self._json_reply({"error": "injected"}, status=500)
        if kind == "malformed_json":
            return HttpReply(200, b'{"jsonrpc":"2.0",,}')
        if kind == "empty_json":
            return self._json_reply({})
        if kind == "drop_connection":
            return HttpReply(0, b"", close_without_reply=True)
        if kind == "stale_payload" and self.engine.last_protocol_payload is not None:
            return self._json_reply(self.engine.last_protocol_payload)
        if kind in {"blank_rf", "invalid_sentinel"}:
            return None
        return None

    def _dispatch(self, method: str, body: bytes) -> tuple[HttpReply, str]:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" and method == "GET":
            model = self.engine.scenario["identity"]["model"]
            cookies = {"Set-Cookie": f"ng_bootstrap={self.engine.bootstrap_cookie}; Path=/; HttpOnly"} if self.engine.dialect == ZWRT_DIALECT else {}
            return HttpReply(200, f"<html><body>{model}</body></html>".encode("utf-8"), "text/html", headers=cookies), "homepage"
        if path.startswith("/_sim/"):
            if not self._control_allowed():
                return self._json_reply({"error": "control_token_required"}, status=403), "control"
            if path == "/_sim/state" and method == "GET":
                return self._json_reply(self.engine.public_state()), "control"
            if path == "/_sim/events" and method == "GET":
                return self._json_reply({"events": list(self.engine.events)}), "control"
            if path == "/_sim/network-sample" and method == "GET":
                return self._json_reply(self.engine.traffic.sample()), "control"
            if path == "/_sim/control" and method == "POST":
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                    if not isinstance(payload, Mapping):
                        raise ValueError("control_payload_mapping_required")
                    return self._json_reply(self.engine.control(payload)), "control"
                except (ValueError, json.JSONDecodeError) as exc:
                    return self._json_reply({"error": str(exc)}, status=400), "control"
            if path == "/_sim/download" and method == "GET":
                query = parse_qs(parsed.query)
                size = min(32 * 1024 * 1024, max(1, _as_int((query.get("bytes") or [1048576])[0], 1048576)))
                self.engine.traffic.add_bytes(rx_bytes=size)
                return HttpReply(200, b"Z" * size, "application/octet-stream"), "control"
            if path == "/_sim/upload" and method == "POST":
                self.engine.traffic.add_bytes(tx_bytes=len(body))
                return self._json_reply({"accepted_bytes": len(body)}), "control"
            return self._json_reply({"error": "not_found"}, status=404), "control"
        if path.rstrip("/") == "/ubus" and method == "POST":
            if self.engine.dialect == ZWRT_DIALECT:
                try:
                    calls = json.loads(body)
                    if not isinstance(calls, list) or not calls:
                        raise ValueError
                    services = [(r["params"][1], r["params"][2]) for r in calls if r.get("method") != "list"]
                except (ValueError, KeyError, IndexError, TypeError):
                    return self._json_reply({"error": "native_session_envelope_contract"}, status=400), "read"
                origin = self.headers.get("Origin", "")
                if (self.headers.get("X-Requested-With") != "XMLHttpRequest"
                        or origin not in {"http://" + self.headers.get("Host", ""), "https://" + self.headers.get("Host", "")}
                        or self.headers.get("Referer") != origin + "/"
                        or self.headers.get("Cookie") != f"ng_bootstrap={self.engine.bootstrap_cookie}"):
                    return self._json_reply({"error": "native_session_context_required"}, status=400), "read"
                anonymous = bool(services) and calls[0]["params"][0] == ZERO_SESSION_ID
                certificate = services == [("zwrt_web", "web_crt_get")]
                expected_mode = "0" if anonymous or certificate else "1"
                tag = None
                if len(services) == 1:
                    service, operation = services[0]
                    tag = calls[0]["params"][3].get("config") if service == "uci" else operation
                if self.headers.get("Z-Mode") != expected_mode or self.headers.get("Z-Tag") != tag:
                    return self._json_reply({"error": "native_session_header_contract"}, status=400), "read"
                for service, operation in services:
                    self.engine.record("ubus_call", service=service, operation=operation)
                if any(service == "zte_nwinfo_api" and operation == "nwinfo_get_netinfo" for service, operation in services):
                    expected = [("zte_nwinfo_api", "nwinfo_get_netinfo"),
                                ("zwrt_bsp.thermal", "get_cpu_temp"),
                                ("zwrt_mc.device.manager", "get_device_info"),
                                ("zwrt_router.api", "router_get_status"),
                                ("zwrt_data", "get_wwandst")]
                    configuration = [("zte_nwinfo_api", "nwinfo_get_netinfo"),
                                     ("zwrt_zte_mdm.api", "get_sim_info"),
                                     ("zwrt_data", "get_wwaniface"),
                                     ("zwrt_apn_object", "get_manu_apn_list"),
                                     ("zwrt_mc.device.manager", "get_device_info"),
                                     ("uci", "get"),
                                     ("zwrt_apn_object", "get_apn_mode"),
                                     ("zwrt_zte_mdm.api", "get_modem_msn"),
                                     ("zwrt_apn_object", "get_auto_apn_list")]
                    if (services != expected and services != expected[:1]
                            and not (self.engine.mode_choices and services == configuration)):
                        return self._json_reply({"error": "primary_status_batch_contract"}, status=400), "read"
            if time.monotonic() < self.engine.reboot_until:
                return self._json_reply({"error": "booting"}, status=503), "read"
            fault = self.engine.faults.popleft() if self.engine.faults else None
            if fault:
                injected = self._fault_reply(fault)
                if injected is not None:
                    return injected, "read"
                if str(fault.get("kind") or "") in {"blank_rf", "invalid_sentinel"}:
                    replacement: Any = "" if fault["kind"] == "blank_rf" else -999
                    prefixes = (
                        "signal.lte.rssi", "signal.lte.rsrp",
                        "signal.lte.rsrq", "signal.lte.snr",
                        "signal.lte.sinr", "signal.nr5g.rsrp",
                        "signal.nr5g.rsrq", "signal.nr5g.snr",
                        "lte_rssi", "lte_rsrp", "lte_rsrq", "lte_snr",
                        "nr5g_rsrp", "nr5g_rsrq", "nr5g_snr",
                    )
                    saved = dict(self.engine.field_overrides)
                    try:
                        for prefix in prefixes:
                            self.engine.field_overrides[prefix] = replacement
                        return self._dispatch_ubus(body), "read"
                    finally:
                        self.engine.field_overrides = saved
            return self._dispatch_ubus(body), "read"
        return self._json_reply({"error": "not_found"}, status=404), "read"

    def _send(self, reply: HttpReply) -> None:
        if reply.close_without_reply:
            self.close_connection = True
            return
        self.send_response(reply.status)
        self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(reply.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for key, value in reply.headers.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(reply.body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle(self, method: str) -> None:
        received = time.monotonic()
        body = self._read_body() if method == "POST" else b""
        path = urlparse(self.path).path
        if path.startswith("/_sim/"):
            with self.engine.state_lock:
                reply, lane = self._dispatch(method, body)
            time.sleep(self.engine.response_delay(lane))
            self._send(reply)
            return
        with self.engine.state_lock:
            self.engine.queued_requests += 1
            self.engine.max_queued_requests = max(
                self.engine.max_queued_requests, self.engine.queued_requests
            )
        with self.engine.request_lock:
            acquired = time.monotonic()
            with self.engine.state_lock:
                self.engine.queued_requests -= 1
                self.engine.physical_inflight += 1
                self.engine.max_physical_inflight = max(
                    self.engine.max_physical_inflight, self.engine.physical_inflight
                )
                self.engine.request_counter += 1
                request_id = self.engine.request_counter
            status = 0
            lane = "read"
            try:
                with self.engine.state_lock:
                    reply, lane = self._dispatch(method, body)
                time.sleep(self.engine.response_delay(lane))
                status = reply.status
                self._send(reply)
            finally:
                finished = time.monotonic()
                with self.engine.state_lock:
                    self.engine.physical_inflight -= 1
                    self.engine.record(
                        "http_request",
                        request_id=request_id,
                        method=method,
                        path=path,
                        lane=lane,
                        status=status,
                        queue_wait_ms=round((acquired - received) * 1000.0, 3),
                        elapsed_ms=round((finished - received) * 1000.0, 3),
                    )


class OpenWrtSimulatorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], engine: OpenWrtScenarioEngine) -> None:
        self.engine = engine
        super().__init__(address, _Handler)


class RunningOpenWrtSimulator:
    def __init__(self, server: OpenWrtSimulatorServer) -> None:
        self.server = server
        self.thread = threading.Thread(
            target=server.serve_forever,
            name=f"{server.engine.dialect}-simulator",
            daemon=True,
        )

    @property
    def host(self) -> str:
        return str(self.server.server_address[0])

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def engine(self) -> OpenWrtScenarioEngine:
        return self.server.engine

    def start(self) -> "RunningOpenWrtSimulator":
        self.thread.start()
        return self

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3.0)

    def __enter__(self) -> "RunningOpenWrtSimulator":
        return self.start()

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


def validate_scenario(value: Mapping[str, Any]) -> dict[str, Any]:
    data = json.loads(json.dumps(dict(value or {})))
    if "script_contract_strict" in data:
        raise ValueError("openwrt_simulator_browser_session_contract_retired")
    if data.get("schema") != SCENARIO_SCHEMA:
        raise ValueError("openwrt_simulator_scenario_schema_invalid")
    if data.get("dialect") not in SUPPORTED_DIALECTS:
        raise ValueError("openwrt_simulator_dialect_invalid")
    for key in ("name", "evidence", "identity", "authentication", "timing", "radio", "traffic"):
        if not data.get(key):
            raise ValueError(f"openwrt_simulator_scenario_{key}_required")
    if data["evidence"].get("physical_certification") is not False:
        raise ValueError("openwrt_simulator_physical_certification_must_be_false")
    identity = data["identity"]
    for key in (
        "manufacturer", "model", "router_model", "hardware_version",
        "firmware", "openwrt_version", "serial", "imei", "imsi", "iccid",
    ):
        if key not in identity:
            raise ValueError(f"openwrt_simulator_identity_{key}_required")
    for key in (
        "homepage_response_s", "read_response_s", "login_response_s",
        "write_ack_s", "control_response_s", "lte_reattach_s", "reboot_downtime_s",
    ):
        if _as_float(data["timing"].get(key), -1.0) < 0.0:
            raise ValueError(f"openwrt_simulator_timing_{key}_invalid")
    return data


def load_scenario(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    with source.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise ValueError("openwrt_simulator_scenario_mapping_required")
    return validate_scenario(value)


def create_simulator(
    scenario: Mapping[str, Any],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    password: str | None = None,
    control_token: str = "openwrt-lab",
    time_scale: float = 1.0,
    event_log: str | Path | None = None,
) -> RunningOpenWrtSimulator:
    bind_host = str(host or "127.0.0.1").strip()
    if bind_host not in {"127.0.0.1", "localhost"}:
        raise ValueError("openwrt_simulator_loopback_bind_required")
    engine = OpenWrtScenarioEngine(
        scenario,
        password=password,
        control_token=control_token,
        time_scale=time_scale,
        event_log=event_log,
    )
    return RunningOpenWrtSimulator(OpenWrtSimulatorServer((bind_host, int(port)), engine))


__all__ = [
    "CONTROL_HEADER",
    "OpenWrtScenarioEngine",
    "RunningOpenWrtSimulator",
    "SCENARIO_SCHEMA",
    "STANDARD_DIALECT",
    "ZWRT_DIALECT",
    "create_simulator",
    "load_scenario",
    "validate_scenario",
]
