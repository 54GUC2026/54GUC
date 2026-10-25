"""
arch4/modem/huawei_b818.py
═══════════════════════════════════════════════════════════════
ARCH4 v1 — Huawei HiLink-like Modem Backend (F1.7)

Implements ModemInterface for Huawei HiLink-like CPE routers.
This module originated on the B818-263 and is also reused for
other Huawei models such as B535, H112-370 (5G CPE Pro),
and H122-373 (5G CPE Pro 2). They share a similar HTTP/XML API
surface, but firmware/session behavior is model-specific.

Communication via Huawei HiLink HTTP/XML API — no AT commands,
no SSH. All data extracted from the same endpoints that the
bookmarklet (ca_v25) uses in the browser.

Key differences from MikroTik backend:
  - HTTP/XML API (not RouterOS API + AT over at-chat)
  - CSRF token required for all POST requests
  - No per-carrier CA detail (B818 FW doesn't expose SCell info)
  - CA detected via CurrentNetworkTypeEx == "1011"
  - No cell lock capability — band selection is the primary lever
  - MCS DL has two codewords (Code0/Code1 → MIMO rank)
  - Neighbor list: PCI only (no per-neighbor RSRP)
  - RTT via subprocess ping (no built-in RouterOS /ping)
  - Traffic via /api/monitoring/traffic-statistics (cumulative)

API auth: Huawei HiLink API uses session-based auth:
  1. GET / → SessionID cookie
  2. GET /api/webserver/SesTokInfo → SessionID + TokInfo
  3. GET /api/user/state-login → check if login required
  4. POST /api/user/login → SHA256 challenge-response auth
  5. Every POST needs __RequestVerificationToken header
  Token refreshed from response headers after each request.
═══════════════════════════════════════════════════════════════
"""

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

import requests

from arch4.modem_live.profile_diagnostics import decode_native_profile
from arch4.modem_live.startup_trace import traced_http
from arch4.modem.backend_diagnostic import record_native_read_failure, native_exception_chain

from arch4.modem.base import (
    AuthenticationError,
    SessionExpiredError,
    LiveAcquisitionContract,
    ModemInterface,
    TargetGuardError,
    build_public_modem_status,
    evaluate_target_guard,
    normalize_identity_value,
    target_guard_observed_identity,
)
from arch4.atomic_io import atomic_write_json
from arch4.modem.backend_contracts import (
    BackendFeatureResult,
    backend_failed,
    backend_ok,
)
from arch4.modem.backend_acquisition import (
    BackendAcquisitionReceipt,
    BackendAcquisitionRequest,
    DECLARED_CANONICAL_SOURCE_FIELDS,
    matrix_cells_for_canonical_fields,
)
from arch4.modem.backend_acquisition_cells import (
    CONFIGURED_BAND_CELLS,
)
from arch4.interfaces import (
    ModemInput, CarrierInfo, NeighborCell,
    LeverAction, LeverResult,
    get_capabilities,
)
from arch4.core_contracts.command_targets import (
    CoreBandAutoTarget,
    CoreMobileDataTarget,
    CoreBandSetTarget,
    CorePBandTarget,
    CoreRestartTarget,
    CoreWifiRadioTarget,
    CoreNetworkSettingsTarget,
    CoreRadioModeTarget,
)
from arch4.core_contracts.physical_commands import (
    PhysicalCommandDispatchEvidence,
)
from arch4.modem_live.physical_command import (
    ModemLivePhysicalStagePlan,
    ModemLivePhysicalStageResult,
)
from arch4.modem_live.physical_request_authority import (
    ModemLivePhysicalRequestAuthority,
)
from arch4.modem.utils import RttProbeResult, measure_host_ping_rtt, xml_text
from arch4.modem.huawei_parsers import (
    ALL_BANDS_HEX,
    BW_TO_MHZ,
    NETWORK_TYPE_MAP,
    bands_to_hex as _bands_to_hex,
    dl_mcs_to_tbs as _dl_mcs_to_tbs,
    extract_xml_mask_values as _extract_xml_mask_values,
    hex_to_bands as _hex_to_bands,
    is_all_bands_mask as _is_all_bands_mask,
    parse_b818_cqi as _parse_b818_cqi,
    parse_band_list_text as _parse_band_list_text,
    parse_bandwidth_mhz,
    parse_dl_mcs,
    parse_serving_earfcns,
    parse_freq_mhz,
    parse_int,
    parse_signal_val,
    parse_ul_mcs,
    ul_mcs_to_tbs as _ul_mcs_to_tbs,
)
from arch4.modem.signal_normalization import (
    assess_signal_contract,
    rate_limit_signal_contract_warning,
)
from arch4.modem.lock_confirmation import (
    build_lock_confirmation,
    configured_band_match,
    effective_band_match,
    normalized_bands,
)
from arch4.modem.identity import normalize_modem_identity_fields
from arch4.modem.live_lane_contract import modem_live_access_profile_for
from arch4.operator_profiles import resolve_provider_name
from arch4.lte_mcs import (
    is_lte_retx_mcs,
    lte_nominal_qm_from_mcs,
    resolve_lte_qm_with_retx_hold,
    qm_to_qam as lte_qm_to_qam,
)


_HUAWEI_NR_ACQUIRED_FIELDS = frozenset(
    {
        "nr5g_active",
        "nsa_active",
        "sa_active",
        "nr5g_mode",
        "nr5g_state_text",
        "nr5g_arfcn",
        "nr5g_rsrp_dbm",
        "nr5g_rsrq_db",
        "nr5g_sinr_db",
        "nr5g_cells",
    }
)

_HUAWEI_NR_STATE_FIELDS = frozenset(
    {
        "nr5g_active",
        "nsa_active",
        "sa_active",
        "nr5g_mode",
        "nr5g_state_text",
    }
)

_HUAWEI_OPTIONAL_NR_READS = {
    "slow.nr_bandwidth_mhz": "nrdlbandwidth",
    "slow.nr_ul_bandwidth_mhz": "nrulbandwidth",
    "slow.nr_dl_freq_mhz": "nrdlfreq",
    "slow.nr_ul_freq_mhz": "nrulfreq",
    "radio.nr_mcs_dl": "nrdlmcs",
    "radio.nr_mcs_ul": "nrulmcs",
}

log = logging.getLogger("arch4.modem.huawei")


@dataclass(frozen=True)
class HuaweiControlTransportReceipt:
    """Exact transport boundary evidence for one Huawei control POST."""

    path: str
    request_dispatched_at: float
    transport_returned_at: float
    outcome: str

    def as_physical_command_evidence(
        self,
        *,
        stage_kind: str,
        ordinal: int,
    ) -> Dict[str, Any]:
        """Translate the private Huawei receipt into Core vocabulary."""

        return {
            "schema_version": "HuaweiControlTransportReceipt.v1",
            "owner": "huawei_hilink_transport",
            "stage_kind": str(stage_kind),
            "ordinal": int(ordinal),
            "request_dispatched_at": self.request_dispatched_at,
            "transport_returned_at": self.transport_returned_at,
            "outcome": self.outcome,
        }

_HUAWEI_HILINK_FAMILY_HINTS = {
    "h173",
    "huawei",
    "b818",
    "b535",
    "b535-232",
    "b311",
    "b530",
    "h112",
    "h122",
    "h155",
}

def _is_b818_retx_mcs(mcs: int) -> bool:
    return bool(is_lte_retx_mcs(mcs))


def _mcs_to_qm(mcs: int) -> int:
    """
    Map Huawei HiLink nominal LTE MCS index to modulation order Qm (2/4/6/8).

    Note:
      - MCS 28..31 are treated as retransmission-class markers and are not
        used as fresh modulation evidence in B818 processing path.
    """
    return lte_nominal_qm_from_mcs(mcs)


def _resolve_b818_qm(mcs: int, last_qm: int = 0) -> tuple[int, bool]:
    """
    Resolve Huawei HiLink effective modulation while ignoring retransmission markers.

    Returns: (qm, used_retx_hold)
      - For nominal MCS ranges, returns direct Qm.
      - For MCS 28..31, reuses last known valid Qm (if any), otherwise 0.
    """
    return resolve_lte_qm_with_retx_hold(mcs, last_qm)


