"""Translate modem-native names to Modem Live Matrix names.

Only Modem Live performs this translation. Other code must read the normalized
Matrix cells.
"""

from __future__ import annotations

from arch4.core_contracts.network_settings import validate_network_observation

from dataclasses import dataclass
import ipaddress
import math
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping

from arch4.modem.base import PUBLIC_MODEM_STATUS_KEYS
from arch4.modem_live.truth_matrix import MODEM_LIVE_TRUTH_FIELD_CATALOG


OWNER = "54guc.modem_live.canonical_aliases"
MAX_UNKNOWN_ALIAS_EVIDENCE = 25


Normalizer = Callable[[Any], Any]

CANONICAL_LIVE_FIELD_ALIASES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "serving_band": (
            "serving_band",
            "primary_band",
            "pband",
            "p_band",
            "band",
            "anchor_band",
        ),
        "configured_band_pool": (
            "configured_band_pool",
            "ca_pool",
            "dl_bands",
            "band_mask",
            "lte_band_mask",
            "configured_bands",
        ),
        "pci": ("pci", "physical_cell_id", "cell_pci", "lte_pci"),
        "earfcn_dl": ("earfcn_dl", "dl_earfcn", "earfcn", "lte_earfcn"),
        "earfcn_ul": ("earfcn_ul", "ul_earfcn"),
        "nr_band": (
            "nr_band", "nr5g_band", "5g_band",
            "network_Z5g_CELLINFO_band", "Z5g_CELLINFO_band",
        ),
        "nr_pci": (
            "nr_pci", "nr5g_pci", "5g_pci",
            "network_Z5g_PCI", "Z5g_PCI",
        ),
        "nr_arfcn": (
            "nr_arfcn",
            "nr5g_arfcn",
            "nr_action_channel",
            "nr5g_action_channel",
            "Z5g_dlEarfcn",
        ),
        "nr_cell_id": ("nr_cell_id", "nr5g_cell_id", "Z5g_CELL_ID"),
        "nr_cells": ("nr_cells", "nr5g_cells"),
        "nr_bandwidth_mhz": (
            "nr_bandwidth_mhz",
            "nr5g_bandwidth_mhz",
            "nr5g_nsa_bandwidth",
        ),
        "enb_id": ("enb_id", "enb", "e_nb_id", "enodeb_id"),
        "cell_id": ("cell_id", "lte_cell_id", "eci", "global_cell_id"),
        "tac": (
            "tac",
            "TAC",
            "Tracking Area Code",
            "tracking_area_code",
            "lac/tac",
            "lac_tac",
            "lac_code",
            "lte-tac",
            "lte_tac",
            "area",
        ),
        "sinr_db": ("sinr_db", "sinr", "lte_sinr", "rs_sinr"),
        "rsrp_dbm": ("rsrp_dbm", "rsrp", "lte_rsrp"),
        "rsrq_db": (
            "rsrq_db",
            "rsrq",
            "RSRQ",
            "rsrq_current",
            "signal.rsrq",
            "signal_rsrq",
            "lte_rsrq",
            "lte-rsrq",
            "current_rsrq",
            "rsrqCurrent",
        ),
        "rssi_dbm": ("rssi_dbm", "rssi", "lte_rssi"),
        "cqi": ("cqi", "dl_cqi", "lte_cqi", "scheduler_cqi"),
        "cqi0": (
            "cqi0",
            "dl_cqi0",
            "dl_cqi",
            "stream0_cqi",
            "lte_cqi",
            "scheduler_cqi0",
        ),
        "cqi1": ("cqi1", "dl_cqi1", "stream1_cqi", "scheduler_cqi1"),
        "mcs_dl": ("mcs_dl", "dl_mcs", "mcs", "lte_mcs_dl"),
        "mcs_dl0": (
            "mcs_dl0",
            "dl_mcs0",
            "dl_mcs",
            "stream0_mcs",
            "lte_mcs_dl",
            "scheduler_mcs_dl0",
        ),
        "mcs_dl1": (
            "mcs_dl1",
            "dl_mcs1",
            "stream1_mcs",
            "scheduler_mcs_dl1",
        ),
        "mcs_ul": ("mcs_ul", "ul_mcs", "lte_mcs_ul", "scheduler_mcs_ul"),
        "qam_dl0": (
            "qam_dl0",
            "mod0",
            "dl_qam0",
            "dl_qam",
            "stream0_qam",
            "scheduler_qam0",
        ),
        "qam_dl1": (
            "qam_dl1",
            "mod1",
            "dl_qam1",
            "stream1_qam",
            "scheduler_qam1",
        ),
        "qam_ul": ("qam_ul", "ul_qam", "scheduler_qam_ul"),
        "ri": ("ri", "rank_indicator", "mimo_rank"),
        "tx_power": ("tx_power", "tx_power_dbm", "lte_tx_power"),
        "dl_bandwidth_mhz": (
            "dl_bandwidth_mhz",
            "dl_bandwidth",
            "dl_bw",
            "bandwidth_dl",
            "lte_dl_bandwidth",
            "dl_bw_mhz",
            "bandwidth_mhz",
            "lte_dl_bw_mhz",
        ),
        "ul_bandwidth_mhz": (
            "ul_bandwidth_mhz",
            "ul_bandwidth",
            "ul_bw",
            "bandwidth_ul",
            "lte_ul_bandwidth",
            "ul_bw_mhz",
            "lte_ul_bw_mhz",
        ),
        "dl_freq_mhz": (
            "dl_freq_mhz",
            "dl_frequency_mhz",
            "dl_freq",
            "frequency_mhz",
            "lte_dl_freq_mhz",
        ),
        "ul_freq_mhz": (
            "ul_freq_mhz",
            "ul_frequency_mhz",
            "ul_freq",
            "lte_ul_freq_mhz",
        ),
        "antenna_mode": ("antenna_mode", "antenna", "selected_antenna"),
        "wan_ip": (
            "wan_ip",
            "WAN IP",
            "IP Address",
            "ipaddress",
            "ip_address",
            "external_ip",
            "ipv4_addr",
            "ipv4-address",
            "bearer_ipv4",
            "wan_ipv4",
        ),
        "radio_state_text": (
            "radio_state_text",
            "radioState",
            "radio_state",
            "registration_state",
            "registrationStatus",
            "connection_state",
            "connectionStatusText",
            "ppp_status",
        ),
        "network_type": (
            "network_type",
            "networkType",
            "currentNetworkType",
            "accessTechnology",
            "access_technology",
            "access-tech",
            "access_tech",
            "rat",
            "RAT",
            "mode",
            "workmode",
            "network-mode",
            "network_mode",
            "networkband",
            "network_type_ex",
        ),
        "ca_state_text": (
            "ca_state_text",
            "CA state",
            "ca_state",
            "carrierAggregationState",
            "carrier_aggregation_state",
        ),
        "ca_active": (
            "ca_active",
            "CA active",
            "carrierAggregation",
            "carrier_aggregation",
            "ca_state",
            "scc_state",
            "scell",
            "scc",
            "lte_ca_pcell",
            "lte_ca_scell",
            "secondary_cell",
        ),
        "nr_mode": (
            "nr_mode",
            "nr5g_mode",
            "nrMode",
            "nr_mode_text",
            "5g_mode",
            "nr_rat_mode",
        ),
        "nr_state_text": (
            "nr_state_text",
            "nr5g_state_text",
            "nr_state",
            "nrStatus",
            "nr_status",
            "5g_state",
            "5g_status",
        ),
        "connection_status_code": (
            "connection_status_code",
            "ConnectionStatus",
            "connectionStatus",
            "connection_status",
            "packet_session_status",
            "packetSessionStatus",
            "ppp_status",
            "pppStatus",
            "session_status",
        ),
        "rrc": ("rrc", "RRC", "rrc_state", "rrc-state", "rrc_status"),
        "trans_mode": (
            "trans_mode",
            "transmode",
            "transmission_mode",
            "transmission-mode",
            "lte_trans_mode",
            "lte_transmode",
        ),
    }
)


