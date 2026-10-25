"""
arch4/modem/zte_zwrt.py
═══════════════════════════════════════════════════════════════
ARCH4 v1 — ZTE zwrt/ubus Modem Backend (Passive-first)

Read-only adapter for newer ZTE NG-class routers that expose their modem
state through /ubus and zwrt_* services. This keeps the transport split
honest:
  - older goform devices stay on zte_goform.py
  - newer zwrt/ubus devices use this lane

No vendor-specific names leave this module.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import math
import re
import secrets
import time
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlsplit

import requests

from arch4.interfaces import LeverAction, LeverResult, LeverType, ModemInput, get_capabilities
from arch4.modem.zte_goform import ZteGoformModem
from arch4.modem.zte_parsers import encode_zwrt_lte_mask, decode_zwrt_lte_mask
from arch4.modem.zte_zwrt_sim import active_sim
from arch4.modem.zte_zwrt_traffic import (
    decode_zwrt_traffic, ZWRT_TRAFFIC_OUTPUT_FIELDS, ZWRT_TRAFFIC_PARENT_FIELDS,
)
from arch4.modem.backend_diagnostic import ModemBackendDiagnostic
from arch4.modem.lte_frequency import lte_band_from_dl_earfcn
from arch4.modem.base import (
    AuthenticationError,
    LiveAcquisitionContract,
    ModemIdentityRequiredError,
    TargetGuardError,
)
from arch4.modem.backend_acquisition import (
    BackendAcquisitionReceipt,
    BackendAcquisitionRequest,
)
from arch4.modem.backend_acquisition_cells import SLOW_CELLS
from arch4.core_contracts.command_targets import (
    CoreMobileDataTarget,
    CoreNetworkSettingsTarget,
    CoreBandAutoTarget,
    CoreBandSetTarget,
    CoreCellLockTarget,
    CoreNrBandTarget,
    CorePBandTarget,
    CoreRadioModeTarget,
    CoreRestartTarget,
    CoreWifiRadioTarget,
)
from arch4.core_contracts.physical_commands import (
    PhysicalCommandCoordinationTarget,
    PhysicalCommandDispatchEvidence,
)
from arch4.modem_live.physical_command import (
    ModemLivePhysicalStagePlan,
    ModemLivePhysicalStageResult,
)


log = logging.getLogger("arch4.modem.zte_zwrt")


def _cpu_temperature(thermal: Dict[str, Any]) -> tuple[float | None, dict]:
    """Decode only get_cpu_temp replies; never infer a different sensor."""
    values = []
    evidence = {}
    invalid = False
    for key, value in thermal.items():
        name = re.sub(r"[^a-z0-9]", "", str(key).lower())
        if name in {"unit", "units", "temperatureunit", "cpuunit", "cputempunit", "cputemperatureunit"}:
            if len(evidence) < 16:
                evidence[str(key)[:80]] = str(value)[:120]
            if str(value).strip().lower() not in {"c", "°c", "celsius"}:
                invalid = True
            continue
        recognized = bool(re.fullmatch(
            r"(?:(?:cpu|cpuss)(?:temp|temperature)|(?:temp|temperature)(?:cpu|cpuss)?)(?:c|celsius)?",
            name,
        ))
        if "temp" in name and len(evidence) < 16:
            evidence[str(key)[:80]] = str(value)[:120]
        if not recognized:
            continue
        match = re.fullmatch(
            r"\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:°?\s*[cC]|[cC]elsius)?\s*",
            str(value),
        ) if not isinstance(value, bool) else None
        number = float(match.group(1)) if match else None
        if number is None or not math.isfinite(number) or not -100 <= number <= 200:
            invalid = True
        else:
            values.append(number)
    reason = (
        "invalid_cpu_temperature" if invalid else
        "conflicting_cpu_temperatures" if len(set(values)) > 1 else
        "cpu_temperature_not_reported" if not values else "observed"
    )
    return (values[0] if reason == "observed" else None), {
        "reason": reason, "fields": evidence,
    }

ZERO_SESSION_ID = "00000000000000000000000000000000"


class ZwrtSessionSetupError(ConnectionError):
    """Sanitized optional payload-key failure, distinct from password login."""

# Diagnostic vocabulary only. Presence does not authorize a physical write.
ZWRT_NETWORK_DISCOVERY_METHODS = frozenset({
    "nwinfo_manual_register", "nwinfo_manual_scan", "nwinfo_m_netselect_status",
    "nwinfo_m_netselect_contents", "nwinfo_m_netselect_result", "nwinfo_set_netselect",
})

# Permanent NG measurement adapter. Only NR measurements extend the shared
# record projection; CA/configuration and other requested fields are unchanged.
ZWRT_NR_MEASUREMENT_FIELDS = frozenset({
    "nr_ca_pcell_band", "nr5g_action_nsa_band", "nr5g_action_band",
    "network_Z5g_CELLINFO_band", "Z5g_CELLINFO_band",
    "nr5g_pci", "network_Z5g_PCI", "Z5g_PCI",
    "nr5g_cell_id", "Z5g_CELL_ID", "nr_ca_pcell_freq",
    "nr5g_action_channel", "Z5g_dlEarfcn", "nr5g_nsa_bandwidth",
    "5g_rx0_rsrp", "5g_rx1_rsrp", "Z5g_rsrp", "Z5g_rsrq",
    "Z5g_SINR", "Z5g_snr",
})

ZWRT_STATUS_CALLS: tuple[Dict[str, Any], ...] = (
    {"service": "zte_nwinfo_api", "method": "nwinfo_get_netinfo", "params": {}},
    {"service": "zwrt_bsp.thermal", "method": "get_cpu_temp", "params": {}},
    {"service": "zwrt_mc.device.manager", "method": "get_device_info", "params": {}},
    {"service": "zwrt_router.api", "method": "router_get_status", "params": {}},
    {
        "service": "zwrt_data",
        "method": "get_wwandst",
        "params": {"source_module": "web", "cid": 1, "type": 4},
    },
)

# Permanent metadata enrichment boundary. Never mixed into the
# primary five-service batch, and never used to retry a failed primary read.
ZWRT_METADATA_CALLS: tuple[Dict[str, Any], ...] = (
    {"service": "zwrt_zte_mdm.api", "method": "get_sim_info", "params": {}},
    {"service": "network.interface.zte_wan", "method": "status", "params": {}},
    {
        "service": "uci",
        "method": "get",
        "params": {"config": "zwrt_common_info", "section": "common_config"},
    },
    # B17-class ZWRT firmware may omit physical identity from the generic
    # device response. Ask its dedicated modem endpoints before declaring the
    # router unidentified; each call remains optional on older firmware.
    {"service": "zwrt_zte_mdm.api", "method": "get_modem_msn", "params": {}},
    {"service": "zwrt_zte_mdm.api", "method": "get_imei", "params": {}},
    {
        "service": "uci",
        "method": "get",
        "params": {"config": "zwrt_zte_mdm", "section": "device_info"},
    },
)


class ZteZwrtUbusModem(ZteGoformModem):
    """ZTE NG adapter using the zwrt/ubus API family.

    Covers G5TC and devices verified to expose this NG transport.
    A newer MC888/MC889 firmware number alone does not prove an API migration.
    Read path via zte_nwinfo_api / nwinfo_get_netinfo.
    Write path via nwinfo_set_gwl_bandlock, nwinfo_set_nrbandlock,
    nwinfo_lock_lte_cell, nwinfo_lock_nr_cell, nwinfo_set_netselect.
    """

    @staticmethod
    def _decode_band_mask(value: Any) -> List[int]:
        return decode_zwrt_lte_mask(value)


    def __init__(
        self,
        host: str = "192.168.1.1",
        username: str = "admin",
        password: str = "",
        ping_interface: Optional[str] = None,
        backend_name: str = "zte_zwrt",
        expected_model: str = "",
        expected_serial: str = "",
        expected_firmware: str = "",
        expected_imei: str = "",
        profile_id: str = "",
    ):
        super().__init__(
            host=host,
            username=username,
            password=password,
            ping_interface=ping_interface,
            backend_name=backend_name,
            expected_model=expected_model,
            expected_serial=expected_serial,
            expected_firmware=expected_firmware,
            expected_imei=expected_imei,
            profile_id=profile_id,
        )
        origin = self._base_url
        self._initial_origin = origin
        # Permanent G5TC local-router TLS adapter. Never retry a failed verified
        # connection with weaker validation; select the policy before any I/O.
        self._verify_router_tls = self._router_tls_verification(origin)
        self._session.trust_env = False  # No proxy/netrc credentials on router I/O.
        self._ubus_session_id = ZERO_SESSION_ID
        self._zwrt_session_security = {"state": "not_attempted", "reason": ""}
        self._network_discovery_attempted = False
        self._network_diagnostic_sequence = 0
        self._network_diagnostic_episode = None
        self._startup_identity_prompt: Callable[[], str] | None = None
        self._last_wifi_status: tuple[Optional[bool], Optional[bool], Optional[bool]] = (None, None, None)
        self._last_wifi_status_ts: float = 0.0

    @staticmethod
    def _selected_origin(host: str) -> str:
        try:
            return ZteGoformModem._selected_origin(host)
        except ValueError:
            raise ValueError("zte_zwrt_router_origin_invalid") from None

    def _invalidate_zwrt_session(self) -> None:
        self._connected = False
        self._authenticated = False
        self._ubus_session_id = ZERO_SESSION_ID
        self._zwrt_session_security = {"state": "not_attempted", "reason": ""}
        self._session.cookies.clear()
        self._network_diagnostic_episode = None

    def _bootstrap_zwrt_session(self) -> None:
        self._base_url = self._initial_origin
        url = self._base_url + "/"
        for hop in range(4):
            response = self._zte_http_get(
                url, operation="zwrt_session_bootstrap", lane="startup",
                timeout=(self._connect_timeout, self._read_timeout),
            )
            if response.status_code not in {301, 302, 303, 307, 308}:
                response.raise_for_status()
                if response.status_code != 200:
                    raise ConnectionError("zwrt_bootstrap_status_unavailable")
                return
            destination = urlsplit(urljoin(url, response.headers.get("Location", "")))
            previous = urlsplit(url)
            if (hop == 3 or not response.headers.get("Location")
                    or destination.hostname != previous.hostname
                    or destination.scheme not in {"http", "https"}
                    or (previous.scheme == "https" and destination.scheme != "https")
                    or destination.username is not None or destination.password is not None
                    or destination.path not in {"", "/", "/index.html"}
                    or destination.query or "\\" in response.headers["Location"]):
                raise ConnectionError("zwrt_bootstrap_redirect_forbidden")
            try:
                _ = destination.port
            except ValueError:
                raise ConnectionError("zwrt_bootstrap_redirect_forbidden") from None
            self._base_url = f"{destination.scheme}://{destination.netloc}"
            url = self._base_url + (destination.path or "/")

    @staticmethod
    def _wifi_section_names() -> tuple[str, ...]:
        # Public NG scripts show both radio* and wifi*/main_* section naming.
        return ("radio0", "wifi0", "main_2g", "radio1", "wifi1", "main_5g")

    def _assert_zwrt_endpoint(self, url: str, method: str) -> None:
        """Inherited parsers cannot open a legacy or foreign transport path."""
        target, origin = urlsplit(url), urlsplit(self._base_url)
        allowed_paths = {"", "/", "/index.html"} if method == "GET" else {"/ubus", "/ubus/"}
        if ((target.scheme, target.netloc) != (origin.scheme, origin.netloc)
                or target.path not in allowed_paths):
            raise RuntimeError("zte_zwrt_http_endpoint_forbidden")

    def _zte_http_get(self, url: str, **kwargs: Any) -> requests.Response:
        self._assert_zwrt_endpoint(url, "GET")
        kwargs.update(allow_redirects=False, verify=self._verify_router_tls)
        return super()._zte_http_get(url, **kwargs)

    def _zte_http_post(self, url: str, **kwargs: Any) -> requests.Response:
        self._assert_zwrt_endpoint(url, "POST")
        kwargs.update(allow_redirects=False, verify=self._verify_router_tls)
        response = super()._zte_http_post(url, **kwargs)
        if 300 <= response.status_code < 400:
            raise requests.TooManyRedirects("zwrt_rpc_redirect_forbidden")
        return response

    def _try_login(self) -> bool:
        raise RuntimeError("zte_zwrt_goform_authentication_forbidden")

    def _try_login_multi_user(self, tokens: Optional[Dict[str, Any]] = None) -> bool:
        raise RuntimeError("zte_zwrt_goform_authentication_forbidden")

    def bind_startup_identity_prompt(
        self,
        prompt: Callable[[], str] | None,
    ) -> None:
        if prompt is not None and not callable(prompt):
            raise TypeError("zte_zwrt_startup_identity_prompt_callable_required")
        self._startup_identity_prompt = prompt

    def connect(self) -> bool:
        self._invalidate_zwrt_session()
        completed = False
        try:
            if self._password:
                self._bootstrap_zwrt_session()
                if not self._try_login_zwrt():
                    raise AuthenticationError(
                        f"Authentication failed for ZTE zwrt at {self._host} — check password"
                    )
            else:
                raise AuthenticationError(
                    f"ZTE zwrt at {self._host} requires a password but none was provided"
                )
            # Permanent G5TC authenticated-session continuation. This exchange
            # enables encrypted payloads on some firmware; it is not password
            # authentication. Prove whether the accepted session works below.
            self._zwrt_session_security = self._attempt_optional_zwrt_session_key()
            self._authenticated = True
            probe = self._fetch_flat_status()
            from arch4.runtime_identity import is_placeholder_identity_text
            identity = self._profile_identity_from_raw(probe)
            has_physical_identity = any(
                not is_placeholder_identity_text(identity[key])
                and set(str(identity[key]).strip()) != {"0"}
                for key in ("serial", "imei")
            )
            if probe and not has_physical_identity:
                from arch4.modem.operator_identity import require_operator_serial
                operator_serial = require_operator_serial(self._expected_serial, self._startup_identity_prompt)
                self._expected_serial = operator_serial
                probe["serial_number"] = operator_serial
                probe["physical_identity_source"] = "operator_supplied_serial"
            self._connected = bool(probe)
            if self._connected:
                self._apply_target_guard(
                    self._profile_identity_from_raw(
                        probe if isinstance(probe, dict) else {}
                    )
                )
            if self._connected:
                self._discover_network_selection_api()
                log.info("Connected to ZTE zwrt router at %s (authenticated)", self._host)
            else:
                raise ConnectionError("zwrt_initial_status_unavailable")
            completed = self._connected
            return self._connected
        except requests.RequestException as exc:
            log.error("ZTE zwrt connection failed: %s", type(exc).__name__)
            raise ConnectionError(f"zwrt_connect_transport_error:{type(exc).__name__}") from exc
        finally:
            if not completed:
                self._invalidate_zwrt_session()

    def disconnect(self) -> None:
        self._session.close()
        self._invalidate_zwrt_session()
        log.info("Disconnected from ZTE zwrt router")

    def is_connected(self) -> bool:
        if not self._connected:
            return False
        probe = self._call_ubus_current_session(
            {"service": "zte_nwinfo_api", "method": "nwinfo_get_netinfo", "params": {}},
        )
        ok = isinstance(probe, dict) and bool(probe.get("success")) and isinstance(probe.get("data"), dict)
        if ok:
            return True
        if self._result_access_denied(probe):
            return False  # The protocol boundary already cleared the expired token.
        raise ConnectionError("zwrt_session_status_unavailable")

    def execute_physical_stage(
        self,
        plan: ModemLivePhysicalStagePlan,
    ) -> ModemLivePhysicalStageResult:
        if not isinstance(plan, ModemLivePhysicalStagePlan):
            raise TypeError("zte_zwrt_physical_stage_plan_required")
        if not self._connected:
            raise RuntimeError("zte_zwrt_not_connected")
        target = plan.target
        call: Dict[str, Any] | None = None
        if isinstance(target, CoreRadioModeTarget):
            from arch4.modem.zte_zwrt_radio_mode import mode_call
            call = mode_call(self, plan)
        elif isinstance(target, CoreMobileDataTarget):
            from arch4.modem.zte_ng_network_settings import mobile_data_call
            call = mobile_data_call(self, target)
        elif isinstance(target, CoreNetworkSettingsTarget):
            from arch4.modem.zte_ng_network_settings import network_call
            call = network_call(self, plan)
        elif isinstance(target, PhysicalCommandCoordinationTarget):
            call = {
                "wifi_commit": {
                    "service": "uci",
                    "method": "commit",
                    "params": {"config": "wireless"},
                },
                "wifi_reload": {
                    "service": "network",
                    "method": "reload",
                    "params": {},
                },
            }.get(target.operation)
            if call is None:
                raise RuntimeError(
                    "zte_zwrt_coordination_operation_unsupported"
                )
        elif isinstance(target, CoreWifiRadioTarget):
            if plan.stage_kind != "wifi_config":
                raise RuntimeError("zte_zwrt_wifi_stage_invalid")
            radio_band = {
                "2.4ghz": "2.4",
                "5ghz": "5",
            }.get(target.band)
            if radio_band is None:
                raise RuntimeError("zte_zwrt_wifi_band_unsupported")
            target_radio = None
            for radio_name in self._wifi_section_names():
                result = self._ubus_call(
                    {
                        "service": "uci",
                        "method": "get",
                        "params": {
                            "config": "wireless",
                            "section": radio_name,
                        },
                    }
                )
                values = (
                    dict(result.get("data") or {}).get("values", {})
                    if isinstance(result, dict) and result.get("success")
                    else {}
                )
                if (
                    values
                    and self._classify_zwrt_radio_band(
                        radio_name,
                        {"config": values},
                    )
                    == radio_band
                ):
                    target_radio = radio_name
                    break
            if not target_radio:
                raise RuntimeError("zte_zwrt_wifi_radio_not_found")
            call = {
                "service": "uci",
                "method": "set",
                "params": {
                    "config": "wireless",
                    "section": target_radio,
                    "values": {
                        "disabled": "0" if target.enabled else "1",
                    },
                },
            }
        elif isinstance(target, (CoreBandSetTarget, CorePBandTarget)):
            if isinstance(target, CorePBandTarget) and target.pband == 0:
                raise RuntimeError("zte_auto_restore_target_unqualified")
            else:
                bands = (
                    target.bands
                    if isinstance(target, CoreBandSetTarget)
                    else (target.pband,)
                )
                mask = encode_zwrt_lte_mask(bands)
                call = {
                    "service": "zte_nwinfo_api",
                    "method": "nwinfo_set_gwl_bandlock",
                    "params": {
                        "is_gw_band": "0",
                        "gw_band_mask": "0",
                        "is_lte_band": "1",
                        "lte_band_mask": str(mask),
                    },
                }
        elif isinstance(target, CoreBandAutoTarget):
            raise RuntimeError("zte_auto_restore_target_unqualified")
        elif isinstance(target, CoreCellLockTarget):
            call = {
                "service": "zte_nwinfo_api",
                "method": "nwinfo_lock_lte_cell",
                "params": {
                    "lock_lte_pci": "0" if target.unlock else str(target.pci),
                    "lock_lte_earfcn": (
                        "0" if target.unlock else str(target.earfcn)
                    ),
                },
            }
        elif isinstance(target, CoreNrBandTarget):
            if plan.preceding_mode is not None:
                observed = self._radio_mode_status("event_write", required=True)["radio_mode"]
                if observed != plan.preceding_mode.target.configuration():
                    raise RuntimeError("zte_zwrt_nr_enabled_mode_configuration_changed")
            call = {
                "service": "zte_nwinfo_api",
                "method": "nwinfo_set_nrbandlock",
                "params": {
                    "nr5g_type": "SA",
                    "nr5g_band": ",".join(str(b) for b in target.nr_bands),
                },
            }
        elif isinstance(target, CoreRestartTarget):
            call = {"service": "system", "method": "reboot", "params": {}}
        if call is None:
            raise RuntimeError("zte_zwrt_typed_target_unsupported")
        call={**call,'params':self._definition.prepare_native_write_fields(
            'zwrt:'+call['service']+'.'+call['method'],call['params'])}
        if isinstance(target, CoreRadioModeTarget):
            from arch4.modem_live.radio_mode_configuration import native_mode_for_key
            if call['params'] != {'net_select':native_mode_for_key(self._definition, target.mode_key)}:
                raise RuntimeError("zte_zwrt_radio_mode_definition_mismatch")
        started = time.time()
        result = self._ubus_call(call, lane="event_write")
        returned = time.time()
        accepted = result["success"]
        reason = "accepted" if accepted else (result.get("reason") or "zte_zwrt_write_rejected")
        outcome = "accepted" if accepted else "rejected"
        if isinstance(target, (CoreNetworkSettingsTarget, CoreMobileDataTarget, CoreRadioModeTarget)):
            from arch4.modem.zte_ng_network_settings import network_write_disposition
            outcome, reason = network_write_disposition(result)
        return ModemLivePhysicalStageResult(
            plan=plan,
            request_dispatched_at=started,
            transport_returned_at=returned,
            outcome=outcome,
            reason=reason,
            radio_drop_detected=False,
        )

    def execute_lever(self, action: LeverAction) -> LeverResult:
        del action
        raise RuntimeError("zte_zwrt_generic_execute_lever_forbidden")

    def restart_modem(self) -> bool:
        raise RuntimeError("zte_zwrt_direct_restart_write_forbidden")

    # ── ubus actuation methods ─────────────────────────────────

    def _ubus_set_band_priority(self, bands: List[int]) -> bool:
        del bands
        raise RuntimeError("zte_zwrt_direct_band_write_forbidden")

    def _ubus_set_auto_bands(self) -> bool:
        raise RuntimeError("zte_zwrt_direct_band_auto_write_forbidden")

    def _ubus_set_nr_band_lock(self, bands: List[int]) -> bool:
        del bands
        raise RuntimeError("zte_zwrt_direct_nr_band_write_forbidden")

    def _ubus_set_cell_lock(self, *, pci: Optional[int], earfcn: Optional[int]) -> bool:
        del pci, earfcn
        raise RuntimeError("zte_zwrt_direct_cell_lock_write_forbidden")

    def get_capabilities(self):
        return get_capabilities("zte_zwrt")

    def _radio_mode_status(self, lane, *, required=False):
        from arch4.modem.zte_zwrt_radio_mode import status
        return status(self, lane, required=required)

    def read_modem_input(self) -> Optional[ModemInput]:
        """Override to inject Wi-Fi status into the shared contract."""
        modem = super().read_modem_input()
        if modem is None:
            return None
        self._augment_last_status_from_last_raw()
        wifi_on, wifi_24, wifi_5 = self._read_wifi_status()
        self._last_wifi_status = (wifi_on, wifi_24, wifi_5)
        self._last_wifi_status_ts = time.time()
        modem.wifi_enabled = wifi_on
        modem.wifi_24ghz_enabled = wifi_24
        modem.wifi_5ghz_enabled = wifi_5
        # Keep the backend status cache aligned with the modem input sample.
        self._last_status["wifi_enabled"] = wifi_on
        self._last_status["wifi_24ghz_enabled"] = wifi_24
        self._last_status["wifi_5ghz_enabled"] = wifi_5
        return modem

    def read_actionable_modem_input(self) -> Optional[ModemInput]:
        modem = super().read_actionable_modem_input()
        if modem is None:
            return None
        self._augment_last_status_from_last_raw()
        wifi_on, wifi_24, wifi_5 = self._last_wifi_status
        modem.wifi_enabled = wifi_on
        modem.wifi_24ghz_enabled = wifi_24
        modem.wifi_5ghz_enabled = wifi_5
        self._last_status["wifi_enabled"] = wifi_on
        self._last_status["wifi_24ghz_enabled"] = wifi_24
        self._last_status["wifi_5ghz_enabled"] = wifi_5
        return modem

    def refresh_live_maintenance(self) -> None:
        raise RuntimeError("zte_zwrt_maintenance_requires_owned_steps")

    def prepare_read_diagnostics(self):
        from arch4.modem.read_diagnostics import ReadDiagnosticUnavailable
        from arch4.modem.zte_zwrt_read_diagnostics import ZwrtReadDiagnosticAdapter
        if not self._connected or not self._ubus_session_id:
            raise ReadDiagnosticUnavailable("Connect to the router before running read diagnostics.")
        profile = self._resolve_firmware_profile()
        if profile is None or profile.network_settings != "ng_g5tc_b17_experimental":
            raise ReadDiagnosticUnavailable("Read diagnostics are not implemented for this router profile.")
        self._read_diagnostic_adapter = ZwrtReadDiagnosticAdapter(self)
        return self._read_diagnostic_adapter

    def _iter_network_settings_observation(self):
        from arch4.modem.zte_ng_network_settings import iter_network_settings
        return iter_network_settings(self)

    def acquire_post_write_modem_input(self, request, stage):
        if not isinstance(stage, PhysicalCommandDispatchEvidence):
            raise TypeError("zte_zwrt_post_write_stage_evidence_required")
        if isinstance(stage.target, CoreRadioModeTarget):
            if request.lane != "post_write" or not stage.permits_target_verification:
                raise ValueError("zte_zwrt_mode_post_write_evidence_required")
            observed = self._radio_mode_status("readback")
            self._last_status.update(observed)
            return self._capture_backend_acquisition_receipt(request=request, modem=None,
                acquired_cells=frozenset({"slow.radio_mode"}), status_override=observed)
        if isinstance(stage.target, CoreMobileDataTarget):
            if request.lane != "post_write" or not stage.permits_target_verification:
                raise ValueError("zte_zwrt_mobile_post_write_evidence_required")
            from arch4.modem.zte_ng_network_settings import mobile_data_observation
            observed = mobile_data_observation(self)
            return self._capture_backend_acquisition_receipt(
                request=request, modem=None,
                acquired_cells=frozenset("slow." + field for field in observed),
                status_override=observed)
        return super().acquire_post_write_modem_input(request, stage)

    def acquire_live_maintenance(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        raise RuntimeError("zte_zwrt_maintenance_requires_owned_steps")

    def iter_acquire_live_maintenance(self, request: BackendAcquisitionRequest):
        """Keep the two status batches together, then yield after each UCI read."""
        if request.lane != "slow_live":
            raise ValueError(
                "zte_zwrt_slow_live_acquisition_lane_required"
            )
        if not self._connected:
            return
        yield super().acquire_live_maintenance(request)
        # Diagnose one observed loss episode through the existing yielding
        # maintenance owner. No polling thread or healthy APN sweep is added.
        from arch4.modem.zte_ng_network_settings import _qualified
        if _qualified(self):
            from arch4.modem.zte_ng_network_settings import mobile_data_observation
            observed = mobile_data_observation(self)
            yield self._capture_backend_acquisition_receipt(
                request=request, modem=None,
                acquired_cells=frozenset("slow." + field for field in observed),
                status_override=observed)
            from arch4.core_contracts.network_settings import subscription_key
            connected = self._raw_radio_connected(self._last_raw)
            if connected is True:
                if self._network_diagnostic_episode is not None:
                    from arch4.modem.zte_ng_network_settings import _diagnostic
                    diagnostic = _diagnostic(self, connected=True,
                        imsi=str(self._last_raw.get("sim_imsi") or ""), issues=[])
                    yield self._capture_backend_acquisition_receipt(
                        request=request, modem=None,
                        acquired_cells=frozenset({"slow.backend_diagnostic"}),
                        status_override={"backend_diagnostic": diagnostic.to_payload()})
                self._network_diagnostic_episode = None
            elif connected is False:
                episode = subscription_key(str(self._last_raw.get("sim_imsi") or ""))
                if self._network_diagnostic_episode != episode:
                    self._network_diagnostic_episode = episode
                    yield from self.iter_acquire_network_settings(request)
        from arch4.modem.zte_zwrt_radio_mode import qualified
        if qualified(self):
            mode_status = self._radio_mode_status("slow_live")
            self._last_status.update(mode_status)
            yield self._capture_backend_acquisition_receipt(request=request, modem=None,
                acquired_cells=frozenset({"slow.radio_mode"}), status_override=mode_status)
        observed = {}
        for observation in self._iter_wifi_radio_observations():
            if observation is None:
                yield None
                continue
            band, enabled = observation
            observed[band] = enabled
            field = "wifi_24ghz_enabled" if band == "2.4" else "wifi_5ghz_enabled"
            updates = {field: enabled}
            # An observed enabled radio proves aggregate ON; aggregate OFF
            # requires both bands from this cycle, never the retained cache.
            if enabled or {"2.4", "5"} <= observed.keys():
                updates["wifi_enabled"] = any(observed.values())
            self._last_status.update(updates)
            self._last_wifi_status = tuple(self._last_status.get(key) for key in
                ("wifi_enabled", "wifi_24ghz_enabled", "wifi_5ghz_enabled"))
            self._last_wifi_status_ts = time.time()
            yield self._capture_backend_acquisition_receipt(
                request=request, modem=None,
                acquired_cells=frozenset(cell for cell in SLOW_CELLS
                    if cell.partition(".")[2] in updates),
            )

    def _augment_last_status_from_last_raw(self) -> None:
        if not isinstance(self._last_status, dict):
            return
        self._last_status["backend_session"] = {
            "payload_encryption": dict(self._zwrt_session_security),
        }
        raw = dict(self._last_raw or {})
        iccid = str(raw.get("iccid") or "").strip()
        sim_status = str(raw.get("sim_status") or "").strip()
        wan_iface_name = str(raw.get("wan_iface_name") or "").strip()
        if iccid:
            self._last_status["iccid"] = iccid
        if sim_status:
            self._last_status["sim_status"] = sim_status
        if wan_iface_name:
            self._last_status["wan_iface_name"] = wan_iface_name
        if raw.get("wan_connected") is not None:
            self._last_status["wan_connected"] = bool(raw.get("wan_connected"))

    def _parse_nr5g_status(self, raw: Dict[str, Any], network_type: str, *, profile_values=None) -> Dict[str, Any]:
        status = super()._parse_nr5g_status(raw, network_type, profile_values=profile_values)
        # NG's aggregate nr5g_rsrp is not evidence of a receiver-chain reading.
        # The GoForm compatibility alias must not relabel it as measured RX0.
        if (raw.get("5g_rx0_rsrp") in (None, "")
                and 'profile.nr5g_rx0_rsrp_dbm'not in (profile_values or {})):
            status["rx0_rsrp_dbm"] = None
            for cell in status["cells"]:
                if cell["role"] == "PCell":
                    cell["rx0_rsrp_dbm"] = None
        # NG/ubus NSA replies alternate these unavailable-NCI sentinels while
        # band/PCI/ARFCN remain unchanged. Keep raw evidence intact, but do not
        # certify either sentinel as an acquired cell or a radio transition.
        # This is this protocol's NSA encoding, not a global ban on NCI zero.
        if status["mode"] == "NSA" and status["cell_id"] in (0, 0xFFFFFFFF):
            status["cell_id"] = None
        return status

    def _build_modem_input_from_raw(
        self, raw: Dict[str, Any], *, traffic_observed_at: float | None = None,
        expect_rf: bool = True,
    ) -> Optional[ModemInput]:
        modem = super()._build_modem_input_from_raw(
            raw, traffic_observed_at=traffic_observed_at, expect_rf=expect_rf,
        )
        # Use this reply, not retained status, for both value and receipt proof.
        self._last_status["temperature_cpu_c"] = raw.get("temperature_cpu_c")
        self._last_status["temperature_cpu_diagnostic"] = raw.get("temperature_cpu_diagnostic", {})
        self._last_status.update({key: raw[key] for key in ZWRT_TRAFFIC_OUTPUT_FIELDS
                                 if key.startswith("traffic_") and key in raw})
        self._last_status["zwrt_traffic_diagnostic"] = raw.get("zwrt_traffic_diagnostic", {})
        from arch4.modem.zte_zwrt_read_diagnostics import signal_evidence
        adapter = getattr(self, "_read_diagnostic_adapter", None)
        if adapter is not None and adapter.observing:
            adapter.observe_normal_read(raw, modem, self._canonical_fields_proven_by_raw(raw, modem))
        self._read_signal_evidence_created = False
        if (modem is not None and modem.radio_connected and modem.rsrp_dbm > -40
                and any(raw.get(k) not in (None, "") for k in ("lte_rsrp", "lte_rsrp_1"))
                and "backend_diagnostic" not in self._last_status
                and time.time() - getattr(self, "_read_signal_at", 0.) >= 60.):
            self._read_signal_at = time.time()
            self._read_signal_sequence = getattr(self, "_read_signal_sequence", 0) + 1
            self._last_status["backend_diagnostic"] = signal_evidence(raw, modem, self._backend_name, self._read_signal_sequence)
            self._read_signal_evidence_created = True
        return modem

    def _canonical_fields_proven_by_raw(
        self, raw: Dict[str, Any], modem: Optional[ModemInput],
    ) -> frozenset[str]:
        proven = set(super()._canonical_fields_proven_by_raw(raw, modem))
        if raw.get("temperature_cpu_c") is not None:
            proven.add("temperature_cpu_c")
        proven.update(key for key in ZWRT_TRAFFIC_OUTPUT_FIELDS if key.startswith("traffic_") and key in raw)
        if getattr(self, "_read_signal_evidence_created", False):
            proven.add("backend_diagnostic")
        return frozenset(proven)

    def get_live_acquisition_contract(self) -> LiveAcquisitionContract:
        return LiveAcquisitionContract(
            acquisition_class="aligned_split",
            clocked_register_read=True,
            live_register_interval_s=2.0,
            live_register_stale_s=6.0,
            profile_name="zte_zwrt_ubus_status",
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
            fast_read_groups=("ubus_radio_status", "rf_quality", "traffic_presence", "traffic_volume", "nr_status"),
            slow_read_groups=("ca_details", "wifi_status", "router_status", "network_mode"),
            static_capability_groups=(
                "device_identity",
                "firmware",
                "supported_bands",
                "antenna_capability",
                "qam_capability",
                "wifi_capability",
            ),
            control_write_groups=("band_selection", "cell_lock", "nr_band_lock", "restart", "wifi_toggle", "ubus_rpc"),
            export_groups=("sidecars", "support_bundle", "history"),
            static_capability_refresh_policy="identity_or_operator_refresh_only",
        )

    # ─── Wi-Fi status and control ──────────────────────────

    def _read_wifi_status(self) -> tuple:
        """One UCI reader; unavailable fields never select another service."""
        return self._read_wifi_via_uci()

    def _read_wifi_via_uci(self) -> tuple:
        observed = {}
        for observation in self._iter_wifi_radio_observations():
            if observation is not None:
                observed[observation[0]] = observation[1]
        if not observed:
            return None, None, None
        return any(observed.values()), observed.get("2.4"), observed.get("5")

    def _iter_wifi_radio_observations(self):
        """Permanent UCI wireless-section adapter for reviewed NG names.

        Each declared section is read once. Missing sections stay unavailable;
        no netifd or legacy protocol is selected after denial.
        """
        device_bands = set()
        for radio_name in self._wifi_section_names():
            result = self._ubus_call(self._portable_ubus_read('uci_wireless', {
                "service": "uci",
                "method": "get",
                "params": {"config": "wireless", "section": radio_name},
            },runtime_parameters={'section':radio_name}))
            if not isinstance(result, dict) or not result.get("success"):
                yield None
                continue
            values = (result.get("data") or {}).get("values", {}) or {}
            if not isinstance(values, dict) or not values:
                yield None
                continue

            band = self._classify_zwrt_radio_band(radio_name, {"config": values})
            if band is None:
                yield None
                continue

            # A wifi-iface/SSID can stay enabled while its parent radio is
            # disabled. Radio controls/readback are owned by wifi-device; the
            # interface is a fallback only when no device section was found.
            interface = values.get(".type") == "wifi-iface" or radio_name.startswith("main_")
            if interface and band in device_bands:
                yield None
                continue
            if not interface:
                device_bands.add(band)

            field = "slow.wifi_24ghz_enabled" if band == "2.4" else "slow.wifi_5ghz_enabled"
            replies = {"uci_wireless":result.get("data") or {}}
            addresses = self._definition.available_addresses(replies, addresses={field})
            converted = self._decode_profile_values(replies, addresses)
            if field in converted:
                enabled = converted[field]
                yield (band, enabled) if type(enabled) is bool else None
                continue

            disabled = values.get("disabled")
            if "disabled" not in values:
                # No disabled field = enabled by default
                enabled = True
            elif type(disabled) in (bool, int, str) and str(disabled).strip().lower() in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
                enabled = str(disabled).strip().lower() in {"0", "false", "no", "off"}
            else:
                yield None
                continue

            yield band, enabled

    @staticmethod
    def _classify_zwrt_radio_band(radio_name: str, radio_data: dict) -> Optional[str]:
        """Classify a ZTE radio as '2.4' or '5' GHz.

        Heuristics (checked in order):
          1. config.band: '2g'/'5g'
          2. config.hwmode: '11g'/'11b' → 2.4, '11a'/'11ac' → 5
          3. config.channel: 1-14 → 2.4, 36+ → 5
          4. Radio name: 'radio0' → 2.4, 'radio1' → 5
        """
        config = radio_data.get("config", {}) or {}
        if not isinstance(config, dict):
            return None

        band = str(config.get("band", "")).lower()
        if band in ("2g", "2.4g", "2.4ghz"):
            return "2.4"
        if band in ("5g", "5ghz"):
            return "5"

        hwmode = str(config.get("hwmode", "")).lower()
        if hwmode in ("11g", "11b", "11ng"):
            return "2.4"
        if hwmode in ("11a", "11na", "11ac", "11ax"):
            return "5"

        channel = config.get("channel")
        if channel is not None and str(channel).isdigit():
            ch = int(channel)
            if 1 <= ch <= 14:
                return "2.4"
            if ch >= 36:
                return "5"

        name = str(radio_name).lower()
        if name in ("radio0", "wifi0", "main_2g"):
            return "2.4"
        if name in ("radio1", "wifi1", "main_5g"):
            return "5"

        return None

    def wifi_set_radio(self, radio: str, enabled: bool) -> bool:
        del radio, enabled
        raise RuntimeError("zte_zwrt_direct_wifi_write_forbidden")

    def _wifi_set_radio_via_zwrt_wlan(self, radio: str, enabled: bool) -> bool:
        del radio, enabled
        raise RuntimeError("zte_zwrt_hidden_wifi_transport_forbidden")

    def _read_goform(
        self,
        fields: Iterable[str],
        *,
        multi_data: bool = True,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        # Keep the shared non-NR projection. The NG measurement adapter alone
        # admits the NR facts physically acquired in this same RPC batch.
        del multi_data, timeout_s
        raw = self._fetch_flat_status()
        requested = {str(field or "").strip() for field in fields}
        if not requested:
            return raw
        # These fields are already physically returned by the required status
        # and metadata calls. Admit current evidence, never _last_raw cache.
        admitted = requested | ZWRT_NR_MEASUREMENT_FIELDS | ZWRT_TRAFFIC_OUTPUT_FIELDS | {
            "temperature_cpu_c", "temperature_cpu_diagnostic",
            "sim_imsi", "imsi", "network_provider_fullname", "network_provider",
            "mccmnc", "rmcc", "rmnc", "physical_identity_source",
        }
        return {key: value for key, value in raw.items() if key in admitted}

    def _call_ubus_current_session(
        self, calls: Any, *, invalidate_session_on_denial: bool = True
    ) -> Any:
        """Use only the current session; Connectivity owns any repair."""

        result = self._ubus_call(calls)
        # A denied optional service is not loss of an otherwise accepted session.
        # Expired sessions deny the batch; a successful core call proves it lives.
        session_denied = self._result_access_denied(result) and not (
            isinstance(result, list)
            and any(isinstance(item, dict) and item.get("success") for item in result)
        )
        if session_denied and invalidate_session_on_denial:
            self._invalidate_zwrt_session()
        return result

    def _ubus_headers(self, calls: list[dict], session: str, *, describe_only: bool) -> dict[str, str]:
        """Standalone NG session dialect; all RPC callers share this boundary."""
        certificate = len(calls) == 1 and calls[0].get("service") == "zwrt_web" and calls[0].get("method") == "web_crt_get"
        headers = {
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "54GUC", "X-Requested-With": "XMLHttpRequest",
            "Origin": self._base_url, "Referer": self._base_url + "/",
            "Z-Mode": "0" if session == ZERO_SESSION_ID or certificate else "1",
        }
        if len(calls) == 1 and not describe_only:
            call = calls[0]
            tag = str((call.get("params") or {}).get("config") or "") if call.get("service") == "uci" else str(call.get("method") or "")
            if tag:
                headers["Z-Tag"] = tag
        return headers

    def _discover_network_selection_api(self) -> None:
        """One bounded, read-only signature query; never a capability verdict.

        OpenWrt uhttpd's explicit JSON-RPC list dialect takes service names,
        not a session ID, and returns a result object. Firmware may deny it.
        Keep refusal separate from the already verified normal login.
        """
        if self._network_discovery_attempted:
            return
        self._network_discovery_attempted = True
        try:
            result = self._ubus_call({"service": "zte_nwinfo_api"}, describe_only=True)
            if not isinstance(result, dict) or not result.get("success"):
                log.info("ZTE NG network API discovery: unavailable; strict operator control unverified")
                return
            methods = result["data"]["zte_nwinfo_api"]
            if not isinstance(methods, dict):
                log.info("ZTE NG network API discovery: invalid signatures; strict operator control unverified")
                return
            signatures = {}
            for name in sorted(ZWRT_NETWORK_DISCOVERY_METHODS & methods.keys()):
                signature = methods[name]
                signatures[name] = (
                    sorted(key for key in signature
                           if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", key))[:32]
                    if isinstance(signature, dict) else "unavailable"
                )
            # Whitelisted method/argument names only; raw replies use the
            # existing redacted startup trace and Foundation support exporter.
            log.info("ZTE NG network API signatures: %s; strict operator control unverified", signatures)
        except Exception as exc:
            # Optional diagnostics must never undo successful authentication.
            log.warning("ZTE NG network API discovery failed: %s", type(exc).__name__)

    def _ubus_call(
        self, calls: Any, *, session_id: str = "", lane: str = "slow_live",
        describe_only: bool = False, diagnostic_read: bool = False,
    ) -> Any:
        if diagnostic_read:
            from arch4.modem.zte_zwrt_read_diagnostics import PROBES
            allowed = [{"service": p.service} if not p.method else
                       {"service": p.service, "method": p.method, "params": dict(p.params)}
                       for p in PROBES if p.service != "@normal_read"]
            if isinstance(calls, list) or calls not in allowed or describe_only != (set(calls) == {"service"}):
                raise ValueError("read_diagnostic_probe_not_registered")
        active_session = str(session_id or self._ubus_session_id or ZERO_SESSION_ID)
        calls_array = list(calls) if isinstance(calls, list) else [calls]
        req = []
        for idx, call in enumerate(calls_array):
            if describe_only and (set(call) != {"service"} or call["service"] not in ({"zte_nwinfo_api", "*"} if diagnostic_read else {"zte_nwinfo_api"})):
                raise ValueError("zte_zwrt_discovery_service_forbidden")
            req.append(
                {
                    "jsonrpc": "2.0",
                    "id": idx,
                    "method": "list" if describe_only else "call",
                    "params": [call["service"]] if describe_only else [
                        active_session,
                        str(call.get("service") or ""),
                        str(call.get("method") or ""),
                        dict(call.get("params") or {}),
                    ],
                }
            )
        try:
            resp = self._zte_http_post(
                f"{self._base_url}/ubus/?t={int(time.time() * 1000)}",
                operation="ubus_network_description" if describe_only else "ubus_batch",
                lane=lane,
                json=req,
                headers=self._ubus_headers(calls_array, active_session, describe_only=describe_only),
                timeout=min(self._read_timeout, 2.5) if describe_only or diagnostic_read else self._read_timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("ZTE zwrt ubus call failed: %s", type(exc).__name__)
            failures = [
                {"success": False, "id": item["id"], "data": None, "accessDenied": False,
                 "reason": f"zte_zwrt_transport_error:{type(exc).__name__}"}
                for item in req
            ]
            return failures if isinstance(calls, list) else failures[0]
        try:
            payload = resp.json()
        except requests.exceptions.JSONDecodeError as exc:
            log.warning("ZTE zwrt ubus returned invalid JSON: %s", type(exc).__name__)
            failures = [
                {"success": False, "id": item["id"], "data": None, "accessDenied": False,
                 "reason": "zte_zwrt_protocol_error:invalid_json"}
                for item in req
            ]
            return failures if isinstance(calls, list) else failures[0]
        if not isinstance(payload, list):
            payload = [payload]
        by_id: dict[int, dict] = {}
        duplicates: set[int] = set()
        for item in payload:
            if not isinstance(item, dict) or type(item.get("id")) is not int:
                continue
            reply_id = item["id"]
            if reply_id in by_id:
                duplicates.add(reply_id)
            by_id[reply_id] = item
        for reply_id in duplicates:
            del by_id[reply_id]
        out: List[Dict[str, Any]] = []
        for request_item in req:
            current = by_id.get(request_item["id"]) or {}
            error = current.get("error") or {}
            if error:
                out.append(
                    {
                        "success": False,
                        "id": request_item["id"],
                        "data": None,
                        "accessDenied": isinstance(error, dict) and error.get("code") == -32002,
                        "reason": (
                            f"zte_zwrt_rpc_error:{error['code']}"
                            if isinstance(error, dict) and type(error.get("code")) is int
                            else "zte_zwrt_rpc_error:invalid_code"
                        ),
                    }
                )
                continue
            result = current.get("result") or []
            if describe_only:
                code = result[0] if isinstance(result, list) and result else None
                if type(code) is int and code != 0:
                    out.append({"success": False, "id": request_item["id"],
                                "data": {}, "accessDenied": code == 6,
                                "reason": f"zte_zwrt_ubus_error:{code}"})
                    continue
                valid = isinstance(result, dict) and (calls_array[request_item["id"]]["service"] == "*" or isinstance(result.get(calls_array[request_item["id"]]["service"]), dict))
                out.append({"success": valid, "id": request_item["id"],
                            "data": result if valid else {}, "accessDenied": False,
                            "reason": "" if valid else "zte_zwrt_protocol_error:invalid_description"})
                continue
            code = result[0] if isinstance(result, list) and result else -1
            data = result[1] if isinstance(result, list) and len(result) > 1 else None
            out.append(
                {
                    "success": type(code) is int and code == 0 and (data is None or isinstance(data, dict)),
                    "id": request_item["id"],
                    "data": data if isinstance(data, dict) else {},
                    "accessDenied": type(code) is int and code == 6,
                    "reason": (f"zte_zwrt_ubus_error:{code}" if type(code) is int and code != 0 else
                               "zte_zwrt_protocol_error:invalid_data" if data is not None and not isinstance(data, dict) else ""),
                }
            )
        return out if isinstance(calls, list) else out[0]

    def _result_access_denied(self, result: Any) -> bool:
        if isinstance(result, list):
            return any(bool(item.get("accessDenied")) for item in result if isinstance(item, dict))
        return isinstance(result, dict) and bool(result.get("accessDenied"))

    def _try_login_zwrt(self) -> bool:
        login_info = self._ubus_call(
            {"service": "zwrt_web", "method": "web_login_info", "params": {}},
            session_id=ZERO_SESSION_ID,
        )
        salt = str(
            (login_info.get("data") or {}).get("zte_web_sault")
            or (login_info.get("data") or {}).get("zte_web_salt")
            or ""
        ).strip()
        if not login_info.get("success") or not salt:
            raise ConnectionError("zwrt_login_challenge_unavailable:" + str(login_info.get("reason") or "invalid_reply"))
        first_hash = self._sha256_hex(self._password)
        final_hash = self._sha256_hex(first_hash + salt)
        login = self._ubus_call(
            {
                "service": "zwrt_web",
                "method": "web_login",
                "params": {"password": final_hash},
            },
            session_id=ZERO_SESSION_ID,
        )
        if login.get("accessDenied"):
            return False
        if not login.get("success") or not isinstance(login.get("data"), dict):
            raise ConnectionError("zwrt_login_reply_unavailable:" + str(login.get("reason") or "invalid_reply"))
        data = login["data"]
        result = data.get("result")
        if type(result) is not int and not (isinstance(result, str) and len(result) <= 10 and result.isdecimal()):
            raise ConnectionError("zwrt_login_result_unavailable")
        if int(result) != 0:
            return False
        session = data.get("ubus_rpc_session")
        if not isinstance(session, str) or not session.strip() or session.strip() == ZERO_SESSION_ID:
            raise ConnectionError("zwrt_login_session_unavailable")
        self._ubus_session_id = session.strip()
        return True

    def _attempt_optional_zwrt_session_key(self) -> dict[str, str]:
        """Named optional exchange boundary; never absorbs login/read/write faults."""
        try:
            self._establish_zwrt_session_key()
        except Exception as exc:
            reason = (
                str(exc) if isinstance(exc, ZwrtSessionSetupError)
                else f"zwrt_session_key_setup_error:{type(exc).__name__}"
            )
            log.warning("ZTE NG payload-key setup incomplete (%s); validating accepted login", reason)
            return {"state": "unavailable", "reason": reason}
        return {"state": "established", "reason": ""}

    def _establish_zwrt_session_key(self) -> None:
        """Attempt optional payload encryption once; connect owns continuation."""
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.exceptions import UnsupportedAlgorithm

        reply = self._ubus_call({"service": "zwrt_web", "method": "web_crt_get", "params": {}})
        if not reply.get("success"):
            raise ZwrtSessionSetupError("zwrt_session_certificate_unavailable:" + str(reply.get("reason") or "rejected"))
        data = reply.get("data") or {}
        encoded = data.get("result") or data.get("crt")
        if not isinstance(encoded, str) or not encoded.strip() or len(encoded) > 16384:
            raise ZwrtSessionSetupError("zwrt_session_certificate_invalid")
        try:
            value = encoded.strip()
            if value.startswith("-----BEGIN"):
                match = re.fullmatch(r"-----BEGIN (PUBLIC KEY|RSA PUBLIC KEY|CERTIFICATE)-----\s*(.*?)\s*-----END \1-----", value, re.S)
                if match is None:
                    raise ValueError
                label, body = match.groups()
                compact = re.sub(r"\s+", "", body)
                wrapped = "\n".join(compact[i:i + 64] for i in range(0, len(compact), 64))
                pem = f"-----BEGIN {label}-----\n{wrapped}\n-----END {label}-----\n".encode("ascii")
                public = x509.load_pem_x509_certificate(pem).public_key() if label == "CERTIFICATE" else serialization.load_pem_public_key(pem)
            else:
                public = serialization.load_der_public_key(base64.b64decode(re.sub(r"\s+", "", value), validate=True))
            if not isinstance(public, rsa.RSAPublicKey):
                raise ValueError
            encrypted = public.encrypt(secrets.token_hex(32).encode("ascii"), padding.PKCS1v15())
        except (ValueError, TypeError, UnicodeError, UnsupportedAlgorithm):
            raise ZwrtSessionSetupError("zwrt_session_certificate_invalid") from None
        result = self._ubus_call({
            "service": "zwrt_web", "method": "web_http_enstr_set",
            "params": {"web_enstr": base64.b64encode(encrypted).decode("ascii")},
        })
        payload = result.get("data") or {}
        acknowledgement = payload.get("result")
        accepted = (payload == {} or type(acknowledgement) is int and acknowledgement == 0
                    or isinstance(acknowledgement, str) and acknowledgement.lower() in {"0", "success", "ok"})
        if not result.get("success") or not accepted:
            raise ZwrtSessionSetupError("zwrt_session_key_setup_failed:" + str(result.get("reason") or "rejected"))

    @staticmethod
    def _sha256_hex(value: str) -> str:
        return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest().upper()

    def _portable_ubus_read(self, alias, native, *, runtime_parameters=None):
        """Map one admitted descriptor into its existing native read owner."""
        definition=getattr(self,'_definition',None)
        if definition is None or not definition.has_request(alias):return native
        request=definition.request(alias,protocol='zwrt_ubus')
        if 'calls'in request:return request['calls']
        parameters=dict(request.get('parameters',{}))
        if runtime_parameters:
            parameters.update({key:value for key,value in runtime_parameters.items()
                if key in request.get('runtime_parameters',[])})
        return {'service':request['service'],'method':request['operation'],'params':parameters}

    def _fetch_flat_status(self) -> Dict[str, Any]:
        results = self._call_ubus_current_session(self._portable_ubus_read('status_batch', list(ZWRT_STATUS_CALLS)))
        if not isinstance(results, list):
            return {}
        # Optional service ACLs do not establish whether the session is alive.
        # Primary status and the explicit session probe retain that authority.
        metadata = self._call_ubus_current_session(
            self._portable_ubus_read('metadata_batch', list(ZWRT_METADATA_CALLS)), invalidate_session_on_denial=False
        )
        # Definitions consume the declared ordered success/data view. Keep
        # transport IDs/disposition in the native trace, not in this parser
        # input snapshot; the existing checker reconstructs this exact view.
        self._portable_reply_groups = {
            'status_batch':[{"success":bool(item.get("success")), "data":item.get("data")}
                if isinstance(item,dict) else {"success":False,"data":None} for item in results],
            'metadata_batch':[{"success":bool(item.get("success")), "data":item.get("data")}
                if isinstance(item,dict) else {"success":False,"data":None}
                for item in (metadata if isinstance(metadata,list) else [])],
        }
        results = results + (metadata if isinstance(metadata, list) else [])

        def data_at(index: int) -> Dict[str, Any]:
            item = results[index] if index < len(results) else None
            if not isinstance(item, dict) or not item.get("success"):
                return {}
            data = item.get("data")
            return dict(data) if isinstance(data, dict) else {}

        net_info = data_at(0)
        thermal_info = data_at(1)
        device_info = data_at(2)
        router_status = data_at(3)
        traffic_info = data_at(4)
        sim_info = data_at(5)
        wan_status = data_at(6)
        values = data_at(7).get("values")
        common_values = dict(values) if isinstance(values, dict) else {}
        modem_serial = data_at(8)
        modem_imei = data_at(9)
        values = data_at(10).get("values")
        modem_values = dict(values) if isinstance(values, dict) else {}
        # Firmware may omit optional services or supply identity through UCI.
        # Keep available truth; Matrix still admits identity and marks absent
        # measurements unavailable. Never admit an empty radio/identity read.
        # A replayed metadata reply can reuse ids 0..2. Require the primary
        # status call itself to succeed. Missing RAT/cell fields are valid only
        # with an explicit down report from the WAN interface; a blank stale
        # reply while WAN remains up is not a fresh mobile-status observation.
        identity_metadata_keys = {
            "model_name", "product_name", "device_name", "model", "product",
            "modem_msn", "msn", "serial_number", "sn", "sn_num", "serial",
            "imei", "modem_imei", "IMEI",
        }
        has_identity_metadata = any(
            source.get(key) not in (None, "")
            for source in (device_info, common_values, modem_serial, modem_imei, modem_values)
            for key in identity_metadata_keys
        )
        primary = results[0] if results else None
        if (not isinstance(primary, dict) or not primary.get("success")
                or not has_identity_metadata
                or not (any(key in net_info for key in ("network_type", "lte_pci", "nr5g_pci"))
                        or wan_status.get("up") is False)):
            return {}

        raw = dict(net_info)

        self._set_first(
            raw,
            "model_name",
            common_values,
            device_info,
            keys=("model_name", "product_name", "device_name", "model", "product"),
        )
        if raw.get("model_name") and not raw.get("product_name"):
            raw["product_name"] = raw["model_name"]
        self._set_first(
            raw,
            "hardware_version",
            common_values,
            device_info,
            keys=("hardware_version", "hw_version"),
        )
        self._set_first(
            raw,
            "wa_inner_version",
            common_values,
            modem_values,
            device_info,
            keys=("wa_inner_version", "software_version", "firmware_version", "modem_version", "inner_version"),
        )
        self._set_first(
            raw,
            "web_version",
            common_values,
            device_info,
            keys=("webui_version", "web_version", "ui_version", "gui_version", "GUI_version"),
        )
        self._set_first(
            raw,
            "cr_version",
            common_values,
            device_info,
            keys=("config_version", "configuration_file_version"),
        )
        self._set_first(
            raw,
            "serial_number",
            modem_serial,
            modem_values,
            common_values,
            device_info,
            keys=("modem_msn", "msn", "serial_number", "sn", "sn_num", "serial"),
        )
        self._set_first(
            raw,
            "imei",
            modem_imei,
            modem_values,
            common_values,
            device_info,
            keys=("imei", "modem_imei", "IMEI"),
        )
        from arch4.runtime_identity import is_placeholder_identity_text
        if (
            is_placeholder_identity_text(raw.get("serial_number"))
            or set(str(raw.get("serial_number") or "").strip()) == {"0"}
        ):
            from arch4.modem.operator_identity import operator_serial_text
            operator_serial = operator_serial_text(self._expected_serial)
            if operator_serial:
                raw["serial_number"] = operator_serial
                raw["physical_identity_source"] = "operator_supplied_serial"
        sim = active_sim(sim_info, common_values, device_info, net_info)
        raw.pop("imsi", None)  # No second alias may bypass a conflict verdict.
        raw.update(sim_imsi=sim["imsi"], iccid=sim["iccid"], sim_status=sim["status"])
        self._set_first(
            raw,
            "wan_ipaddr",
            router_status,
            traffic_info,
            keys=("wan_ipaddr", "wan_ip", "ipv4_ip", "ipaddr", "wwan_ip"),
        )
        wan_ipaddr = self._extract_zwrt_wan_ip(wan_status)
        if wan_ipaddr and not raw.get("wan_ipaddr"):
            raw["wan_ipaddr"] = wan_ipaddr
        if wan_status.get("up") is not None and raw.get("wan_connected") in (None, ""):
            raw["wan_connected"] = bool(wan_status.get("up"))
        self._set_first(raw, "wan_iface_name", wan_status, keys=("l3_device", "device", "proto"))
        for key in ZWRT_TRAFFIC_PARENT_FIELDS:
            raw.pop(key, None)
        raw.update(decode_zwrt_traffic(traffic_info))
        self._set_first(
            raw,
            "network_provider_fullname",
            net_info,
            common_values,
            keys=("network_provider_fullname", "operator_name", "provider_name"),
        )
        self._set_first(raw, "mccmnc", net_info, keys=("mccmnc", "plmn"))
        self._set_first(raw, "rmcc", net_info, keys=("rmcc",))
        self._set_first(raw, "rmnc", net_info, keys=("rmnc",))
        self._set_first(raw, "network_provider", net_info, common_values, keys=("network_provider", "provider_name", "operator_name"))

        # This endpoint measures the CPU, not Wi-Fi or the 5G modem die.
        cpu_temp, diagnostic = _cpu_temperature(thermal_info)
        values = self._decode_profile_values(
            self._portable_reply_groups, {"slow.temperature_cpu_c"})
        corrected = values.get("slow.temperature_cpu_c")
        if corrected != cpu_temp:
            # Retain the original sensor diagnostic and its physical range
            # guard when a human changes a field location or scale.
            cpu_temp, corrected_diagnostic = _cpu_temperature({"cpu_temp": corrected})
            diagnostic["profile_field"] = "slow.temperature_cpu_c"
            diagnostic["profile_reason"] = corrected_diagnostic["reason"]
        raw.pop("temperature_cpu_c", None)
        if cpu_temp is not None:
            raw["temperature_cpu_c"] = cpu_temp
        raw["temperature_cpu_diagnostic"] = diagnostic
        if thermal_info.get("ambient") not in (None, "") and not raw.get("pm_sensor_ambient"):
            raw["pm_sensor_ambient"] = thermal_info.get("ambient")
        if thermal_info.get("modem_temp") not in (None, "") and not raw.get("pm_sensor_mdm"):
            raw["pm_sensor_mdm"] = thermal_info.get("modem_temp")
        if thermal_info.get("nr_temp") not in (None, "") and not raw.get("pm_sensor_5g"):
            raw["pm_sensor_5g"] = thermal_info.get("nr_temp")

        # ── NR5G signal field aliasing ─────────────────────────────────
        # The ubus nwinfo_get_netinfo response uses nr5g_rsrp / nr5g_rsrq /
        # nr5g_snr while the goform parent (_parse_nr5g_status) expects the
        # Older serialized Z5g_rsrp / Z5g_rsrq / Z5g_SINR names are accepted
        # at the vendor boundary. Map them so the
        # inherited signal parser finds the data.
        # Confirmed from tpoechtrager/ZTE-Script-NG (G5TC+, 32 stars):
        #   netInfo.nr5g_rsrp, netInfo.nr5g_rsrq, netInfo.nr5g_snr
        _NR_ALIASES = (
            ("nr5g_rsrp", "Z5g_rsrp"),
            ("nr5g_rsrq", "Z5g_rsrq"),
            ("nr5g_snr", "Z5g_SINR"),
        )
        for ubus_key, goform_key in _NR_ALIASES:
            if raw.get(ubus_key) not in (None, "") and raw.get(goform_key) in (None, ""):
                raw[goform_key] = raw[ubus_key]

        if "nr5g_bandwidth" in raw:
            raw["nr5g_nsa_bandwidth"] = raw["nr5g_bandwidth"]

        # Native NG lock readback is PCI,EARFCN, not the two GoForm keys.
        # 0,0 is the explicit unlock encoding; missing/malformed is unknown.
        lock = raw.get("lock_lte_cell")
        parts = lock.split(",") if isinstance(lock, str) else []
        if len(parts) == 2 and all(part.strip().isdecimal() for part in parts):
            pci, earfcn = (int(part.strip()) for part in parts)
            if pci == earfcn == 0:
                raw["lte_pci_lock"] = raw["lte_earfcn_lock"] = ""
            elif 0 <= pci <= 503 and 0 < earfcn <= 262143:
                raw["lte_pci_lock"], raw["lte_earfcn_lock"] = str(pci), str(earfcn)

        # The aggregate NG state remains explicit evidence with only a PCell
        # row. Missing SCell details must not discard a reported inactive flag.
        ca_state = raw.get("lteca_state")
        if type(ca_state) in (int, str) and str(ca_state) in {"0", "1", "2"}:
            raw["wan_lte_ca"] = "0" if str(ca_state) == "0" else "1"

        # NG LTE packing differs from GoForm and includes the PCell first.
        # NR packing has the same PCI/band/ARFCN/width/signal positions.
        if "lteca" in raw and not raw.get("lte_multi_ca_scell_info"):
            self._translate_native_lte_carriers(raw)
        if raw.get("nrca") and not raw.get("nr_multi_ca_scell_info"):
            raw["nr_multi_ca_scell_info"] = raw["nrca"]

        if not raw.get("lte_ca_scell_band"):
            scell_bands: List[str] = []
            for cell in [part for part in str(raw.get("lte_multi_ca_scell_info") or "").split(";") if part.strip()]:
                parts = [part.strip() for part in cell.split(",")]
                if len(parts) >= 4:
                    band = "".join(ch for ch in parts[3] if ch.isdigit())
                    if band and band not in scell_bands:
                        scell_bands.append(band)
            if scell_bands:
                raw["lte_ca_scell_band"] = ",".join(scell_bands)

        self._normalize_service_measurements(raw)
        return raw

    @staticmethod
    def _normalize_service_measurements(raw: Dict[str, Any]) -> None:
        """Retained native slots are not fresh measurements of an absent RAT."""
        rat = str(raw.get("network_type") or "").strip().upper()
        if not rat:
            return
        if re.search(r"WCDMA|UMTS|HSPA|GSM|GPRS|EDGE|NO[_ -]?SERVICE", rat):
            for key in tuple(raw):
                if (key.startswith(("lte_", "ZCELLINFO_")) and key != "lte_band_lock") or key in {
                    "lteca", "wan_active_band", "wan_active_channel", "wan_lte_ca",
                    "bandwidth", "Z_dl_earfcn",
                }:
                    raw.pop(key, None)
        if re.search(r"WCDMA|UMTS|HSPA|GSM|GPRS|EDGE|NO[_ -]?SERVICE", rat) or rat in {
            "LTE", "LTE CA", "LTE-NSA", "LIMITED_SERVICE_LTE",
        }:
            for key in ZWRT_NR_MEASUREMENT_FIELDS | {
                "nr5g_rsrp", "nr5g_rsrq", "nr5g_snr", "nr5g_bandwidth",
                "nrca", "nr_multi_ca_scell_info",
            }:
                raw.pop(key, None)

    @staticmethod
    def _translate_native_lte_carriers(raw: Dict[str, Any]) -> None:
        """Normalize NG packing once, before every shared acquisition projection.

        lteca: PCI,unused,unused,EARFCN,width; PCell is row zero.
        ltecasig: RSRP,RSRQ,SINR,RSSI,UL,state; SCells only, state 2 active.
        Preserve row positions even for malformed/blank records. Unknown state
        cannot create an active carrier or a fresh negative CA observation.
        """
        rows = str(raw.get("lteca") or "").rstrip("; ").split(";")
        signals = str(raw.get("ltecasig") or "").split(";")

        def carrier(row: str) -> tuple[int, int, int, float] | None:
            parts = [p.strip() for p in row.split(",")]
            if len(parts) < 5 or not all(re.fullmatch(r"[0-9]+", parts[i]) for i in (0, 3)):
                return None
            pci, channel = int(parts[0]), int(parts[3])
            try:
                width = float(parts[4])
            except ValueError:
                return None
            band = lte_band_from_dl_earfcn(channel)
            if pci > 503 or not band or width not in (1.4, 3, 5, 10, 15, 20):
                return None
            return pci, channel, band, width

        primary = carrier(rows[0])
        if primary is None:
            return
        pci, channel, band, width = primary
        for key, value in (("lte_pci", pci), ("lte_ca_pcell_freq", channel),
                           ("lte_ca_pcell_band", band), ("lte_ca_pcell_bandwidth", width)):
            if raw.get(key) in (None, ""):
                raw[key] = str(value)
        infos, signal_infos, active_bands = [], [], []
        all_states_known = len(rows) > 1
        for index, row in enumerate(rows[1:]):
            cell = carrier(row)
            sig = [p.strip() for p in signals[index].split(",")] if index < len(signals) else []
            state = sig[5] if len(sig) >= 6 else ""
            if cell is None or state not in {"0", "1", "2"}:
                all_states_known = False
                continue
            if state != "2":
                continue
            pci, channel, band, width = cell
            infos.append(f"1,{pci},2,{band},{channel},{width}")
            signal_infos.append(",".join(sig[:3]))
            active_bands.append(str(band))
        if infos:
            raw["lte_multi_ca_scell_info"] = ";".join(infos)
            raw["lte_multi_ca_scell_sig_info"] = ";".join(signal_infos)
            raw["lte_ca_scell_band"] = ",".join(dict.fromkeys(active_bands))
            # Same-band CA is still CA; a unique-band count cannot prove this.
            raw["wan_lte_ca"] = "1"
        elif all_states_known:
            raw["wan_lte_ca"] = "0"

    @staticmethod
    def _extract_zwrt_wan_ip(wan_status: Dict[str, Any]) -> str:
        if not isinstance(wan_status, dict):
            return ""
        def address(value):
            if not isinstance(value, str):
                return ""
            try:
                parsed = ipaddress.ip_address(value.strip())
            except ValueError:
                return ""
            return "" if parsed.is_unspecified else str(parsed)
        for addr_key in ("ipv4-address", "ipv6-address"):
            entries = wan_status.get(addr_key)
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        value = address(entry.get("address"))
                        if value:
                            return value
        for key in ("ipv4_address", "ipv6_address", "address", "wan_ip", "ipaddr"):
            value = address(wan_status.get(key))
            if value:
                return value
        return ""

    @staticmethod
    def _coalesce(source: Dict[str, Any], keys: Iterable[str]) -> Any:
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
        return None

    def _set_first(
        self,
        raw: Dict[str, Any],
        target_key: str,
        *sources: Dict[str, Any],
        keys: Iterable[str],
    ) -> None:
        if raw.get(target_key) not in (None, ""):
            return
        for source in sources:
            if not isinstance(source, dict):
                continue
            value = self._coalesce(source, keys)
            if value not in (None, ""):
                raw[target_key] = value
                return