class HuaweiHiLinkModem(ModemInterface):
    """
    Huawei HiLink modem backend via HTTP/XML API.

    Standard Huawei HiLink gateway is usually 192.168.8.1.
    Auth flow: session → SesTokInfo → state-login check → SHA256 login.
    """

    physical_request_authority_required = True
    requires_provider_pool_policy = True

    _AUTH_FAILURE_BACKOFF_S = (120.0, 240.0, 960.0, 3840.0)
    _AUTH_BACKOFF_FILENAME = "hilink_auth_backoff.json"

    def __init__(self, host: str = "192.168.8.1",
                 username: str = "admin",
                 password: str = "",
                 timeout: float = 5.0,
                 ping_interface: Optional[str] = None,
                 backend_name: str = "b818",
                 profile_id: str = "",
                 expected_model: str = "",
                 expected_serial: str = "",
                 expected_firmware: str = "",
                 expected_imei: str = "",
                 netmode_submit_timeout_s: float = 2.5,
                 netmode_continue_on_read_timeout_for_verification: bool = True,
                 sequential_step_wait_s: float = 20.0,
                 pband_wait_s: float = 5.0,
                 signal_sinr_scale: float = 1.0,
                 signal_rsrp_scale: float = 1.0,
                 signal_rsrq_scale: float = 1.0,
                 signal_rssi_scale: float = 1.0,
                 signal_contract_warn: bool = True):
        """
        Args:
            host: Huawei HiLink router IP address
            username: HiLink web UI username (default "admin")
            password: HiLink web UI password (empty = no auth)
            timeout: HTTP request timeout in seconds
            ping_interface: OS network interface for RTT ping
                            (e.g. 'eth0' if the Huawei router is on a specific NIC).
                            None = OS default route.
        """
        self._host = host
        self._username = username
        self._password = password
        self._provider_pool_policy = None
        self._backend_name = str(backend_name or "b818").strip().lower()
        self._selected_profile_id = str(profile_id or "").strip()
        self._authentication_contract = "hilink_sha256_type4"
        if self._backend_name == "h173":
            from arch4.modem_live.profile_catalog import modem_profile_record
            from arch4.modem.huawei_scram import CONTRACT
            selected = modem_profile_record(self._selected_profile_id)
            if selected.get("backend") != "h173" or selected.get("authentication_contract") != CONTRACT:
                raise ValueError("h173_exact_authentication_contract_required")
            self._authentication_contract = CONTRACT
            if expected_model and expected_model != selected["target_guard_model"]:
                raise ValueError("h173_selected_model_guard_conflict")
            if expected_firmware and expected_firmware != selected["firmware_exact"]:
                raise ValueError("h173_selected_firmware_guard_conflict")
            expected_model = selected["target_guard_model"]
            expected_firmware = selected["firmware_exact"]
        from arch4.modem_live.profile_catalog import modem_profile_definition
        default_profile = {
            "b818": "huawei_hilink_b818_baseline",
            "b535": "huawei_b535_hilink_baseline",
        }.get(self._backend_name, "huawei_hilink_generic_baseline")
        self._definition = modem_profile_definition(self._selected_profile_id or default_profile)
        address = self._definition.http_address(host)
        self._base_url = address if '://' in address else f'http://{address}'
        self._profile_field_issues = {}
        self._profile_identity_unavailable = set()
        self._access_profile = self._resolve_access_profile()
        self._physical_request_authority = ModemLivePhysicalRequestAuthority(
            self._access_profile
        )
        self._expected_model = str(expected_model or "").strip()
        self._expected_serial = str(expected_serial or "").strip()
        self._expected_firmware = str(expected_firmware or "").strip()
        self._expected_imei = str(expected_imei or "").strip()
        self._timeout = timeout
        self._ping_iface = ping_interface
        self._netmode_continue_on_read_timeout_for_verification = bool(
            netmode_continue_on_read_timeout_for_verification
        )
        configured_netmode_timeout_s = max(
            0.5,
            float(netmode_submit_timeout_s or 2.5),
        )
        self._netmode_submit_timeout_s = configured_netmode_timeout_s
        self._sequential_step_wait_s = max(0.0, float(sequential_step_wait_s or 20.0))
        self._pband_wait_s = max(0.0, float(pband_wait_s or 5.0))
        self._signal_scales = {
            "sinr": float(signal_sinr_scale or 1.0),
            "rsrp": float(signal_rsrp_scale or 1.0),
            "rsrq": float(signal_rsrq_scale or 1.0),
            "rssi": float(signal_rssi_scale or 1.0),
        }
        self._signal_contract_warn = bool(signal_contract_warn)
        self._session = requests.Session()
        self._api_request_lock = threading.RLock()
        self._api_control_operation_lock = threading.RLock()
        self._fast_read_session = requests.Session()
        self._slow_read_session = requests.Session()
        self._fast_read_lock = threading.RLock()
        self._slow_read_lock = threading.RLock()
        self._read_session_generation: int = 0
        self._csrf_token: Optional[str] = None
        self._session_id: Optional[str] = None
        self._connected = False
        self._logged_in = False
        self._last_login_error_code: str = ""
        self._auth_failure_until_ts: float = 0.0
        self._auth_failure_attempts: int = 0
        self._auth_failure_fingerprint_hash: str = ""
        self._auth_failure_reason: str = ""
        self._capabilities = None

        # Traffic delta tracking
        self._prev_rx_bytes: Optional[int] = None
        self._prev_tx_bytes: Optional[int] = None
        self._prev_traffic_ts: Optional[float] = None
        self._last_rx_bps: float = 0.0
        self._last_tx_bps: float = 0.0
        self._last_traffic_rate_ts: float = 0.0
        self._fast_traffic_cache_s: float = float(self._access_profile.fast_traffic_cache_s)
        self._fast_traffic_connect_timeout_s: float = float(self._access_profile.fast_traffic_connect_timeout_s)
        self._fast_traffic_min_timeout_s: float = float(self._access_profile.fast_traffic_min_timeout_s)
        self._fast_traffic_max_timeout_s: float = float(self._access_profile.fast_traffic_max_timeout_s)
        self._traffic_native_cache: Dict[str, Any] = {}
        self._traffic_native_ts: float = 0.0
        self._traffic_month_cache: Dict[str, Any] = {}
        self._traffic_month_ts: float = 0.0
        self._traffic_quota_cache: Dict[str, Any] = {}
        self._traffic_quota_ts: float = 0.0
        self._fast_read_slo_s: float = float(self._access_profile.fast_read_slo_s)
        self._fast_signal_timeout: Any = (
            float(self._access_profile.fast_signal_connect_timeout_s),
            float(self._access_profile.fast_signal_read_timeout_s),
        )
        self._maintenance_timeout: Any = (
            float(self._access_profile.maintenance_connect_timeout_s),
            float(self._access_profile.maintenance_read_timeout_s),
        )
        self._last_fast_read_profile: Dict[str, Any] = {}
        self._api_timing_lock = threading.Lock()
        self._radio_diagnostic_lock = threading.Lock()
        self._normal_channel_conversion_key = None
        self._api_timing_by_path: Dict[str, Dict[str, Any]] = {}
        self._api_session_expiry_by_lane: Dict[str, int] = {}
        self._last_huawei_acquired_fields: frozenset[str] = frozenset()
        self._nr_evidence_seen = False
        self._hilink_session_refresh_ts: float = 0.0
        self._hilink_session_refresh_source: str = ""
        self._hilink_last_proactive_session_attempt_ts: float = 0.0
        self._hilink_proactive_session_interval_s: float = float(
            self._access_profile.proactive_session_refresh_interval_s
        )
        self._hilink_proactive_session_min_gap_s: float = float(
            self._access_profile.proactive_session_refresh_min_gap_s
        )
        self._last_wifi_status: tuple[Optional[bool], Optional[bool], Optional[bool]] = (None, None, None)
        self._last_wifi_status_ts: float = 0.0
        self._netmode_refresh_requested_reason: str = ""
        self._last_netmode_event_refresh_ts: float = 0.0
        self._netmode_event_refresh_min_interval_s: float = 15.0
        self._last_netmode_rf_identity: Optional[tuple[int, int, int, int]] = None
        self._initial_netmode_read_attempted: bool = False

        # Boot time tracking
        self._boot_time_str: str = ""

        # Cached device info / PLMN
        self._device_info_cache: Dict[str, str] = {}
        self._device_info_ts: float = 0.0
        self._device_info_acquired_fields: frozenset[str] = frozenset()
        self._device_info_attempt_ts: float = 0.0
        self._device_info_missing_license_retry_s: float = 10.0
        self._plmn_cache: Dict[str, str] = {}
        self._plmn_cache_ts: float = 0.0
        self._netmode_cache: Dict[str, Any] = {}
        self._netmode_ts: float = 0.0
        self._netmode_list_cache: Dict[str, Any] = {}
        self._netmode_list_ts: float = 0.0
        self._netmode_actionable_probe_ts: float = 0.0
        self._last_post_response_unavailable_path: str = ""
        self._last_control_transport_receipt = HuaweiControlTransportReceipt(
            path="",
            request_dispatched_at=0.0,
            transport_returned_at=0.0,
            outcome="not_started",
        )

        # Cached antenna status
        self._antenna_cache: Dict[str, Any] = {}
        self._antenna_ts: float = 0.0

        # Last parsed status snapshot for GUI
        self._last_status: Dict[str, Any] = {}
        self._last_status_ts: float = 0.0
        self._signal_contract_seq: int = 0
        self._signal_contract_warning_state: Dict[tuple, Dict[str, Any]] = {}
        # Last known effective Qm per stream (used when Huawei HiLink reports retx markers 28..31).
        self._last_qm_dl0: int = 0
        self._last_qm_dl1: int = 0
        self._last_qm_ul: int = 0

    def _resolve_access_profile(self):
        backend_name = str(getattr(self, "_backend_name", "") or "").strip().lower()
        family = "huawei_hilink_like" if backend_name in _HUAWEI_HILINK_FAMILY_HINTS else ""
        definition = getattr(self, "_definition", None)
        return modem_live_access_profile_for(
            backend=backend_name, backend_family=family,
            profile_id=definition.profile_id if definition is not None else "",
        )

    def physical_request_lane_scope(self, lane: str):
        return self._physical_request_authority.lane_scope(lane)

    def physical_request_authority_status(self) -> Dict[str, Any]:
        return self._physical_request_authority.snapshot()

    def _auth_credential_fingerprint(self) -> tuple:
        return (
            str(getattr(self, "_host", "") or ""),
            str(getattr(self, "_username", "") or ""),
            str(getattr(self, "_password", "") or ""),
        )

    def _auth_credential_fingerprint_hash(self) -> str:
        raw = "\0".join(self._auth_credential_fingerprint())
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _auth_backoff_path(self) -> str:
        memory_dir = str(os.environ.get("ARCH_RUNTIME_MEMORY_DIR", "") or "").strip()
        if not memory_dir:
            return ""
        return os.path.join(memory_dir, self._AUTH_BACKOFF_FILENAME)

    def _read_auth_backoff_state(self) -> Dict[str, Any]:
        path = self._auth_backoff_path()
        if not path:
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            log.debug("Huawei auth backoff state ignored: %s", exc)
            return {}

    def _write_auth_backoff_state(self, payload: Dict[str, Any]) -> None:
        path = self._auth_backoff_path()
        if not path:
            return
        try:
            atomic_write_json(
                path,
                payload,
                indent=2,
                sort_keys=True,
                trailing_newline=True,
                logger=log,
            )
        except Exception as exc:
            log.warning("Huawei auth backoff state write failed: %s", exc)

    def _clear_auth_failure_latch(self) -> None:
        self._auth_failure_until_ts = 0.0
        self._auth_failure_attempts = 0
        self._auth_failure_fingerprint_hash = ""
        self._auth_failure_reason = ""
        path = self._auth_backoff_path()
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except Exception as exc:
                log.debug("Huawei auth backoff clear ignored: %s", exc)

    def _load_auth_failure_latch(self, now: float) -> Dict[str, Any]:
        fingerprint_hash = self._auth_credential_fingerprint_hash()
        state = self._read_auth_backoff_state()
        state_hash = str(state.get("fingerprint_hash") or "")
        if state_hash and state_hash != fingerprint_hash:
            self._clear_auth_failure_latch()
            return {}
        until_ts = float(state.get("until_ts", 0.0) or 0.0)
        attempts = int(state.get("attempts", 0) or 0)
        reason = str(state.get("reason") or "")
        if until_ts <= now:
            self._auth_failure_until_ts = 0.0
            self._auth_failure_attempts = max(0, attempts)
            self._auth_failure_fingerprint_hash = fingerprint_hash if attempts else ""
            self._auth_failure_reason = reason
            return {}
        self._auth_failure_until_ts = until_ts
        self._auth_failure_attempts = max(1, attempts)
        self._auth_failure_fingerprint_hash = fingerprint_hash
        self._auth_failure_reason = reason
        return {
            "until_ts": until_ts,
            "attempts": max(1, attempts),
            "reason": reason,
        }

    def _raise_if_auth_failure_latched(self, now: float) -> None:
        state = self._load_auth_failure_latch(now)
        if not state:
            return
        remaining_s = max(1, int(round(float(state["until_ts"]) - now)))
        reason = str(state.get("reason") or "previous Huawei login rejection")
        raise AuthenticationError(
            f"Huawei HiLink authentication blocked for {remaining_s}s after {reason}; "
            "wait for the cooldown or change credentials before retrying"
        )

    def _record_auth_failure_latch(self, reason: str, now: float) -> None:
        fingerprint_hash = self._auth_credential_fingerprint_hash()
        state = self._read_auth_backoff_state()
        previous_hash = str(state.get("fingerprint_hash") or "")
        previous_attempts = int(state.get("attempts", 0) or 0)
        attempts = previous_attempts + 1 if previous_hash == fingerprint_hash else 1
        schedule = self._AUTH_FAILURE_BACKOFF_S
        backoff_s = float(schedule[min(max(0, attempts - 1), len(schedule) - 1)])
        until_ts = now + backoff_s
        self._auth_failure_until_ts = until_ts
        self._auth_failure_attempts = attempts
        self._auth_failure_fingerprint_hash = fingerprint_hash
        self._auth_failure_reason = str(reason or "login_failed")
        self._write_auth_backoff_state(
            {
                "schema": "huawei_hilink_auth_backoff.v1",
                "backend": str(getattr(self, "_backend_name", "") or ""),
                "host": str(getattr(self, "_host", "") or ""),
                "username": str(getattr(self, "_username", "") or ""),
                "fingerprint_hash": fingerprint_hash,
                "attempts": attempts,
                "backoff_s": backoff_s,
                "until_ts": until_ts,
                "reason": self._auth_failure_reason,
                "updated_ts": now,
            }
        )
        log.warning(
            "Huawei HiLink authentication cooldown active for %.0fs after %s (attempt %d)",
            backoff_s,
            self._auth_failure_reason,
            attempts,
        )

    def _login_failure_reason(self) -> str:
        code = str(getattr(self, "_last_login_error_code", "") or "").strip()
        if code == "108006":
            return "wrong_username_or_password"
        if code == "108007":
            return "too_many_login_attempts"
        if code:
            return f"login_failed_code_{code}"
        return "login_failed"

    def _target_guard_enabled(self) -> bool:
        return bool(self._expected_model or self._expected_serial or self._expected_firmware or self._expected_imei)

    def _target_guard_identity_observable(
        self,
        observed: Optional[Dict[str, Any]],
    ) -> bool:
        """Say whether every configured guard axis has concrete evidence."""

        identity = target_guard_observed_identity(observed)
        provisional = {
            "",
            "unknown",
            "unavailable",
            "unsupported",
            "n/a",
            "na",
            "---",
        }
        label = normalize_identity_value(self._device_label())
        backend = normalize_identity_value(self._backend_name)
        provisional_model = provisional | {
            label,
            backend,
            normalize_identity_value(f"Huawei {self._backend_name}"),
            f"{label} (unreachable)",
            f"{backend} (unreachable)",
        }
        expected = {
            "model": self._expected_model,
            "serial": self._expected_serial,
            "firmware": self._expected_firmware,
            "imei": self._expected_imei,
        }
        for axis, configured in expected.items():
            if not normalize_identity_value(configured):
                continue
            actual = identity[axis]
            if actual in (provisional_model if axis == "model" else provisional):
                return False
        return True

    def _apply_target_guard_when_observable(
        self,
        observed: Optional[Dict[str, Any]],
    ) -> bool:
        """Apply the strict guard only after startup has concrete identity."""

        if not self._target_guard_enabled():
            return True
        if not self._target_guard_identity_observable(observed):
            if isinstance(observed, dict):
                observed.pop("target_guard_ok", None)
                observed.pop("target_guard_reason", None)
            log.debug(
                "%s target identity is pending concrete modem evidence",
                self._device_label(),
            )
            return False
        return self._apply_target_guard(observed)

    def _apply_target_guard(self, observed: Optional[Dict[str, Any]]) -> bool:
        if self._backend_name == "h173":
            from arch4.modem_live.profile_catalog import modem_profile_native_identity_qualified
            info = observed or {}
            if not modem_profile_native_identity_qualified(self._selected_profile_id,
                    model=str(info.get("model") or ""), firmware=str(info.get("firmware") or ""),
                    hardware=str(info.get("hardware_version") or "")):
                self._connected = False
                raise TargetGuardError("H173 selected native identity does not match", reason="h173_selected_identity_mismatch")
        ok, reason = evaluate_target_guard(
            expected_model=self._expected_model,
            expected_serial=self._expected_serial,
            expected_firmware=self._expected_firmware,
            expected_imei=self._expected_imei,
            observed=observed,
        )
        if isinstance(observed, dict):
            observed["target_guard_ok"] = bool(ok)
            observed["target_guard_reason"] = str(reason or "")
        if ok:
            return True
        self._connected = False
        log.error("%s target guard rejected current router: %s", self._device_label(), reason)
        raise TargetGuardError(
            f"Target guard failed for {self._device_label()}: {reason}",
            reason=str(reason or ""),
        )

    def _device_label(self) -> str:
        if self._backend_name == "b535":
            return "Huawei B535"
        if self._backend_name == "b530":
            return "Huawei B530"
        if self._backend_name == "b818":
            return "Huawei B818"
        if self._backend_name == "h112":
            return "Huawei 5G CPE Pro"
        if self._backend_name == "h122":
            return "Huawei 5G CPE Pro 2"
        return "Huawei HiLink"

    def _capability_backend_key(self) -> str:
        if self._backend_name in {"b818", "b535", "b530", "h112", "h122"}:
            return self._backend_name
        return "huawei"

    def _normalize_device_identity(self, info: Dict[str, str]) -> Dict[str, str]:
        """Normalize identity from hard evidence reported by the modem.

        Some Huawei firmwares leave DeviceName blank while still reporting
        hardware, firmware, serial and IMEI. That blank descriptive field must
        not make the connection or support surface look like a different
        router. Model normalization belongs inside the modem live boundary,
        before GUI/support/capability code consumes identity.
        """
        out = dict(info or {})
        out.setdefault("backend", self._backend_name)
        return normalize_modem_identity_fields(out)

    def _has_real_device_identity(self, info: Optional[Dict[str, Any]]) -> bool:
        """Return true only when static identity has hard modem evidence.

        The boot/static lane may run during session churn. A Huawei error XML
        or empty response can parse into placeholders such as "Huawei B818" and
        "unknown"; those values are useful as provisional display labels but
        must not be frozen as the boot identity.
        """
        if not isinstance(info, dict) or not info:
            return False
        model = str(info.get("model") or info.get("router_model") or "").strip()
        label = self._device_label().strip()
        firmware = str(info.get("firmware") or info.get("software_version") or "").strip().lower()
        hard_fields = (
            "serial",
            "imei",
            "imsi",
            "hardware_version",
            "webui",
            "lan_mac",
            "wan_ip",
            "config_version",
        )
        if any(str(info.get(key) or "").strip() for key in hard_fields):
            return True
        if model and model != label:
            return True
        if firmware and firmware != "unknown":
            return True
        return False

    @staticmethod
    def _has_license_identity(info: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(info, dict):
            return False
        return bool(str(info.get("serial") or "").strip() or str(info.get("imei") or "").strip())

    def _device_info_license_retry_due(self, *, now: Optional[float] = None) -> bool:
        if self._has_license_identity(getattr(self, "_device_info_cache", {}) or {}):
            return False
        current = time.time() if now is None else float(now)
        last_attempt = float(getattr(self, "_device_info_attempt_ts", 0.0) or 0.0)
        last_cache = float(getattr(self, "_device_info_ts", 0.0) or 0.0)
        last = max(last_attempt, last_cache)
        retry_s = max(1.0, float(getattr(self, "_device_info_missing_license_retry_s", 10.0) or 10.0))
        return last <= 0.0 or (current - last) >= retry_s

    def _record_signal_contract_event(self, event_type: str, assess: Dict[str, Any]) -> None:
        self._signal_contract_seq += 1
        payload = {
            "seq": int(self._signal_contract_seq),
            "event_type": str(event_type),
            "metric": str(assess.get("metric", "")),
            "raw": assess.get("raw"),
            "scale": float(assess.get("scale", 1.0) or 1.0),
            "norm": float(assess.get("norm", 0.0) or 0.0),
            "ok": bool(assess.get("ok", False)),
            "alt_scale": float(assess.get("alt_scale", 1.0) or 1.0),
            "alt_norm": float(assess.get("alt_norm", 0.0) or 0.0),
            "alt_ok": bool(assess.get("alt_ok", False)),
            "ambiguous": bool(assess.get("ambiguous", False)),
        }
        try:
            self._last_status["signal_contract_last"] = payload
        except Exception as exc:
            log.debug("_record_signal_contract_event: suppressed %s", exc)
            pass

    def _norm_signal(self, metric: str, raw_value: float, *, scale: float | None = None) -> float:
        assess = assess_signal_contract(
            metric, raw_value,
            self._signal_scales.get(metric, 1.0) if scale is None else scale,
        )
        norm = float(assess["norm"])
        ok = bool(assess["ok"])
        if bool(assess.get("ambiguous", False)):
            self._record_signal_contract_event("ambiguous", assess)
            if bool(self._signal_contract_warn):
                decision = rate_limit_signal_contract_warning(
                    self._signal_contract_warning_state,
                    "ambiguous",
                    assess,
                )
                if bool(decision.get("emit", False)):
                    log.warning(
                        "signal contract ambiguous backend=%s metric=%s raw=%s scale=%.4f norm=%.4f alt_scale=%.4f alt_norm=%.4f suppressed=%d",
                        self._backend_name,
                        metric,
                        raw_value,
                        float(assess["scale"]),
                        float(assess["norm"]),
                        float(assess["alt_scale"]),
                        float(assess["alt_norm"]),
                        int(decision.get("suppressed", 0) or 0),
                    )
        if self._signal_contract_warn and not ok:
            lo, hi = float(assess["lo"]), float(assess["hi"])
            self._record_signal_contract_event("out_of_contract", assess)
            decision = rate_limit_signal_contract_warning(
                self._signal_contract_warning_state,
                "out_of_contract",
                assess,
            )
            if bool(decision.get("emit", False)):
                log.warning(
                    "signal contract warning backend=%s metric=%s raw=%s scale=%.4f norm=%.4f expected=[%.1f, %.1f] suppressed=%d",
                    self._backend_name,
                    metric,
                    raw_value,
                    float(assess["scale"]),
                    norm,
                    lo,
                    hi,
                    int(decision.get("suppressed", 0) or 0),
                )
        return norm

    def _decode_profile_rf(self, signal_xml: str) -> dict[str, Any]:
        """Decode in ModemLive before carrier construction and receipt admission."""
        replies = {"/api/device/signal": signal_xml}
        addresses = {"rf.sinr_db", "rf.rsrp_dbm", "rf.rsrq_db", "rf.rssi_dbm",
                     "rf.pci", "rf.cell_id", "rf.enb_id", "rf.earfcn_dl", "slow.tac",
                     "radio.serving_band", "rf.mcs_dl0", "rf.mcs_dl1", "rf.mcs_ul",
                     "rf.cqi", "rf.cqi0", "rf.cqi1", "rf.ri", "radio.tx_power",
                     "slow.dl_bandwidth_mhz", "slow.ul_bandwidth_mhz",
                     "slow.dl_freq_mhz", "slow.ul_freq_mhz", "slow.trans_mode",
                     "radio.nr_sinr_db", "radio.nr_rsrp_dbm", "radio.nr_rsrq_db",
                     "radio.nr_arfcn"}
        addresses.update(_HUAWEI_OPTIONAL_NR_READS)
        values = self._decode_profile_values(replies, addresses)
        # The portable decoder deliberately uses a generic, privacy-safe error.
        # Refine duplicate/conflicting scalar shape using field names only.
        active = {key: tag for key, tag in _HUAWEI_OPTIONAL_NR_READS.items()
                  if key in self._profile_field_issues}
        if active:
            from arch4.modem_live.profile_definition.results import FieldIssue
            root = ET.fromstring(signal_xml)
            for key, tag in active.items():
                rows = root.findall(tag)
                if len(rows) > 1:
                    texts = {(row.text or "").strip() for row in rows}
                    issue = self._profile_field_issues[key]
                    reason = "native_scalar_conflict" if len(texts) > 1 else "native_scalar_duplicate"
                    self._profile_field_issues[key] = FieldIssue(issue.address, issue.pointer, reason)
        return values

    def _decode_status_profile(self, status_xml, signal_xml=None):
        """Convert only status/signal replies read by this existing lane."""
        replies={}
        if status_xml:replies['/api/monitoring/status']=status_xml
        if signal_xml:replies['/api/device/signal']=signal_xml
        context={'native_status_current':bool(status_xml)}
        values=self._decode_profile_values(replies,
            {'radio.network_type','traffic.connection_status_code','radio.ca_active'},context=context)
        if status_xml:
            context['native_connection_status']=values.get('traffic.connection_status_code',
                xml_text(status_xml,'ConnectionStatus')) or ''
        values.update(self._decode_profile_values(replies,{'health.radio_connected'},context=context))
        return values

    def _decode_profile_values(self, replies, addresses, *, context=None):
        decode_context = {"signal_scales": self._signal_scales, **(context or {})}
        # Ordinary reads keep their compiled dependency plan. Only readers
        # with an explicit source choice need context-dependent selection.
        selected = self._definition.available_addresses(replies, context=context, addresses=addresses)
        issues = []
        values = decode_native_profile(self._definition,
            replies, decode_context,
            addresses=selected,
            issues=issues,
        )
        for address in selected:
            self._profile_field_issues.pop(address, None)
        self._profile_field_issues.update({issue.address: issue for issue in issues})
        return values

    def _profile_signal(self, metric: str, native_raw: float, value: float | None) -> float:
        if value is None:
            return 0.0  # Existing ModemInput default; not admitted as fresh.
        original = assess_signal_contract(metric, native_raw, self._signal_scales.get(metric, 1.0))
        if value == original["norm"]:
            return self._norm_signal(metric, native_raw)
        # An edited source or conversion already yields canonical units. Check
        # that actual result without applying the old native scale a second time.
        return self._norm_signal(metric, value, scale=1.0)

    # ─── HiLink auth helpers ───────────────────────────────

    @staticmethod
    def _b64_sha256(data: str) -> str:
        """SHA256 hash → hex → base64.  Huawei's HiLink password encoding."""
        sha = hashlib.sha256(data.encode("utf-8")).digest()
        hex_str = binascii.hexlify(sha)
        return base64.urlsafe_b64encode(hex_str).decode("utf-8")

    def _get_ses_tok(self) -> bool:
        """
        GET /api/webserver/SesTokInfo → SessionID + __RequestVerificationToken.
        This is the canonical way to get CSRF tokens (not scraping meta tags).
        """
        try:
            resp = self._hilink_http_get(
                f"{self._base_url}/api/webserver/SesTokInfo",
                timeout=self._timeout,
                lane="control",
            )
            if resp.status_code != 200:
                log.warning("SesTokInfo → %d", resp.status_code)
                return False
            xml = resp.text
            ses = xml_text(xml, "SesInfo")
            tok = xml_text(xml, "TokInfo")
            if ses and tok:
                # SesInfo may be "SessionID=XYZ" or just the value
                if "=" in ses:
                    ses = ses.split("=", 1)[1]
                self._session_id = ses
                self._csrf_token = tok
                self._note_hilink_session_refreshed("ses_tok_info")
                # Set cookie so subsequent requests are in the same session
                self._session.cookies.set("SessionID", ses)
                if hasattr(self, "_fast_read_session") and hasattr(self, "_slow_read_session"):
                    self._sync_read_sessions_from_control()
                log.debug("SesTok: session=%s... token=%s...",
                          ses[:12], tok[:12])
                return True
            log.warning("SesTokInfo: missing SesInfo or TokInfo")
        except requests.RequestException as e:
            log.warning("SesTokInfo failed: %s", e)
        return False

    def _check_login_required(self) -> bool | None:
        """True: logged out; False: authenticated; None: no valid answer."""
        try:
            resp = self._hilink_http_get(
                f"{self._base_url}/api/user/state-login",
                timeout=self._timeout,
                lane="control",
            )
            if resp.status_code == 200:
                state = xml_text(resp.text, "State")
                # State: 0 = already logged in, -1 = login required
                if state and int(state) == -1:
                    return True
                if state and int(state) == 0:
                    log.debug("Already authenticated (State=0)")
                    self._logged_in = True
                    return False
        except (requests.RequestException, ValueError) as e:
            log.warning("state-login check failed: %s", e)
        return None

    def _login(self) -> bool:
        if self._authentication_contract == "hilink_sha256_type4":
            return self._login_type4()
        from arch4.modem.huawei_scram import CONTRACT
        if self._authentication_contract == CONTRACT:
            return self._login_scram()
        raise ValueError("huawei_unknown_selected_authentication_contract")

    def _login_scram(self) -> bool:
        import secrets
        from arch4.modem.huawei_scram import proofs, request_xml, response_fields, verify_server
        self._raise_if_auth_failure_latched(time.time())
        self._last_login_error_code = ""
        self._logged_in = False
        if not self._csrf_token:
            raise AuthenticationError("Huawei challenge login has no session token")
        try:
            firstnonce = secrets.token_hex(32)
            previous_token = self._csrf_token
            challenge = self._hilink_http_post(f"{self._base_url}/api/user/challenge_login",
                data=request_xml({"username":self._username,"firstnonce":firstnonce,"mode":"1","loginflag":"2"}),
                headers={"__RequestVerificationToken":self._csrf_token,"Content-Type":"application/xml"},
                timeout=self._timeout, lane="control")
            self._update_token_from_response(challenge)
            if challenge.status_code != 200:
                raise ValueError("huawei_scram_challenge_http_refused")
            if xml_text(challenge.text, "code"):
                self._last_login_error_code = str(xml_text(challenge.text, "code"))
                raise ValueError("huawei_scram_challenge_refused")
            clientproof, serverproof, finalnonce = proofs(self._password, firstnonce, response_fields(challenge.text))
            if not self._csrf_token or self._csrf_token == previous_token:
                raise ValueError("huawei_scram_challenge_token_missing")
            reply = self._hilink_http_post(f"{self._base_url}/api/user/authentication_login",
                data=request_xml({"clientproof":clientproof,"finalnonce":finalnonce,"loginflag":"2"}),
                headers={"__RequestVerificationToken":self._csrf_token,"Content-Type":"application/xml"},
                timeout=self._timeout, lane="control")
            self._update_token_from_response(reply)
            if reply.status_code != 200:
                raise ValueError("huawei_scram_authentication_http_refused")
            verify_server(reply.text, serverproof)
            # A proof response alone is not a logged-in session.
            if self._check_login_required() is not False:
                raise ValueError("huawei_scram_authenticated_session_not_confirmed")
            self._clear_auth_failure_latch()
            self._note_hilink_session_refreshed("scram_login")
            return True
        except (ValueError, requests.RequestException) as exc:
            self._connected = False
            self._logged_in = False
            self._mark_control_token_unknown()
            reason = str(exc) if isinstance(exc, ValueError) else "huawei_scram_transport_unavailable"
            self._record_auth_failure_latch(reason, time.time())
            raise AuthenticationError(reason) from exc

    def _login_type4(self) -> bool:
        """
        POST /api/user/login with Huawei SHA256 challenge-response.

        Password encoding (password_type=4):
            hash = base64(sha256( username + base64(sha256(password)) + token ))
        """
        self._raise_if_auth_failure_latched(time.time())
        if not self._csrf_token:
            log.error("Cannot login without CSRF token")
            return False
        self._last_login_error_code = ""

        # Build hashed password: b64_sha256(user + b64_sha256(pass) + token)
        pass_hash = self._b64_sha256(self._password)
        final_hash = self._b64_sha256(
            self._username + pass_hash + self._csrf_token
        )

        xml_body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<request>'
            f'<Username>{self._username}</Username>'
            f'<Password>{final_hash}</Password>'
            '<password_type>4</password_type>'
            '</request>'
        )

        try:
            resp = self._hilink_http_post(
                f"{self._base_url}/api/user/login",
                data=xml_body,
                headers={
                    "__RequestVerificationToken": self._csrf_token,
                    "Content-Type": "application/xml",
                },
                timeout=self._timeout,
                lane="control",
            )
            # After login, token rotates — grab new one from response header
            self._update_token_from_response(resp)

            if resp.status_code == 200 and "<response>OK</response>" in resp.text:
                self._logged_in = True
                self._clear_auth_failure_latch()
                self._note_hilink_session_refreshed("login")
                log.info("Logged in to %s as '%s'", self._device_label(), self._username)
                return True
            # Check for specific error codes
            err = xml_text(resp.text, "code")
            self._last_login_error_code = str(err or "")
            if err == "108006":
                log.error("Login failed: wrong username/password")
            elif err == "108007":
                log.error("Login failed: too many attempts, locked out")
            else:
                log.error("Login failed: %s", resp.text[:200])
            return False
        except requests.RequestException as e:
            log.error("Login request failed: %s", e)
            return False

    def _update_token_from_response(self, resp: requests.Response) -> None:
        """
        After every POST, B818 rotates the CSRF token.
        New token comes in __RequestVerificationTokenone header
        (or __RequestVerificationToken).
        """
        # Try the "one" variant first (post-login rotation)
        new_tok = resp.headers.get("__RequestVerificationTokenone")
        if not new_tok:
            new_tok = resp.headers.get("__RequestVerificationToken")
        if new_tok:
            self._csrf_token = new_tok
            log.debug("Token rotated: %s...", new_tok[:12])

    def _mark_control_token_unknown(self) -> None:
        """Forget a token when a POST response cannot prove its rotation."""

        self._csrf_token = None

    def _note_hilink_session_refreshed(self, source: str) -> None:
        self._hilink_session_refresh_ts = time.time()
        self._hilink_session_refresh_source = str(source or "")

    def _hilink_session_age_s(self, *, now: Optional[float] = None) -> Optional[float]:
        refresh_ts = float(getattr(self, "_hilink_session_refresh_ts", 0.0) or 0.0)
        if refresh_ts <= 0.0:
            return None
        current = time.time() if now is None else float(now)
        return max(0.0, current - refresh_ts)

    def _proactive_hilink_session_enabled(self) -> bool:
        return bool(getattr(self._access_profile, "proactive_session_refresh_enabled", True))

    def _record_hilink_session_maintenance(
        self,
        *,
        ok: bool,
        method: str,
        reason: str,
        attempted: bool = True,
        failure_reason: str = "",
    ) -> None:
        now = time.time()
        session_age_s = self._hilink_session_age_s(now=now)
        status = dict(self._last_status or {})
        payload = {
            "enabled": bool(self._proactive_hilink_session_enabled()),
            "attempted": bool(attempted),
            "ok": bool(ok),
            "method": str(method or ""),
            "reason": str(reason or ""),
            "failure_reason": str(failure_reason or ""),
            "interval_s": float(getattr(self, "_hilink_proactive_session_interval_s", 0.0) or 0.0),
            "min_gap_s": float(getattr(self, "_hilink_proactive_session_min_gap_s", 0.0) or 0.0),
            "session_age_s": None if session_age_s is None else round(float(session_age_s), 3),
            "session_refresh_source": str(getattr(self, "_hilink_session_refresh_source", "") or ""),
            "read_session_generation": int(getattr(self, "_read_session_generation", 0) or 0),
            "ts": now,
        }
        status["hilink_session_maintenance"] = payload
        self._last_status = status
        self._last_status_ts = now

    def _refresh_hilink_session_preemptively(self, *, reason: str) -> bool:
        """Refresh HiLink session/token before the fast lane is forced to miss."""
        with self._api_control_operation_lock:
            self._hilink_last_proactive_session_attempt_ts = time.time()
            try:
                if not self._get_ses_tok():
                    self._record_hilink_session_maintenance(
                        ok=False,
                        method="ses_tok_info",
                        reason=reason,
                        failure_reason="ses_tok_failed",
                    )
                    return False
                method = "ses_tok_info"
                login_required = self._check_login_required()
                if login_required is None:
                    self._record_hilink_session_maintenance(
                        ok=False, method=method, reason=reason,
                        failure_reason="login_state_unavailable",
                    )
                    return False
                if login_required:
                    self._note_api_session_expired(
                        path="/api/user/state-login",
                        lane_name="control",
                        deferred=True,
                    )
                    self._connected = False
                    self._logged_in = False
                    self._record_hilink_session_maintenance(
                        ok=False,
                        method=method,
                        reason=reason,
                        failure_reason="connectivity_repair_required",
                    )
                    return False
                self._sync_read_sessions_from_control()
                self._record_hilink_session_maintenance(ok=True, method=method, reason=reason)
                return True
            except requests.RequestException as exc:
                self._record_hilink_session_maintenance(
                    ok=False,
                    method="ses_tok_info",
                    reason=reason,
                    failure_reason=type(exc).__name__,
                )
                log.debug("Huawei HiLink proactive session refresh suppressed: %s", exc)
                return False

    def _maybe_refresh_hilink_session_preemptively(self, *, reason: str) -> bool:
        if not bool(getattr(self, "_connected", False)):
            return False
        if not self._proactive_hilink_session_enabled():
            return False
        now = time.time()
        last_attempt = float(getattr(self, "_hilink_last_proactive_session_attempt_ts", 0.0) or 0.0)
        min_gap_s = max(1.0, float(getattr(self, "_hilink_proactive_session_min_gap_s", 60.0) or 60.0))
        if last_attempt > 0.0 and now - last_attempt < min_gap_s:
            return False
        refresh_ts = float(getattr(self, "_hilink_session_refresh_ts", 0.0) or 0.0)
        if refresh_ts <= 0.0:
            return False
        interval_s = max(30.0, float(getattr(self, "_hilink_proactive_session_interval_s", 180.0) or 180.0))
        if refresh_ts > 0.0 and now - refresh_ts < interval_s:
            return False
        return self._refresh_hilink_session_preemptively(reason=reason)

    def _sync_read_sessions_from_control(self) -> None:
        """Rebuild read-only lanes from the authenticated control session."""
        cookie_map = requests.utils.dict_from_cookiejar(self._session.cookies)
        if self._session_id:
            cookie_map["SessionID"] = self._session_id
        new_fast = requests.Session()
        new_slow = requests.Session()
        new_fast.headers.update(self._session.headers)
        new_slow.headers.update(self._session.headers)
        for session in (new_fast, new_slow):
            for name, value in cookie_map.items():
                session.cookies.set(name, value)
        with self._fast_read_lock:
            self._fast_read_session = new_fast
        with self._slow_read_lock:
            self._slow_read_session = new_slow
        self._read_session_generation += 1

    @staticmethod
    def _api_lane_name(lane: str) -> str:
        lane_name = str(lane or "slow").strip().lower()
        if lane_name in {"fast", "control"}:
            return lane_name
        return "slow"

    def _single_hilink_request_lane_required(self) -> bool:
        backend = str(getattr(self, "_backend_name", "") or "").strip().lower()
        return backend in {"b818", "b535", "b535-232"}

    def _careful_huawei_http_profile(self) -> bool:
        return self._single_hilink_request_lane_required()

    def _ensure_api_lane_state(self) -> None:
        if not hasattr(self, "_session"):
            self._session = requests.Session()
        if not hasattr(self, "_api_request_lock"):
            self._api_request_lock = threading.RLock()
        if not hasattr(self, "_api_control_operation_lock"):
            self._api_control_operation_lock = threading.RLock()
        if not hasattr(self, "_fast_read_session"):
            self._fast_read_session = requests.Session()
        if not hasattr(self, "_slow_read_session"):
            self._slow_read_session = requests.Session()
        if not hasattr(self, "_fast_read_lock"):
            self._fast_read_lock = threading.RLock()
        if not hasattr(self, "_slow_read_lock"):
            self._slow_read_lock = threading.RLock()
        if not hasattr(self, "_read_session_generation"):
            self._read_session_generation = 0
        if not hasattr(self, "_access_profile"):
            self._access_profile = self._resolve_access_profile()
        if not hasattr(self, "_physical_request_authority"):
            self._physical_request_authority = ModemLivePhysicalRequestAuthority(
                self._access_profile
            )

    def _note_api_session_expired(self, *, path: str, lane_name: str, deferred: bool) -> None:
        lane = self._api_lane_name(lane_name)
        count = int(self._api_session_expiry_by_lane.get(lane, 0) or 0) + 1
        self._api_session_expiry_by_lane[lane] = count
        status = dict(self._last_status or {})
        session = {
            "expired": True,
            "lane": lane,
            "path": str(path or ""),
            "expired_count": count,
            "fast_lane_reauth_deferred": bool(deferred),
            "ts": time.time(),
        }
        status["backend_session"] = dict(session)
        status["hilink_session"] = dict(session)
        self._last_status = status
        self._last_status_ts = float(status.get("ts") or time.time())

    def _api_get_session_for_lane(self, lane: str) -> tuple[requests.Session, threading.RLock]:
        self._ensure_api_lane_state()
        lane_name = self._api_lane_name(lane)
        if self._single_hilink_request_lane_required():
            return self._session, self._api_request_lock
        if lane_name == "fast":
            return self._fast_read_session, self._fast_read_lock
        if lane_name == "control":
            return self._session, self._api_request_lock
        return self._slow_read_session, self._slow_read_lock

    @contextmanager
    def _hilink_http_request_scope(self, lane: str = "slow"):
        self._ensure_api_lane_state()
        lane_name = self._api_lane_name(lane)
        session, lock = self._api_get_session_for_lane(lane_name)
        default_lane = {
            "fast": "fast_live",
            "control": "session",
            "slow": "slow_live",
        }[lane_name]
        physical_lane = self._physical_request_authority.current_lane(
            default=default_lane
        )
        with self._physical_request_authority.request(
            lane=physical_lane,
            operation=f"huawei:http:{lane_name}",
        ) as request_entry:
            with lock:
                yield session, float(request_entry.get("waited_s", 0.0) or 0.0)

    def _hilink_http_get(self, url: str, *, timeout: Optional[Any] = None, lane: str = "slow", allow_redirects: bool = True) -> requests.Response:
        with self._hilink_http_request_scope(lane) as (session, _gate_wait_s):
            options = {} if allow_redirects else {"allow_redirects": False}
            return traced_http(session.get, url, timeout=self._timeout if timeout is None else timeout, **options)

    def prepare_read_diagnostics(self):
        from arch4.modem.read_diagnostics import ReadDiagnosticUnavailable
        from arch4.modem.huawei_read_diagnostics import HiLinkReadDiagnosticAdapter
        if not self._connected:
            raise ReadDiagnosticUnavailable("Connect to the router before running read diagnostics.")
        return HiLinkReadDiagnosticAdapter(self)

    def _hilink_http_get_timed(
        self,
        url: str,
        *,
        timeout: Optional[Any] = None,
        lane: str = "slow",
    ) -> tuple[requests.Response, float, float]:
        with self._hilink_http_request_scope(lane) as (session, gate_wait_s):
            http_started = time.perf_counter()
            resp = traced_http(session.get, url, timeout=self._timeout if timeout is None else timeout)
            return resp, max(0.0, gate_wait_s), max(0.0, time.perf_counter() - http_started)

    def _hilink_http_post(
        self,
        url: str,
        *,
        data: str,
        headers: Dict[str, str],
        timeout: Optional[Any] = None,
        lane: str = "control",
    ) -> requests.Response:
        with self._hilink_http_request_scope(lane) as (session, _gate_wait_s):
            try:
                return traced_http(session.post,
                    url,
                    data=data,
                    headers=headers,
                    timeout=self._timeout if timeout is None else timeout,
                )
            except requests.RequestException:
                self._mark_control_token_unknown()
                raise

    # ─── Connection lifecycle ───────────────────────────────

    def connect(self) -> bool:
        """
        Full HiLink connection + authentication flow:
          1. GET / to establish HTTP session
          2. GET /api/webserver/SesTokInfo for SessionID + token
          3. GET /api/user/state-login to check if login needed
          4. POST /api/user/login with SHA256 challenge-response
        """
        with self._radio_diagnostic_lock:
            self._normal_channel_conversion_key = None
        self._raise_if_auth_failure_latched(time.time())
        try:
            # Step 1: hit homepage to start session
            resp = self._hilink_http_get(self._base_url, timeout=self._timeout, lane="control")
            if resp.status_code != 200:
                log.warning("%s homepage returned %d", self._device_label(), resp.status_code)
                return False
            log.debug("Session started with %s at %s", self._device_label(), self._host)

            # Step 2: get SessionID + CSRF token
            if not self._get_ses_tok():
                if self._backend_name == "h173":
                    raise AuthenticationError("H173 session/token endpoint unavailable; selected contract cannot login")
                # Some HiLink firmware publishes the same session token only
                # in the homepage meta tag.
                self._extract_csrf_from_html(resp.text)
                if not self._csrf_token:
                    log.error("Cannot get CSRF token from %s", self._device_label())
                    return False

            # Step 3: check if login is required
            login_required = self._check_login_required()
            if login_required is None:
                log.warning("%s login state unavailable; no authentication attempted", self._device_label())
                return False
            if login_required:
                if not self._password:
                    log.error("%s requires password but none configured", self._device_label())
                    raise AuthenticationError(
                        f"{self._device_label()} requires a password but none was provided"
                    )
                # Step 4: login
                if not self._login():
                    transient_login_code = str(getattr(self, "_last_login_error_code", "") or "")
                    recovered = False
                    if transient_login_code in {"125002", "125003"}:
                        # HiLink can reject a freshly fetched token during
                        # startup/session churn even though the password is
                        # valid. The failed login is the evidence that permits
                        # one named token retry. The shared request gate owns
                        # the delay; no local sleep or duplicate token read is
                        # legal here.
                        token_refreshed = self._get_ses_tok()
                        if token_refreshed:
                            retry_login_required = self._check_login_required()
                            if retry_login_required is False:
                                recovered = True
                            elif retry_login_required is True:
                                recovered = bool(self._login())
                    if not recovered:
                        self._record_auth_failure_latch(
                            self._login_failure_reason(),
                            time.time(),
                        )
                        raise AuthenticationError(
                            f"Authentication failed for {self._device_label()} — check password"
                        )

            self._connected = True
            self._clear_auth_failure_latch()
            self._sync_read_sessions_from_control()
            if self._target_guard_enabled():
                try:
                    info = self._get_device_info_cached(max_age_s=300.0)
                except TargetGuardError:
                    raise
                except requests.RequestException:
                    info = {}
                guard_info = info if isinstance(info, dict) else {}
                if self._backend_name == "h173":
                    self._apply_target_guard(guard_info)
                else:
                    self._apply_target_guard_when_observable(guard_info)
            log.info(
                "Connected to %s at %s (auth=%s)",
                self._device_label(),
                self._host,
                "yes" if self._logged_in else "no",
            )
            return True

        except requests.RequestException as e:
            log.error("%s connection failed: %s", self._device_label(), e)
            self._connected = False
            return False

    def disconnect(self) -> None:
        """Logout and close HTTP session."""
        with self._radio_diagnostic_lock:
            self._normal_channel_conversion_key = None
        if self._logged_in:
            try:
                # Polite logout (optional, session times out anyway)
                self._api_post("/api/user/logout",
                               '<?xml version="1.0" encoding="UTF-8"?>'
                               '<request><Logout>1</Logout></request>')
            except Exception as exc:
                log.debug("disconnect: suppressed %s", exc)
                pass
            self._logged_in = False
        self._session.close()
        self._fast_read_session.close()
        self._slow_read_session.close()
        self._connected = False
        log.info("Disconnected from %s", self._device_label())

    def is_connected(self) -> bool:
        """Quick connectivity check via lightweight API call."""
        if not self._connected:
            return False
        try:
            resp = self._hilink_http_get(
                f"{self._base_url}/api/monitoring/status",
                timeout=3.0,
                lane="slow",
            )
            if resp.status_code == 200:
                # Check if we got real data, not an auth error
                if "<error>" in resp.text:
                    self._note_api_session_expired(
                        path="/api/monitoring/status",
                        lane_name="slow",
                        deferred=True,
                    )
                    self._connected = False
                    self._logged_in = False
                    log.warning(
                        "Huawei session expired; Connectivity repair required"
                    )
                    return False
                return True
            return False
        except requests.RequestException:
            self._connected = False
            return False

    # ─── CSRF token management ──────────────────────────────

    def _extract_csrf_from_html(self, html: str) -> None:
        """Extract a CSRF token from the HiLink homepage meta tag."""
        match = re.search(r'name="csrf_token"\s+content="([^"]+)"', html)
        if match:
            self._csrf_token = match.group(1)
            log.debug("CSRF from HTML: %s...", self._csrf_token[:8])

    def _control_csrf_token(self) -> Optional[str]:
        """Return the control session's current token, acquiring one if absent."""
        if self._csrf_token:
            return self._csrf_token
        if self._get_ses_tok():
            return self._csrf_token
        # Some firmware exposes the token only through the homepage.
        try:
            resp = self._hilink_http_get(self._base_url, timeout=self._timeout, lane="control")
            if resp.status_code == 200:
                self._extract_csrf_from_html(resp.text)
                return self._csrf_token
        except requests.RequestException as e:
            log.warning("CSRF refresh failed: %s", e)
        return None

    def _record_native_read_result(self, path, lane, started, *, response=None, error=None):
        """Failure-only evidence for RF and permanent-identity reads; no wire bodies."""
        if path not in {"/api/device/signal", "/api/device/information"}:
            return
        try:
            status = int(response.status_code) if response is not None else None
            code, outcome = "", ""
            if error is not None:
                outcome = "transport_error"
            else:
                outcome = "http_error" if status != 200 else ""
                body = response.text
                if not body.strip():
                    outcome = outcome or "empty_response"
                elif len(body) > 128 * 1024 or "<!" in body:
                    outcome = outcome or "response_diagnostic_size_or_declaration_limit"
                else:
                    try:
                        root = ET.fromstring(body)
                        if root.tag == "error":
                            value = root.findtext("code", "").strip()
                            code = value if re.fullmatch(r"[0-9]{1,12}", value) else ""
                            outcome = outcome or "hilink_error"
                        elif root.tag != "response":
                            outcome = outcome or "unexpected_xml_root"
                        elif path == "/api/device/information" and not any(
                            root.findtext(tag, "").strip() for tag in ("SerialNumber", "Imei", "IMEI")
                        ):
                            outcome = outcome or "permanent_identity_missing"
                    except ET.ParseError:
                        outcome = outcome or "invalid_xml"
            if outcome:
                record_native_read_failure(api_path=path, lane=lane,
                    observed_at=time.time(), elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
                    outcome=outcome, http_status=status, hilink_code=code,
                    exception_chain=native_exception_chain(error))
        except Exception as exc:
            log.error("Native read capture failed: %s", type(exc).__name__)

    def _api_get(
        self,
        path: str,
        *,
        timeout: Optional[Any] = None,
        lane: str = "slow",
    ) -> Optional[str]:
        """GET an API endpoint, return XML string or None."""
        if self._definition.has_request(path):
            path = self._definition.request(path,protocol='hilink_xml')['path']
        started = time.perf_counter()
        ok = False
        gate_wait_s = 0.0
        http_elapsed_s = 0.0
        lane_name = self._api_lane_name(lane)
        try:
            resp, gate_wait_s, http_elapsed_s = self._hilink_http_get_timed(
                f"{self._base_url}{path}",
                timeout=self._timeout if timeout is None else timeout,
                lane=lane_name,
            )
            self._record_native_read_result(path, lane_name, started, response=resp)
            if resp.status_code == 200:
                # Check for session-expired error responses
                if "<code>125002</code>" in resp.text or "<code>125003</code>" in resp.text:
                    self._note_api_session_expired(
                        path=path,
                        lane_name=lane_name,
                        deferred=True,
                    )
                    self._connected = False
                    self._logged_in = False
                    log.warning(
                        "Session expired on GET %s; Connectivity repair required",
                        path,
                    )
                    return None
                ok = True
                return resp.text
            log.warning("API GET %s → %d", path, resp.status_code)
        except requests.RequestException as e:
            self._record_native_read_result(path, lane_name, started, error=e)
            log.warning("API GET %s failed: %s", path, e)
        finally:
            self._record_api_timing(
                path,
                time.perf_counter() - started,
                ok=ok,
                gate_wait_s=gate_wait_s,
                http_elapsed_s=http_elapsed_s,
            )
        return None

    def _record_api_timing(
        self,
        path: str,
        elapsed_s: float,
        *,
        ok: bool,
        gate_wait_s: float = 0.0,
        http_elapsed_s: float = 0.0,
    ) -> None:
        key = str(path or "unknown")
        elapsed_ms = round(max(0.0, float(elapsed_s or 0.0)) * 1000.0, 1)
        gate_wait_ms = round(max(0.0, float(gate_wait_s or 0.0)) * 1000.0, 1)
        http_ms = round(max(0.0, float(http_elapsed_s or 0.0)) * 1000.0, 1)
        with self._api_timing_lock:
            current = dict(self._api_timing_by_path.get(key) or {})
            current["count"] = int(current.get("count", 0) or 0) + 1
            current["last_ms"] = elapsed_ms
            current["max_ms"] = max(float(current.get("max_ms", 0.0) or 0.0), elapsed_ms)
            current["last_gate_wait_ms"] = gate_wait_ms
            current["max_gate_wait_ms"] = max(float(current.get("max_gate_wait_ms", 0.0) or 0.0), gate_wait_ms)
            current["last_http_ms"] = http_ms
            current["max_http_ms"] = max(float(current.get("max_http_ms", 0.0) or 0.0), http_ms)
            current["last_ok"] = bool(ok)
            if not ok:
                current["fail_count"] = int(current.get("fail_count", 0) or 0) + 1
            self._api_timing_by_path[key] = current

    def _api_timing_snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._api_timing_lock:
            snapshot = {
                key: dict(value)
                for key, value in self._api_timing_by_path.items()
            }
        self._ensure_api_lane_state()
        authority = self._physical_request_authority.snapshot()
        observed_gaps = [
            float(row["observed_gap_s"])
            for row in authority.get("journal", [])
            if row.get("observed_gap_s") is not None
        ]
        snapshot["__request_gap_contract__"] = {
            "required_ms": round(float(authority["minimum_gap_s"]) * 1000.0, 1),
            "observed_count": len(observed_gaps),
            "last_ms": (
                round(observed_gaps[-1] * 1000.0, 1) if observed_gaps else 0.0
            ),
            "minimum_ms": (
                round(min(observed_gaps) * 1000.0, 1) if observed_gaps else None
            ),
            "violation_count": int(authority["gap_violation_count"]),
        }
        return snapshot

    def _record_fast_read_profile(
        self,
        *,
        started_perf: float,
        success: bool,
        required_paths: List[str],
        traffic_inline: bool,
        traffic_refresh_state: str = "",
        reason: str = "",
    ) -> None:
        duration_s = max(0.0, time.perf_counter() - float(started_perf or time.perf_counter()))
        late = bool(duration_s > self._fast_read_slo_s)
        traffic_state = str(traffic_refresh_state or "").strip()
        primary_traffic_state = traffic_state.partition(";")[0]
        miss_reason = str(reason or "").strip()
        if not miss_reason and late and not success:
            miss_reason = "slo_exceeded"
        elif not miss_reason and success and primary_traffic_state in {"slo_skipped", "failed"}:
            miss_reason = f"traffic_{primary_traffic_state}"
        profile = {
            "slo_s": float(self._fast_read_slo_s),
            "duration_ms": round(duration_s * 1000.0, 1),
            "late": late,
            "success": bool(success),
            "required_paths": list(required_paths or []),
            "signal_timeout_s": round(self._timeout_total_s(self._fast_signal_timeout), 3),
            "traffic_timeout_max_s": round(
                float(self._fast_traffic_connect_timeout_s) + float(self._fast_traffic_max_timeout_s),
                3,
            ),
            "traffic_inline": bool(traffic_inline),
            "traffic_refresh_state": traffic_state or ("refreshed" if traffic_inline else "not_attempted"),
            "traffic_cache_age_s": round(max(0.0, time.time() - float(self._last_traffic_rate_ts or 0.0)), 3)
            if self._last_traffic_rate_ts else None,
            "reason": str(reason or ""),
            "miss_reason": miss_reason or "none",
        }
        self._last_fast_read_profile = profile
        status = dict(self._last_status or {})
        status["hilink_fast_read"] = dict(profile)
        status["hilink_api_timing"] = self._api_timing_snapshot()
        self._last_status = status
        self._last_status_ts = float(status.get("ts") or time.time())
        warning_s = max(
            float(self._fast_read_slo_s) + 1.0,
            float(self._fast_read_slo_s) * 1.5,
        )
        if duration_s > warning_s:
            log.warning(
                "Huawei HiLink fast read materially exceeded profile target: target=%.1fs warning=%.1fs actual=%.1fms paths=%s reason=%s",
                self._fast_read_slo_s,
                warning_s,
                profile["duration_ms"],
                ",".join(profile["required_paths"]),
                profile["reason"] or ("ok" if success else "failed"),
            )

    @staticmethod
    def _timeout_total_s(timeout: Any) -> float:
        if isinstance(timeout, tuple):
            total = 0.0
            for value in timeout:
                try:
                    total += max(0.0, float(value or 0.0))
                except (TypeError, ValueError):
                    continue
            return total
        try:
            return max(0.0, float(timeout or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _fast_traffic_timeout(self, *, started_perf: float) -> Optional[Any]:
        elapsed_s = max(0.0, time.perf_counter() - float(started_perf or time.perf_counter()))
        # The previous fixed allowance underestimated the protected wire gap.
        admission_allowance_s = max(0.10, float(self._access_profile.min_inter_request_gap_s))
        remaining_s = float(self._fast_read_slo_s) - elapsed_s - admission_allowance_s
        connect_timeout_s = float(self._fast_traffic_connect_timeout_s)
        remaining_read_s = remaining_s - connect_timeout_s
        if remaining_read_s < float(self._fast_traffic_min_timeout_s):
            return None
        read_timeout_s = min(
            float(self._fast_traffic_max_timeout_s),
            max(float(self._fast_traffic_min_timeout_s), remaining_read_s),
        )
        return (connect_timeout_s, read_timeout_s)

    def _read_slo_limited_fast_traffic(self, *, started_perf: float) -> tuple[float, float, bool, str]:
        """Refresh live traffic opportunistically without making RF/cell reads fail.

        B818 exposes traffic as a separate HiLink endpoint. The endpoint is
        useful for speedtest/user-load visibility, but it is not allowed to
        decide whether the modem live sample exists. This read is therefore
        bounded by the remaining fast-read SLO and falls back to the last
        known rate on any miss.
        """
        age_s = time.time() - float(self._last_traffic_rate_ts or 0.0)
        if self._traffic_native_cache and age_s <= float(self._fast_traffic_cache_s):
            return float(self._last_rx_bps or 0.0), float(self._last_tx_bps or 0.0), False, "cache_hit"

        timeout = self._fast_traffic_timeout(started_perf=started_perf)
        if timeout is None:
            return float(self._last_rx_bps or 0.0), float(self._last_tx_bps or 0.0), False, "slo_skipped"

        before_ts = float(self._last_traffic_rate_ts or 0.0)
        try:
            rx_bps, tx_bps = self._read_traffic(timeout=timeout, max_age_s=0.0, lane="fast")
        except Exception as exc:
            log.debug("Huawei HiLink fast traffic refresh suppressed: %s", exc)
            return float(self._last_rx_bps or 0.0), float(self._last_tx_bps or 0.0), False, "failed"

        refreshed = float(self._last_traffic_rate_ts or 0.0) > before_ts
        return float(rx_bps or 0.0), float(tx_bps or 0.0), bool(refreshed), "refreshed" if refreshed else "stale"

    @staticmethod
    def _cache_due(cache: Dict[str, Any], ts: float, *, max_age_s: float) -> bool:
        if not cache:
            return True
        if max_age_s <= 0.0:
            return True
        return time.time() - float(ts or 0.0) > float(max_age_s)

    def _refresh_slo_limited_volume_accounting(
        self,
        *,
        started_perf: float,
        max_age_s: float = 3600.0,
    ) -> tuple[bool, str]:
        """Refresh slow volume counters opportunistically inside the ModemLive fast lane."""
        refreshed = False
        states: list[str] = []

        if self._cache_due(self._traffic_month_cache, self._traffic_month_ts, max_age_s=max_age_s):
            timeout = self._fast_traffic_timeout(started_perf=started_perf)
            if timeout is None:
                states.append("month_deferred_to_maintenance")
            else:
                before_ts = float(self._traffic_month_ts or 0.0)
                try:
                    self._get_month_statistics_cached(max_age_s=max_age_s, timeout=timeout, lane="fast")
                except Exception as exc:
                    log.debug("Huawei HiLink fast volume month refresh suppressed: %s", exc)
                    states.append("month_failed")
                else:
                    if float(self._traffic_month_ts or 0.0) > before_ts:
                        refreshed = True
                        states.append("month_refreshed")
                    else:
                        states.append("month_stale")

        if self._cache_due(self._traffic_quota_cache, self._traffic_quota_ts, max_age_s=max_age_s):
            timeout = self._fast_traffic_timeout(started_perf=started_perf)
            if timeout is None:
                states.append("quota_deferred_to_maintenance")
            else:
                before_ts = float(self._traffic_quota_ts or 0.0)
                try:
                    self._get_quota_settings_cached(max_age_s=max_age_s, timeout=timeout, lane="fast")
                except Exception as exc:
                    log.debug("Huawei HiLink fast volume quota refresh suppressed: %s", exc)
                    states.append("quota_failed")
                else:
                    if float(self._traffic_quota_ts or 0.0) > before_ts:
                        refreshed = True
                        states.append("quota_refreshed")
                    else:
                        states.append("quota_stale")

        return refreshed, "+".join(states) if states else "cache_hit"

    def _request_netmode_event_refresh(self, reason: str) -> None:
        reason = str(reason or "").strip()
        if reason:
            self._netmode_refresh_requested_reason = reason

    def live_maintenance_requested(self) -> bool:
        now = time.time()
        if self._device_info_license_retry_due(now=now):
            return True
        if not str(getattr(self, "_netmode_refresh_requested_reason", "") or "").strip():
            return False
        min_interval = max(1.0, float(getattr(self, "_netmode_event_refresh_min_interval_s", 15.0) or 15.0))
        return now - float(getattr(self, "_last_netmode_event_refresh_ts", 0.0) or 0.0) >= min_interval

    @staticmethod
    def _configured_pool_signature(bands: list[int]) -> str:
        return "+".join(f"B{int(b)}" for b in bands if int(b or 0) > 0)

    def bind_provider_pool_policy(self, resolver) -> None:
        if not callable(resolver):
            raise TypeError("huawei_provider_pool_policy_required")
        if self._provider_pool_policy is not None:
            raise RuntimeError("huawei_provider_pool_policy_already_bound")
        self._provider_pool_policy = resolver

    def _valid_configured_pool_from_netmode(self, netmode: Dict[str, Any]) -> tuple[list[int], str, str]:
        """R15.4 pool policy; native masks remain technical restriction evidence."""
        if self._provider_pool_policy is None:
            raise RuntimeError("huawei_provider_pool_policy_unbound")
        source = "foundation_provider_policy_intersection"
        if not isinstance(netmode, dict):
            return [], "", source
        plmn = str(self._plmn_cache.get("plmn") or "").strip()
        policy = (self._provider_pool_policy(domain="lte", provider_plmn=plmn)
                  if re.fullmatch(r"[0-9]{5,6}", plmn) else None)
        if policy is not None and (policy.get("schema") != "FoundationProviderBandProfile.v1"
                or policy.get("owner") != "54guc.foundation.band_auto_policy"
                or policy.get("rat") != "lte" or policy.get("band_auto_provider_plmn") != plmn):
            raise RuntimeError("huawei_provider_pool_policy_contract_invalid")
        supported = set(netmode.get("netmode_list_supported_bands") or ())
        restriction = set(netmode.get("allowed_bands") or ())
        if _is_all_bands_mask(netmode.get("lte_band_hex")):
            restriction = supported
        pool = set(policy["bands"] if policy else ()) & restriction
        if supported:
            pool &= supported
        bands = sorted(pool)
        return bands, self._configured_pool_signature(bands), source

    def _netmode_has_valid_configured_pool(self, netmode: Dict[str, Any]) -> bool:
        pool, _sig, _source = self._valid_configured_pool_from_netmode(netmode)
        return bool(pool)

    def _maybe_request_netmode_refresh_from_live_band(
        self,
        *,
        serving_band: int,
        cell_id: int = 0,
        pci: int = 0,
        earfcn: int = 0,
        netmode: Dict[str, Any],
        radio_connected: bool,
    ) -> None:
        if not bool(radio_connected) or int(serving_band or 0) <= 0:
            return
        identity = (
            int(serving_band or 0),
            int(cell_id or 0),
            int(pci or 0),
            int(earfcn or 0),
        )
        self._last_netmode_rf_identity = identity
        if not self._netmode_has_valid_configured_pool(netmode):
            self._request_netmode_event_refresh("configured_ca_pool_invalid")
            return
        cache_known = bool(getattr(self, "_netmode_cache", {}) or netmode.get("lte_band_hex"))
        configured = [
            int(b)
            for b in list(netmode.get("configured_band_pool") or netmode.get("allowed_bands") or [])
            if int(b or 0) > 0
        ]
        configured_sig = str(
            netmode.get("configured_band_pool_signature")
            or netmode.get("allowed_bands_str")
            or ""
        ).strip()
        if not cache_known and not configured_sig:
            self._request_netmode_event_refresh("netmode_cache_missing")
            return
        if configured and int(serving_band) not in configured:
            self._request_netmode_event_refresh("serving_band_outside_configured_pool")
            return

    def _read_initial_netmode_for_live_band(
        self,
        *,
        serving_band: int,
        radio_connected: bool,
    ) -> Dict[str, Any]:
        if not self._careful_huawei_http_profile():
            return {}
        netmode_cache = dict(getattr(self, "_netmode_cache", {}) or {})
        if netmode_cache:
            return netmode_cache
        if self._initial_netmode_read_attempted:
            return {}
        if not bool(radio_connected) or int(serving_band or 0) <= 0:
            return {}

        self._initial_netmode_read_attempted = True
        now = time.time()
        try:
            netmode = self._get_netmode_cached(max_age_s=0.0, include_supported_list=False)
        except Exception as exc:
            log.debug("%s initial configured-pool read suppressed: %s", self._device_label(), exc)
            self._last_netmode_event_refresh_ts = now
            self._request_netmode_event_refresh("configured_ca_pool_initial_read_failed")
            return {}

        if self._netmode_has_valid_configured_pool(netmode):
            self._netmode_refresh_requested_reason = ""
            self._last_netmode_event_refresh_ts = now
            return dict(netmode or {})

        self._last_netmode_event_refresh_ts = now
        self._request_netmode_event_refresh("configured_ca_pool_invalid")
        return dict(netmode or getattr(self, "_netmode_cache", {}) or {})

    def _refresh_netmode_if_requested(self) -> tuple[Dict[str, Any], str, bool]:
        reason = str(getattr(self, "_netmode_refresh_requested_reason", "") or "").strip()
        if not reason:
            return dict(getattr(self, "_netmode_cache", {}) or {}), "", False
        now = time.time()
        min_interval = max(1.0, float(getattr(self, "_netmode_event_refresh_min_interval_s", 15.0) or 15.0))
        if now - float(getattr(self, "_last_netmode_event_refresh_ts", 0.0) or 0.0) < min_interval:
            return dict(getattr(self, "_netmode_cache", {}) or {}), reason, False
        previous_sample_ts = float(
            getattr(self, "_netmode_ts", 0.0) or 0.0
        )
        netmode = self._get_netmode_cached(max_age_s=0.0)
        sampled_at = float(
            getattr(self, "_netmode_ts", 0.0) or 0.0
        )
        physically_refreshed = bool(sampled_at > previous_sample_ts)
        self._last_netmode_event_refresh_ts = now
        if (
            physically_refreshed
            and self._netmode_has_valid_configured_pool(netmode)
        ):
            self._netmode_refresh_requested_reason = ""
            return dict(netmode or getattr(self, "_netmode_cache", {}) or {}), reason, True
        retry_reason = (
            "configured_ca_pool_invalid"
            if physically_refreshed
            else reason
        )
        self._request_netmode_event_refresh(retry_reason)
        return (
            dict(netmode or getattr(self, "_netmode_cache", {}) or {}),
            retry_reason,
            False,
        )

    def _netmode_status_updates(self, netmode: Dict[str, Any]) -> Dict[str, Any]:
        if not netmode:
            return {}
        pool, sig, source = self._valid_configured_pool_from_netmode(netmode)
        updates = {
            "selected_band_set": list(pool),
            "selected_band_set_signature": sig,
            "lte_band_hex": netmode.get("lte_band_hex"),
            "netmode_list_lte_band_hex": netmode.get("netmode_list_lte_band_hex"),
            "netmode_list_band_list_hex": netmode.get("netmode_list_band_list_hex"),
            "netmode_list_supported_bands": list(netmode.get("netmode_list_supported_bands") or []),
        }
        supported = list(netmode.get("netmode_list_supported_bands") or [])
        if supported:
            updates["supported_lte_bands"] = supported
            updates["supported_lte_bands_source"] = "api.net.net-mode-list"
            updates["supported_lte_bands_observed_at"] = float(
                netmode.get("netmode_list_supported_bands_observed_at", 0.0)
                or 0.0
            )
        updates["configured_band_pool"] = list(pool)
        updates["configured_band_pool_signature"] = sig
        updates["configured_band_pool_source"] = source
        updates["configured_band_pool_status"] = "derived" if pool else "unavailable_provider_pool"
        return updates

    def _target_guard_observed_snapshot(self, *, max_age_s: float = 0.0) -> Dict[str, Any]:
        max_age = max(0.0, float(max_age_s or 0.0))
        # Target identity is stable device information.  RF/live status is a
        # different owner and may legitimately omit every identity field, so
        # it must never mask the canonical device-info snapshot here.
        return self._get_device_info_cached(max_age_s=max_age)

    def _record_control_transport_receipt(
        self,
        *,
        path: str,
        request_dispatched_at: float,
        outcome: str,
    ) -> HuaweiControlTransportReceipt:
        receipt = HuaweiControlTransportReceipt(
            path=str(path or ""),
            request_dispatched_at=float(request_dispatched_at or 0.0),
            transport_returned_at=time.time(),
            outcome=str(outcome or "unknown"),
        )
        self._last_control_transport_receipt = receipt
        return receipt

    def _current_netmode_transport_receipt(self) -> HuaweiControlTransportReceipt:
        receipt = self._last_control_transport_receipt
        if receipt.path != "/api/net/net-mode":
            return HuaweiControlTransportReceipt(
                path="/api/net/net-mode",
                request_dispatched_at=0.0,
                transport_returned_at=time.time(),
                outcome="current_netmode_transport_receipt_missing",
            )
        return receipt

    def _api_post(
        self,
        path: str,
        xml_body: str,
        timeout: Optional[Any] = None,
        *,
        target_guard_max_age_s: float = 0.0,
        csrf_delay_s: float = 0.5,
        continue_on_read_timeout_for_verification: bool = False,
    ) -> bool:
        """POST to an API endpoint with CSRF token. Returns success."""
        post_timeout = timeout or self._timeout
        request_dispatched_at = 0.0
        try:
            with self._api_control_operation_lock:
                if self._target_guard_enabled():
                    info = self._target_guard_observed_snapshot(
                        max_age_s=target_guard_max_age_s
                    )
                    self._apply_target_guard(info if isinstance(info, dict) else {})
                token = self._control_csrf_token()
                if not token:
                    self._record_control_transport_receipt(
                        path=path,
                        request_dispatched_at=0.0,
                        outcome="csrf_token_unavailable",
                    )
                    log.error("Cannot POST without CSRF token")
                    return False
                delay_s = max(0.0, float(csrf_delay_s or 0.0))
                if delay_s > 0.0:
                    time.sleep(delay_s)
                request_dispatched_at = time.time()
                resp = self._hilink_http_post(
                    f"{self._base_url}{path}",
                    data=xml_body,
                    headers={
                        "__RequestVerificationToken": token,
                        "Content-Type": "application/xml",
                    },
                    timeout=post_timeout,
                    lane="control",
                )
                # Token rotates after every POST — capture the new one
                self._update_token_from_response(resp)

                if resp.status_code == 200 and "OK" in resp.text:
                    self._last_post_response_unavailable_path = ""
                    self._record_control_transport_receipt(
                        path=path,
                        request_dispatched_at=request_dispatched_at,
                        outcome="confirmed_post_ok",
                    )
                    return True
                # Session expired during POST?
                if "<code>125002</code>" in resp.text or "<code>125003</code>" in resp.text:
                    self._note_api_session_expired(
                        path=path,
                        lane_name="control",
                        deferred=True,
                    )
                    self._connected = False
                    self._logged_in = False
                    log.warning(
                        "Session expired on POST %s; Connectivity repair required",
                        path,
                    )
                log.warning("API POST %s → %d: %s", path, resp.status_code,
                            resp.text[:200])
                self._record_control_transport_receipt(
                    path=path,
                    request_dispatched_at=request_dispatched_at,
                    outcome=f"http_status_{int(resp.status_code)}",
                )
                return False
        except TargetGuardError:
            self._record_control_transport_receipt(
                path=path,
                request_dispatched_at=0.0,
                outcome="target_guard_rejected",
            )
            raise
        except requests.ReadTimeout as e:
            if continue_on_read_timeout_for_verification:
                self._last_post_response_unavailable_path = str(path or "")
                self._record_control_transport_receipt(
                    path=path,
                    request_dispatched_at=request_dispatched_at,
                    outcome="response_unavailable_verification_pending",
                )
                log.warning(
                    "API POST %s response unavailable; target verification pending: %s",
                    path,
                    e,
                )
                return True
            self._last_post_response_unavailable_path = ""
            self._record_control_transport_receipt(
                path=path,
                request_dispatched_at=request_dispatched_at,
                outcome="read_timeout",
            )
            log.error("API POST %s timed out: %s", path, e)
            return False
        except requests.RequestException as e:
            self._last_post_response_unavailable_path = ""
            self._record_control_transport_receipt(
                path=path,
                request_dispatched_at=request_dispatched_at,
                outcome=f"request_error:{type(e).__name__}",
            )
            log.error("API POST %s failed: %s", path, e)
            return False

    # ─── POST that returns response body (for SMS inbox etc.) ──

    def _api_post_read(self, path: str, xml_body: str) -> Optional[str]:
        """POST and return response XML text (not just success bool)."""
        try:
            with self._api_control_operation_lock:
                token = self._control_csrf_token()
                if not token:
                    log.error("Cannot POST-read without CSRF token")
                    return None
                time.sleep(0.5)
                resp = self._hilink_http_post(
                    f"{self._base_url}{path}",
                    data=xml_body,
                    headers={
                        "__RequestVerificationToken": token,
                        "Content-Type": "application/xml",
                    },
                    timeout=self._timeout,
                    lane="control",
                )
                self._update_token_from_response(resp)
                if resp.status_code == 200:
                    if "<code>125002</code>" in resp.text or "<code>125003</code>" in resp.text:
                        self._note_api_session_expired(
                            path=path,
                            lane_name="control",
                            deferred=True,
                        )
                        self._connected = False
                        self._logged_in = False
                        return None
                    return resp.text
                log.warning("API POST-read %s → %d", path, resp.status_code)
        except requests.RequestException as e:
            log.warning("API POST-read %s failed: %s", path, e)
        return None

    # ─── Observation ────────────────────────────────────────

    def _read_modem_input_impl(self, *, actionable_only: bool) -> Optional[ModemInput]:
        if not self._connected:
            return None

        if actionable_only:
            self._maybe_refresh_hilink_session_preemptively(reason="fast_actionable_read")

        # Fast actionable snapshot is the hard real-time path. RF/cell truth is
        # mandatory; traffic is optional and SLO-limited so it can make speedtest
        # graphs responsive without turning a slow optional endpoint into a
        # false modem miss.
        fast_started_perf = time.perf_counter()
        read_started_at = time.time()
        fast_required_paths: List[str] = []
        fast_traffic_inline = False
        last_status = self._last_status if isinstance(self._last_status, dict) else {}
        fast_signal_timeout: Any = self._fast_signal_timeout
        if actionable_only:
            fast_required_paths.append("/api/device/signal")
            sig_xml = self._api_get(
                "/api/device/signal",
                timeout=fast_signal_timeout,
                lane="fast",
            )
            if not sig_xml:
                self._record_fast_read_profile(
                    started_perf=fast_started_perf,
                    success=False,
                    required_paths=fast_required_paths,
                    traffic_inline=False,
                    traffic_refresh_state="not_attempted",
                    reason="signal_unavailable",
                )
                return None
            status_xml = None
            rx_bps, tx_bps, fast_traffic_inline, fast_traffic_state = self._read_slo_limited_fast_traffic(
                started_perf=fast_started_perf
            )
            if self._careful_huawei_http_profile():
                volume_refreshed = False
                volume_state = "deferred_to_maintenance"
            else:
                volume_refreshed, volume_state = self._refresh_slo_limited_volume_accounting(
                    started_perf=fast_started_perf,
                    max_age_s=3600.0,
                )
            if volume_state != "cache_hit":
                fast_traffic_state = f"{fast_traffic_state};volume_{volume_state}"
            fast_traffic_inline = bool(fast_traffic_inline or volume_refreshed)
            wifi_on = last_status.get("wifi_enabled")
            wifi_24 = last_status.get("wifi_24ghz_enabled")
            wifi_5 = last_status.get("wifi_5ghz_enabled")
        else:
            sig_xml = self._api_get(
                "/api/device/signal",
                timeout=None,
                lane="slow",
            )
            if not sig_xml:
                return None
            status_xml = self._api_get("/api/monitoring/status")
            rx_bps, tx_bps = self._read_traffic()
            fast_traffic_state = "not_fast"
            self._last_rx_bps = float(rx_bps or 0.0)
            self._last_tx_bps = float(tx_bps or 0.0)
            wifi_on, wifi_24, wifi_5 = self._read_wifi_status()

        try:
            sig_root = ET.fromstring(sig_xml)
        except ET.ParseError as e:
            log.warning("Signal XML parse error: %s", e)
            return None

        signal_present_tags = frozenset(
            str(element.tag).rsplit("}", 1)[-1].strip().lower()
            for element in sig_root.iter()
            if str(element.tag).strip()
        )
        signal_value_tags = frozenset(
            str(element.tag).rsplit("}", 1)[-1].strip().lower()
            for element in sig_root.iter()
            if str(element.tag).strip()
            and str(element.text or "").strip()
        )
        status_value_tags: frozenset[str] = frozenset()
        if status_xml:
            try:
                status_root = ET.fromstring(status_xml)
                status_value_tags = frozenset(
                    str(element.tag).rsplit("}", 1)[-1].strip().lower()
                    for element in status_root.iter()
                    if str(element.tag).strip()
                    and str(element.text or "").strip()
                )
            except ET.ParseError:
                status_value_tags = frozenset()

        def sig(tag, default=""):
            el = sig_root.find(tag)
            return el.text.strip() if el is not None and el.text else default

        # ── Parse RF signals ──
        sinr_raw = self._parse_signal_val(sig("sinr"))
        rsrp_raw = self._parse_signal_val(sig("rsrp"))
        rsrq_raw = self._parse_signal_val(sig("rsrq"))
        rssi_raw = self._parse_signal_val(sig("rssi"))
        profile_rf = self._decode_profile_rf(sig_xml)
        profile_state = self._decode_status_profile(status_xml,sig_xml)
        sinr = self._profile_signal("sinr", sinr_raw, profile_rf.get("rf.sinr_db"))
        rsrp = self._profile_signal("rsrp", rsrp_raw, profile_rf.get("rf.rsrp_dbm"))
        rsrq = self._profile_signal("rsrq", rsrq_raw, profile_rf.get("rf.rsrq_db"))
        rssi = self._profile_signal("rssi", rssi_raw, profile_rf.get("rf.rssi_dbm"))
        nr_sinr_raw = self._parse_signal_val(sig("nrsinr"))
        nr_rsrp_raw = self._parse_signal_val(sig("nrrsrp"))
        nr_rsrq_raw = self._parse_signal_val(sig("nrrsrq"))
        nr_sinr = (self._profile_signal("sinr", nr_sinr_raw, profile_rf["radio.nr_sinr_db"])
                   if "radio.nr_sinr_db" in profile_rf else
                   self._norm_signal("sinr", nr_sinr_raw) if nr_sinr_raw else 0.0)
        nr_rsrp = (self._profile_signal("rsrp", nr_rsrp_raw, profile_rf["radio.nr_rsrp_dbm"])
                   if "radio.nr_rsrp_dbm" in profile_rf else
                   self._norm_signal("rsrp", nr_rsrp_raw) if nr_rsrp_raw else 0.0)
        nr_rsrq = (self._profile_signal("rsrq", nr_rsrq_raw, profile_rf["radio.nr_rsrq_db"])
                   if "radio.nr_rsrq_db" in profile_rf else
                   self._norm_signal("rsrq", nr_rsrq_raw) if nr_rsrq_raw else 0.0)

        # ── Band and cell identity ──
        band = int(profile_rf.get("radio.serving_band", self._parse_int(sig("band"))) or 0)
        pci = int(profile_rf.get("rf.pci", self._parse_int(sig("pci") or sig("sc"))) or 0)
        cell_id_raw = sig("cell_id")
        cell_id = int(profile_rf.get("rf.cell_id", self._parse_int(cell_id_raw)) or 0)
        tac_raw = sig("tac") or sig("lac")
        tac = int(profile_rf.get("slow.tac", self._parse_int(tac_raw)) or 0)

        # eNB ID = cell_id >> 8 (for LTE mode=7)
        enb_id = int(profile_rf.get("rf.enb_id", cell_id >> 8 if cell_id else 0) or 0)

        # Both native formats must be adapted before any identity is published.
        earfcn_raw = sig("earfcn")
        earfcn, earfcn_ul = parse_serving_earfcns(
            earfcn_raw, band=band, dl_frequency_100khz=sig("ltedlfreq"),
            ul_frequency_100khz=sig("lteulfreq"),
        )
        earfcn = int(profile_rf.get("rf.earfcn_dl", earfcn) or 0)
        # Retain the native spelling even when the adapter corrects its units.
        from arch4.modem.huawei_parsers import parse_earfcn_dl, parse_earfcn_ul
        raw_channels = (parse_earfcn_dl(earfcn_raw), parse_earfcn_ul(earfcn_raw))
        nr_earfcn = int(profile_rf.get("radio.nr_arfcn", self._parse_int(sig("nrearfcn"))) or 0)
        nr_dl_bw_str = sig("nrdlbandwidth")
        nr_ul_bw_str = sig("nrulbandwidth")
        nr_dl_bw_mhz = self._parse_bandwidth_mhz(nr_dl_bw_str)
        if nr_dl_bw_mhz is not None and (nr_dl_bw_mhz < 0 or nr_dl_bw_mhz > 400
                or str(nr_dl_bw_str).strip().startswith("-")):
            nr_dl_bw_mhz = None
        nr_ul_bw_mhz = self._parse_bandwidth_mhz(nr_ul_bw_str)
        if self._definition.has_read("slow.nr_bandwidth_mhz"):
            nr_dl_bw_mhz = profile_rf.get("slow.nr_bandwidth_mhz")
        if self._definition.has_read("slow.nr_ul_bandwidth_mhz"):
            nr_ul_bw_mhz = profile_rf.get("slow.nr_ul_bandwidth_mhz")
        nr_tx_power = sig("nrtxpower")
        nr_dl_mcs_0, nr_dl_mcs_1 = self._parse_dl_mcs(sig("nrdlmcs"))
        nr_ul_mcs = self._parse_ul_mcs(sig("nrulmcs"))

        # ── MCS ──
        # B818 DL MCS format: "mcsDownCarrier1Code0:XX mcsDownCarrier1Code1:YY"
        dl_mcs_raw = sig("dl_mcs")
        has_dl_mcs_evidence = bool(str(dl_mcs_raw or "").strip())
        dl_mcs_code0_present = bool(re.search(r"Code0:\s*\d+", str(dl_mcs_raw or "")))
        dl_mcs_code1_present = bool(re.search(r"Code1:\s*\d+", str(dl_mcs_raw or "")))
        native_mcs_dl_0, native_mcs_dl_1 = self._parse_dl_mcs(dl_mcs_raw)
        mcs_dl_0 = int(profile_rf.get("rf.mcs_dl0", native_mcs_dl_0) or 0)
        mcs_dl_1 = int(profile_rf.get("rf.mcs_dl1", native_mcs_dl_1) or 0)
        # Use Code0 as primary DL MCS (Code1 is MIMO second stream)
        mcs_dl = mcs_dl_0

        # UL MCS: "mcsUpCarrier1:XX"
        ul_mcs_raw = sig("ul_mcs")
        mcs_ul = int(profile_rf.get("rf.mcs_ul", self._parse_ul_mcs(ul_mcs_raw)) or 0)
        # B818-specific effective modulation:
        # - use nominal MCS buckets for fresh evidence
        # - for retx markers (28..31), keep last known valid Qm
        qam_dl0, mcs_dl0_retx = _resolve_b818_qm(mcs_dl_0, self._last_qm_dl0)
        qam_dl1, mcs_dl1_retx = _resolve_b818_qm(mcs_dl_1, self._last_qm_dl1)
        qam_ul, mcs_ul_retx = _resolve_b818_qm(mcs_ul, self._last_qm_ul)
        if qam_dl0 > 0:
            self._last_qm_dl0 = int(qam_dl0)
        if qam_dl1 > 0:
            self._last_qm_dl1 = int(qam_dl1)
        if qam_ul > 0:
            self._last_qm_ul = int(qam_ul)
        has_qam = any(q > 0 for q in (qam_dl0, qam_dl1, qam_ul))
        used_retx_hold = bool(mcs_dl0_retx or mcs_dl1_retx or mcs_ul_retx)
        qam_conf = 0.65 if (has_qam and used_retx_hold) else (0.75 if has_qam else 0.0)

        # ── CQI ──
        cqi0 = sig("cqi0")
        cqi1 = sig("cqi1")
        cqi0_val = profile_rf.get("rf.cqi0", _parse_b818_cqi(cqi0))
        cqi1_val = profile_rf.get("rf.cqi1", _parse_b818_cqi(cqi1))
        cqi = int(profile_rf.get("rf.cqi", cqi0_val) or 0)
        # A present HiLink sentinel (for example B535 CQI1=127) is not a
        # measured CQI zero. Preserve the parser's missingness in receipt proof.
        invalid_cqi = {tag for tag, value in (("cqi0", cqi0_val), ("cqi1", cqi1_val)) if value is None}
        signal_value_tags = signal_value_tags.difference(invalid_cqi)

        # ── MIMO rank ──
        # Some HiLink firmwares expose RI directly while withholding
        # CQI/MCS/QAM scheduler lanes. Use direct modem RI first; derive from
        # MCS codewords only when MCS evidence exists.
        ri_raw = (
            sig("ri")
            or sig("rankindicator")
            or sig("rank_indicator")
            or sig("rank")
            or sig("lte_ri")
        )
        direct_ri = self._parse_int(ri_raw)
        ri_source = ""
        if direct_ri > 0:
            ri = direct_ri
            ri_source = "native_signal"
        elif has_dl_mcs_evidence:
            if dl_mcs_code0_present and dl_mcs_code1_present:
                ri = 2
                ri_source = "derived_mcs_codewords"
            elif dl_mcs_code0_present or dl_mcs_code1_present:
                ri = 1
                ri_source = "derived_mcs_codewords"
            else:
                ri = 0
        elif actionable_only:
            if str(last_status.get("ri_source") or "").strip():
                ri = int(last_status.get("ri") or 0)
                ri_source = str(last_status.get("ri_source") or "")
            else:
                ri = 0
        else:
            ri = 0
        if "rf.ri" in profile_rf and (profile_rf["rf.ri"] is not None or not actionable_only):
            decoded_ri = int(profile_rf.get("rf.ri") or 0)
            if decoded_ri != ri:
                ri_source = "profile_native_signal" if decoded_ri > 0 else ""
            ri = decoded_ri
        ri_valid = bool(ri > 0 and ri_source)

        # ── CA detection ──
        net_type_ex = (
            str(last_status.get("network_type_ex") or "")
            if actionable_only
            else xml_text(status_xml, "CurrentNetworkTypeEx") if status_xml else ""
        )
        network_type_value = (profile_state['radio.network_type']or''
            if 'radio.network_type'in profile_state else
            last_status.get('network_type',NETWORK_TYPE_MAP.get(net_type_ex,net_type_ex))
            if actionable_only else NETWORK_TYPE_MAP.get(net_type_ex,net_type_ex))
        ca_active = bool(last_status.get("ca_active", False)) if actionable_only else (net_type_ex == "1011")
        if 'radio.ca_active'in profile_state:
            ca_active=profile_state['radio.ca_active']is True
        nr_present = any(
            bool(value)
            for value in (
                nr_sinr_raw,
                nr_rsrp_raw,
                nr_rsrq_raw,
                nr_earfcn,
                nr_dl_bw_str,
                nr_ul_bw_str,
                nr_dl_mcs_0,
                nr_dl_mcs_1,
                nr_ul_mcs,
                nr_tx_power,
                nr_sinr, nr_rsrp, nr_rsrq,
            )
        )
        if nr_present:
            self._nr_evidence_seen = True
        nr_mode = str(last_status.get("nr5g_mode") or "") if actionable_only else ""
        if not nr_mode:
            if net_type_ex == "111":
                nr_mode = "SA"
            elif nr_present:
                nr_mode = "NSA"
        self._last_huawei_acquired_fields = (
            self._huawei_fields_proven_by_response(
                signal_present_tags=signal_present_tags,
                signal_value_tags=signal_value_tags,
                status_value_tags=status_value_tags,
                nr_present=nr_present,
                nr_evidence_seen=self._nr_evidence_seen,
                nr_bandwidth_observed=nr_dl_bw_mhz is not None,
            )
        )
        fresh_fields = set(self._last_huawei_acquired_fields)
        for address in profile_rf:
            name = address.split(".", 1)[1]
            names = {name}
            if address == "radio.serving_band":
                names |= {"band", "serving_band_signature", "active_band_set",
                          "active_band_set_signature", "band_set", "band_set_signature"}
            elif address == "rf.earfcn_dl":
                names.add("earfcn")
            elif address == "rf.ri":
                names |= {"ri_valid", "ri_source"}
            elif address.startswith("radio.nr_"):
                names.add({"nr_arfcn": "nr5g_arfcn", "nr_sinr_db": "nr5g_sinr_db",
                           "nr_rsrp_dbm": "nr5g_rsrp_dbm", "nr_rsrq_db": "nr5g_rsrq_db"}.get(name, name))
            fresh_fields.difference_update(names)
            if profile_rf.get(address) is not None:
                fresh_fields.update(names)
        self._last_huawei_acquired_fields = frozenset(fresh_fields)
        for address,value in profile_state.items():
            name=address.split('.',1)[1]
            names={name}
            if name=='radio_connected':names.add('radio_state_text')
            if name=='ca_active':names.add('ca_state_text')
            self._last_huawei_acquired_fields-=frozenset(names)
            if value is not None and value!='':self._last_huawei_acquired_fields|=frozenset(names)
        nr_state_text = ""
        if nr_mode == "SA":
            nr_state_text = "ACTIVE"
        elif nr_mode == "NSA":
            nr_state_text = "ACTIVE"
        nr_cells = []
        if nr_present:
            nr_cells.append(
                {
                    "role": "PCell",
                    "arfcn": profile_rf.get("radio.nr_arfcn", nr_earfcn or None),
                    "bandwidth_dl_mhz": nr_dl_bw_mhz or None,
                    "bandwidth_ul_mhz": nr_ul_bw_mhz or None,
                    "rsrp_dbm": profile_rf.get("radio.nr_rsrp_dbm", nr_rsrp or None),
                    "rsrq_db": profile_rf.get("radio.nr_rsrq_db", nr_rsrq or None),
                    "sinr_db": profile_rf.get("radio.nr_sinr_db", nr_sinr or None),
                    "tx_power": nr_tx_power or None,
                    "mcs_dl0": nr_dl_mcs_0 or None,
                    "mcs_dl1": nr_dl_mcs_1 or None,
                    "mcs_ul": nr_ul_mcs or None,
                }
            )
            if self._definition.has_read("radio.nr_mcs_dl"):
                nr_cells[0]["mcs_dl0"] = profile_rf.get("radio.nr_mcs_dl")
                code1 = re.search(r"(?:NR)?mcsDownCarrier1Code1:\s*([^\s]+)", sig("nrdlmcs"))
                if (profile_rf.get("radio.nr_mcs_dl") is None or code1 is not None
                        and (not re.fullmatch(r"[0-9]{1,2}", code1.group(1))
                             or int(code1.group(1)) > 31)):
                    nr_cells[0]["mcs_dl1"] = None
            if self._definition.has_read("radio.nr_mcs_ul"):
                nr_cells[0]["mcs_ul"] = profile_rf.get("radio.nr_mcs_ul")

        # B818 doesn't expose per-SCell data, so we build a minimal
        # CarrierInfo for the PCell only.
        dlbw_str = sig("dlbandwidth")
        ulbw_str = sig("ulbandwidth")
        dl_bw_mhz = profile_rf.get("slow.dl_bandwidth_mhz")
        ul_bw_mhz = profile_rf.get("slow.ul_bandwidth_mhz")
        if dl_bw_mhz is not None and dl_bw_mhz != BW_TO_MHZ.get(dlbw_str):
            dlbw_str = f"{dl_bw_mhz:g}MHz"
        if ul_bw_mhz is not None and ul_bw_mhz != BW_TO_MHZ.get(ulbw_str):
            ulbw_str = f"{ul_bw_mhz:g}MHz"
        bw_mhz = float(dl_bw_mhz) if dl_bw_mhz is not None else 10.0

        pcell_carrier = CarrierInfo(
            band=band,
            earfcn=earfcn,
            pci=pci,
            bandwidth_mhz=bw_mhz,
            sinr_db=sinr,
            rsrp_dbm=rsrp,
            state=1,
            is_pcell=True,
        )
        carriers = [pcell_carrier]

        # CA activity and detailed component evidence are separate truth cells.
        # Signature cells contain only known LTE band numbers.
        ca_sig = f"B{band}" if band else ""

        # ── Radio connected / uptime ──
        conn_status = xml_text(status_xml, "ConnectionStatus") if status_xml else ""
        if 'traffic.connection_status_code'in profile_state:
            conn_status=profile_state['traffic.connection_status_code']or''
        rrc_raw = sig("rrc") or sig("rrc_status")
        rrc_state = str(rrc_raw or "").strip().lower()
        if rrc_state in {"1", "connected", "connect", "rrcconnected", "registered"}:
            rrc_translated = "Connected"
        elif rrc_state in {"0", "idle", "noconn", "noconnection", "rrcidle"}:
            rrc_translated = "Idle"
        else:
            rrc_translated = ""
        rf_evidence_connected = bool(
            rrc_translated == "Connected"
            or band
            or earfcn
            or cell_id
            or pci
            or rsrp_raw
            or sinr_raw
        )
        if status_xml:
            radio_connected = bool(conn_status == "901" or rf_evidence_connected)
        else:
            radio_connected = rf_evidence_connected
        if 'health.radio_connected'in profile_state:
            radio_connected=profile_state['health.radio_connected']is True
        elif self._definition.has_read('health.radio_connected'):
            # A status-only human rule cannot be refreshed by a signal poll.
            radio_connected=bool(last_status.get('radio_connected',radio_connected))
            self._last_huawei_acquired_fields-=frozenset({'radio_connected','radio_state_text'})
        packet_session_connected = (conn_status == "901") if conn_status else None
        traffic_valid = bool(packet_session_connected is not False)
        traffic_state_detail = "packet_session_connected" if packet_session_connected is True else "unknown"
        traffic_invalid_reason = ""
        if packet_session_connected is False:
            traffic_state_detail = "packet_session_invalid"
            traffic_invalid_reason = f"connection_status_{conn_status or 'unknown'}"

        # Packet-session time belongs to the traffic reply, not router boot.
        packet_time = self._traffic_native_cache.get("traffic_session_duration_s")
        uptime_s = float(packet_time) if packet_time is not None else 0.0
        uptime_source = (
            "reported" if float(self._traffic_native_ts or 0.0) >= read_started_at
            else "held"
        ) if packet_time is not None else "unavailable"

        # ── Neighbor PCIs (limited — no RSRP) ──
        nei_raw = sig("nei_cellid")
        neighbors = self._parse_neighbor_pcis(nei_raw, band)
        for n in neighbors:
            n.rsrp_dbm = self._norm_signal("rsrp", n.rsrp_dbm)
            n.rsrq_db = self._norm_signal("rsrq", n.rsrq_db)

        # ── Extra status snapshot (for GUI) ──
        rrc = rrc_translated or ("Connected" if radio_connected else "Idle")
        # RRC has an existing same-read derived fallback. Convert this one
        # dependent field after that value exists, without duplicating the
        # RF/connection rules inside the editable profile or adding I/O.
        rrc_values = self._decode_profile_values(
            {"/api/device/signal": sig_xml}, {"slow.rrc"},
            context={"native_rrc_fallback": rrc})
        if "slow.rrc" in rrc_values:
            rrc = rrc_values["slow.rrc"] or ""
            self._last_huawei_acquired_fields -= frozenset({"rrc"})
            if rrc_values["slow.rrc"] is not None:
                self._last_huawei_acquired_fields |= frozenset({"rrc"})
        transmode = profile_rf.get("slow.trans_mode") or ""
        if not transmode and actionable_only:
            # Fast samples should not erase a value reported by an earlier full
            # modem read; only the modem backend owns this held value.
            transmode = str(last_status.get("trans_mode") or "")
        tx_power = profile_rf.get("radio.tx_power", sig("txpower"))
        dlfreq_raw = sig("dlfrequency")
        ulfreq_raw = sig("ulfrequency")
        dlfreq_mhz = profile_rf.get("slow.dl_freq_mhz")
        ulfreq_mhz = profile_rf.get("slow.ul_freq_mhz")
        if actionable_only:
            plmn_info = dict(self._plmn_cache or {})
            if not plmn_info:
                plmn_info = {
                    "plmn": str(last_status.get("provider_plmn") or last_status.get("plmn") or ""),
                    "name": str(last_status.get("plmn_name") or ""),
                    "short_name": str(last_status.get("provider_name") or last_status.get("provider") or ""),
                    "full_name": str(last_status.get("network_provider_fullname") or ""),
                    "rat": "",
                }
        else:
            plmn_info = self._get_plmn_cached()
        plmn = plmn_info.get("plmn", "")
        provider = (
            resolve_provider_name(plmn)
            or plmn_info.get("short_name", "")
            or plmn_info.get("full_name", "")
            or ""
        ) if plmn else ""
        if '_profile_provider_name'in plmn_info:
            provider=plmn_info['_profile_provider_name']or''
        ims = ""
        if status_xml:
            for tag in ("IMSRegistrationStatus", "IMSRegStatus",
                        "IMSVoiceStatus", "IMSSmsStatus", "IMSReg"):
                ims = xml_text(status_xml, tag)
                if ims:
                    break
        if actionable_only:
            info = dict(self._device_info_cache or {})
            if not info:
                info = {
                    "model": last_status.get("model"),
                    "firmware": last_status.get("firmware"),
                    "software_version": last_status.get("software_version") or last_status.get("software"),
                    "webui": last_status.get("webui"),
                    "serial": last_status.get("serial"),
                    "imei": last_status.get("imei"),
                    "imsi": last_status.get("imsi"),
                    "hardware_version": last_status.get("hardware_version"),
                    "config_version": last_status.get("config_version") or last_status.get("configuration_file_version"),
                    "lan_mac": last_status.get("lan_mac"),
                    "wan_ip": last_status.get("wan_ip"),
                }
        else:
            info = self._get_device_info_cached()
        if actionable_only:
            netmode = dict(getattr(self, "_netmode_cache", {}) or {})
            if not netmode:
                netmode = self._read_initial_netmode_for_live_band(
                    serving_band=band,
                    radio_connected=radio_connected,
                )
            self._maybe_request_netmode_refresh_from_live_band(
                serving_band=band,
                cell_id=cell_id,
                pci=pci,
                earfcn=earfcn,
                netmode=netmode,
                radio_connected=radio_connected,
            )
        else:
            netmode = self._get_netmode_cached()
        if actionable_only:
            antenna = dict(self._antenna_cache or {})
            antenna_label = str(antenna.get("antenna") or "")
            if not antenna_label:
                last_antenna = str(last_status.get("antenna") or "")
                if last_antenna:
                    antenna = {"antenna": last_antenna}
        else:
            antenna = self._get_antenna_cached()
        native_traffic = dict(self._traffic_native_cache or {})
        if actionable_only:
            month_stats = dict(self._traffic_month_cache or {})
            quota_settings = dict(self._traffic_quota_cache or {})
        else:
            month_stats = self._get_month_statistics_cached()
            quota_settings = self._get_quota_settings_cached()
        configured_pool, configured_pool_sig, configured_pool_source = self._valid_configured_pool_from_netmode(netmode)
        configured_pool_age_s = (
            max(0.0, time.time() - float(self._netmode_ts or 0.0))
            if self._netmode_ts
            else None
        )
        supported_lte_bands = list(netmode.get("netmode_list_supported_bands") or [])
        supported_lte_bands_observed_at = float(
            netmode.get("netmode_list_supported_bands_observed_at", 0.0) or 0.0
        )

        self._last_status = {
            "ts": time.time(),
            "rrc": rrc,
            "trans_mode": transmode,
            "network_type_ex": net_type_ex,
            "network_type": network_type_value,
            "plmn": plmn,
            "provider": provider,
            "provider_name": provider,
            "provider_plmn": plmn,
            "plmn_name": plmn_info.get("name", ""),
            "network_provider_fullname": (
                plmn_info.get("full_name")
                or plmn_info.get("short_name")
                or provider
                or None
            ),
            "tac": tac or None,
            "cell_id": cell_id or None,
            "enb_id": enb_id or None,
            "band": band or None,
            "serving_band": band or None,
            "serving_band_signature": f"B{band}" if band else "",
            "ca_active": ca_active,
            "ca_state_text": "ACTIVE" if ca_active else "INACTIVE",
            "radio_connected": radio_connected,
            "radio_state_text": "Connected" if radio_connected else "Disconnected",
            "connection_status_code": conn_status or "",
            "packet_session_connected": packet_session_connected,
            "traffic_valid": traffic_valid,
            "traffic_state_detail": traffic_state_detail,
            "traffic_invalid_reason": traffic_invalid_reason,
            "dl_bandwidth": dlbw_str or None,
            "ul_bandwidth": ulbw_str or None,
            "dl_bandwidth_mhz": dl_bw_mhz,
            "ul_bandwidth_mhz": ul_bw_mhz,
            "earfcn_dl": earfcn or None,
            "earfcn_ul": earfcn_ul or None,
            "dl_freq_mhz": dlfreq_mhz,
            "ul_freq_mhz": ulfreq_mhz,
            "mcs_dl0": mcs_dl_0,
            "mcs_dl1": mcs_dl_1,
            "mcs_ul": mcs_ul,
            "tbs_dl0": _dl_mcs_to_tbs(mcs_dl_0),
            "tbs_dl1": _dl_mcs_to_tbs(mcs_dl_1),
            "tbs_ul": _ul_mcs_to_tbs(mcs_ul),
            "qam_dl0": lte_qm_to_qam(qam_dl0),
            "qam_dl1": lte_qm_to_qam(qam_dl1),
            "mod0": qam_dl0,
            "mod1": qam_dl1,
            "qam_ul": qam_ul,
            "qam_source": "inferred",
            "qam_confidence": qam_conf,
            "mcs_dl_retx": bool(mcs_dl0_retx),
            "mcs_dl1_retx": bool(mcs_dl1_retx),
            "mcs_ul_retx": bool(mcs_ul_retx),
            "cqi0": cqi0_val,
            "cqi1": cqi1_val,
            "tx_power": tx_power or None,
            "ri": ri,
            "ri_valid": ri_valid,
            "ri_source": ri_source,
            "session_uptime_s": uptime_s,
            "session_uptime_source": uptime_source,
            "rtt_ms": None,
            "ims": ims or None,
            "nr5g_active": bool(nr_present),
            "nr_bandwidth_mhz": nr_dl_bw_mhz,
            "nr5g_mode": nr_mode or None,
            "nr5g_state_text": nr_state_text or None,
            "nr5g_cell_id": None,
            "nr5g_pci": None,
            "nr5g_arfcn": nr_earfcn or None,
            "nr5g_band": None,
            "nr5g_band_set": [],
            "nr5g_rsrp_dbm": profile_rf.get("radio.nr_rsrp_dbm", nr_rsrp or None),
            "nr5g_rsrq_db": profile_rf.get("radio.nr_rsrq_db", nr_rsrq or None),
            "nr5g_sinr_db": profile_rf.get("radio.nr_sinr_db", nr_sinr or None),
            "nr5g_rx0_rsrp_dbm": None,
            "nr5g_rx1_rsrp_dbm": None,
            "nr5g_cells": nr_cells,
            "nr5g_sa_band_lock": None,
            "nr5g_nsa_band_lock": None,
            "model": info.get("model") if info else None,
            "router_model": info.get("model") if info else None,
            "firmware": info.get("firmware") if info else None,
            "software": info.get("software_version") if info else None,
            "software_version": info.get("software_version") if info else None,
            "webui": info.get("webui") if info else None,
            "serial": info.get("serial") if info else None,
            "imei": info.get("imei") if info else None,
            "imsi": info.get("imsi") if info else None,
            "hardware_version": info.get("hardware_version") if info else None,
            "config_version": info.get("config_version") if info else None,
            "configuration_file_version": info.get("config_version") if info else None,
            "lan_mac": info.get("lan_mac") if info else None,
            "wan_ip": info.get("wan_ip") if info else None,
            "antenna": antenna.get("antenna") if antenna else None,
            "antenna_mode": antenna.get("antenna") if antenna else None,
            "antenna_mode_source": "huawei_static_antenna" if antenna and antenna.get("antenna") else "",
            "selected_band_set": list(configured_pool),
            "selected_band_set_signature": configured_pool_sig,
            "configured_band_pool": list(configured_pool),
            "configured_band_pool_signature": configured_pool_sig,
            "configured_band_pool_source": configured_pool_source,
            "configured_band_pool_age_s": configured_pool_age_s,
            "configured_band_pool_last_sample_ts": float(self._netmode_ts or 0.0),
            "lte_band_hex": netmode.get("lte_band_hex"),
            "netmode_list_lte_band_hex": netmode.get("netmode_list_lte_band_hex"),
            "netmode_list_band_list_hex": netmode.get("netmode_list_band_list_hex"),
            "netmode_list_supported_bands": list(netmode.get("netmode_list_supported_bands") or []),
            "supported_lte_bands": supported_lte_bands,
            "supported_lte_bands_source": "api.net.net-mode-list" if supported_lte_bands else "",
            "supported_lte_bands_observed_at": supported_lte_bands_observed_at,
            "raw_sinr": sinr_raw,
            "raw_rsrp": rsrp_raw,
            "raw_rsrq": rsrq_raw,
            "raw_rssi": rssi_raw,
            "norm_sinr": sinr,
            "norm_rsrp": rsrp,
            "norm_rsrq": rsrq,
            "norm_rssi": rssi,
            "rx_bytes_per_sec": rx_bps,
            "tx_bytes_per_sec": tx_bps,
            "wifi_enabled": wifi_on,
            "wifi_24ghz_enabled": wifi_24,
            "wifi_5ghz_enabled": wifi_5,
        }
        self._last_status.update(native_traffic)
        self._last_status.update(month_stats)
        self._last_status.update(quota_settings)
        self._last_status["hilink_api_timing"] = self._api_timing_snapshot()
        self._last_status_ts = self._last_status["ts"]
        if actionable_only:
            self._record_fast_read_profile(
                started_perf=fast_started_perf,
                success=True,
                required_paths=fast_required_paths,
                traffic_inline=fast_traffic_inline,
                traffic_refresh_state=fast_traffic_state,
            )
        from arch4.modem_live.radio_identity_audit import identity_diagnostic, channel_disagrees
        # Only supplied, selected read rules can add these optional quantities.
        # None clears this packet's value; no previous packet fills the gap.
        for address in _HUAWEI_OPTIONAL_NR_READS:
            if self._definition.has_read(address):
                self._last_status[address.split(".", 1)[1]] = profile_rf.get(address)
        converted = raw_channels != (earfcn, earfcn_ul)
        unresolved = (channel_disagrees(band, earfcn)
                      or channel_disagrees(band, earfcn_ul, uplink=True))
        conversion_key = (self._selected_profile_id, info.get("model"), info.get("serial"),
                          band, raw_channels, earfcn, earfcn_ul)
        with self._radio_diagnostic_lock:
            announce_conversion = converted and conversion_key != self._normal_channel_conversion_key
            self._normal_channel_conversion_key = conversion_key if converted and not unresolved else None
        if unresolved or announce_conversion:
            # Same parsed XML object, before canonical conversion; never cached
            # identity, whole XML, credentials, SIM IDs or unknown future tags.
            native_fields = {}
            for tag in ("band", "cell_id", "pci", "earfcn", "ltedlfreq", "lteulfreq", "dlfrequency", "ulfrequency"):
                value = sig(tag)
                native_fields[tag] = (value if len(value) <= 128 and re.fullmatch(
                    r"(?:[0-9. +:/\-]|DL|UL|MHz|KHz|kHz|Hz)*", value)
                    else "[omitted:non_radio_token_or_size]")
            self._last_status["backend_diagnostic"] = identity_diagnostic(
                backend="huawei", observed_at=time.time(),
                source=("huawei_native_channel_units" if raw_channels != (earfcn, earfcn_ul)
                        else "huawei_native_signal_identity"),
                severity="WARNING" if unresolved else "INFO",
                kind="radio_identity_inconsistency" if unresolved else "radio_identity_normalization",
                evidence={"original": {"band": band, "cell_id": cell_id, "pci": pci,
                            "earfcn_dl": raw_channels[0], "earfcn_ul": raw_channels[1]},
                          "published": {"band": band, "cell_id": cell_id, "pci": pci,
                            "earfcn_dl": earfcn, "earfcn_ul": earfcn_ul},
                          "native_frequency_100khz": {"dl": native_fields["ltedlfreq"], "ul": native_fields["lteulfreq"]},
                          "native_signal": {"api_path": "/api/device/signal", "fields": native_fields}})
            self._last_huawei_acquired_fields |= frozenset({"backend_diagnostic"})
        from arch4.modem_live.profile_diagnostics import field_diagnostic
        diagnostic = field_diagnostic(self._definition, self._profile_field_issues,
            native=self._last_status.get("backend_diagnostic"),
            previous=last_status.get("backend_diagnostic"))
        if diagnostic is not None:
            self._last_status["backend_diagnostic"] = diagnostic
            self._last_huawei_acquired_fields |= frozenset({"backend_diagnostic"})
        try:
            self._apply_target_guard_when_observable(self._last_status)
        except TargetGuardError:
            return None

        return ModemInput(
            sinr_db=sinr,
            rsrp_dbm=rsrp,
            rsrq_db=rsrq,
            rssi_dbm=rssi,
            mcs_dl=mcs_dl,
            mcs_ul=mcs_ul,
            cqi=cqi,
            ri=ri,
            carriers=carriers,
            ca_active=ca_active,
            ca_band_signature=ca_sig,
            primary_band=band,
            pci=pci,
            earfcn=earfcn,
            earfcn_ul=earfcn_ul,
            enb_id=enb_id,
            cell_id=cell_id,
            tac=tac,
            radio_connected=radio_connected,
            session_uptime_s=uptime_s,
            session_uptime_source=uptime_source,
            rx_bytes_per_sec=rx_bps,
            tx_bytes_per_sec=tx_bps,
            traffic_valid=traffic_valid,
            traffic_state_detail=traffic_state_detail,
            traffic_invalid_reason=traffic_invalid_reason,
            packet_session_connected=packet_session_connected,
            connection_status_code=conn_status or "",
            traffic_day_download_bytes=int(month_stats.get("traffic_day_download_bytes", 0) or 0),
            traffic_day_upload_bytes=int(month_stats.get("traffic_day_upload_bytes", 0) or 0),
            traffic_day_used_bytes=int(month_stats.get("traffic_day_used_bytes", 0) or 0),
            traffic_month_download_bytes=int(month_stats.get("traffic_month_download_bytes", 0) or 0),
            traffic_month_upload_bytes=int(month_stats.get("traffic_month_upload_bytes", 0) or 0),
            traffic_total_download_bytes=int(native_traffic.get("traffic_total_download_bytes", 0) or 0),
            traffic_total_upload_bytes=int(native_traffic.get("traffic_total_upload_bytes", 0) or 0),
            traffic_device_download_counter_bytes=native_traffic.get("traffic_device_download_counter_bytes"),
            traffic_device_upload_counter_bytes=native_traffic.get("traffic_device_upload_counter_bytes"),
            traffic_session_download_bytes=int(native_traffic.get("traffic_session_download_bytes", 0) or 0),
            traffic_session_upload_bytes=int(native_traffic.get("traffic_session_upload_bytes", 0) or 0),
            traffic_accounting_source=str(native_traffic.get("traffic_accounting_source") or ""),
            traffic_accounting_status="ok" if month_stats else "not_sampled",
            traffic_volume_last_sample_ts=float(self._traffic_month_ts or 0.0),
            traffic_volume_age_s=(
                max(0.0, time.time() - float(self._traffic_month_ts or 0.0))
                if self._traffic_month_ts else None
            ),
            traffic_rate_source=str(native_traffic.get("traffic_rate_source") or ""),
            traffic_quota_start_day=quota_settings.get("traffic_quota_start_day"),
            traffic_quota_limit_label=str(quota_settings.get("traffic_quota_limit_label", "") or ""),
            traffic_quota_limit_bytes=quota_settings.get("traffic_quota_limit_bytes"),
            traffic_quota_month_threshold_pct=quota_settings.get("traffic_quota_month_threshold_pct"),
            traffic_quota_day_threshold_pct=quota_settings.get("traffic_quota_day_threshold_pct"),
            selected_band_set=list(configured_pool),
            configured_band_pool=list(configured_pool),
            configured_band_pool_signature=configured_pool_sig,
            configured_band_pool_source=configured_pool_source,
            configured_band_pool_age_s=configured_pool_age_s,
            configured_band_pool_last_sample_ts=float(self._netmode_ts or 0.0),
            neighbors=neighbors,
            dl_bandwidth=dlbw_str or "",
            ul_bandwidth=ulbw_str or "",
            dl_bandwidth_mhz=float(dl_bw_mhz or 0.0),
            ul_bandwidth_mhz=float(ul_bw_mhz or 0.0),
            dl_freq_mhz=float(dlfreq_mhz or 0.0),
            ul_freq_mhz=float(ulfreq_mhz or 0.0),
            rrc=str(rrc or ""),
            trans_mode=str(transmode or ""),
            network_type=str(network_type_value or ""),
            network_type_ex=str(net_type_ex or ""),
            provider_name=str(provider or ""),
            provider_plmn=str(plmn or ""),
            provider_full_name=str(
                plmn_info.get("full_name")
                or plmn_info.get("short_name")
                or provider
                or ""
            ),
            cqi0=int(cqi0_val or 0),
            cqi1=int(cqi1_val or 0),
            ri_valid=ri_valid,
            ri_source=ri_source,
            mcs_dl0=mcs_dl_0,
            mcs_dl1=mcs_dl_1,
            mcs_ul_supported=True,
            tbs_dl0=int(_dl_mcs_to_tbs(mcs_dl_0) or 0),
            tbs_dl1=int(_dl_mcs_to_tbs(mcs_dl_1) or 0),
            tbs_ul=int(_ul_mcs_to_tbs(mcs_ul) or 0),
            mcs_dl_retx=bool(mcs_dl0_retx),
            mcs_dl1_retx=bool(mcs_dl1_retx),
            mcs_ul_retx=bool(mcs_ul_retx),
            mod0=qam_dl0,
            mod1=qam_dl1,
            qam_dl_primary=qam_dl0,
            qam_dl_secondary=qam_dl1 if mcs_dl_1 > 0 else 0,
            qam_dl0=lte_qm_to_qam(qam_dl0) if qam_dl0 > 0 else "",
            qam_dl1=lte_qm_to_qam(qam_dl1) if mcs_dl_1 > 0 and qam_dl1 > 0 else "",
            qam_ul=qam_ul,
            qam_source="inferred",
            qam_confidence=qam_conf,
            tx_power=tx_power or "",
            ims=str(ims or ""),
            antenna=str((antenna or {}).get("antenna") or ""),
            antenna_mode=str((antenna or {}).get("antenna") or ""),
            antenna_mode_source="huawei_static_antenna" if (antenna or {}).get("antenna") else "",
            lte_band_hex=str(netmode.get("lte_band_hex") or ""),
            nsa_active=bool(nr_mode == "NSA" and nr_present),
            sa_active=bool(nr_mode == "SA" and nr_present),
            nr_mode=str(nr_mode or ""),
            nr_state_text=str(nr_state_text or ""),
            nr_cells=list(nr_cells or []),
            nr_bandwidth_mhz=float(nr_dl_bw_mhz or 0.0),
            nr_cell_id=0,
            nr_band=0,
            nr_pci=0,
            nr_arfcn=int(nr_earfcn or 0),
            nr_rsrp_dbm=float(nr_rsrp or 0.0),
            nr_sinr_db=float(nr_sinr or 0.0),
            nr_rsrq_db=float(nr_rsrq or 0.0),
            wifi_enabled=wifi_on,
            wifi_24ghz_enabled=wifi_24,
            wifi_5ghz_enabled=wifi_5,
            model=str(info.get("model") or "") if info else "",
            firmware=str(info.get("firmware") or "") if info else "",
            software_version=str(info.get("software_version") or "") if info else "",
            webui=str(info.get("webui") or "") if info else "",
            serial=str(info.get("serial") or "") if info else "",
            imei=str(info.get("imei") or "") if info else "",
            imsi=str(info.get("imsi") or "") if info else "",
            hardware_version=str(info.get("hardware_version") or "") if info else "",
            config_version=str(info.get("config_version") or "") if info else "",
            lan_mac=str(info.get("lan_mac") or "") if info else "",
            wan_ip=str(info.get("wan_ip") or "") if info else "",
        )

    @staticmethod
    def _huawei_fields_proven_by_response(
        *,
        signal_present_tags: frozenset[str],
        signal_value_tags: frozenset[str],
        status_value_tags: frozenset[str],
        nr_present: bool,
        nr_evidence_seen: bool,
        nr_bandwidth_observed: bool = False,
    ) -> frozenset[str]:
        """Translate exact Huawei response tags into canonical fresh fields."""

        fields: set[str] = set()
        groups = (
            ({"sinr"}, {"sinr_db"}),
            ({"rsrp"}, {"rsrp_dbm"}),
            ({"rsrq"}, {"rsrq_db"}),
            ({"rssi"}, {"rssi_dbm"}),
            (
                {"band"},
                {
                    "band",
                    "serving_band",
                    "serving_band_signature",
                    "active_band_set",
                    "active_band_set_signature",
                    "band_set",
                    "band_set_signature",
                },
            ),
            ({"pci", "sc"}, {"pci"}),
            ({"cell_id"}, {"cell_id", "enb_id"}),
            ({"tac", "lac"}, {"tac"}),
            ({"earfcn"}, {"earfcn", "earfcn_dl", "earfcn_ul"}),
            (
                {"dl_mcs"},
                {
                    "mcs_dl",
                    "mcs_dl0",
                    "mcs_dl1",
                    "qam_dl0",
                    "qam_dl1",
                    "ri",
                    "ri_valid",
                    "ri_source",
                },
            ),
            ({"ul_mcs"}, {"mcs_ul", "qam_ul"}),
            ({"cqi0"}, {"cqi", "cqi0"}),
            ({"cqi1"}, {"cqi1"}),
            (
                {"ri", "rankindicator", "rank_indicator", "rank", "lte_ri"},
                {"ri", "ri_valid", "ri_source"},
            ),
            ({"rrc", "rrc_status"}, {"rrc"}),
            (
                {
                    "transmode",
                    "trans_mode",
                    "transmissionmode",
                    "transmission_mode",
                    "lte_transmode",
                    "lte_trans_mode",
                },
                {"trans_mode"},
            ),
            ({"txpower"}, {"tx_power"}),
            ({"dlfrequency"}, {"dl_freq_mhz"}),
            ({"ulfrequency"}, {"ul_freq_mhz"}),
            ({"dlbandwidth"}, {"dl_bandwidth", "dl_bandwidth_mhz"}),
            ({"ulbandwidth"}, {"ul_bandwidth", "ul_bandwidth_mhz"}),
        )
        for source_tags, canonical_fields in groups:
            if signal_value_tags.intersection(source_tags):
                fields.update(canonical_fields)
        # Old 54GUC's Huawei translator used the native RRC tag when present,
        # otherwise the same current response's connection/RF evidence to
        # produce canonical Connected/Idle.  Prove that translated field here;
        # Matrix and its consumers must never interpret Huawei names/codes.
        if (
            "connectionstatus" in status_value_tags
            or signal_value_tags.intersection(
                {
                    "rrc",
                    "rrc_status",
                    "band",
                    "earfcn",
                    "cell_id",
                    "pci",
                    "sc",
                    "rsrp",
                    "sinr",
                }
            )
        ):
            fields.add("rrc")
        if "nei_cellid" in signal_present_tags:
            fields.update({"neighbors", "neighbor_count"})
        if signal_value_tags.intersection(
            {"sinr", "rsrp", "rsrq", "rssi", "band", "pci", "sc"}
        ):
            fields.update({"radio_connected", "radio_state_text"})
        if status_value_tags.intersection(
            {"currentnetworktype", "currentnetworktypeex"}
        ):
            fields.update({"network_type", "ca_active", "ca_state_text"})
        if "connectionstatus" in status_value_tags:
            fields.update(
                {
                    "radio_connected",
                    "radio_state_text",
                    "packet_session_connected",
                    "connection_status_code",
                    "traffic_valid",
                    "traffic_state_detail",
                    "traffic_invalid_reason",
                }
            )
        if status_value_tags.intersection(
            {
                "imsregistrationstatus",
                "imsregstatus",
                "imsvoicestatus",
                "imssmsstatus",
                "imsreg",
            }
        ):
            fields.add("ims")
        if nr_present:
            fields.update(_HUAWEI_NR_ACQUIRED_FIELDS)
        elif nr_evidence_seen and status_value_tags.intersection(
            {"currentnetworktype", "currentnetworktypeex"}
        ):
            fields.update(_HUAWEI_NR_STATE_FIELDS)
        if nr_bandwidth_observed:
            fields.add("nr_bandwidth_mhz")
        return frozenset(fields)

    def read_modem_input(self) -> Optional[ModemInput]:
        """
        Read all signal data from B818 APIs.

        Combines:
          /api/device/signal  → RF, MCS, CQI, neighbors, band
          /api/monitoring/status → connection type, CA detection
          /api/monitoring/traffic-statistics → rx/tx bytes (delta)
        """
        return self._read_modem_input_impl(actionable_only=False)

    def read_actionable_modem_input(self) -> Optional[ModemInput]:
        return self._read_modem_input_impl(actionable_only=True)

    def acquire_modem_input(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "startup_full":
            raise ValueError(
                "huawei_startup_full_acquisition_lane_required"
            )
        modem = self.read_modem_input()
        if modem is not None:
            first_status = dict(getattr(self, "_last_status", {}) or {})
            if not self._has_license_identity(first_status):
                # Permanent identity is required for the first Matrix
                # generation.  The full read already tried this endpoint, so
                # one named retry is legal only because that result was
                # incomplete.  _api_get still enforces the profile-owned gap.
                retry_started_at = time.time()
                retry_error = ""
                try:
                    retry_info = self.get_modem_info(
                        timeout=self._maintenance_timeout,
                    )
                except requests.RequestException as exc:
                    retry_info = {}
                    retry_error = f"{type(exc).__name__}: {exc}"
                retry_ok = self._has_license_identity(retry_info)
                if self._has_real_device_identity(retry_info):
                    self._device_info_acquired_fields = frozenset(
                        key for key, value in retry_info.items()
                        if str(value or "").strip() and key not in self._profile_identity_unavailable
                    )
                    merged_info = dict(self._device_info_cache or {})
                    merged_info.update(
                        {
                            key: value
                            for key, value in dict(retry_info or {}).items()
                            if str(value or "").strip()
                        }
                    )
                    self._device_info_cache = merged_info
                    self._device_info_ts = time.time()
                    refreshed_status = dict(self._last_status or {})
                    for key in (
                        "model",
                        "firmware",
                        "software_version",
                        "webui",
                        "serial",
                        "imei",
                        "imsi",
                        "hardware_version",
                        "config_version",
                        "lan_mac",
                        "wan_ip",
                        "antenna",
                        "antenna_mode",
                        "antenna_mode_source",
                    ):
                        value = merged_info.get(key)
                        if str(value or "").strip():
                            refreshed_status[key] = value
                    if refreshed_status.get("model"):
                        refreshed_status["router_model"] = refreshed_status[
                            "model"
                        ]
                        refreshed_status["device_model"] = refreshed_status[
                            "model"
                        ]
                    if refreshed_status.get("software_version"):
                        refreshed_status["software"] = refreshed_status[
                            "software_version"
                        ]
                    if refreshed_status.get("config_version"):
                        refreshed_status[
                            "configuration_file_version"
                        ] = refreshed_status["config_version"]
                    self._last_status = refreshed_status
                    self._last_status_ts = time.time()
                retry_status = dict(self._last_status or {})
                diagnostic = dict(
                    retry_status.get("backend_diagnostic") or {}
                )
                diagnostic["startup_identity_retry"] = {
                    "attempted": True,
                    "reason": "startup_full_missing_permanent_identity",
                    "succeeded": bool(retry_ok),
                    "started_at": float(retry_started_at),
                    "finished_at": float(time.time()),
                    "error": retry_error,
                }
                retry_status["backend_diagnostic"] = diagnostic
                self._last_status = retry_status
                self._last_status_ts = time.time()
        raw_status = dict(getattr(self, "_last_status", {}) or {})
        additional_fields: set[str] = set()
        additional_cells: set[str] = set()
        from arch4.modem.huawei_radio_mode import contract as radio_mode_contract
        if radio_mode_contract(self) is not None:
            self._last_status.update(self._radio_mode_status("slow", timeout=self._maintenance_timeout))
            additional_cells.add("slow.radio_mode")
        if float(self._device_info_ts or 0.0) >= request.started_at:
            additional_fields.update(
                field
                for field in (
                    "model",
                    "router_model",
                    "firmware",
                    "software",
                    "software_version",
                    "webui",
                    "serial",
                    "imei",
                    "imsi",
                    "hardware_version",
                    "config_version",
                    "configuration_file_version",
                    "lan_mac",
                    "wan_ip",
                )
                if (raw_status.get(field) not in (None, "")
                    and {"software": "software_version",
                         "configuration_file_version": "config_version"}.get(field, field)
                    in self._device_info_acquired_fields)
            )
        if float(self._plmn_cache_ts or 0.0) >= request.started_at:
            additional_fields.update(
                field
                for field in (
                    "provider",
                    "provider_name",
                    "provider_plmn",
                    "plmn",
                    "plmn_name",
                    "network_provider_fullname",
                )
                if raw_status.get(field) not in (None, "")
            )
        if float(self._antenna_ts or 0.0) >= request.started_at:
            additional_fields.update(
                field
                for field in (
                    "antenna",
                    "antenna_mode",
                    "antenna_mode_source",
                )
                if raw_status.get(field) not in (None, "")
            )
        if str(raw_status.get("session_uptime_source") or "") == "reported":
            additional_fields.update(
                {"session_uptime_s", "session_uptime_source"}
            )
        return self._capture_huawei_actionable_receipt(
            request=request,
            modem=modem,
            additional_fields=frozenset(additional_fields),
            additional_cells=frozenset(additional_cells),
            failure_reason="huawei_startup_acquisition_unavailable",
        )

    def _capture_huawei_actionable_receipt(
        self,
        *,
        request: BackendAcquisitionRequest,
        modem: Optional[ModemInput],
        additional_cells: frozenset[str] = frozenset(),
        additional_fields: frozenset[str] = frozenset(),
        failure_reason: str = "huawei_actionable_acquisition_unavailable",
    ) -> BackendAcquisitionReceipt:
        if modem is None:
            return self._capture_backend_acquisition_receipt(
                request=request,
                modem=None,
                acquired_cells=frozenset(),
                failure_reason=failure_reason,
            )
        raw_status = dict(getattr(self, "_last_status", {}) or {})
        configured_sample_ts = float(
            raw_status.get(
                "configured_band_pool_last_sample_ts",
                0.0,
            )
            or 0.0
        )
        if configured_sample_ts >= request.started_at:
            additional_fields = additional_fields | frozenset(
                {
                    "selected_band_set",
                    "selected_band_set_signature",
                    "configured_band_pool",
                    "configured_band_pool_signature",
                    "configured_band_pool_source",
                    "configured_band_pool_status",
                    "configured_band_pool_age_s",
                    "configured_band_pool_last_sample_ts",
                }
            )
            additional_cells = additional_cells | CONFIGURED_BAND_CELLS
        wifi_sample_ts = float(
            getattr(self, "_last_wifi_status_ts", 0.0) or 0.0
        )
        if wifi_sample_ts >= request.started_at:
            wifi_fields = frozenset(
                {
                    "wifi",
                    "wifi_enabled",
                    "wifi_24ghz_enabled",
                    "wifi_5ghz_enabled",
                    "wifi_6ghz_enabled",
                }
            )
            additional_fields = additional_fields | wifi_fields
            additional_cells = additional_cells | (
                matrix_cells_for_canonical_fields(wifi_fields)
            )
        acquired_fields = (
            frozenset(self._last_huawei_acquired_fields)
            | additional_fields
            | self._huawei_traffic_fields_since(request.started_at)
        ).intersection(DECLARED_CANONICAL_SOURCE_FIELDS)
        acquired_cells = (
            matrix_cells_for_canonical_fields(acquired_fields)
            | additional_cells
        )
        return self._capture_backend_acquisition_receipt(
            request=request,
            modem=modem,
            acquired_cells=acquired_cells,
            failure_reason=failure_reason,
        )

    def _huawei_traffic_fields_since(
        self,
        started_at: float,
    ) -> frozenset[str]:
        fields: set[str] = set()
        if float(self._prev_traffic_ts or 0.0) >= started_at:
            fields.update({"rx_bytes_per_sec", "tx_bytes_per_sec"})
        for timestamp, cache in (
            (self._traffic_native_ts, self._traffic_native_cache),
            (self._traffic_month_ts, self._traffic_month_cache),
            (self._traffic_quota_ts, self._traffic_quota_cache),
        ):
            if float(timestamp or 0.0) < started_at:
                continue
            fields.update(
                str(field)
                for field, value in dict(cache or {}).items()
                if field in DECLARED_CANONICAL_SOURCE_FIELDS
                and value is not None
                and (not isinstance(value, str) or bool(value.strip()))
            )
        if (float(self._traffic_native_ts or 0.0) >= started_at
                and self._traffic_native_cache.get("traffic_session_duration_s") is not None):
            fields.update({"session_uptime_s", "session_uptime_source"})
        return frozenset(fields)

    def acquire_actionable_modem_input(
        self,
        request: BackendAcquisitionRequest,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "fast_live":
            raise ValueError(
                "huawei_fast_live_acquisition_lane_required"
            )
        return self._capture_huawei_actionable_receipt(
            request=request,
            modem=self.read_actionable_modem_input(),
        )

    def read_post_write_modem_input(
        self,
        stage: PhysicalCommandDispatchEvidence,
    ) -> Optional[ModemInput]:
        del stage
        raise RuntimeError(
            "huawei_untyped_post_write_read_forbidden:"
            "use_acquire_post_write_modem_input"
        )

    def acquire_post_write_modem_input(
        self,
        request: BackendAcquisitionRequest,
        stage: PhysicalCommandDispatchEvidence,
    ) -> BackendAcquisitionReceipt:
        if request.lane != "post_write":
            raise ValueError(
                "huawei_post_write_acquisition_lane_required"
            )
        if not isinstance(stage, PhysicalCommandDispatchEvidence):
            raise TypeError("huawei_post_write_stage_evidence_required")
        if not stage.permits_target_verification:
            raise ValueError("huawei_post_write_accepted_stage_required")

        if isinstance(stage.target, CoreRadioModeTarget):
            status = self._radio_mode_status("control", timeout=self._maintenance_timeout)
            self._last_status.update(status)
            return self._capture_backend_acquisition_receipt(request=request, modem=None,
                acquired_cells=frozenset({"slow.radio_mode"}), status_override=status)

        if isinstance(stage.target, CoreMobileDataTarget):
            from arch4.modem.huawei_network_settings import read_mobile_data
            return self._capture_backend_acquisition_receipt(
                request=request, modem=None,
                acquired_cells=frozenset({"slow.mobile_data_enabled"}),
                status_override=read_mobile_data(self),
            )

        if isinstance(stage.target, CoreNetworkSettingsTarget):
            provider_before = float(self._plmn_cache_ts or 0.0)
            receipt = self._acquire_network_settings_readback(request)
            if stage.target.selection != "manual":
                return receipt
            # Fast HiLink reads do not acquire serving PLMN. Manual selection
            # needs a post-write provider observation while polling is paused.
            from arch4.modem.huawei_network_settings import profile_for
            profile = profile_for(self)
            if profile is not None and profile.network_settings == "hilink_b818":
                # Exact B818 manual inspection already acquired this provider.
                # A failed read must not cause a second attempt or cache reuse.
                provider = dict(self._plmn_cache)
            else:
                provider = self._get_plmn_cached(max_age_s=0.0)
            if float(self._plmn_cache_ts or 0.0) <= provider_before:
                return receipt  # Retained provider is not fresh confirmation.
            status = receipt.status_copy()
            status.update(provider_plmn=str(provider.get("plmn") or ""),
                          provider_name=str(provider.get("name") or ""))
            return self._capture_backend_acquisition_receipt(
                request=request, modem=None, status_override=status,
                acquired_cells=receipt.acquired_cells | frozenset({"identity.provider_plmn", "identity.provider_name"}),
            )

        if stage.stage_kind in {
            "pband_anchor",
            "ca_pool",
            "band_selection",
            "band_lock",
            "band_auto",
        }:
            netmode = self._get_netmode_cached(
                max_age_s=0.0,
                include_supported_list=False,
            )
            sampled_at = float(self._netmode_ts or 0.0)
            if sampled_at < request.started_at:
                return self._capture_backend_acquisition_receipt(
                    request=request,
                    modem=None,
                    acquired_cells=frozenset(),
                    failure_reason="huawei_post_write_netmode_unavailable",
                )
            pool, signature, source = (
                self._valid_configured_pool_from_netmode(netmode)
            )
            target_status = {
                "selected_band_set": list(pool),
                "selected_band_set_signature": signature,
                "configured_band_pool": list(pool),
                "configured_band_pool_signature": signature,
                "configured_band_pool_source": source,
                "configured_band_pool_status": (
                    "available" if pool else "unavailable"
                ),
                "configured_band_pool_age_s": 0.0,
                "configured_band_pool_last_sample_ts": sampled_at,
            }
            latest = dict(self._last_status or {})
            latest.update(target_status)
            diagnostic = dict(latest.get("backend_diagnostic") or {})
            diagnostic["native_auto_release_readback"] = {
                "acquisition_attempt_id": request.attempt_id,
                "native_auto": bool(_is_all_bands_mask(netmode.get("lte_band_hex"))
                    or netmode.get("allowed_bands") and set(netmode["allowed_bands"])
                    == set(netmode.get("netmode_list_supported_bands") or ())),
            }
            latest["backend_diagnostic"] = diagnostic
            latest["ts"] = time.time()
            self._last_status = latest
            self._last_status_ts = float(latest["ts"])
            return self._capture_backend_acquisition_receipt(
                request=request,
                modem=None,
                acquired_cells=CONFIGURED_BAND_CELLS,
            )

        if stage.stage_kind in {"wifi_radio", "wifi_config"}:
            result = self.get_wifi_status_result()
            if not result.ok:
                return self._capture_backend_acquisition_receipt(
                    request=request,
                    modem=None,
                    acquired_cells=frozenset(),
                    failure_reason=(
                        result.reason or "huawei_post_write_wifi_unavailable"
                    ),
                )
            wifi_fields = frozenset(
                {
                    "wifi_enabled",
                    "wifi_24ghz_enabled",
                    "wifi_5ghz_enabled",
                    "wifi_6ghz_enabled",
                }
            )
            return self._capture_backend_acquisition_receipt(
                request=request,
                modem=None,
                acquired_cells=matrix_cells_for_canonical_fields(wifi_fields),
            )

        return self._capture_huawei_actionable_receipt(
            request=request,
            modem=self.read_actionable_modem_input(),
        )

    def recover_live_register_session(self, base_status: Optional[Dict[str, Any]] = None) -> bool:
        """Repair authentication only; Modem Live separately proves identity/data.

        A valid login-status response is the readiness check. RF can be blank
        and Internet can be down while management authentication is healthy.
        """
        del base_status
        def _record_recovery(*, ok: bool, method: str, reason: str = "") -> None:
            status = dict(self._last_status or {})
            payload = {
                "ok": bool(ok),
                "method": str(method or ""),
                "proof": "authenticated_session" if ok else "",
                "ts": time.time(),
            }
            if reason:
                payload["reason"] = str(reason)
            status["hilink_live_register_recovery"] = payload
            status["hilink_read_session_generation"] = int(self._read_session_generation)
            self._last_status = status
            self._last_status_ts = float(status.get("ts") or time.time())

        login_required = self._check_login_required()
        if login_required is None:
            _record_recovery(ok=False, method="state_login", reason="login_state_unavailable")
            return False
        if not login_required:
            self._connected = True
            self._sync_read_sessions_from_control()
            _record_recovery(ok=True, method="sync_read_sessions_from_control")
            return True
        self._connected = False
        recovered = bool(self.connect())
        if recovered:
            _record_recovery(ok=True, method="reconnect_control_session")
        else:
            _record_recovery(ok=False, method="reconnect_control_session", reason="connect_failed")
        return recovered

    def refresh_live_maintenance(self) -> None:
        raise RuntimeError("huawei_maintenance_requires_owned_steps")

    def iter_acquire_live_maintenance(self, request: BackendAcquisitionRequest):
        """Capture each read before yielding the existing Modem Live I/O gate."""
        if request.lane != "slow_live":
            raise ValueError("huawei_slow_live_acquisition_lane_required")
        if not self._connected:
            return

        base_status = dict(self._last_status or {})
        updates: Dict[str, Any] = {}
        fast_timeout: Any = self._maintenance_timeout
        cycle_fields: set[str] = set()

        def capture(updates: Dict[str, Any], acquired_fields: Optional[frozenset[str]] = None):
            if not updates:
                return None
            cycle_fields.update(updates)
            latest = dict(self._last_status or {})
            latest.update(updates)
            latest["ts"] = time.time()
            self._last_status = latest
            self._last_status_ts = latest["ts"]
            status = self.get_last_status()
            cells = matrix_cells_for_canonical_fields(
                (frozenset(updates) if acquired_fields is None else acquired_fields)
                .intersection(DECLARED_CANONICAL_SOURCE_FIELDS).intersection(status)
            )
            if not cells:
                return None
            return self._capture_backend_acquisition_receipt(
                request=request, modem=None, acquired_cells=cells,
                status_override=status, failure_reason="",
            )

        from arch4.modem.huawei_network_settings import profile_for, read_mobile_data
        profile = profile_for(self)
        if profile is not None and profile.capabilities.get("supports_mobile_data", False) and profile.network_settings in {
            "hilink_b818", "hilink_b535_232", "hilink_b535_333"
        }:
            receipt = capture(read_mobile_data(self, timeout=fast_timeout))
            if receipt is not None:
                yield receipt

        status_xml = self._api_get("/api/monitoring/status", timeout=fast_timeout)
        profile_status = {}
        if status_xml:
            profile_status = self._decode_status_profile(status_xml)
            net_type_ex = xml_text(status_xml, "CurrentNetworkTypeEx")
            conn_status = profile_status.get('traffic.connection_status_code',xml_text(status_xml,"ConnectionStatus")) or ''
            packet_session_connected = (conn_status == "901") if conn_status else base_status.get("packet_session_connected")
            radio_connected = (
                bool(base_status.get("radio_connected"))
                if packet_session_connected is False
                else ((conn_status == "901") if conn_status else bool(base_status.get("radio_connected")))
            )
            if 'health.radio_connected'in profile_status:
                radio_connected=profile_status['health.radio_connected']is True
            traffic_valid = bool(packet_session_connected is not False)
            updates.update(
                {
                    "network_type_ex": net_type_ex or base_status.get("network_type_ex"),
                    "network_type": NETWORK_TYPE_MAP.get(net_type_ex, net_type_ex) if net_type_ex else base_status.get("network_type"),
                    "ca_active": (net_type_ex == "1011") if net_type_ex else bool(base_status.get("ca_active", False)),
                    "ca_state_text": "ACTIVE" if net_type_ex == "1011" else "INACTIVE",
                    "radio_connected": radio_connected,
                    "radio_state_text": "Connected" if radio_connected else "Disconnected",
                    "connection_status_code": conn_status or base_status.get("connection_status_code", ""),
                    "packet_session_connected": packet_session_connected,
                    "traffic_valid": traffic_valid,
                    "traffic_state_detail": (
                        "packet_session_invalid"
                        if packet_session_connected is False
                        else ("packet_session_connected" if packet_session_connected is True else "unknown")
                    ),
                    "traffic_invalid_reason": (
                        f"connection_status_{conn_status or 'unknown'}"
                        if packet_session_connected is False
                        else ""
                    ),
                }
            )
            if 'radio.network_type'in profile_status:
                updates['network_type']=profile_status['radio.network_type']or''
            if 'radio.ca_active'in profile_status:
                updates['ca_active']=profile_status['radio.ca_active']is True
                updates['ca_state_text']='ACTIVE'if updates['ca_active']else'INACTIVE'

        status_tags = frozenset(
            name for name, value in (("currentnetworktypeex", xml_text(status_xml or "", "CurrentNetworkTypeEx")),
                                     ("connectionstatus", xml_text(status_xml or "", "ConnectionStatus")))
            if value
        )
        status_fields = self._huawei_fields_proven_by_response(
            signal_present_tags=frozenset(), signal_value_tags=frozenset(),
            status_value_tags=status_tags, nr_present=False, nr_evidence_seen=False,
        )
        status_fields=set(status_fields)
        for address,value in profile_status.items():
            name=address.split('.',1)[1]
            status_fields.discard(name)
            unavailable=value is None or value==''
            if unavailable:
                updates.pop(name,None)
                if name=='radio_connected':updates.pop('radio_state_text',None)
                if name=='ca_active':updates.pop('ca_state_text',None)
            if unavailable and name=='connection_status_code':
                status_fields.difference_update({'packet_session_connected','traffic_valid',
                    'traffic_state_detail','traffic_invalid_reason','radio_connected','radio_state_text'})
            if name=='radio_connected':status_fields.discard('radio_state_text')
            if name=='ca_active':status_fields.discard('ca_state_text')
        for address,value in profile_status.items():
            if value is not None and value!='':
                name=address.split('.',1)[1];status_fields.add(name)
                if name=='radio_connected':status_fields.add('radio_state_text')
                if name=='ca_active':status_fields.add('ca_state_text')
        status_fields=frozenset(status_fields)
        yield capture(updates, status_fields)
        updates = {}
        try:
            traffic_before = float(self._last_traffic_rate_ts or 0.0)
            rx_bps, tx_bps = self._read_traffic(timeout=fast_timeout, max_age_s=2.0)
            if float(self._last_traffic_rate_ts or 0.0) > traffic_before:
                updates["rx_bytes_per_sec"] = float(rx_bps or 0.0)
                updates["tx_bytes_per_sec"] = float(tx_bps or 0.0)
                updates.update(dict(self._traffic_native_cache or {}))
                packet_time = self._traffic_native_cache.get("traffic_session_duration_s")
                updates["session_uptime_s"] = packet_time
                updates["session_uptime_source"] = "reported" if packet_time is not None else "unavailable"
        except Exception as exc:
            log.debug("Huawei HiLink maintenance traffic refresh suppressed: %s", exc)

        yield capture(updates, frozenset(
            key for key, value in updates.items() if value is not None
            and (key != "session_uptime_source" or value == "reported")))
        updates = {}
        try:
            previous_provider_plmn = str(self._plmn_cache.get("plmn") or "")
            provider_before = float(self._plmn_cache_ts or 0.0)
            provider = self._get_plmn_cached(max_age_s=10.0, timeout=fast_timeout)
            provider_fresh = float(self._plmn_cache_ts or 0.0) > provider_before
            provider_plmn = str(provider.get("plmn") or "").strip()
            provider_name = str(
                provider.get("name")
                or provider.get("full_name")
                or provider.get("short_name")
                or ""
            ).strip()
            if '_profile_provider_name'in provider:
                provider_name=str(provider['_profile_provider_name']or'')
            if provider_fresh:
                updates["provider_plmn"] = provider_plmn
            if provider_fresh:
                updates["provider_name"] = provider_name
                if provider_plmn != previous_provider_plmn:
                    updates.update(self._netmode_status_updates(dict(self._netmode_cache)))
        except Exception as exc:
            log.debug("Huawei HiLink maintenance provider refresh suppressed: %s", exc)

        yield capture(updates)
        updates = {}
        static_refresh_max_age_s = 1800.0 if self._careful_huawei_http_profile() else 300.0
        wifi_before = float(self._last_wifi_status_ts or 0.0)
        wifi_on, wifi_24, wifi_5 = self._read_wifi_status(
            max_age_s=static_refresh_max_age_s,
            timeout=fast_timeout,
        )
        if float(self._last_wifi_status_ts or 0.0) > wifi_before and any(value is not None for value in (wifi_on, wifi_24, wifi_5)):
            updates["wifi_enabled"] = wifi_on
            updates["wifi_24ghz_enabled"] = wifi_24
            updates["wifi_5ghz_enabled"] = wifi_5

        yield capture(updates)
        updates = {}
        try:
            identity_before = float(self._device_info_ts or 0.0)
            info = self._get_device_info_cached(max_age_s=300.0, timeout=fast_timeout)
            if self._has_real_device_identity(info) and float(self._device_info_ts or 0.0) > identity_before:
                for key in (
                    "model",
                    "router_model",
                    "device_model",
                    "firmware",
                    "software",
                    "software_version",
                    "webui",
                    "serial",
                    "imei",
                    "imsi",
                    "hardware_version",
                    "config_version",
                    "configuration_file_version",
                    "lan_mac",
                    "wan_ip",
                ):
                    value = info.get(key) if isinstance(info, dict) else None
                    text = str(value or "").strip()
                    if key in self._device_info_acquired_fields:
                        updates[key] = text
                if updates.get("model"):
                    updates.setdefault("router_model", updates["model"])
                    updates.setdefault("device_model", updates["model"])
                if updates.get("firmware"):
                    updates.setdefault("software_version", updates["firmware"])
                    updates.setdefault("software", updates.get("software_version", updates["firmware"]))
                if updates.get("config_version"):
                    updates.setdefault("configuration_file_version", updates["config_version"])
        except Exception as exc:
            log.debug("Huawei HiLink maintenance identity refresh suppressed: %s", exc)

        yield capture(updates)
        updates = {}
        netmode, netmode_reason, netmode_refreshed = self._refresh_netmode_if_requested()
        if netmode_refreshed:
            updates.update(self._netmode_status_updates(netmode))

        yield capture(updates)
        from arch4.modem.huawei_radio_mode import contract as radio_mode_contract, iter_status
        if radio_mode_contract(self) is not None:
            for mode_status in iter_status(self, "slow", timeout=fast_timeout):
                yield capture(mode_status) if mode_status is not None else None
        volume_max_age_s = 1800.0 if self._careful_huawei_http_profile() else 300.0
        for reader, timestamp in (
            (self._get_month_statistics_cached, "_traffic_month_ts"),
            (self._get_quota_settings_cached, "_traffic_quota_ts"),
        ):
            updates = {}
            try:
                before = float(getattr(self, timestamp) or 0.0)
                value = reader(max_age_s=volume_max_age_s, timeout=fast_timeout)
                if float(getattr(self, timestamp) or 0.0) > before:
                    updates.update(value)
            except Exception as exc:
                log.debug("Huawei HiLink maintenance volume refresh suppressed: %s", exc)
            yield capture(updates)
        self._last_status["hilink_maintenance"] = {
            "mode": "serial_read_steps",
            "updated_fields": sorted(cycle_fields),
            "timeout_s": round(self._timeout_total_s(fast_timeout), 3),
            "netmode_event_reason": netmode_reason,
            "netmode_event_refreshed": bool(netmode_refreshed),
        }

    def _iter_network_settings_observation(self):
        from arch4.modem.huawei_network_settings import iter_network_settings
        return iter_network_settings(self)

    def acquire_live_maintenance(self, request: BackendAcquisitionRequest) -> BackendAcquisitionReceipt:
        raise RuntimeError("huawei_maintenance_requires_owned_steps")

    def get_live_acquisition_contract(self) -> LiveAcquisitionContract:
        profile = self._access_profile
        careful_huawei_http = self._careful_huawei_http_profile()
        lane_groups = dict(profile.lane_groups or {})
        fast_groups = tuple(lane_groups.get("fast_live", ()) or ())
        slow_groups = tuple(lane_groups.get("slow_live", ()) or ())
        static_groups = tuple(lane_groups.get("boot_static", ()) or ())
        write_groups = tuple(lane_groups.get("event_write", ()) or ())
        export_groups = tuple(lane_groups.get("export", ()) or ())
        fast_publish_fields = (
            "rx_bytes_per_sec",
            "tx_bytes_per_sec",
            "traffic_current_download_rate_bps",
            "traffic_current_upload_rate_bps",
        ) if "traffic_presence" in fast_groups else ()
        return LiveAcquisitionContract(
            acquisition_class="aligned_split",
            clocked_register_read=True,
            live_register_interval_s=float(profile.fast_live_interval_s),
            live_register_stale_s=float(profile.fast_live_stale_s),
            profile_name=str(profile.profile_id),
            backend_io_parallel_safe=not careful_huawei_http,
            io_serial_required=careful_huawei_http,
            startup_parallel_allowed=not careful_huawei_http,
            has_explicit_actionable_snapshot=True,
            has_explicit_maintenance_refresh=True,
            maintenance_stepwise=True,
            maintenance_step_requires_fast_margin=True,
            runtime_action_requires_io_lock=True,
            rtt_holds_io_lock=False,
            rtt_min_interval_s=1.0,
            maintenance_holds_io_lock=careful_huawei_http,
            neighbor_reads_hold_io_lock=False,
            generic_neighbor_scan_enabled=False,
            fast_modem_read_groups=fast_groups,
            slow_modem_read_groups=slow_groups,
            fast_publish_fields=fast_publish_fields,
            static_capability_groups=static_groups,
            control_write_groups=write_groups,
            export_groups=export_groups,
            static_capability_refresh_policy=str(profile.boot_static_refresh_policy),
        )

    # ─── Signal parsing helpers ─────────────────────────────

    @staticmethod
    def _parse_signal_val(raw: str) -> float:
        return parse_signal_val(raw)

    @staticmethod
    def _parse_bandwidth_mhz(raw: str) -> Optional[float]:
        return parse_bandwidth_mhz(raw)

    @staticmethod
    def _parse_int(raw: str) -> int:
        return parse_int(raw)

    @staticmethod
    def _parse_freq_mhz(raw: str) -> Optional[float]:
        return parse_freq_mhz(raw)

    @staticmethod
    def _parse_dl_mcs(raw: str) -> tuple:
        return parse_dl_mcs(raw)

    @staticmethod
    def _parse_ul_mcs(raw: str) -> int:
        return parse_ul_mcs(raw)

    def _parse_neighbor_pcis(self, raw: str, band: int) -> List[NeighborCell]:
        """
        Parse B818 neighbor list: 'No0:303No1:304No2:309'
        B818 only gives PCI — no RSRP, no RSRQ, no band per neighbor.
        We assume same band as serving cell and use dummy signal values.
        """
        neighbors = []
        if not raw:
            return neighbors
        pcis = re.findall(r'No\d+:(\d+)', raw)
        for pci_str in pcis:
            pci = int(pci_str)
            neighbors.append(NeighborCell(
                band=band,       # assumption: same band
                earfcn=0,        # unknown
                pci=pci,
                rsrp_dbm=-999.0,  # sentinel: "unknown"
                rsrq_db=-999.0,
            ))
        return neighbors

    # ─── Traffic counters ───────────────────────────────────

    def _read_traffic(self, *, timeout=None, max_age_s: float = 0.0, lane: Optional[str] = None) -> tuple:
        """
        Read traffic via /api/monitoring/traffic-statistics.
        Returns (rx_bytes_per_sec, tx_bytes_per_sec) as delta.
        """
        now = time.time()
        age_s = now - float(self._last_traffic_rate_ts or 0.0)
        if max_age_s > 0.0 and self._traffic_native_cache and age_s <= max_age_s:
            return float(self._last_rx_bps or 0.0), float(self._last_tx_bps or 0.0)

        kwargs: Dict[str, Any] = {"timeout": timeout}
        if lane:
            kwargs["lane"] = lane
        xml = self._api_get("/api/monitoring/traffic-statistics", **kwargs)
        if not xml:
            return float(self._last_rx_bps or 0.0), float(self._last_tx_bps or 0.0)

        decoded = self._decode_profile_values(
            {"/api/monitoring/traffic-statistics": xml},
            {"traffic.session_uptime_s", "traffic.rx_bytes_per_sec", "traffic.tx_bytes_per_sec"})
        try:
            rx_total = int(xml_text(xml, "CurrentDownload", "0"))
            tx_total = int(xml_text(xml, "CurrentUpload", "0"))
            current_download_rate = (decoded["traffic.rx_bytes_per_sec"]
                if "traffic.rx_bytes_per_sec" in decoded else int(xml_text(xml, "CurrentDownloadRate", "0")))
            current_upload_rate = (decoded["traffic.tx_bytes_per_sec"]
                if "traffic.tx_bytes_per_sec" in decoded else int(xml_text(xml, "CurrentUploadRate", "0")))
            total_upload = int(xml_text(xml, "TotalUpload", "0"))
            total_download = int(xml_text(xml, "TotalDownload", "0"))
            total_connect_time = int(xml_text(xml, "TotalConnectTime", "0"))
        except ValueError:
            return float(self._last_rx_bps or 0.0), float(self._last_tx_bps or 0.0)
        # Optional timer failure must not discard valid byte/rate readings.
        timer = xml_text(xml, "CurrentConnectTime", "").strip()
        current_connect_time = int(timer) if re.fullmatch(r"[0-9]+", timer) else None
        current_connect_time = decoded.get("traffic.session_uptime_s", current_connect_time)
        if current_connect_time is not None and current_connect_time < 0:
            current_connect_time = None
        now = time.time()
        self._traffic_native_cache = {
            "traffic_session_upload_bytes": tx_total,
            "traffic_session_download_bytes": rx_total,
            "traffic_session_duration_s": current_connect_time,
            "traffic_total_upload_bytes": total_upload,
            "traffic_total_download_bytes": total_download,
            "traffic_total_duration_s": total_connect_time,
        }
        if xml_text(xml, "TotalDownload", "") and xml_text(xml, "TotalUpload", ""):
            self._traffic_native_cache.update(
                traffic_device_download_counter_bytes=total_download,
                traffic_device_upload_counter_bytes=total_upload,
                traffic_accounting_source="huawei_traffic_statistics",
            )
        self._traffic_native_ts = now
        native_rx_bps = float(current_download_rate or 0.0) if (current_download_rate or 0.0) > 0 else 0.0
        native_tx_bps = float(current_upload_rate or 0.0) if (current_upload_rate or 0.0) > 0 else 0.0
        if (self._prev_rx_bytes is not None and
                self._prev_traffic_ts is not None):
            dt = now - self._prev_traffic_ts
            if dt > 0.5:  # at least 0.5s between reads
                delta_rx_bps = max(0, (rx_total - self._prev_rx_bytes)) / dt
                delta_tx_bps = max(0, (tx_total - self._prev_tx_bytes)) / dt
            else:
                delta_rx_bps = delta_tx_bps = 0.0
        else:
            delta_rx_bps = delta_tx_bps = 0.0

        # Huawei reports instantaneous Current*Rate in the same traffic
        # endpoint, but B818 firmware can update DL and UL native rates
        # independently from the cumulative byte counters. Use the strongest
        # same-sample evidence per direction so a stale low native rate cannot
        # hide a real speed-test burst visible in CurrentDownload/Upload deltas.
        rx_bps = max(native_rx_bps, delta_rx_bps)
        tx_bps = max(native_tx_bps, delta_tx_bps)
        if (delta_rx_bps > native_rx_bps) or (delta_tx_bps > native_tx_bps):
            source = "counter_delta"
            if native_rx_bps > 0.0 or native_tx_bps > 0.0:
                source = "native_current_rate+counter_delta"
        elif native_rx_bps > 0.0 or native_tx_bps > 0.0:
            source = "native_current_rate"
        else:
            source = "zero"
        self._traffic_native_cache.update(
            {
                "traffic_current_download_rate_bps": float(rx_bps or 0.0),
                "traffic_current_upload_rate_bps": float(tx_bps or 0.0),
                "traffic_rate_source": source,
            }
        )

        self._prev_rx_bytes = rx_total
        self._prev_tx_bytes = tx_total
        self._prev_traffic_ts = now
        self._last_rx_bps = float(rx_bps or 0.0)
        self._last_tx_bps = float(tx_bps or 0.0)
        self._last_traffic_rate_ts = now
        return rx_bps, tx_bps

    def _get_month_statistics_cached(
        self,
        *,
        max_age_s: float = 30.0,
        timeout: Optional[Any] = None,
        lane: str = "slow",
    ) -> Dict[str, Any]:
        now = time.time()
        if self._traffic_month_cache and max_age_s > 0.0:
            age = max(0.0, now - float(self._traffic_month_ts or 0.0))
            if age <= max_age_s:
                return dict(self._traffic_month_cache)

        xml = self._api_get("/api/monitoring/month_statistics", timeout=timeout, lane=lane)
        fields = self._optional_billing_response_fields(xml)
        if not fields:
            return dict(self._traffic_month_cache)
        values = self._optional_billing_integer_fields(fields, {
            "CurrentMonthDownload": "traffic_month_download_bytes",
            "CurrentMonthUpload": "traffic_month_upload_bytes",
            "MonthDuration": "traffic_month_duration_s",
            "CurrentDayUsed": "traffic_day_used_bytes",
            "CurrentDayDuration": "traffic_day_duration_s",
        })
        if fields.get("MonthLastClearTime"):
            values["traffic_month_last_clear_date"] = fields["MonthLastClearTime"]
        if not values:
            return dict(self._traffic_month_cache)
        self._traffic_month_cache = values
        self._traffic_month_ts = now
        return dict(self._traffic_month_cache)

    def _get_quota_settings_cached(
        self,
        *,
        max_age_s: float = 300.0,
        timeout: Optional[Any] = None,
        lane: str = "slow",
    ) -> Dict[str, Any]:
        now = time.time()
        if self._traffic_quota_cache and max_age_s > 0.0:
            age = max(0.0, now - float(self._traffic_quota_ts or 0.0))
            if age <= max_age_s:
                return dict(self._traffic_quota_cache)

        xml = self._api_get("/api/monitoring/start_date", timeout=timeout, lane=lane)
        fields = self._optional_billing_response_fields(xml)
        if not fields:
            return dict(self._traffic_quota_cache)
        values = self._optional_billing_integer_fields(fields, {
            "StartDay": "traffic_quota_start_day",
            "trafficmaxlimit": "traffic_quota_limit_bytes",
            "DataLimitAwoke": "traffic_quota_awoke",
            "MonthThreshold": "traffic_quota_month_threshold_pct",
            "DayThreshold": "traffic_quota_day_threshold_pct",
            "SetMonthData": "traffic_quota_set_month_data",
            "turnoffdataenable": "traffic_quota_turnoff_data_enable",
            "turnoffdataswitch": "traffic_quota_turnoff_data_switch",
            "turnoffdataflag": "traffic_quota_turnoff_data_flag",
        })

        def _limit_bytes_from_label(label: str) -> int | None:
            text = str(label or "").strip().replace(" ", "")
            match = re.match(r"^([0-9]+(?:\.[0-9]+)?)([KMGTP]?B)?$", text, re.IGNORECASE)
            if not match:
                return None
            value = float(match.group(1))
            unit = (match.group(2) or "B").upper()
            factor = {
                "B": 1,
                "KB": 1024,
                "MB": 1024 ** 2,
                "GB": 1024 ** 3,
                "TB": 1024 ** 4,
                "PB": 1024 ** 5,
            }.get(unit, 1)
            return int(value * factor)

        limit_label = fields.get("DataLimit", "")
        if limit_label:
            values["traffic_quota_limit_label"] = limit_label
            # HiLink's zero raw maximum is a native sentinel when DataLimit
            # supplies a valid unit-bearing allowance (existing B818 contract).
            if not values.get("traffic_quota_limit_bytes"):
                label_bytes = _limit_bytes_from_label(limit_label)
                if label_bytes is not None:
                    values["traffic_quota_limit_bytes"] = label_bytes
        if not values:
            return dict(self._traffic_quota_cache)
        self._traffic_quota_cache = values
        self._traffic_quota_ts = now
        return dict(self._traffic_quota_cache)

    @staticmethod
    def _optional_billing_response_fields(xml: str | None) -> Dict[str, str]:
        """One successful-response boundary for both optional billing APIs."""
        if not xml:
            return {}
        try:
            root = ET.fromstring(xml)
        except (ET.ParseError, TypeError, ValueError):
            return {}
        if root.tag != "response" or root.find("error") is not None:
            return {}
        values: Dict[str, str] = {}
        conflicts: set[str] = set()
        for item in root:
            value = str(item.text or "").strip()
            if item.tag in values and values[item.tag] != value:
                conflicts.add(item.tag)
            values[item.tag] = value
        return {key: value for key, value in values.items() if key not in conflicts}

    @staticmethod
    def _optional_billing_integer_fields(fields: Dict[str, str], names: Dict[str, str]) -> Dict[str, Any]:
        return {canonical: int(fields[tag]) for tag, canonical in names.items()
                if tag in fields and fields[tag].isascii() and fields[tag].isdecimal()}

    # ─── Wi-Fi status (read-only) ──────────────────────────

    def _read_wifi_status(self, *, max_age_s: float = 0.0, timeout: Optional[Any] = None) -> tuple:
        """
        Read Wi-Fi per-band enable state via /api/wlan/multi-basic-settings.

        Returns (wifi_enabled, wifi_24ghz_enabled, wifi_5ghz_enabled).
        All values are Optional[bool] — None if endpoint unavailable.

        Huawei firmware may expose multiple SSIDs per radio. On the live B818:
          Radio.1.* = 2.4 GHz
          Radio.2.* = 5 GHz
        so we must group by <ID> rather than assume Index 0/1 means 2.4/5.
        """
        now = time.time()
        cached_status = tuple(getattr(self, "_last_wifi_status", (None, None, None)))
        if max_age_s > 0.0:
            age = max(0.0, now - float(getattr(self, "_last_wifi_status_ts", 0.0) or 0.0))
            if age <= max_age_s:
                return cached_status

        model = str(
            (self._device_info_cache or {}).get("model")
            or (self._device_info_cache or {}).get("device_model")
            or self._expected_model
            or ""
        ).strip().lower()
        if "b535-333" in model:
            physical = self._read_wifi_switch_status(timeout=timeout)
            if physical is not None:
                self._last_wifi_status = physical
                self._last_wifi_status_ts = now
                return tuple(physical)

        if timeout is None:
            xml = self._api_get("/api/wlan/multi-basic-settings")
        else:
            xml = self._api_get("/api/wlan/multi-basic-settings", timeout=timeout)
        if not xml:
            return cached_status
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            log.debug("WiFi status XML parse error: %s", e)
            return cached_status

        # Check for error response (endpoint may not exist on some firmware)
        if root.find("code") is not None:
            log.debug("WiFi status endpoint returned error code: %s",
                      root.find("code").text if root.find("code") is not None else "?")
            return cached_status

        status = self._decode_wifi_profile(xml, reader="ssids")
        if all(value is None for value in status):
            return cached_status
        self._last_wifi_status = status
        self._last_wifi_status_ts = now
        return tuple(self._last_wifi_status)

    def _read_wifi_switch_status(
        self,
        *,
        timeout: Optional[Any] = None,
    ) -> Optional[tuple[bool, Optional[bool], Optional[bool]]]:
        """Return physical HiLink radio-switch state when the endpoint supplies it."""

        if timeout is None:
            xml = self._api_get("/api/wlan/status-switch-settings")
        else:
            xml = self._api_get("/api/wlan/status-switch-settings", timeout=timeout)
        if not xml:
            return None
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return None
        if root.find("code") is not None:
            return None
        status = self._decode_wifi_profile(xml, reader="switches")
        return None if all(value is None for value in status) else status

    def _decode_wifi_profile(self, xml: str, *, reader: str) -> tuple:
        """Decode this actual read; only the native owner may retain old values."""
        group = ("/api/wlan/status-switch-settings" if reader == "switches"
                 else "/api/wlan/multi-basic-settings")
        addresses = ("slow.wifi_enabled", "slow.wifi_24ghz_enabled", "slow.wifi_5ghz_enabled")
        values = self._decode_profile_values({group: xml}, set(addresses), context={
            "native_wifi_reader": reader, "previous_wifi": [None, None, None]})
        return tuple(values.get(address) for address in addresses)

    def get_wifi_status_result(self) -> BackendFeatureResult:
        """Read one fresh Wi-Fi state for a Modem Live event readback.

        Normal B818 maintenance keeps this slow/static endpoint cached for up
        to 30 minutes.  This explicit result method is only for a post-write
        confirmation owned by Modem Live; it does not alter the normal polling
        cadence.
        """

        read_started_at = time.time()
        wifi_on, wifi_24, wifi_5 = self._read_wifi_status(
            max_age_s=0.0,
            timeout=self._maintenance_timeout,
        )
        wifi_sample_ts = float(
            getattr(self, "_last_wifi_status_ts", 0.0) or 0.0
        )
        if (
            wifi_sample_ts < read_started_at
            or all(value is None for value in (wifi_on, wifi_24, wifi_5))
        ):
            return backend_failed(
                reason="wifi_status_unavailable",
                source="huawei_hilink.get_wifi_status",
            )
        status = {
            "wifi_enabled": wifi_on,
            "wifi_24ghz_enabled": wifi_24,
            "wifi_5ghz_enabled": wifi_5,
            "wifi_6ghz_enabled": None,
        }
        latest = dict(getattr(self, "_last_status", {}) or {})
        latest.update(status)
        latest["ts"] = time.time()
        self._last_status = latest
        self._last_status_ts = float(latest["ts"])
        return backend_ok(
            status,
            source="huawei_hilink.get_wifi_status",
            metadata={"fresh": True, "lane": "readback"},
        )

    def wifi_set_radio(self, radio: str, enabled: bool) -> bool:
        del radio, enabled
        raise RuntimeError(
            "huawei_direct_wifi_radio_write_forbidden:"
            "use_execute_physical_stage"
        )

    # ─── Uptime ─────────────────────────────────────────────

    def _read_uptime(self, *, timeout: Optional[Any] = None) -> float:
        """Read boot time and compute uptime in seconds."""
        xml = self._api_get("/api/device/boot_time", timeout=timeout)
        if not xml:
            return 0.0
        boot_raw = xml_text(xml, "boot_time")
        if not boot_raw:
            return 0.0
        # Format: "[DD]:[HH]:[MM]:[SS]" — parse to seconds
        try:
            parts = boot_raw.replace("[", "").replace("]", "").split(":")
            parts = [int(p) for p in parts if p]
            if len(parts) == 4:
                return parts[0] * 86400 + parts[1] * 3600 + parts[2] * 60 + parts[3]
            elif len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
        except (ValueError, IndexError):
            pass
        return 0.0

    # ─── RTT measurement ────────────────────────────────────

    def measure_rtt(
        self,
        target: str = "8.8.8.8",
        count: int = 3,
        *,
        timeout_s: float,
    ) -> RttProbeResult:
        """
        Measure RTT via subprocess ping.

        B818 doesn't have RouterOS /ping. We ping from the host OS
        through the B818's network (must be default route or specify
        interface).
        """
        return measure_host_ping_rtt(
            target=target, count=count, ping_interface=self._ping_iface,
            timeout_s=timeout_s,
        )

    # ─── Neighbor scan (limited on B818) ────────────────────

    def read_neighbors(self) -> List[NeighborCell]:
        """B818 neighbor data comes from /api/device/signal inline."""
        sig_xml = self._api_get("/api/device/signal")
        if not sig_xml:
            return []
        try:
            root = ET.fromstring(sig_xml)
            nei = root.find("nei_cellid")
            nei_text = nei.text.strip() if nei is not None and nei.text else ""
            band_el = root.find("band")
            band = int(band_el.text) if band_el is not None and band_el.text else 0
            return self._parse_neighbor_pcis(nei_text, band)
        except (ET.ParseError, ValueError):
            return []

    # ─── Lever execution ────────────────────────────────────

    def execute_lever(self, action: LeverAction) -> LeverResult:
        """Reject the deleted mapping-driven physical command boundary."""

        raise RuntimeError(
            "huawei_generic_execute_lever_forbidden:"
            "use_execute_physical_stage"
        )

    def _execute_physical_post(
        self,
        plan: ModemLivePhysicalStagePlan,
        *,
        path: str,
        xml_body: str,
        timeout: Any,
        allow_response_unavailable: bool,
        csrf_delay_s: float,
        content_type: str = "application/xml",
    ) -> ModemLivePhysicalStageResult:
        """Execute one authorized POST and capture the actual transport call.

        Authentication and target-guard work happens before the physical
        dispatch timestamp. Ambiguous transport results are never retried;
        their possibly rotated token is discarded before the next stage.
        """

        with self._api_control_operation_lock:
            if self._target_guard_enabled():
                info = self._target_guard_observed_snapshot(max_age_s=300.0)
                self._apply_target_guard(info if isinstance(info, dict) else {})

            preservation_detail = {}
            if path == "/api/net/net-mode" and plan.stage_kind in {
                    "pband_anchor", "ca_pool", "band_selection", "band_lock", "band_auto"}:
                from arch4.modem.huawei_radio_mode import preserve_lte_write
                xml_body, preserved = preserve_lte_write(self, xml_body)
                if preserved is not None:
                    preservation_detail["radio_mode_fields"] = (
                        "preserved_from_fresh_native_read" if preserved else "unavailable_lte_fallback")
            definition=getattr(self,'_definition',None)
            if definition is not None and 'hilink:'+path in definition.writes:
                root=ET.fromstring(xml_body)
                if any(list(child)or child.attrib for child in root):
                    raise ValueError('portable_hilink_parameter_stage_must_be_flat')
                parameters={child.tag:child.text or '' for child in root}
                translated=definition.prepare_native_write_fields('hilink:'+path,parameters)
                if isinstance(plan.target, CoreRadioModeTarget) and translated != parameters:
                    raise RuntimeError("huawei_radio_mode_native_write_changed")
                if preservation_detail.get("radio_mode_fields") == "preserved_from_fresh_native_read":
                    protected = {"NetworkMode", "NetworkBand", "networkOption"}
                    if ({key: value for key, value in parameters.items() if key in protected}
                            != {key: value for key, value in translated.items() if key in protected}):
                        raise RuntimeError("huawei_lte_preserved_fields_changed")
                if translated!=parameters:
                    root.clear()
                    for key,value in translated.items():ET.SubElement(root,key).text=str(value)
                    xml_body=ET.tostring(root,encoding='unicode')
            token = self._control_csrf_token()
            if not token:
                raise RuntimeError("huawei_physical_stage_csrf_token_unavailable")

            delay_s = max(0.0, float(csrf_delay_s or 0.0))
            if delay_s:
                time.sleep(delay_s)
            with self._hilink_http_request_scope("control") as (
                session,
                _gate_wait_s,
            ):
                request_dispatched_at = time.time()
                try:
                    response = traced_http(session.post,
                        f"{self._base_url}{path}",
                        data=xml_body,
                        headers={
                            "__RequestVerificationToken": token,
                            "Content-Type": content_type,
                        },
                        timeout=timeout,
                    )
                except requests.ReadTimeout as exc:
                    transport_returned_at = time.time()
                    self._mark_control_token_unknown()
                    permits_verification = bool(allow_response_unavailable)
                    reason = (
                        "response_unavailable_verification_pending"
                        if permits_verification
                        else "read_timeout"
                    )
                    self._last_post_response_unavailable_path = (
                        path if permits_verification else ""
                    )
                    return ModemLivePhysicalStageResult(
                        plan=plan,
                        request_dispatched_at=request_dispatched_at,
                        transport_returned_at=transport_returned_at,
                        outcome=(
                            "response_unavailable"
                            if permits_verification
                            else "rejected"
                        ),
                        reason=reason,
                        detail={
                            **preservation_detail,
                            "transport_path": path,
                            "transport_error": type(exc).__name__,
                        },
                    )
                except requests.RequestException as exc:
                    transport_returned_at = time.time()
                    self._mark_control_token_unknown()
                    self._last_post_response_unavailable_path = ""
                    return ModemLivePhysicalStageResult(
                        plan=plan,
                        request_dispatched_at=request_dispatched_at,
                        transport_returned_at=transport_returned_at,
                        outcome="rejected",
                        reason=f"request_error:{type(exc).__name__}",
                        detail={
                            **preservation_detail,
                            "transport_path": path,
                            "transport_error": type(exc).__name__,
                        },
                    )

                transport_returned_at = time.time()
            self._update_token_from_response(response)
            self._last_post_response_unavailable_path = ""
            response_text = str(getattr(response, "text", "") or "")
            status_code = int(getattr(response, "status_code", 0) or 0)
            if isinstance(plan.target, CoreRadioModeTarget):
                from arch4.modem.huawei_radio_mode import native_ok
                accepted = status_code == 200 and native_ok(response_text)
            else:
                accepted = status_code == 200 and "OK" in response_text
            if accepted:
                reason = "confirmed_post_ok"
            elif (
                "<code>125002</code>" in response_text
                or "<code>125003</code>" in response_text
            ):
                self._mark_control_token_unknown()
                reason = "session_expired"
            else:
                reason = f"http_status_{status_code}"
            return ModemLivePhysicalStageResult(
                plan=plan,
                request_dispatched_at=request_dispatched_at,
                transport_returned_at=transport_returned_at,
                outcome="accepted" if accepted else "rejected",
                reason=reason,
                detail={
                    **preservation_detail,
                    "transport_path": path,
                    "http_status": status_code,
                },
            )

    @staticmethod
    def _netmode_xml_for_bands(bands: List[int]) -> str:
        return (
            "<request>"
            "<NetworkMode>03</NetworkMode>"
            "<NetworkBand>3FFFFFFF</NetworkBand>"
            f"<LTEBand>{_bands_to_hex(bands)}</LTEBand>"
            "</request>"
        )

    def _wifi_radio_xml_from_target(
        self,
        target: CoreWifiRadioTarget,
    ) -> str:
        radio = {
            "2.4ghz": "2.4",
            "5ghz": "5",
        }.get(target.band)
        if radio is None:
            raise ValueError(
                f"huawei_wifi_radio_unsupported:{target.band}"
            )

        xml = self._api_get("/api/wlan/status-switch-settings")
        if not xml:
            raise RuntimeError("huawei_wifi_radio_state_unavailable")
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise RuntimeError(
                "huawei_wifi_radio_state_malformed"
            ) from exc
        radios_el = root.find("radios")
        if radios_el is None:
            raise RuntimeError("huawei_wifi_radio_rows_unavailable")

        target_pattern = ".Radio.1." if radio == "2.4" else ".Radio.2."
        default_idx = "0" if radio == "2.4" else "1"
        new_value = "1" if target.enabled else "0"
        modified = False
        for radio_el in radios_el.findall("radio"):
            radio_id = (radio_el.findtext("ID") or "").strip()
            index = (radio_el.findtext("index") or "").strip()
            enabled_el = radio_el.find("wifienable")
            if enabled_el is None:
                continue
            if (
                target_pattern in radio_id
                or f".Wifi{target_pattern}" in radio_id
                or (not radio_id and index == default_idx)
            ):
                enabled_el.text = new_value
                modified = True
        if not modified:
            raise RuntimeError(
                f"huawei_wifi_radio_row_unavailable:{target.band}"
            )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<request>{ET.tostring(radios_el, encoding='unicode')}</request>"
        )

    def execute_physical_stage(
        self,
        plan: ModemLivePhysicalStagePlan,
    ) -> ModemLivePhysicalStageResult:
        """Translate and execute exactly one typed Modem Live stage."""

        if not isinstance(plan, ModemLivePhysicalStagePlan):
            raise TypeError("huawei_physical_stage_plan_required")

        target = plan.target
        if isinstance(target, CoreRadioModeTarget):
            from arch4.modem.huawei_radio_mode import execute
            return execute(self, plan)
        if self._backend_name == "h173" and (
                plan.stage_kind not in {"ca_pool", "band_selection", "band_lock"}
                or not isinstance(target, CoreBandSetTarget)):
            raise NotImplementedError("h173_physical_control_not_qualified")
        if isinstance(target, CoreNetworkSettingsTarget):
            from arch4.modem.huawei_network_settings import execute_network_stage
            return execute_network_stage(self, plan)
        if isinstance(target, CoreMobileDataTarget):
            from arch4.modem.huawei_network_settings import require_mobile_data_profile
            require_mobile_data_profile(self)
            return self._execute_physical_post(
                plan, path="/api/dialup/mobile-dataswitch",
                xml_body=f"<request><dataswitch>{int(target.enabled)}</dataswitch></request>",
                timeout=self._timeout, allow_response_unavailable=False,
                csrf_delay_s=0.5,
            )
        if plan.stage_kind == "pband_anchor":
            if not isinstance(target, CorePBandTarget):
                raise TypeError("huawei_pband_anchor_target_required")
            bands = (
                []
                if target.pband == 0
                else [target.pband]
            )
            xml_body = (
                "<request>"
                f"<NetworkMode>{'00' if not bands else '03'}</NetworkMode>"
                "<NetworkBand>3FFFFFFF</NetworkBand>"
                f"<LTEBand>{ALL_BANDS_HEX if not bands else _bands_to_hex(bands)}</LTEBand>"
                "</request>"
            )
            return self._execute_physical_post(
                plan,
                path="/api/net/net-mode",
                xml_body=xml_body,
                timeout=(
                    2.0,
                    self._netmode_submit_timeout_s,
                ),
                allow_response_unavailable=(
                    self._netmode_continue_on_read_timeout_for_verification
                ),
                csrf_delay_s=0.1,
            )

        if plan.stage_kind in {
            "ca_pool",
            "band_selection",
            "band_lock",
        }:
            if not isinstance(target, CoreBandSetTarget):
                raise TypeError("huawei_band_set_target_required")
            return self._execute_physical_post(
                plan,
                path="/api/net/net-mode",
                xml_body=(f"<request><LTEBand>{_bands_to_hex(list(target.bands))}</LTEBand></request>"
                          if self._backend_name == "h173" else self._netmode_xml_for_bands(list(target.bands))),
                timeout=(
                    2.0,
                    self._netmode_submit_timeout_s,
                ),
                allow_response_unavailable=(
                    self._netmode_continue_on_read_timeout_for_verification
                ),
                csrf_delay_s=0.1,
            )

        if isinstance(target, CoreBandAutoTarget):
            if target.domain != "lte_allocated_auto":
                raise NotImplementedError(
                    "huawei_nr_band_auto_unsupported"
                )
            return self._execute_physical_post(
                plan,
                path="/api/net/net-mode",
                xml_body=(
                    "<request>"
                    "<NetworkMode>00</NetworkMode>"
                    "<NetworkBand>3FFFFFFF</NetworkBand>"
                    f"<LTEBand>{ALL_BANDS_HEX}</LTEBand>"
                    "</request>"
                ),
                timeout=(2.0, self._netmode_submit_timeout_s),
                allow_response_unavailable=(
                    self._netmode_continue_on_read_timeout_for_verification
                ),
                csrf_delay_s=0.1,
            )

        if isinstance(target, CoreRestartTarget):
            return self._execute_physical_post(
                plan,
                path="/api/device/control",
                xml_body="<request><Control>1</Control></request>",
                timeout=self._timeout,
                allow_response_unavailable=False,
                csrf_delay_s=0.5,
            )

        if isinstance(target, CoreWifiRadioTarget):
            result = self._execute_physical_post(
                plan,
                path="/api/wlan/status-switch-settings",
                xml_body=self._wifi_radio_xml_from_target(target),
                timeout=15.0,
                allow_response_unavailable=False,
                csrf_delay_s=0.5,
            )
            if result.confirmed_by_transport:
                self._last_wifi_status = (None, None, None)
                self._last_wifi_status_ts = 0.0
            return result

        raise NotImplementedError(
            "huawei_physical_stage_target_unsupported:"
            f"{target.command_kind}"
        )

    def _effective_serving_cell_for_confirmation(self) -> Dict[str, Any]:
        try:
            cell = self.read_serving_cell() or {}
        except Exception as exc:
            return {"error": str(exc)}
        return {
            "band": cell.get("band"),
            "pci": cell.get("pci"),
            "earfcn": cell.get("earfcn"),
            "cell_id": cell.get("cell_id"),
        }

    def _band_lock_confirmation(
        self,
        *,
        lock_type: str,
        requested: Dict[str, Any],
        accepted: bool,
        requested_bands: List[int],
        pband: Optional[int] = None,
    ) -> Dict[str, Any]:
        readback_bands: List[int] = []
        readback_error = ""
        if self._backend_name == "b818":
            pool, _sig, _source = self._valid_configured_pool_from_netmode(
                dict(getattr(self, "_netmode_cache", {}) or {})
            )
            if not pool:
                pool, _sig, _source = self._valid_configured_pool_from_netmode(
                    dict(getattr(self, "_last_status", {}) or {})
                )
            readback_bands = normalized_bands(pool)
            if not readback_bands and accepted:
                readback_bands = normalized_bands(requested_bands)
            last_status = dict(getattr(self, "_last_status", {}) or {})
            effective = {
                "band": last_status.get("serving_band") or last_status.get("primary_band"),
                "pci": last_status.get("pci"),
                "earfcn": last_status.get("earfcn"),
                "cell_id": last_status.get("cell_id"),
                "source": "status_cache",
            } if accepted else {}
            readback_source = "status_cache"
        else:
            try:
                readback_bands = normalized_bands(self.get_current_bands())
            except Exception as exc:
                readback_error = str(exc)
            effective = self._effective_serving_cell_for_confirmation() if accepted else {}
            readback_source = "/api/net/net-mode"
        effective_band_match_value = None
        effective_pband_match = None
        effective_band = effective.get("band")
        if accepted and effective_band not in (None, ""):
            try:
                effective_band_match_value = effective_band_match([int(effective_band or 0)], requested_bands)
            except Exception:
                effective_band_match_value = None
        if pband and effective_band not in (None, ""):
            try:
                effective_pband_match = int(effective_band or 0) == int(pband)
            except Exception:
                effective_pband_match = None
        readback: Dict[str, Any] = {
            "available": bool(readback_bands) or not readback_error,
            "bands": readback_bands,
            "source": readback_source,
        }
        if readback_error:
            readback["error"] = readback_error
        return build_lock_confirmation(
            lock_type=lock_type,
            requested=requested,
            accepted=bool(accepted),
            readback=readback,
            effective=effective,
            effective_band_match_value=effective_band_match_value,
            effective_pband_match=effective_pband_match,
            configured_band_match_value=configured_band_match(readback_bands, requested_bands),
            source="huawei_hilink_net_mode",
        )

    def _exec_band_selection(self, action: LeverAction) -> LeverResult:
        """
        Set allowed LTE bands via /api/net/net-mode.

        B818 band selection mechanics (from bookmarklet):
        1. Fetch fresh CSRF token
        2. POST XML with NetworkMode=03 (4G), LTEBand=hex mask
        3. Router applies within ~5-10s (drops and re-establishes)
        """
        del action
        raise RuntimeError(
            "huawei_mapping_band_selection_forbidden:"
            "use_execute_physical_stage"
        )

    def _exec_band_lock(self, action: LeverAction) -> LeverResult:
        """Huawei HiLink band lock uses the same net-mode POST as band selection.

        The protocol is identical: restrict LTEBand to the requested mask.
        The semantic split exists so product logic can treat a hard single-band
        lock as a different action class from ordinary allowed-band-set edits.
        """
        del action
        raise RuntimeError(
            "huawei_mapping_band_lock_forbidden:"
            "use_execute_physical_stage"
        )

    def _exec_pband_anchor(self, action: LeverAction) -> LeverResult:
        """
        Force PBAND via /api/net/net-mode (HiLink B818/B535).

        Use two sequential POST requests to /api/net/net-mode.
        Field testing confirmed this sequence:

        Step 1 — Lock: POST PBAND only. Modem has no choice but to camp.
                  Tower is forced to assign it as PCell.
        Step 2 — Expand: POST full CA pool including PBAND. Tower performs
                  SCell addition while keeping PBAND as PCell.

        A single combined-mask POST fails because the modem picks PBAND by RF
        quality (B3 wins if strongest). Some router tools expose this sequence
        through one UI action, but the modem API still needs two requests.
        NetworkBand=3FFFFFFF is correct — device-specific value makes no difference.
        Tower remains ultimate authority (can re-assign PCell on congestion).

        Params:
          pband   (int): desired primary band. 0 = release (restore auto).
          ca_pool (list[int]): allowed CA pool, including pband.
          dl_bands remains accepted as older CA-pool input.

        Release (pband=0): posts ALL_BANDS_HEX to restore automatic selection.
        """
        del action
        raise RuntimeError(
            "huawei_mapping_pband_anchor_forbidden:"
            "use_execute_physical_stage"
        )

    def _exec_restart(self, action: LeverAction) -> LeverResult:
        del action
        raise RuntimeError(
            "huawei_mapping_restart_forbidden:"
            "use_execute_physical_stage"
        )

    # ─── Startup verification (simplified for B818) ─────────

    def verify_static_config(self) -> Dict[str, bool]:
        """
        B818 manages its own firewall/NAT. No RouterOS rules to check.
        Return all-True — nothing to fix on B818 side.
        """
        return {
            "ttl_ipv4": True,
            "ttl_ipv6": True,
            "mss_ipv4": True,
            "mss_ipv6": True,
            "keepalive": True,
        }

    def apply_static_config(self, item: str) -> bool:
        del item
        raise RuntimeError(
            "huawei_direct_static_config_write_forbidden:"
            "startup_is_verify_only"
        )

    def verify_ue_capability(self) -> Dict[str, Any]:
        """
        Huawei HiLink UE capabilities are firmware-managed. No AT access.
        Return values that match checklist's expected string formats.
        """
        try:
            info = self.get_modem_info()
        except Exception:
            info = {}
        model = str((info or {}).get("model") or "").strip().lower()
        if self._backend_name == "b535" and "b535-333" in model:
            return {
                "dl_256qam": "1",
                "ul_64qam": "1",
                "ue_category": 13,
                "pci_lock_available": False,
                "dl_mimo_layers": 2,
            }
        if self._backend_name == "b535":
            return {
                "dl_256qam": "not_supported",
                "ul_64qam": "1",
                "ue_category": 7,
                "pci_lock_available": False,
                "dl_mimo_layers": 2,
            }
        if self._backend_name == "b530":
            return {
                "dl_256qam": "not_supported",  # B530: LTE Cat 7 (300/100)
                "ul_64qam": "1",
                "ue_category": 7,
                "pci_lock_available": False,
                "dl_mimo_layers": 2,
            }
        if self._backend_name == "h112":
            return {
                "dl_256qam": "1",              # H112-370: LTE Cat 19 + NR NSA
                "ul_64qam": "1",
                "ue_category": 19,
                "pci_lock_available": False,    # no cell lock on HiLink 5G CPEs
                "dl_mimo_layers": 4,
                "nsa_supported": True,
                "nr_bands": [1, 3, 7, 28, 78],
            }
        if self._backend_name == "h122":
            return {
                "dl_256qam": "1",              # H122-373: LTE Cat 19 + NR NSA/SA
                "ul_64qam": "1",
                "ue_category": 19,
                "pci_lock_available": False,    # no cell lock on HiLink 5G CPEs
                "dl_mimo_layers": 4,
                "nsa_supported": True,
                "nr_bands": [1, 3, 7, 28, 78],
            }
        if self._backend_name not in {"b818", "b535", "b530"}:
            return {
                "dl_256qam": "unknown",
                "ul_64qam": "unknown",
                "ue_category": 0,
                "pci_lock_available": False,
                "dl_mimo_layers": 0,
            }
        return {
            "dl_256qam": "1",              # B818 Cat 19 has 256QAM DL (firmware-managed)
            "ul_64qam": "cat12_implied",   # Cat 19 hardware implies 64QAM UL
            "ue_category": 19,             # Cat 19 (5CA capable)
            "pci_lock_available": False,    # no cell lock on B818
            "dl_mimo_layers": 4,           # 4x4 MIMO capable
        }

    def set_ue_capability(self, key: str, value: Any) -> bool:
        del key, value
        raise RuntimeError(
            "huawei_direct_ue_capability_write_forbidden:"
            "use_execute_physical_stage"
        )

    # ─── Restart ────────────────────────────────────────────

    def restart_modem(self) -> bool:
        raise RuntimeError(
            "huawei_direct_restart_write_forbidden:"
            "use_execute_physical_stage"
        )

    # ─── Info ───────────────────────────────────────────────

    def read_fresh_device_identity(self) -> Dict[str, str]:
        started_at = time.time()
        identity = self.get_modem_info(timeout=self._maintenance_timeout)
        session_state = self._last_status.get("backend_session", {})
        if (session_state.get("expired") is True
                and session_state.get("path") == "/api/device/information"
                and float(session_state.get("ts", 0.0)) >= started_at):
            raise SessionExpiredError("fresh_identity_requires_login")
        if self._has_real_device_identity(identity):
            self._device_info_cache = dict(identity)
            self._device_info_ts = time.time()
        return identity

    def get_modem_info(self, timeout: Any = None) -> Dict[str, str]:
        """Get B818 device info from /api/device/information."""
        xml = self._api_get("/api/device/information", timeout=timeout, lane="control")
        if not xml:
            return {}
        imei = (
            xml_text(xml, "Imei", "")
            or xml_text(xml, "IMEI", "")
            or xml_text(xml, "SerialNumber", "")
        )
        info = {
            "model": xml_text(xml, "DeviceName", self._device_label()),
            "serial": xml_text(xml, "SerialNumber", ""),
            "hardware_version": xml_text(xml, "HardwareVersion", ""),
            "software_version": xml_text(xml, "SoftwareVersion", "unknown"),
            "firmware": xml_text(xml, "SoftwareVersion", "unknown"),
            "webui": xml_text(xml, "WebUIVersion", "unknown"),
            "config_version": (
                xml_text(xml, "ConfigVersion", "")
                or xml_text(xml, "CfgVersion", "")
                or xml_text(xml, "ConfigurationFileVersion", "")
                or xml_text(xml, "ConfigFileVersion", "")
                or xml_text(xml, "ExternalVersion", "")
                or xml_text(xml, "customize_version", "")
                or xml_text(xml, "iniversion", "")
            ),
            "lan_mac": (
                xml_text(xml, "LanMacAddress", "")
                or xml_text(xml, "MacAddress1", "")
                or xml_text(xml, "macAddress1", "")
            ),
            "wan_ip": xml_text(xml, "WanIPAddress", ""),
            "imei": imei,
            "imsi": xml_text(xml, "Imsi", "") or xml_text(xml, "IMSI", ""),
            "iccid": xml_text(xml, "Iccid", "") or xml_text(xml, "ICCID", ""),
            "wifi_24ghz_mac": xml_text(xml, "WifiMacAddrWl0", ""),
            "wifi_5ghz_mac": xml_text(xml, "WifiMacAddrWl1", ""),
            "wan_dns_address": xml_text(xml, "wan_dns_address", ""),
        }
        identity_fields = {
            "identity.model": "model", "identity.serial": "serial",
            "identity.imei": "imei", "identity.imsi": "imsi",
            "identity.hardware_version": "hardware_version",
            "identity.firmware": "firmware", "identity.software_version": "software_version",
            "identity.webui": "webui", "identity.lan_mac": "lan_mac",
        }
        # WAN address comes from this existing native response, not a second
        # probe or a cached identity fallback. Omitted rules preserve R24's
        # existing native branch; an admitted malformed edit stays unavailable.
        if "slow.wan_ip" in self._definition.available_addresses({"/api/device/information":xml}):
            identity_fields["slow.wan_ip"] = "wan_ip"
        values = self._decode_profile_values({"/api/device/information": xml}, set(identity_fields))
        original_identity = self._normalize_device_identity(info)
        if (values.get("identity.model") == original_identity.get("model")
                and original_identity.get("model_source")):
            info["model_source"] = original_identity["model_source"]
        self._profile_identity_unavailable = {
            key for address, key in identity_fields.items() if values.get(address) is None
        }
        for address, key in identity_fields.items():
            value = values.get(address)
            info[key] = value if value is not None else ""
        if values.get("identity.model") == "":
            info["model"] = self._device_label()  # Existing provisional display label.
        # Full startup observation owns physical antenna discovery. Identity
        # refresh must not fan out into antenna endpoint probes.
        antenna = dict(self._antenna_cache or {})
        antenna_label = str((antenna or {}).get("antenna") or "").strip()
        if antenna_label:
            info["antenna"] = antenna_label
            info["antenna_mode"] = antenna_label
            info["antenna_mode_source"] = "huawei_static_antenna"
        return self._normalize_device_identity(info)

    def read_plmn(self) -> Optional[str]:
        """Return the last acquired PLMN; acquisition owns all physical reads."""
        return str((self._plmn_cache or {}).get("plmn") or "").strip() or None

    def _get_device_info_cached(self, max_age_s: float = 300.0, timeout: Any = None) -> Dict[str, str]:
        now = time.time()
        max_age = max(0.0, float(max_age_s or 0.0))
        cache = dict(self._device_info_cache or {})
        if self._has_license_identity(cache) and max_age > 0.0:
            age = max(0.0, now - float(self._device_info_ts or 0.0))
            if age < max_age:
                return cache
        if self._has_real_device_identity(cache) and max_age > 0.0:
            retry_s = max(1.0, float(getattr(self, "_device_info_missing_license_retry_s", 10.0) or 10.0))
            age = max(0.0, now - float(getattr(self, "_device_info_ts", 0.0) or 0.0))
            if age < retry_s:
                return cache
        elif max_age > 0.0:
            last_attempt = float(getattr(self, "_device_info_attempt_ts", 0.0) or 0.0)
            retry_s = max(1.0, float(getattr(self, "_device_info_missing_license_retry_s", 10.0) or 10.0))
            if last_attempt > 0.0 and max(0.0, now - last_attempt) < retry_s:
                return cache
        self._device_info_attempt_ts = now
        info = self.get_modem_info(timeout=timeout)
        if isinstance(info, dict) and self._has_real_device_identity(info):
            self._device_info_acquired_fields = frozenset(
                key for key, value in info.items()
                if (str(value or "").strip() or key in {"imsi", "iccid"})
                and key not in self._profile_identity_unavailable
            )
            merged = dict(cache)
            for key, value in info.items():
                if key in self._device_info_acquired_fields:
                    merged[key] = value
            if self._has_real_device_identity(merged):
                self._device_info_cache = merged
                self._device_info_ts = now
                return dict(self._device_info_cache)
        if not cache and isinstance(info, dict):
            return info
        return cache

    def _get_antenna_cached(self, max_age_s: float = 30.0, *, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        if self._antenna_cache and not force:
            return self._antenna_cache
        if self._antenna_cache and (now - self._antenna_ts) < max_age_s:
            return self._antenna_cache
        info = self._read_antenna_status()
        if info:
            self._antenna_cache = info
            self._antenna_ts = now
        return self._antenna_cache

    def _get_plmn_cached(self, max_age_s: float = 10.0, *, timeout: Any = None) -> Dict[str, str]:
        now = time.time()
        if self._plmn_cache and max_age_s > 0 and now - self._plmn_cache_ts < max_age_s:
            return self._plmn_cache
        xml = self._api_get("/api/net/current-plmn", timeout=timeout)
        if not xml:
            return self._plmn_cache
        plmn = xml_text(xml, "Numeric")
        values = self._decode_profile_values({"/api/net/current-plmn":xml}, {"identity.provider_plmn"})
        if "identity.provider_plmn" in values:
            plmn = values["identity.provider_plmn"] or ""
        name_context={'provider_plmn':plmn,'provider_names':{
            code:resolve_provider_name(code)for code in {plmn,xml_text(xml,'Numeric')}if code}}
        names=self._decode_profile_values({'/api/net/current-plmn':xml},
            {'identity.provider_name'},context=name_context)
        short = xml_text(xml, "ShortName")
        full = xml_text(xml, "FullName")
        rat = xml_text(xml, "Rat")
        if plmn or short or full:
            self._plmn_cache = {
                "plmn": plmn,
                "name": full or short,
                "short_name": short,
                "full_name": full,
                "rat": rat,
            }
            self._plmn_cache_ts = now
            if 'identity.provider_name'in names:
                self._plmn_cache['_profile_provider_name']=names['identity.provider_name']
        return self._plmn_cache

    def _get_netmode_list_cached(self, max_age_s: float = 300.0) -> Dict[str, Any]:
        now = time.time()
        if self._netmode_list_cache:
            return self._netmode_list_cache
        xml = self._api_get("/api/net/net-mode-list")
        if not xml:
            return self._netmode_list_cache
        lte_band_values = _extract_xml_mask_values(xml, "LTEBand")
        lte_band_values = [v for v in lte_band_values if not _is_all_bands_mask(v)] or lte_band_values
        lte_band_hex = lte_band_values[0] if lte_band_values else xml_text(xml, "LTEBand")
        band_list_values = _extract_xml_mask_values(xml, "BandList")
        band_list_values = [v for v in band_list_values if not _is_all_bands_mask(v)] or band_list_values
        band_list_hex = band_list_values[0] if band_list_values else xml_text(xml, "BandList")
        mask_candidates = [*lte_band_values, lte_band_hex, *band_list_values, band_list_hex]
        supported_bands: list[int] = []
        for raw_mask in mask_candidates:
            supported_bands = _hex_to_bands(raw_mask, auto_is_empty=False)
            if supported_bands:
                break
        if not supported_bands:
            # Some firmwares may expose explicit LTE band numbers instead of bitmasks.
            for raw_value in mask_candidates:
                parsed = _parse_band_list_text(raw_value)
                if parsed:
                    supported_bands = parsed
                    break
        self._netmode_list_cache = {
            "lte_band_hex": lte_band_hex,
            "band_list_hex": band_list_hex,
            "supported_bands": list(supported_bands),
            "supported_bands_str": "+".join(f"B{b}" for b in supported_bands) if supported_bands else "",
            "supported_bands_observed_at": now,
        }
        self._netmode_list_ts = now
        return self._netmode_list_cache

    def _get_netmode_cached(
        self,
        max_age_s: float = 30.0,
        *,
        include_supported_list: bool = True,
    ) -> Dict[str, Any]:
        now = time.time()
        netmode_cache = dict(getattr(self, "_netmode_cache", {}) or {})
        if netmode_cache and (now - float(getattr(self, "_netmode_ts", 0.0) or 0.0)) < max_age_s:
            return netmode_cache
        xml = self._api_get("/api/net/net-mode")
        if not xml:
            return netmode_cache
        lte_band_values = _extract_xml_mask_values(xml, "LTEBand")
        constrained_lte_band_values = [v for v in lte_band_values if not _is_all_bands_mask(v)]
        lte_band_hex = (
            constrained_lte_band_values[0]
            if constrained_lte_band_values
            else (lte_band_values[0] if lte_band_values else xml_text(xml, "LTEBand"))
        )
        selected_bands = _hex_to_bands(lte_band_hex, auto_is_empty=True)
        netmode_list = (
            self._get_netmode_list_cached()
            if include_supported_list
            else dict(self._netmode_list_cache or {})
        )
        # Private native evidence; only the Foundation-policy adapter derives CA Pool.
        self._netmode_cache = {
            "lte_band_hex": lte_band_hex,
            "allowed_bands": list(selected_bands),
            "allowed_bands_str": "+".join(f"B{b}" for b in selected_bands) if selected_bands else "AUTO",
            "netmode_list_lte_band_hex": netmode_list.get("lte_band_hex"),
            "netmode_list_band_list_hex": netmode_list.get("band_list_hex"),
            "netmode_list_supported_bands": list(netmode_list.get("supported_bands") or []),
            "netmode_list_supported_bands_observed_at": float(
                netmode_list.get("supported_bands_observed_at", 0.0) or 0.0
            ),
        }
        self._netmode_ts = now
        return self._netmode_cache

    def _read_antenna_status(self) -> Dict[str, Any]:
        """Keep native probe order; translate only the response actually read."""
        paths = [
            "/api/device/antenna_set_type", "/api/device/antenna_status",
            "/api/device/antenna_type", "/api/device/antenna_settings",
            "/api/device/antenna", "/api/device/antennastatus",
            "/api/device/antenna-mode",
        ]
        for path in paths:
            xml = self._api_get(path)
            if not xml:
                continue
            values = self._decode_profile_values({path: xml}, {"slow.antenna"},
                context={"native_antenna_path": path})
            label = values.get("slow.antenna")
            if label:
                return {"antenna": label}
        return {}

    def get_last_status(self) -> Dict[str, Any]:
        raw = getattr(self, "_last_status", {}) or {}
        status = build_public_modem_status(raw)
        if "radio_mode" in raw:
            status["radio_mode"] = raw["radio_mode"]
        return status

    def get_current_bands(self) -> List[int]:
        """Get currently allowed bands.

        B818 treats configured-band truth as a ModemLive-owned slow/event lane.
        If the cache/status register does not currently carry a valid pool, do
        not poll ``/api/net/net-mode`` from this helper; latch a refresh request
        and let live maintenance perform the special-care read.
        """
        if self._backend_name == "b818":
            pool, _sig, _source = self._valid_configured_pool_from_netmode(
                dict(getattr(self, "_netmode_cache", {}) or {})
            )
            if pool:
                return list(pool)
            status_pool, _status_sig, _status_source = self._valid_configured_pool_from_netmode(
                dict(getattr(self, "_last_status", {}) or {})
            )
            if status_pool:
                return list(status_pool)
            self._request_netmode_event_refresh("configured_ca_pool_cache_missing")
            return []
        xml = self._api_get("/api/net/net-mode")
        if not xml:
            return []
        lte_band_hex = xml_text(xml, "LTEBand")
        bands = _hex_to_bands(lte_band_hex)
        return bands if bands else []  # empty = auto/all

    def _radio_mode_status(self, lane, *, timeout=None, required=False):
        from arch4.modem.huawei_radio_mode import status
        return status(self, lane, timeout=timeout, required=required)

    @property
    def radio_mode_preferred_key(self):
        # Recovery restores a saved choice. No new default SA/NSA choice is
        # inferred for Manual NR when firmware supplies an optional preference.
        return None

    def get_capabilities(self):
        if self._capabilities is not None:
            return self._capabilities
        caps = get_capabilities(self._capability_backend_key())
        # Probe critical API endpoints; disable features if blocked
        signal_ok = self._api_get("/api/device/signal") is not None
        status_ok = self._api_get("/api/monitoring/status") is not None
        netmode_ok = self._api_get("/api/net/net-mode") is not None
        supported_band_evidence = (
            self._get_netmode_list_cached(max_age_s=300.0)
            if netmode_ok
            else {}
        )
        plmn_ok = self._api_get("/api/net/current-plmn") is not None
        if not signal_ok:
            caps.supports_neighbors = False
            caps.supports_ca_details = False
        if not status_ok:
            caps.supports_ca_details = False
        if not netmode_ok:
            caps.supports_band_selection = False
            caps.supports_band_priority = False
        supported_lte_bands = list(
            supported_band_evidence.get("supported_bands") or []
        )
        if supported_lte_bands:
            self._last_status["supported_lte_bands"] = supported_lte_bands
            self._last_status[
                "supported_lte_bands_source"
            ] = "api.net.net-mode-list"
            self._last_status["supported_lte_bands_observed_at"] = float(
                supported_band_evidence.get("supported_bands_observed_at", 0.0)
                or self._netmode_list_ts
                or time.time()
            )
            self._last_status[
                "netmode_list_supported_bands"
            ] = supported_lte_bands
        if not plmn_ok:
            caps.supports_plmn = False
        log.info(
            "%s capabilities probe: signal=%s status=%s netmode=%s plmn=%s",
            self._device_label(),
            "ok" if signal_ok else "blocked",
            "ok" if status_ok else "blocked",
            "ok" if netmode_ok else "blocked",
            "ok" if plmn_ok else "blocked",
        )
        self._capabilities = caps
        return caps

    # ─── Extended capabilities (limited on B818) ────────────

    def scan_neighbors(self) -> List[Dict[str, Any]]:
        """B818 neighbor data: PCI only. Returns list of dicts."""
        neighbors = self.read_neighbors()
        return [
            {"pci": n.pci, "band": n.band}
            for n in neighbors
        ]

    def read_serving_cell(self) -> Dict[str, Any]:
        """Read signal data as serving cell info."""
        xml = self._api_get("/api/device/signal")
        if not xml:
            return {}
        try:
            root = ET.fromstring(xml)

            def val(tag, default=""):
                el = root.find(tag)
                return el.text.strip() if el is not None and el.text else default

            band = int(val("band") or "0")
            earfcn, _ = parse_serving_earfcns(
                val("earfcn"), band=band, dl_frequency_100khz=val("ltedlfreq"),
                ul_frequency_100khz=val("lteulfreq"),
            )
            return {
                "band": band,
                "pci": int(val("pci") or "0"),
                "rsrp": self._parse_signal_val(val("rsrp")),
                "rsrq": self._parse_signal_val(val("rsrq")),
                "sinr": self._parse_signal_val(val("sinr")),
                "rssi": self._parse_signal_val(val("rssi")),
                "earfcn": earfcn,
                "dlbw": val("dlbandwidth"),
                "ulbw": val("ulbandwidth"),
                "txpower": val("txpower"),
                "rrc": val("rrc_status"),
                "transmode": val("transmode"),
            }
        except (ET.ParseError, ValueError):
            return {}

    def read_band_priority(self) -> List[int]:
        """B818 doesn't have band priority ordering — return empty (skip check)."""
        return []

    def set_band_priority(self, bands: List[int]) -> bool:
        del bands
        raise RuntimeError(
            "huawei_direct_band_priority_write_forbidden:"
            "use_execute_physical_stage"
        )

    def sequential_band_lock(self, bands: List[int],
                             delay_between: float = 20.0) -> bool:
        del bands, delay_between
        raise RuntimeError(
            "huawei_direct_sequential_band_write_forbidden:"
            "use_typed_scheduled_commands"
        )

    def set_auto_bands(self) -> bool:
        raise RuntimeError(
            "huawei_direct_auto_band_write_forbidden:"
            "use_execute_physical_stage"
        )