TRANSMISSION_MODE_LABELS: Mapping[str, str] = {
    "1": "SISO",
    "2": "TX Diversity",
    "3": "OL-MIMO",
    "4": "CL-MIMO",
    "7": "Beamforming",
}


@dataclass(frozen=True, slots=True)
class CanonicalAliasSpec:
    line: str
    field: str
    aliases: tuple[str, ...]
    normalize: Normalizer
    unit: str = ""
    reason: str = ""

    @property
    def path(self) -> str:
        return f"{self.line}.{self.field}"


@dataclass(frozen=True, slots=True)
class ModemLiveTranslationResult:
    """One translation result for the Matrix writer."""

    lines: Mapping[str, Mapping[str, Any]]
    unknown_alias_evidence: Mapping[str, Any]


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _finite_float(value: Any) -> float | None:
    try:
        out = float(str(value).strip().replace("dB", "").replace("db", ""))
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _normalize_positive_int(value: Any) -> int | None:
    if _empty(value):
        return None
    try:
        number = int(str(value).strip(), 0)
    except Exception:
        return None
    return number if number > 0 else None


def _normalize_nonnegative_int(value: Any) -> int | None:
    if _empty(value):
        return None
    try:
        number = int(str(value).strip(), 0)
    except Exception:
        return None
    return number if number >= 0 else None


