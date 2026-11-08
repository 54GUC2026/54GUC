"""
Universal modem model / emulator backend.

This backend lets LUC run against a synthetic modem profile that speaks the
same shared brain/system vocabulary as the real modem backends.
"""

from __future__ import annotations

from arch4.modem.utils import RttProbeResult

import copy
import csv
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from arch4.interfaces import (
    CarrierInfo,
    LeverAction,
    LeverResult,
    LeverType,
    ModemCapabilities,
    ModemInput,
    NeighborCell,
    band_signature,
    normalize_band_list,
)
from arch4.control.band_resolver import auto_band_release_rejection_reason, is_auto_band_release
from arch4.modem.base import LiveAcquisitionContract, ModemInterface, build_public_modem_status
from arch4.modem.backend_acquisition import (
    BackendAcquisitionReceipt,
    BackendAcquisitionRequest,
    DECLARED_CANONICAL_SOURCE_FIELDS,
    matrix_cells_for_canonical_fields,
)
from arch4.core_contracts.command_targets import (
    CoreBandAutoTarget,
    CoreBandSetTarget,
    CoreCellLockTarget,
    CoreNrBandTarget,
    CorePBandTarget,
    CoreRestartTarget,
)
from arch4.modem_live.physical_command import (
    ModemLivePhysicalStagePlan,
    ModemLivePhysicalStageResult,
)


log = logging.getLogger("arch4.modem.model")
REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = REPO_ROOT / "models" / "modem_profiles"