def _normalize_cell_list(value: Any) -> list[Any] | None:
    if not isinstance(value, (list, tuple)):
        return None
    return list(value)


def _normalize_rsrq_db(value: Any) -> float | None:
    number = _finite_float(value)
    if number is None:
        return None
    # RSRQ is normally negative dB. Some firmware reports dB times ten.
    if number <= -40.0:
        number = number / 10.0
    # Zero or positive LTE RSRQ is invalid here.
    if not (-40.0 < number < 0.0):
        return None
    return round(number, 1)


def _normalize_tac(value: Any) -> int | str | None:
    if _empty(value):
        return None
    text = str(value).strip()
    try:
        number = int(text, 0)
    except Exception:
        number = None
    if number is not None:
        return number if number > 0 else None
    compact = text.replace(" ", "")
    if not compact or compact in {"0", "0000", "-", "--", "---"}:
        return None
    return text


def _normalize_wan_ip(value: Any) -> str | None:
    if _empty(value):
        return None
    text = str(value).strip()
    if not text or text in {"-", "--", "---", "0.0.0.0"}:
        return None
    # RouterOS and Huawei may append the prefix length.
    bare = text.split("/", 1)[0].strip()
    try:
        ipaddress.ip_address(bare)
    except Exception:
        return None
    return bare


def _normalize_bandwidth_mhz(value: Any) -> float | None:
    if _empty(value):
        return None
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(value))
    if match is None:
        return None
    number = _finite_float(match.group(1))
    if number is None or number <= 0.0:
        return None
    return float(number)


def _normalize_network_type(value: Any) -> str | None:
    if _empty(value):
        return None
    raw = str(value).strip()
    text = raw.lower().replace("_", " ").replace("-", " ")
    compact = _normalize_key(raw)
    if compact in {"0", "noservice", "disconnected", "offline", "notregistered"}:
        return "No Service"
    if compact in {"1011", "lteca", "4gplus", "lteadvanced", "lteadvancedpro"}:
        return "LTE (4G)"
    if compact in {"endc", "nsa", "nrnsa", "5gnsa", "4g5g", "lte5g", "5gnsa4g5g"}:
        return "NR NSA (5G)"
    if compact in {"nrsa", "5gsa"}:
        return "NR SA (5G)"
    if compact in {"7", "101", "lte", "4g", "eutran"} or "lte" in text or "4g" in text:
        return "LTE (4G)"
    if "nr" in text or "5g" in text:
        if "sa" in text and "nsa" not in text:
            return "NR SA (5G)"
        return "NR NSA (5G)"
    return None


def _normalize_mobile_data_enabled(value: Any) -> bool | None:
    if value is not None and type(value) is not bool:
        raise TypeError("mobile_data_configuration_bool_required")
    return value


def _normalize_radio_mode(value: Any):
    from arch4.core_contracts.command_targets import validate_radio_mode_observation
    return validate_radio_mode_observation(value) if value is not None else None


def _normalize_ca_active(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if _empty(value):
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "active", "enabled", "ca", "lte ca"}:
        return True
    if text in {"0", "false", "no", "off", "inactive", "disabled", "single", "single band"}:
        return False
    if "scell" in text or "scc" in text or "secondary" in text:
        return True
    if "ca" in text and "inactive" not in text and "disabled" not in text:
        return True
    return None


def _normalize_ca_state_text(value: Any) -> str | None:
    active = _normalize_ca_active(value)
    if active is True:
        return "ACTIVE"
    if active is False:
        return "INACTIVE"
    if _empty(value):
        return None
    text = str(value).strip().lower().replace("_", " ").replace("-", " ")
    if text in {"pending", "unknown", "unproven"}:
        return "PENDING"
    return None


def _normalize_radio_state_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return "Connected" if value else "Disconnected"
    if _empty(value):
        return None
    text = str(value).strip()
    compact = _normalize_key(text)
    if compact in {
        "1",
        "true",
        "connected",
        "connect",
        "registered",
        "online",
        "up",
        "ipv4connected",
        "pppconnected",
    }:
        return "Connected"
    if compact in {
        "0",
        "false",
        "disconnected",
        "disconnect",
        "notconnected",
        "notregistered",
        "noservice",
        "offline",
        "down",
        "wandown",
        "pppdisconnected",
    }:
        return "Disconnected"
    if "wandown" in compact or ("registered" in compact and "down" in compact):
        return "Disconnected"
    if compact in {"reconnecting", "authsessionlost", "sessionlost", "refreshing"}:
        return "Reconnecting"
    if "reconnecting" in compact or "sessionlost" in compact:
        return "Reconnecting"
    if compact in {"unavailable", "unknown", "telemetryunavailable"}:
        return "Unavailable"
    return None


def _normalize_nr_mode(value: Any) -> str | None:
    if _empty(value):
        return None
    compact = _normalize_key(value)
    if compact in {"nsa", "nrnsa", "5gnsa", "endc", "4g5g", "lte5g"}:
        return "NSA"
    if compact in {"sa", "nrsa", "5gsa"}:
        return "SA"
    if compact in {"inactive", "unavailable", "none", "noservice"}:
        return "Unavailable"
    return None


def _normalize_nr_state_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return "ACTIVE" if value else "INACTIVE"
    if _empty(value):
        return None
    compact = _normalize_key(value)
    if compact in {"active", "nractive", "5gactive", "5gnsaactive", "5gsaactive"}:
        return "ACTIVE"
    if compact in {"available", "nravailable", "5gavailable", "5gnsaavailable", "5gsaavailable"}:
        return "AVAILABLE"
    if compact in {"inactive", "disabled", "off", "idle"}:
        return "INACTIVE"
    if compact in {"unavailable", "unknown", "none", "noservice"}:
        return "UNAVAILABLE"
    return None


def _normalize_rrc(value: Any) -> str | None:
    if _empty(value):
        return None
    text = str(value).strip()
    compact = _normalize_key(text)
    if compact in {"connected", "connect", "rrcconnected", "registered"}:
        return "Connected"
    if compact in {"idle", "noconn", "noconnection", "rrcidle"}:
        return "Idle"
    return None