BUILTIN_PROFILE: Dict[str, Any] = {
    "identity": {
        "vendor": "Synthetic",
        "model": "54GUC Model",
        "router_model": "54GUC Model",
        "hardware_version": "EMU-1",
        "software": "",
        "software_version": "1.0",
        "firmware": "1.0",
        "webui": "",
        "serial": "EMU0001",
        "imei": "000000000000000",
        "imsi": "000000000000000",
        "lan_mac": "00:11:22:33:44:55",
        "wan_ip": "100.64.0.10",
        "antenna": "External",
    },
    "provider_name": "Lab",
    "provider_plmn": "99999",
    "static_config": {
        "ttl_ipv4": True,
        "ttl_ipv6": True,
        "mss_ipv4": True,
        "mss_ipv6": True,
        "keepalive": True,
    },
    "ue_capability": {
        "dl_256qam": "1",
        "ul_64qam": "1",
        "pci_lock_available": True,
        "ue_category": 19,
    },
    "capabilities": {
        "supports_band_selection": True,
        "supports_cell_lock": True,
        "supports_traffic_presence": True,
        "supports_traffic_volume": True,
        "supports_ue_capability": True,
        "supports_restart": True,
        "supports_neighbors": True,
        "supports_ca_details": True,
        "supports_band_priority": True,
        "supports_plmn": True,
    },
    "timing": {
        "band_settle_s": 15.0,
        "restart_downtime_s": 20.0,
    },
    "band_priority": [3, 1, 20],
    "state": {
        "network_type": "LTE (4G)",
        "radio_connected": True,
        "selected_band_set": [1, 3, 20],
        "active_band_set": [1, 3, 20],
        "serving_band": 3,
        "ca_active": True,
        "pci": 303,
        "earfcn": 1300,
        "cell_id": 123456,
        "tac": 3456,
        "sinr_db": 10.0,
        "rsrp_dbm": -78.0,
        "rsrq_db": -9.0,
        "rssi_dbm": -55.0,
        "mcs_dl": 18,
        "mcs_ul": 12,
        "cqi": 9,
        "ri": 2,
        "qm_dl0": 6,
        "qm_dl1": 6,
        "qm_ul": 4,
        "tbs_dl0": 32000,
        "tbs_dl1": 28000,
        "tbs_ul": 14000,
        "rx_mbps": 28.0,
        "tx_mbps": 5.0,
        "rtt_ms": 32.0,
        "session_uptime_s": 600.0,
        "neighbors": [
            {"band": 1, "earfcn": 150, "pci": 301, "rsrp_dbm": -92.0, "rsrq_db": -11.0},
            {"band": 20, "earfcn": 6300, "pci": 302, "rsrp_dbm": -95.0, "rsrq_db": -12.0},
        ],
        "serving_cell": {
            "tx_power": 12.0,
            "cqi0": 9,
            "cqi1": 8,
        },
    },
    "band_profiles": {
        "B3": {
            "selected_band_set": [3],
            "active_band_set": [3],
            "serving_band": 3,
            "ca_active": False,
            "network_type": "LTE (4G)",
            "sinr_db": 12.0,
            "rsrp_dbm": -80.0,
            "rsrq_db": -7.0,
            "rssi_dbm": -57.0,
            "rx_mbps": 18.0,
            "tx_mbps": 4.0,
            "rtt_ms": 28.0,
        },
        "B20": {
            "selected_band_set": [20],
            "active_band_set": [20],
            "serving_band": 20,
            "ca_active": False,
            "network_type": "LTE (4G)",
            "sinr_db": 4.0,
            "rsrp_dbm": -98.0,
            "rsrq_db": -14.0,
            "rssi_dbm": -72.0,
            "rx_mbps": 6.0,
            "tx_mbps": 1.5,
            "rtt_ms": 65.0,
        },
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _clone_profile(data: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(data)


def _ordered_band_list(values: Any) -> List[int]:
    if not isinstance(values, list):
        return []
    out: List[int] = []
    for raw in values:
        try:
            band = int(raw or 0)
        except Exception:
            band = 0
        if band > 0 and band not in out:
            out.append(band)
    return out


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "connected"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disconnected"}:
        return False
    return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _canonical_network_type(value: Any, *, radio_connected: bool, ca_active: bool) -> str:
    text = str(value or "").strip()
    normalized = " ".join(text.replace("_", " ").replace("-", " ").split()).upper()
    if not bool(radio_connected):
        return "No Service"
    if not normalized:
        return "LTE (4G)"
    if normalized in {"NO SERVICE", "NONE", "DISCONNECTED", "OFFLINE"}:
        return "No Service"
    if "NSA" in normalized or ("5G" in normalized and "SA" not in normalized):
        return "NR NSA (5G)"
    if "SA" in normalized and ("NR" in normalized or "5G" in normalized):
        return "NR SA (5G)"
    if "LTE" in normalized or "4G" in normalized:
        return "LTE (4G)"
    return "LTE (4G)"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _parse_band_list(value: Any) -> List[int]:
    if isinstance(value, list):
        return normalize_band_list(value)
    if isinstance(value, tuple):
        return normalize_band_list(list(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return normalize_band_list(parsed)
        parts = text.replace("+", ",").split(",")
        values: List[int] = []
        for part in parts:
            digits = "".join(ch for ch in str(part).upper() if ch.isdigit())
            if digits:
                values.append(int(digits))
        return normalize_band_list(values)
    if value is None:
        return []
    return normalize_band_list([value])


class ModemModelBackend(ModemInterface):
    def __init__(self, profile_name: str = "", profile_path: str = ""):
        self._profile_name = str(profile_name or "").strip()
        self._profile_path = str(profile_path or "").strip()
        self._profile_base_dir = PROFILE_DIR
        self._profile = self._load_profile(self._profile_name, self._profile_path)
        self._connected = False
        self._started_at = time.time()
        self._session_started_at = self._started_at
        self._selected_band_set = normalize_band_list(
            self._profile.get("state", {}).get("selected_band_set")
        )
        self._replay_config = dict(self._profile.get("replay") or {})
        self._replay_frames = self._load_replay_frames()
        self._replay_duration_s = float(self._replay_frames[-1]["t"]) if self._replay_frames else 0.0
        self._band_priority = _ordered_band_list(self._profile.get("band_priority"))
        self._static_config = dict(self._profile.get("static_config") or {})
        self._ue_capability = dict(self._profile.get("ue_capability") or {})
        self._cell_lock: Dict[str, int] = {}
        self._restart_until = 0.0
        self._last_status: Dict[str, Any] = {}
        self._last_state: Dict[str, Any] = {}

    def _model_acquired_cells(self) -> frozenset[str]:
        """Declare simulator source facts without inspecting ModemInput defaults."""

        state_fields = set(self._last_state).intersection(
            DECLARED_CANONICAL_SOURCE_FIELDS
        )
        identity_fields = {
            str(name)
            for name, value in self._identity().items()
            if name in DECLARED_CANONICAL_SOURCE_FIELDS
            and value not in (None, "")
        }
        derived_fields: set[str] = set()
        if "radio_connected" in self._last_state:
            derived_fields.add("radio_state_text")
        if "ca_active" in self._last_state:
            derived_fields.add("ca_state_text")
        if "serving_band" in self._last_state:
            derived_fields.update(
                {
                    "band",
                    "serving_band",
                    "serving_band_signature",
                }
            )
        if "active_band_set" in self._last_state:
            derived_fields.update(
                {
                    "active_band_set",
                    "active_band_set_signature",
                    "band_set",
                    "band_set_signature",
                    "ca_signature",
                    "carrier_count",
                }
            )
        if "cell_id" in self._last_state:
            derived_fields.add("enb_id")
        if "earfcn" in self._last_state:
            derived_fields.add("earfcn_dl")
        if "rx_mbps" in self._last_state:
            derived_fields.add("rx_bytes_per_sec")
        if "tx_mbps" in self._last_state:
            derived_fields.add("tx_bytes_per_sec")
        if "nr5g_active" in self._last_state:
            derived_fields.update({"nsa_active", "sa_active"})
        if self._profile.get("provider_name") not in (None, ""):
            derived_fields.add("provider_name")
        if self._profile.get("provider_plmn") not in (None, ""):
            derived_fields.add("provider_plmn")
        return matrix_cells_for_canonical_fields(
            frozenset(
                state_fields | identity_fields | derived_fields
            )
        )

    def acquire_modem_input(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "startup_full":
            raise ValueError(
                "model_backend_startup_full_acquisition_lane_required"
            )
        modem = self.read_modem_input()
        return self._capture_backend_acquisition_receipt(
            request=request,
            modem=modem,
            acquired_cells=(
                self._model_acquired_cells()
                if modem is not None
                else frozenset()
            ),
            failure_reason="model_backend_startup_acquisition_unavailable",
        )

    def acquire_actionable_modem_input(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "fast_live":
            raise ValueError(
                "model_backend_fast_live_acquisition_lane_required"
            )
        modem = self.read_modem_input()
        return self._capture_backend_acquisition_receipt(
            request=request,
            modem=modem,
            acquired_cells=(
                self._model_acquired_cells()
                if modem is not None
                else frozenset()
            ),
            failure_reason="model_backend_actionable_acquisition_unavailable",
        )

    def connect(self) -> bool:
        self._connected = True
        log.info("Connected to modem model profile %s", self._profile_label())
        return True

    def disconnect(self) -> None:
        self._connected = False
        log.info("Disconnected from modem model profile %s", self._profile_label())

    def is_connected(self) -> bool:
        return bool(self._connected)

    def read_modem_input(self) -> Optional[ModemInput]:
        if not self._connected:
            return None
        state = self._compose_state(time.time())
        self._last_state = dict(state)
        carriers = self._build_carriers(state)
        neighbors = self._build_neighbors(state)
        ts = time.time()
        selected_band_set = normalize_band_list(state.get("selected_band_set"))
        active_band_set = normalize_band_list(state.get("active_band_set"))
        serving_band = int(state.get("serving_band") or (active_band_set[0] if active_band_set else 0))
        ca_active = bool(state.get("ca_active", len(active_band_set) > 1))
        ca_signature = band_signature(active_band_set) or (f"B{serving_band}" if serving_band else "")
        session_uptime = float(state.get("session_uptime_s") or 0.0)
        if bool(state.get("radio_connected", True)):
            session_uptime = max(session_uptime, ts - self._session_started_at)

        self._last_status = {
            "ts": ts,
            "provider_name": self._profile.get("provider_name"),
            "provider_plmn": self._profile.get("provider_plmn"),
            "provider": self._profile.get("provider_name"),
            "plmn": self._profile.get("provider_plmn"),
            "network_provider_fullname": state.get("network_provider_fullname") or self._profile.get("provider_name"),
            "network_type": state.get("network_type"),
            "radio_connected": bool(state.get("radio_connected", True)),
            "radio_state_text": "Connected" if bool(state.get("radio_connected", True)) else "Disconnected",
            "ca_active": ca_active,
            "ca_state_text": "ACTIVE" if ca_active else "INACTIVE",
            "band": serving_band or None,
            "serving_band": serving_band or None,
            "serving_band_signature": f"B{serving_band}" if serving_band else "",
            "selected_band_set": selected_band_set,
            "selected_band_set_signature": band_signature(selected_band_set),
            "active_band_set": active_band_set,
            "active_band_set_signature": band_signature(active_band_set),
            "band_set": active_band_set or selected_band_set or ([serving_band] if serving_band else []),
            "band_set_signature": band_signature(active_band_set or selected_band_set or ([serving_band] if serving_band else [])),
            "ca_signature": ca_signature,
            "pci": int(state.get("pci") or 0) or None,
            "earfcn_dl": int(state.get("earfcn") or 0) or None,
            "earfcn_ul": int(state.get("earfcn_ul") or 0) or None,
            "enb_id": int((state.get("cell_id") or 0) // 256) if int(state.get("cell_id") or 0) > 255 else None,
            "cell_id": int(state.get("cell_id") or 0) or None,
            "tac": int(state.get("tac") or 0) or None,
            "carrier_count": len(active_band_set) or (1 if serving_band else 0),
            "mcs_dl0": int(state.get("mcs_dl0") or state.get("mcs_dl") or 0),
            "mcs_dl1": int(state.get("mcs_dl1") or 0),
            "mcs_ul": int(state.get("mcs_ul") or 0),
            "mcs_ul_supported": True,
            "tbs_dl0": int(state.get("tbs_dl0") or 0),
            "tbs_dl1": int(state.get("tbs_dl1") or 0),
            "tbs_ul": int(state.get("tbs_ul") or 0),
            "qm_dl0": int(state.get("qm_dl0") or 0),
            "qm_dl1": int(state.get("qm_dl1") or 0),
            "qm_ul": int(state.get("qm_ul") or 0),
            "cqi": int(state.get("cqi") or 0),
            "cqi0": int(state.get("serving_cell", {}).get("cqi0") or state.get("cqi") or 0),
            "cqi1": int(state.get("serving_cell", {}).get("cqi1") or 0),
            "ri": int(state.get("ri") or 0),
            "tx_power": float(state.get("serving_cell", {}).get("tx_power") or 0.0),
            "session_uptime_s": session_uptime,
            "rtt_ms": float(state.get("rtt_ms") or 0.0),
            "model": self._identity().get("model"),
            "router_model": self._identity().get("router_model"),
            "hardware_version": self._identity().get("hardware_version"),
            "software": self._identity().get("software") or self._identity().get("software_version"),
            "software_version": self._identity().get("software_version"),
            "webui": self._identity().get("webui"),
            "firmware": self._identity().get("firmware"),
            "config_version": self._identity().get("configuration_file_version"),
            "configuration_file_version": self._identity().get("configuration_file_version"),
            "serial": self._identity().get("serial"),
            "imsi": self._identity().get("imsi"),
            "wan_ip": self._identity().get("wan_ip"),
            "antenna": self._identity().get("antenna"),
            "imei": self._identity().get("imei"),
            "lan_mac": self._identity().get("lan_mac"),
            "neighbors": state.get("neighbors") or [],
            "lte_cells": state.get("lte_cells") or [],
            "nr5g_active": bool(state.get("nr5g_active", False)),
            "nr5g_mode": state.get("nr5g_mode") or None,
            "nr5g_state_text": state.get("nr5g_state_text") or None,
            "nr5g_cell_id": int(state.get("nr5g_cell_id") or 0) or None,
            "nr5g_pci": int(state.get("nr5g_pci") or 0) or None,
            "nr5g_arfcn": int(state.get("nr5g_arfcn") or 0) or None,
            "nr5g_band": int(state.get("nr5g_band") or 0) or None,
            "nr5g_band_set": normalize_band_list(state.get("nr5g_band_set")),
            "nr5g_rsrp_dbm": _as_float(state.get("nr5g_rsrp_dbm"), 0.0) or None,
            "nr5g_rsrq_db": _as_float(state.get("nr5g_rsrq_db"), 0.0) or None,
            "nr5g_sinr_db": _as_float(state.get("nr5g_sinr_db"), 0.0) or None,
            "nr5g_rx0_rsrp_dbm": _as_float(state.get("nr5g_rx0_rsrp_dbm"), 0.0) or None,
            "nr5g_rx1_rsrp_dbm": _as_float(state.get("nr5g_rx1_rsrp_dbm"), 0.0) or None,
            "nr5g_cells": state.get("nr5g_cells") or [],
            "nr5g_sa_band_lock": state.get("nr5g_sa_band_lock") or None,
            "nr5g_nsa_band_lock": state.get("nr5g_nsa_band_lock") or None,
            "temperature_ambient_c": _as_float(state.get("temperature_ambient_c"), 0.0) or None,
            "temperature_modem_4g_c": _as_float(state.get("temperature_modem_4g_c"), 0.0) or None,
            "temperature_modem_5g_c": _as_float(state.get("temperature_modem_5g_c"), 0.0) or None,
            "temperature_pa1_c": _as_float(state.get("temperature_pa1_c"), 0.0) or None,
            "temperature_wifi_c": _as_float(state.get("temperature_wifi_c"), 0.0) or None,
            "lte_interface_disabled": False,
        }

        return ModemInput(
            sinr_db=float(state.get("sinr_db") or 0.0),
            rsrp_dbm=float(state.get("rsrp_dbm") or 0.0),
            rsrq_db=float(state.get("rsrq_db") or 0.0),
            rssi_dbm=float(state.get("rssi_dbm") or 0.0),
            mcs_dl=int(state.get("mcs_dl") or state.get("mcs_dl0") or 0),
            mcs_ul=int(state.get("mcs_ul") or 0),
            cqi=int(state.get("cqi") or 0),
            ri=int(state.get("ri") or 0),
            carriers=carriers,
            ca_active=ca_active,
            ca_band_signature=ca_signature,
            primary_band=serving_band,
            pci=int(state.get("pci") or 0),
            earfcn=int(state.get("earfcn") or 0),
            enb_id=int((state.get("cell_id") or 0) // 256) if int(state.get("cell_id") or 0) > 255 else 0,
            cell_id=int(state.get("cell_id") or 0),
            radio_connected=bool(state.get("radio_connected", True)),
            session_uptime_s=session_uptime,
            rx_bytes_per_sec=float(state.get("rx_mbps") or 0.0) * 1_000_000 / 8.0,
            tx_bytes_per_sec=float(state.get("tx_mbps") or 0.0) * 1_000_000 / 8.0,
            selected_band_set=selected_band_set,
            neighbors=neighbors,
            timestamp=ts,
            mcs_dl0=int(state.get("mcs_dl0") or state.get("mcs_dl") or 0),
            mcs_dl1=int(state.get("mcs_dl1") or 0),
            mod0=int(state.get("qm_dl0") or 0),
            mod1=int(state.get("qm_dl1") or 0),
            qam_dl_primary=int(state.get("qm_dl0") or 0),
            qam_dl_secondary=int(state.get("qm_dl1") or 0),
            qam_ul=int(state.get("qm_ul") or 0),
            qam_source="profile",
            qam_confidence=1.0,
            nsa_active=bool(state.get("nr5g_active") and state.get("nr5g_mode") == "NSA"),
            sa_active=bool(state.get("nr5g_active") and state.get("nr5g_mode") == "SA"),
            nr_mode=str(state.get("nr5g_mode") or ""),
            nr_state_text=str(state.get("nr5g_state_text") or ""),
            nr_cells=list(state.get("nr5g_cells") or []),
            nr_band=int(state.get("nr5g_band") or 0),
            nr_pci=int(state.get("nr5g_pci") or 0),
            nr_arfcn=int(state.get("nr5g_arfcn") or 0),
            nr_cell_id=int(state.get("nr5g_cell_id") or 0),
            nr_rsrp_dbm=float(state.get("nr5g_rsrp_dbm") or 0.0),
            nr_sinr_db=float(state.get("nr5g_sinr_db") or 0.0),
            nr_rsrq_db=float(state.get("nr5g_rsrq_db") or 0.0),
        )

    def read_neighbors(self) -> List[NeighborCell]:
        if not self._last_state:
            self.read_modem_input()
        return self._build_neighbors(self._last_state)

    def get_live_acquisition_contract(self) -> LiveAcquisitionContract:
        return LiveAcquisitionContract(
            acquisition_class="aligned_single_read",
            profile_name="model_backend_simulated",
            has_explicit_actionable_snapshot=False,
            has_explicit_maintenance_refresh=False,
            rtt_holds_io_lock=False,
            rtt_min_interval_s=1.0,
        )

    def measure_rtt(
        self,
        target: str = "8.8.8.8",
        count: int = 3,
        *,
        timeout_s: float,
    ) -> RttProbeResult:
        del count, timeout_s
        state = self._last_state or self._compose_state(time.time())
        value = state.get("rtt_ms")
        return RttProbeResult(
            float(value) if value is not None else None, target,
            reason="model_unreachable" if value is None else "",
            status="failed" if value is None else "ok",
        )

    def execute_physical_stage(
        self,
        plan: ModemLivePhysicalStagePlan,
    ) -> ModemLivePhysicalStageResult:
        if not isinstance(plan, ModemLivePhysicalStagePlan):
            raise TypeError("model_backend_physical_stage_plan_required")
        target = plan.target
        caps = self.get_capabilities()
        started = time.time()
        accepted = False
        reason = "model_backend_target_unsupported"
        recovery_time_s = 0.0
        if isinstance(target, CoreBandSetTarget):
            if not caps.supports_band_selection:
                raise RuntimeError("model_band_selection_unsupported")
            accepted = True
            self._selected_band_set = list(target.bands)
            self._session_started_at = time.time()
            recovery_time_s = float(self._timing().get("band_settle_s", 0.0))
            reason = "accepted"
        elif isinstance(target, CoreBandAutoTarget):
            if not caps.supports_band_selection:
                raise RuntimeError("model_band_auto_unsupported")
            accepted = True
            self._selected_band_set = []
            self._session_started_at = time.time()
            recovery_time_s = float(self._timing().get("band_settle_s", 0.0))
            reason = "accepted"
        elif isinstance(target, CorePBandTarget):
            if not caps.supports_band_selection:
                raise RuntimeError("model_pband_unsupported")
            accepted = True
            self._selected_band_set = [target.pband] if target.pband > 0 else []
            self._session_started_at = time.time()
            recovery_time_s = float(self._timing().get("band_settle_s", 0.0))
            reason = "accepted"
        elif isinstance(target, CoreCellLockTarget):
            if not caps.supports_cell_lock:
                raise RuntimeError("model_cell_lock_unsupported")
            accepted = True
            self._cell_lock = (
                {}
                if target.unlock
                else {"pci": target.pci, "earfcn": target.earfcn}
            )
            reason = "accepted"
        elif isinstance(target, CoreRestartTarget):
            accepted = self._execute_typed_restart()
            recovery_time_s = (
                float(self._timing().get("restart_downtime_s", 0.0))
                if accepted
                else 0.0
            )
            reason = "accepted" if accepted else "restart_unsupported"
        elif isinstance(target, CoreNrBandTarget):
            accepted = True
            reason = "accepted"
        else:
            raise RuntimeError("model_backend_typed_target_unsupported")
        returned = time.time()
        return ModemLivePhysicalStageResult(
            plan=plan,
            request_dispatched_at=started,
            transport_returned_at=returned,
            outcome="accepted" if accepted else "rejected",
            reason=reason,
            radio_drop_detected=False,
            recovery_time_s=recovery_time_s,
            detail={"transport": "in_process_model"},
        )

    def execute_lever(self, action: LeverAction) -> LeverResult:
        del action
        raise RuntimeError("model_backend_generic_execute_lever_forbidden")

    def verify_static_config(self) -> Dict[str, bool]:
        return dict(self._static_config)

    def apply_static_config(self, item: str) -> bool:
        del item
        raise RuntimeError("model_backend_direct_static_config_write_forbidden")

    def verify_ue_capability(self) -> Dict[str, Any]:
        if not self.get_capabilities().supports_ue_capability:
            return {}
        return dict(self._ue_capability)

    def set_ue_capability(self, key: str, value: Any) -> bool:
        del key, value
        raise RuntimeError("model_backend_direct_ue_capability_write_forbidden")

    def restart_modem(self) -> bool:
        raise RuntimeError("model_backend_direct_restart_write_forbidden")

    def _execute_typed_restart(self) -> bool:
        if not self.get_capabilities().supports_restart:
            return False
        self._restart_until = time.time() + float(self._timing().get("restart_downtime_s", 0.0))
        self._session_started_at = self._restart_until
        return True

    def read_fresh_device_identity(self) -> Dict[str, str]:
        return self.get_modem_info()

    def get_modem_info(self) -> Dict[str, str]:
        identity = self._identity()
        return {
            "model": str(identity.get("model") or ""),
            "router_model": str(identity.get("router_model") or identity.get("model") or ""),
            "hardware_version": str(identity.get("hardware_version") or ""),
            "software_version": str(identity.get("software_version") or ""),
            "firmware": str(identity.get("firmware") or ""),
            "webui": str(identity.get("webui") or ""),
            "imei": str(identity.get("imei") or ""),
            "imsi": str(identity.get("imsi") or ""),
            "serial": str(identity.get("serial") or ""),
        }

    def get_current_bands(self) -> List[int]:
        return list(self._selected_band_set)

    def get_capabilities(self):
        caps = dict(self._profile.get("capabilities") or {})
        return ModemCapabilities(**caps)

    def get_last_status(self) -> Dict[str, Any]:
        return build_public_modem_status(self._last_status)

    def scan_neighbors(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for cell in self._last_state.get("neighbors") or []:
            out.append(
                {
                    "band": int(cell.get("band") or 0),
                    "earfcn": int(cell.get("earfcn") or 0),
                    "pci": int(cell.get("pci") or 0),
                    "rsrp": float(cell.get("rsrp_dbm") or 0.0),
                    "rsrq": float(cell.get("rsrq_db") or 0.0),
                }
            )
        return out

    def read_serving_cell(self) -> Dict[str, Any]:
        state = self._last_state or self._compose_state(time.time())
        serving = dict(state.get("serving_cell") or {})
        serving.setdefault("band", state.get("serving_band"))
        serving.setdefault("pci", state.get("pci"))
        serving.setdefault("earfcn", state.get("earfcn"))
        serving.setdefault("cell_id", state.get("cell_id"))
        serving.setdefault("network_type", state.get("network_type"))
        serving.setdefault("ca_active", state.get("ca_active"))
        return serving

    def read_band_priority(self) -> List[int]:
        if not self.get_capabilities().supports_band_priority:
            return []
        return list(self._band_priority)

    def set_band_priority(self, bands: List[int]) -> bool:
        del bands
        raise RuntimeError("model_backend_direct_band_priority_write_forbidden")

    def _load_profile(self, profile_name: str, profile_path: str) -> Dict[str, Any]:
        if profile_path:
            path = Path(profile_path)
            self._profile_base_dir = path.resolve().parent if path.exists() else path.parent
            return self._load_profile_file(path)
        if profile_name:
            candidate = PROFILE_DIR / f"{profile_name}.json"
            if candidate.exists():
                self._profile_base_dir = candidate.parent
                return self._load_profile_file(candidate)
        self._profile_base_dir = PROFILE_DIR
        return _clone_profile(BUILTIN_PROFILE)

    def _load_profile_file(self, path: Path) -> Dict[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Model profile must be a JSON object: {path}")
        return _deep_merge(BUILTIN_PROFILE, data)

    def _profile_label(self) -> str:
        return self._profile_path or self._profile_name or "built-in"

    def _identity(self) -> Dict[str, Any]:
        return dict(self._profile.get("identity") or {})

    def _timing(self) -> Dict[str, Any]:
        return dict(self._profile.get("timing") or {})

    def _compose_state(self, now: float) -> Dict[str, Any]:
        base = dict(self._profile.get("state") or {})
        replay_override = self._replay_override(now)
        if replay_override:
            base = _deep_merge(base, replay_override)

        timeline_override = self._timeline_override(now)
        if timeline_override:
            base = _deep_merge(base, timeline_override)

        selected = self._selected_band_set or normalize_band_list(base.get("selected_band_set"))
        if selected:
            base["selected_band_set"] = list(selected)
        profile_sig = band_signature(selected)
        band_profiles = self._profile.get("band_profiles") or {}
        if profile_sig and isinstance(band_profiles, dict):
            selected_override = band_profiles.get(profile_sig) or band_profiles.get("default")
            if isinstance(selected_override, dict):
                base = _deep_merge(base, selected_override)

        anomaly_override = self._anomaly_override(now)
        if anomaly_override:
            base = _deep_merge(base, anomaly_override)

        if now < self._restart_until:
            base = _deep_merge(
                base,
                {
                    "radio_connected": False,
                    "network_type": "No Service",
                    "active_band_set": [],
                    "ca_active": False,
                    "serving_band": 0,
                    "rx_mbps": 0.0,
                    "tx_mbps": 0.0,
                    "rtt_ms": None,
                    "session_uptime_s": 0.0,
                },
            )

        if self._cell_lock:
            if self._cell_lock.get("pci"):
                base["pci"] = self._cell_lock["pci"]
            if self._cell_lock.get("earfcn"):
                base["earfcn"] = self._cell_lock["earfcn"]

        active = normalize_band_list(base.get("active_band_set"))
        serving = int(base.get("serving_band") or (active[0] if active else 0))
        if not active and serving > 0:
            active = [serving]
        if serving <= 0 and active:
            serving = active[0]
        base["active_band_set"] = active
        base["serving_band"] = serving
        base["ca_active"] = bool(base.get("ca_active", len(active) > 1))
        if not base.get("network_type"):
            if base.get("nr5g_active") and base.get("nr5g_mode") == "NSA":
                base["network_type"] = "NR NSA (5G)"
            elif base.get("nr5g_active") and base.get("nr5g_mode") == "SA":
                base["network_type"] = "NR SA (5G)"
            else:
                base["network_type"] = "LTE (4G)"
        base["network_type"] = _canonical_network_type(
            base.get("network_type"),
            radio_connected=bool(base.get("radio_connected", True)),
            ca_active=bool(base.get("ca_active", False)),
        )
        if base.get("radio_connected", True):
            base["session_uptime_s"] = max(float(base.get("session_uptime_s") or 0.0), now - self._session_started_at)
        else:
            base["session_uptime_s"] = 0.0
        return base

    def _resolve_profile_resource(self, raw_path: str) -> Optional[Path]:
        text = str(raw_path or "").strip()
        if not text:
            return None
        path = Path(text)
        if path.is_absolute():
            return path
        candidates = [
            self._profile_base_dir / path,
            PROFILE_DIR / path,
            REPO_ROOT / path,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    def _load_replay_frames(self) -> List[Dict[str, Any]]:
        replay = self._replay_config
        source = str(replay.get("source") or "").strip()
        if not source:
            return []
        path = self._resolve_profile_resource(source)
        if path is None or not path.exists():
            log.warning("Model replay source not found: %s", source)
            return []
        fmt = str(replay.get("format") or path.suffix.lstrip(".") or "auto").strip().lower()
        rows = self._read_replay_rows(path, fmt)
        max_rows = _as_int(replay.get("max_rows"), 0)
        if max_rows > 0 and len(rows) > max_rows:
            rows = rows[-max_rows:]
        frames: List[Dict[str, Any]] = []
        first_ts: Optional[float] = None
        for idx, row in enumerate(rows):
            state = self._replay_row_to_state(row)
            if not state:
                continue
            ts = self._replay_row_timestamp(row, idx)
            if first_ts is None:
                first_ts = ts
            rel_s = max(0.0, ts - float(first_ts or 0.0))
            frames.append({"t": rel_s, "state": state})
        if frames:
            log.info("Loaded %d replay frames for modem model profile %s", len(frames), self._profile_label())
        return frames

    def _read_replay_rows(self, path: Path, fmt: str) -> List[Dict[str, Any]]:
        selected_fmt = fmt
        if selected_fmt == "auto":
            selected_fmt = path.suffix.lstrip(".").lower()
        if selected_fmt in {"jsonl", "json"}:
            rows: List[Dict[str, Any]] = []
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    payload = json.loads(line)
                    if isinstance(payload, dict):
                        rows.append(payload)
            return rows
        if selected_fmt == "csv":
            with open(path, "r", encoding="utf-8", errors="ignore", newline="") as handle:
                return [dict(row) for row in csv.DictReader(handle)]
        raise ValueError(f"Unsupported replay format for model profile: {path}")

    def _replay_row_timestamp(self, row: Dict[str, Any], idx: int) -> float:
        if "ts" in row:
            return _as_float(row.get("ts"), float(idx))
        if "timestamp" in row:
            return _as_float(row.get("timestamp"), float(idx))
        return float(idx)

    def _replay_row_to_state(self, row: Dict[str, Any]) -> Dict[str, Any]:
        if str(row.get("record_type") or "tick") not in {"tick", "status", ""}:
            return {}
        selected = (
            _parse_band_list(row.get("selected_band_set"))
            or _parse_band_list(row.get("band_set"))
            or _parse_band_list(row.get("selected_band_set_signature"))
            or _parse_band_list(row.get("band_set_signature"))
        )
        serving_band = _as_int(
            row.get("serving_band", row.get("primary_band", row.get("band", 0))),
            0,
        )
        active = (
            _parse_band_list(row.get("active_band_set"))
            or _parse_band_list(row.get("active_band_set_signature"))
            or ([serving_band] if serving_band > 0 else [])
        )
        if not selected:
            selected = list(active) or ([serving_band] if serving_band > 0 else [])
        ca_active = _as_bool(row.get("ca_active"), len(active) > 1)
        radio_connected = _as_bool(
            row.get("radio_connected", row.get("radio_state_text", True)),
            True,
        )
        state: Dict[str, Any] = {
            "radio_connected": radio_connected,
            "selected_band_set": selected,
            "active_band_set": active,
            "serving_band": serving_band,
            "ca_active": ca_active,
            "network_type": _canonical_network_type(
                row.get("network_type"),
                radio_connected=radio_connected,
                ca_active=ca_active,
            ),
            "pci": _as_int(row.get("pci"), 0),
            "earfcn": _as_int(row.get("earfcn"), 0),
            "sinr_db": _as_float(row.get("sinr_db"), 0.0),
            "rsrp_dbm": _as_float(row.get("rsrp_dbm"), 0.0),
            "rsrq_db": _as_float(row.get("rsrq_db"), 0.0),
            "rssi_dbm": _as_float(row.get("rssi_dbm"), 0.0),
            "mcs_dl": _as_int(row.get("mcs_dl"), 0),
            "mcs_ul": _as_int(row.get("mcs_ul"), 0),
            "cqi": _as_int(row.get("cqi"), 0),
            "ri": _as_int(row.get("ri"), 0),
            "rx_mbps": _as_float(row.get("dl_mbps", row.get("rx_mbps", 0.0)), 0.0),
            "tx_mbps": _as_float(row.get("ul_mbps", row.get("tx_mbps", 0.0)), 0.0),
            "rtt_ms": _as_float(row.get("rtt_ms"), 0.0),
            "session_uptime_s": _as_float(row.get("session_uptime_s"), 0.0),
        }
        return state

    def _replay_override(self, now: float) -> Dict[str, Any]:
        if not self._replay_frames:
            return {}
        scale = max(0.001, _as_float(self._replay_config.get("time_scale"), 1.0))
        elapsed = max(0.0, (now - self._started_at) * scale)
        loop = _as_bool(self._replay_config.get("loop"), True)
        if loop and self._replay_duration_s > 0.0:
            elapsed = elapsed % self._replay_duration_s
        elif self._replay_duration_s > 0.0:
            elapsed = min(elapsed, self._replay_duration_s)
        selected_state = self._replay_frames[0]["state"]
        for frame in self._replay_frames:
            if elapsed >= float(frame.get("t") or 0.0):
                selected_state = frame["state"]
            else:
                break
        return dict(selected_state or {})

    def _anomaly_override(self, now: float) -> Dict[str, Any]:
        anomalies = self._profile.get("anomalies") or []
        if not isinstance(anomalies, list) or not anomalies:
            return {}
        elapsed = max(0.0, now - self._started_at)
        combined: Dict[str, Any] = {}
        for anomaly in anomalies:
            if not isinstance(anomaly, dict):
                continue
            start_s = _as_float(anomaly.get("start_s"), 0.0)
            duration_s = _as_float(anomaly.get("duration_s"), 0.0)
            repeat_every_s = _as_float(anomaly.get("repeat_every_s"), 0.0)
            if elapsed < start_s:
                continue
            if repeat_every_s > 0.0:
                local_elapsed = (elapsed - start_s) % repeat_every_s
                active = duration_s <= 0.0 or local_elapsed <= duration_s
            else:
                active = duration_s <= 0.0 or elapsed <= (start_s + duration_s)
            if not active:
                continue
            overrides = anomaly.get("overrides")
            if isinstance(overrides, dict):
                combined = _deep_merge(combined, overrides)
        return combined

    def _timeline_override(self, now: float) -> Dict[str, Any]:
        timeline = self._profile.get("timeline") or []
        if not isinstance(timeline, list) or not timeline:
            return {}
        total = 0.0
        for phase in timeline:
            total += max(0.0, float((phase or {}).get("duration_s") or 0.0))
        if total <= 0.0:
            return {}
        elapsed = (now - self._started_at) % total
        cursor = 0.0
        for phase in timeline:
            duration = max(0.0, float((phase or {}).get("duration_s") or 0.0))
            cursor += duration
            if elapsed <= cursor:
                overrides = phase.get("overrides") if isinstance(phase, dict) else {}
                return dict(overrides or {})
        return {}

    def _build_carriers(self, state: Dict[str, Any]) -> List[CarrierInfo]:
        active = normalize_band_list(state.get("active_band_set"))
        if not active:
            return []
        base_earfcn = int(state.get("earfcn") or 0)
        base_pci = int(state.get("pci") or 0)
        bandwidth = float(state.get("dl_bandwidth_mhz") or 20.0)
        out: List[CarrierInfo] = []
        for idx, band in enumerate(active):
            out.append(
                CarrierInfo(
                    band=band,
                    earfcn=base_earfcn + idx,
                    pci=base_pci + idx if base_pci else 0,
                    bandwidth_mhz=bandwidth,
                    sinr_db=float(state.get("sinr_db") or 0.0),
                    rsrp_dbm=float(state.get("rsrp_dbm") or 0.0),
                    state=1,
                    is_pcell=(idx == 0),
                )
            )
        return out

    def _build_neighbors(self, state: Dict[str, Any]) -> List[NeighborCell]:
        out: List[NeighborCell] = []
        for raw in state.get("neighbors") or []:
            out.append(
                NeighborCell(
                    band=int(raw.get("band") or 0),
                    earfcn=int(raw.get("earfcn") or 0),
                    pci=int(raw.get("pci") or 0),
                    rsrp_dbm=float(raw.get("rsrp_dbm") or 0.0),
                    rsrq_db=float(raw.get("rsrq_db") or 0.0),
                )
            )
        return out