def _normalize_trans_mode(value: Any) -> str | None:
    if _empty(value):
        return None
    text = str(value).strip()
    match = re.search(r"(?:TM\s*)?\[?\s*([1-9])\s*\]?", text, flags=re.IGNORECASE)
    if not match:
        return None
    num = match.group(1)
    base = f"TM[{num}]"
    label = TRANSMISSION_MODE_LABELS.get(num, "")
    return f"{base} ({label})" if label else base


def _normalize_connection_status_code(value: Any) -> str | None:
    if _empty(value):
        return None
    text = str(value).strip()
    compact = _normalize_key(text)
    if re.fullmatch(r"[0-9]{3,8}", text):
        return text
    if compact in {
        "connected",
        "connect",
        "online",
        "up",
        "pppconnected",
        "packetconnected",
        "sessionconnected",
    }:
        return "901"
    if compact in {
        "disconnected",
        "disconnect",
        "offline",
        "down",
        "pppdisconnected",
        "packetdisconnected",
        "sessiondisconnected",
        "noservice",
        "notregistered",
    }:
        return "902"
    return None


CANONICAL_ALIAS_SPECS: tuple[CanonicalAliasSpec, ...] = (
    CanonicalAliasSpec(
        "rf",
        "earfcn_dl",
        CANONICAL_LIVE_FIELD_ALIASES["earfcn_dl"],
        _normalize_positive_int,
        reason="lte_earfcn_alias",
    ),
    CanonicalAliasSpec(
        "radio",
        "nr_band",
        CANONICAL_LIVE_FIELD_ALIASES["nr_band"],
        _normalize_positive_int,
        reason="nr_band_alias",
    ),
    CanonicalAliasSpec(
        "radio",
        "nr_pci",
        CANONICAL_LIVE_FIELD_ALIASES["nr_pci"],
        _normalize_nonnegative_int,
        reason="nr_pci_alias",
    ),
    CanonicalAliasSpec(
        "radio",
        "nr_arfcn",
        CANONICAL_LIVE_FIELD_ALIASES["nr_arfcn"],
        _normalize_nonnegative_int,
        reason="nr_arfcn_alias",
    ),
    CanonicalAliasSpec(
        "radio",
        "nr_cell_id",
        CANONICAL_LIVE_FIELD_ALIASES["nr_cell_id"],
        _normalize_nonnegative_int,
        reason="nr_cell_id_alias",
    ),
    CanonicalAliasSpec(
        "slow",
        "nr_cells",
        CANONICAL_LIVE_FIELD_ALIASES["nr_cells"],
        _normalize_cell_list,
        reason="nr_cells_alias",
    ),
    CanonicalAliasSpec(
        "slow",
        "nr_bandwidth_mhz",
        CANONICAL_LIVE_FIELD_ALIASES["nr_bandwidth_mhz"],
        _normalize_bandwidth_mhz,
        unit="MHz",
        reason="nr_bandwidth_alias",
    ),
    CanonicalAliasSpec(
        "rf",
        "rsrq_db",
        CANONICAL_LIVE_FIELD_ALIASES["rsrq_db"],
        _normalize_rsrq_db,
        unit="dB",
        reason="lte_rsrq_alias",
    ),
    CanonicalAliasSpec(
        "slow",
        "tac",
        CANONICAL_LIVE_FIELD_ALIASES["tac"],
        _normalize_tac,
        reason="lte_tac_alias",
    ),
    CanonicalAliasSpec(
        "slow",
        "dl_bandwidth_mhz",
        CANONICAL_LIVE_FIELD_ALIASES["dl_bandwidth_mhz"],
        _normalize_bandwidth_mhz,
        unit="MHz",
        reason="lte_dl_bandwidth_alias",
    ),
    CanonicalAliasSpec(
        "slow",
        "ul_bandwidth_mhz",
        CANONICAL_LIVE_FIELD_ALIASES["ul_bandwidth_mhz"],
        _normalize_bandwidth_mhz,
        unit="MHz",
        reason="lte_ul_bandwidth_alias",
    ),
    CanonicalAliasSpec("slow", "mobile_data_enabled", ("mobile_data_enabled",), _normalize_mobile_data_enabled, reason="mobile_data_configuration"),
    CanonicalAliasSpec("slow", "radio_mode", ("radio_mode",), _normalize_radio_mode, reason="qualified_radio_mode_configuration"),
    CanonicalAliasSpec("slow", "network_settings", ("network_settings",), validate_network_observation, reason="network_settings_contract"),
    CanonicalAliasSpec(
        "slow",
        "wan_ip",
        CANONICAL_LIVE_FIELD_ALIASES["wan_ip"],
        _normalize_wan_ip,
        reason="wan_ip_alias",
    ),
    CanonicalAliasSpec(
        "radio",
        "network_type",
        CANONICAL_LIVE_FIELD_ALIASES["network_type"],
        _normalize_network_type,
        reason="rat_alias",
    ),
    CanonicalAliasSpec(
        "radio",
        "ca_state_text",
        CANONICAL_LIVE_FIELD_ALIASES["ca_state_text"],
        _normalize_ca_state_text,
        reason="ca_state_text_alias",
    ),
    CanonicalAliasSpec(
        "radio",
        "ca_active",
        CANONICAL_LIVE_FIELD_ALIASES["ca_active"],
        _normalize_ca_active,
        reason="ca_active_alias",
    ),
    CanonicalAliasSpec(
        "radio",
        "nr_mode",
        CANONICAL_LIVE_FIELD_ALIASES["nr_mode"],
        _normalize_nr_mode,
        reason="nr_mode_alias",
    ),
    CanonicalAliasSpec(
        "radio",
        "nr_state_text",
        CANONICAL_LIVE_FIELD_ALIASES["nr_state_text"],
        _normalize_nr_state_text,
        reason="nr_state_alias",
    ),
    CanonicalAliasSpec(
        "traffic",
        "connection_status_code",
        CANONICAL_LIVE_FIELD_ALIASES["connection_status_code"],
        _normalize_connection_status_code,
        reason="connection_status_alias",
    ),
    CanonicalAliasSpec(
        "slow",
        "rrc",
        CANONICAL_LIVE_FIELD_ALIASES["rrc"],
        _normalize_rrc,
        reason="rrc_alias",
    ),
    CanonicalAliasSpec(
        "health",
        "radio_state_text",
        CANONICAL_LIVE_FIELD_ALIASES["radio_state_text"],
        _normalize_radio_state_text,
        reason="health_radio_state_alias",
    ),
    CanonicalAliasSpec(
        "slow",
        "trans_mode",
        CANONICAL_LIVE_FIELD_ALIASES["trans_mode"],
        _normalize_trans_mode,
        reason="transmission_mode_alias",
    ),
)


CANONICAL_ALIAS_BY_LINE_FIELD: dict[tuple[str, str], CanonicalAliasSpec] = {
    (spec.line, spec.field): spec for spec in CANONICAL_ALIAS_SPECS
}
RAW_VENDOR_ALIAS_NAMES: frozenset[str] = frozenset(
    alias
    for aliases in CANONICAL_LIVE_FIELD_ALIASES.values()
    for alias in aliases
)
TRANSLATED_RAW_ALIAS_NAMES: frozenset[str] = frozenset(
    alias
    for spec in CANONICAL_ALIAS_SPECS
    for alias in (spec.field, *spec.aliases)
)
RAW_VENDOR_ALIAS_KEYS: frozenset[str] = frozenset(
    _normalize_key(alias) for alias in RAW_VENDOR_ALIAS_NAMES
)
CANONICAL_MODEM_LIVE_NON_MATRIX_STATUS_KEYS: frozenset[str] = frozenset(
    {
        "backend_family",
        "identity_scope_lock",
        "iface",
        "iface_id",
        "modem_live_scout",
        "netmode_list_supported_bands",
        "netmode_list_supported_nr_bands",
        "previous_device_identity_key",
        "previous_session_scope_key",
        "profile_name",
        "session_band_capability_issue",
        "session_changed",
        "session_scope_key",
        "sim_id",
        "target",
        "runtime_session_scope",
        "selected_profile_record",
        "verified_modem_identity",
    }
)
CANONICAL_MODEM_LIVE_MATRIX_SOURCE_KEYS: frozenset[str] = frozenset(
    field
    for fields in MODEM_LIVE_TRUTH_FIELD_CATALOG.values()
    for field in fields
)
DECLARED_MODEM_LIVE_SOURCE_KEYS: frozenset[str] = frozenset(
    _normalize_key(name)
    for name in (
        set(PUBLIC_MODEM_STATUS_KEYS)
        | set(TRANSLATED_RAW_ALIAS_NAMES)
        | set(CANONICAL_MODEM_LIVE_MATRIX_SOURCE_KEYS)
        | set(CANONICAL_MODEM_LIVE_NON_MATRIX_STATUS_KEYS)
    )
)
CANONICAL_ALIAS_SUSPICIOUS_TOKENS: frozenset[str] = frozenset(
    {
        "rsrq",
        "tac",
        "tracking",
        "wan",
        "ip",
        "address",
        "external",
        "network",
        "type",
        "access",
        "technology",
        "rat",
        "carrier",
        "aggregation",
        "scell",
        "scc",
        "connection",
        "status",
        "ppp",
    }
)


def _field_tokens(value: Any) -> frozenset[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    return frozenset(
        token
        for token in re.split(r"[^A-Za-z0-9]+", text.lower())
        if token
    )


def _mapping_sources(
    modem: Any,
    status: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    modem_map: dict[str, Any] = {}
    if modem is not None:
        try:
            modem_map.update(vars(modem))
        except Exception:
            for spec in CANONICAL_ALIAS_SPECS:
                for name in (spec.field, *spec.aliases):
                    if name.isidentifier() and hasattr(modem, name):
                        modem_map[name] = getattr(modem, name)
    return modem_map, dict(status or {})


def _lookup_alias(
    *,
    spec: CanonicalAliasSpec,
    modem_map: Mapping[str, Any],
    status: Mapping[str, Any],
) -> tuple[tuple[str, Any] | None, dict[str, Any] | None]:
    for source_name, source in (("modem", modem_map), ("status", status)):
        alias_keys = frozenset(
            _normalize_key(name)
            for name in (spec.field, *spec.aliases)
        )
        candidates = [
            (str(key), value, spec.normalize(value))
            for key, value in source.items()
            if (
                _normalize_key(key) in alias_keys
                and not _empty(value)
            )
        ]
        candidates = [
            item for item in candidates if item[2] is not None
        ]
        if not candidates:
            continue
        normalized_values = {
            repr(item[2]) for item in candidates
        }
        if len(normalized_values) > 1:
            return None, {
                "canonical_path": spec.path,
                "source": source_name,
                "raw_keys": sorted(item[0] for item in candidates),
                "normalized_values": sorted(normalized_values),
                "disposition": "quarantined",
            }
        selected = candidates[0]
        return (selected[0], selected[1]), None
    return None, None


def _normalize_line_payload(
    line: str,
    payload: Mapping[str, Any],
    *,
    modem: Any,
    status: Mapping[str, Any],
    conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Map known aliases and return one normalized line.

    Invalid sentinel values are removed. Matrix then publishes a missing cell
    instead of a false physical value.
    """

    line_name = str(line or "").strip()
    out = dict(payload or {})
    modem_map, status_map = _mapping_sources(modem, status)
    for (spec_line, field), spec in CANONICAL_ALIAS_BY_LINE_FIELD.items():
        if spec_line != line_name:
            continue
        found, conflict = _lookup_alias(
            spec=spec,
            modem_map=modem_map,
            status=status_map,
        )
        if conflict is not None:
            conflicts.append(conflict)
            out.pop(field, None)
            continue
        if field in out:
            current = out.get(field)
            normalized_current = (
                spec.normalize(current) if not _empty(current) else None
            )
            if normalized_current is not None:
                out[field] = normalized_current
            else:
                out.pop(field, None)
            # Source priority is already decided. If the selected value is
            # invalid, report it missing. Do not use a lower-priority source.
            continue
        if found is None:
            continue
        _alias, value = found
        normalized = spec.normalize(value)
        if normalized is not None:
            out[field] = normalized
    return out


def translate_matrix_payloads(
    payloads: Mapping[str, Mapping[str, Any]],
    *,
    modem: Any,
    status: Mapping[str, Any],
    backend: str,
    profile_id: str,
    firmware: str,
    webui: str,
    access_type: str,
    source: str,
) -> ModemLiveTranslationResult:
    """Translate all Matrix lines once and return bounded evidence."""

    conflicts: list[dict[str, Any]] = []
    translated = {
        str(line): MappingProxyType(
            _normalize_line_payload(
                str(line),
                payload,
                modem=modem,
                status=status,
                conflicts=conflicts,
            )
        )
        for line, payload in payloads.items()
    }
    _modem_map, status_map = _mapping_sources(modem, status)
    evidence = _collect_unknown_alias_evidence(
        backend=backend,
        profile_id=profile_id,
        firmware=firmware,
        webui=webui,
        access_type=access_type,
        source=source,
        status=status_map,
    )
    if conflicts:
        evidence = dict(evidence)
        evidence.setdefault(
            "schema",
            "ModemLiveScoutUnknownTokenReceipt.v1",
        )
        evidence.setdefault("owner", OWNER)
        evidence.setdefault("consumer", "54guc.modem_live.scout")
        evidence.setdefault("disposition", "investigation_pending")
        evidence["same_source_conflicts"] = conflicts[
            :MAX_UNKNOWN_ALIAS_EVIDENCE
        ]
        evidence["bounded"] = True
        evidence["limit"] = MAX_UNKNOWN_ALIAS_EVIDENCE
    return ModemLiveTranslationResult(
        lines=MappingProxyType(translated),
        unknown_alias_evidence=MappingProxyType(dict(evidence)),
    )


def _collect_unknown_alias_evidence(
    *,
    backend: str,
    profile_id: str,
    firmware: str,
    webui: str,
    access_type: str,
    source: str,
    status: Mapping[str, Any],
) -> dict[str, Any]:
    """Return bounded evidence for unknown backend-status names.

    ``ModemInput`` is already normalized. Inspecting it would make Scout treat
    internal fields as vendor vocabulary. Only backend public status is checked
    here. Matrix data is not interpreted again.
    """

    unknown: list[dict[str, str]] = []
    for source_name, source_map in (("backend_status", status),):
        for key, value in sorted(
            dict(source_map or {}).items(),
            key=lambda item: str(item[0]),
        ):
            key_text = str(key or "").strip()
            norm = _normalize_key(key_text)
            if not norm or norm in DECLARED_MODEM_LIVE_SOURCE_KEYS:
                continue
            unknown.append(
                {
                    "source": source_name,
                    "raw_key": key_text,
                    "normalized_key": norm,
                    "value_type": type(value).__name__,
                }
            )
            if len(unknown) >= MAX_UNKNOWN_ALIAS_EVIDENCE:
                break
        if len(unknown) >= MAX_UNKNOWN_ALIAS_EVIDENCE:
            break
    if not unknown:
        return {}
    return {
        "schema": "ModemLiveScoutUnknownTokenReceipt.v1",
        "owner": OWNER,
        "consumer": "54guc.modem_live.scout",
        "disposition": "investigation_pending",
        "backend": str(backend or ""),
        "profile_id": str(profile_id or ""),
        "firmware": str(firmware or ""),
        "webui": str(webui or ""),
        "access_type": str(access_type or ""),
        "source": str(source or ""),
        "unknown_aliases": unknown,
        "bounded": True,
        "limit": MAX_UNKNOWN_ALIAS_EVIDENCE,
    }
