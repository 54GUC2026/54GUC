"""
arch4/privacy_scrub.py

Share-safe scrubber for tester support bundles and exported logs.

Masks sensitive fields while preserving engineering-critical data so
54GUC support can analyse performance, events, and RF quality without exposure
to user-identifiable information.

Must scrub / mask:
  - IMEI
  - IMSI
  - WAN IP / public IP
  - usernames / passwords / tokens / cookies / API keys
  - SSIDs (if present)
  - TR-069 credentials

Must preserve:
  - modem model / family
  - firmware / config version
  - field truth / provenance
  - RF and performance metrics (SINR, RSRP, RSRQ, etc.)
  - event chronology (timestamps, kinds, sequences)

Usage:
    from arch4.privacy_scrub import scrub_record, scrub_text, SCRUB_MASK

    clean = scrub_record({"imei": "123456789012345", "sinr_db": -3.2})
    # {"imei": "***SCRUBBED***", "sinr_db": -3.2}
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

import csv
import gzip
import hashlib
import io
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from xml.etree import ElementTree as ET

# The canonical mask token — short, grep-able, clearly intentional.
SCRUB_MASK = "***SCRUBBED***"

_SUBSCRIPTION_SCOPE_RE = re.compile(r"(?<![\w])sim=([a-f0-9]{64})(?![a-f0-9])")


def _scrub_subscription_scope(text: str) -> str:
    """Keep exported SIM scopes distinct without retaining their private token."""
    if "sim=" not in text:
        return text
    return _SUBSCRIPTION_SCOPE_RE.sub(
        lambda match: "sim=redacted:" + hashlib.sha256(
            ("54guc.support.subscription:" + match.group(1)).encode("utf-8")
        ).hexdigest(), text)

# ── Sensitive field keys (exact match, case-insensitive) ─────────────────────
# A field whose key matches any of these will have its VALUE replaced.
_SENSITIVE_KEYS: frozenset[str] = frozenset({
    # Device identity
    "imei",
    "imsi",
    "sim_imsi",
    "sim_iccid",
    "msisdn",
    "sim_msisdn",
    "iccid",
    "sim_id",
    "serial",
    "serial_number",
    "x_tp_serial_number",
    "x_tp_cpe_id_value",
    "router_serial",
    "modem_msn",
    "msn",
    "modem_imei",
    "lan_mac",
    "mac",
    "mac_address",
    "radio_name",  # RouterOS defaults this user label to a compact Wi-Fi MAC.
    "macaddress",
    "x_tp_macaddress",
    "x_tp_mac_address",
    "mac_address1",
    "lan_mac_address",
    "wifi_mac_addr_wl0",
    "wifi_mac_addr_wl1",
    # Network identity
    "wan_ip",
    "wan_ipv6",
    "wan_ipaddr",
    "wan_ipaddress",
    "wwan_ip",
    "ipaddr",
    "ipv4_address",
    "ipv6_address",
    "public_ip",
    "external_ip",
    "host",
    "hostname",
    "endpoint",
    "base_url",
    "router_host",
    "modem_host",
    # WiFi
    "ssid",
    "bssid",
    "wifi_ssid",
    "wifi_key",
    "wifi_password",
    "key_factory",
    "device_encrypt_key_test",
    # Auth
    "protected_credentials",
    "subscription_key",
    "learning_subscription_key",
    "password",
    "passwd",
    "router_user",
    "router_password",
    "b818_user",
    "b818_password",
    "b535_user",
    "b535_password",
    "zte_user",
    "zte_password",
    "zyxel_user",
    "zyxel_password",
    "openwrt_user",
    "openwrt_password",
    "tplink_user",
    "tplink_password",
    "xiaomi_user",
    "xiaomi_password",
    "username",
    "user",
    "api_key",
    "api_token",
    "token",
    "auth_token",
    "access_token",
    "session_token",
    "cookie",
    "cookies",
    "authorization",
    # Modem wire trace: challenge/session/verification material is never retained.
    "ubus_rpc_session", "sid", "session_id", "ct", "zte_web_sault", "zte_web_salt", "web_enstr",
    "stok", "sessionid", "session_id", "sesinfo", "tokinfo", "ses_info", "tok_info", "salt", "nonce", "challenge",
    "client_proof", "server_proof", "servernonce", "server_nonce", "clientnonce", "client_nonce",
    "serversignature", "server_signature",
    "password_hash", "clientproof", "serverproof", "rsan", "rsae", "ad", "rd", "ld",
    "requestverificationtoken", "requestverificationtokenone", "csrf_token",
    "request_verification_token", "request_verification_tokenone",
    "set_cookie", "sn", "sn_num", "psk", "passphrase",
    "sessionkey", "session_key", "input_account", "input_passwd", "firstnonce", "first_nonce",
    "finalnonce", "final_nonce",
    # License / operator / customer context
    "license",
    "license_key",
    "license_nonce",
    "license_email",
    "email",
    "email_address",
    "operator_name",
    "customer",
    "customer_id",
    "customer_name",
    "user_name",
    "site",
    "site_id",
    "site_name",
    "site_address",
    "site_location",
    "site_notes",
    "install_address",
    "address",
    "address_line1",
    "address_line2",
    "city",
    "contact",
    "contact_name",
    "contact_phone",
    "contact_email",
    "phone",
    "phone_number",
    "sender_number",
    "approved_senders",
    "sms_command_approved_senders",
    # TR-069
    "tr069_username",
    "tr069_password",
    "tr069_acsurl",
    "acs_username",
    "acs_password",
})

# These mappings use modem field names as metadata keys.  Their values are
# public provenance classifications, not values of the named modem fields.
_FIELD_NAME_METADATA_MAP_KEYS: frozenset[str] = frozenset({"field_trust"})

# Provider configuration is public in support reports (operator decision,
# 2026-09-26). This exception belongs only to the typed APN boundary, never
# generic usernames/passwords or router/session/SIM credentials.
_PUBLIC_APN_FIELDS: frozenset[str] = frozenset({
    "profile_id", "name", "apn", "username", "password", "authentication",
    "ip_protocol", "editable",
})

_CORE_APN_SERIALIZED_FIELDS: frozenset[str] = frozenset({
    "profile_id", "name", "apn", "authentication", "ip_protocol", "username",
    "protected_credentials",
})
_RECONNECT_PRESET_FIELDS: frozenset[str] = frozenset({
    "profile_id", "router_serial", "firmware", "subscription_key", "sim_country",
    "roaming_enabled", "selection", "network_plmn", "network_country", "apn",
    "saved_profile_id", "password_known",
})
_RECONNECT_APN_FIELDS: frozenset[str] = frozenset({
    "name", "apn", "username", "authentication", "ip_protocol",
})


def _public_apn_children(record: Mapping[str, Any]) -> dict[str, str]:
    """One public-APN policy for observed, saved and rendered typed boundaries."""
    if record.get("schema") == "NetworkSettingsObservation.v1" and isinstance(record.get("apn"), Mapping):
        return {"apn": "observation"}
    if (record.get("schema") == "FoundationOperatorSettings.v3"
            and record.get("owner") == "arch4.foundation.operator_settings"
            and isinstance(record.get("apn_profiles"), Mapping)):
        return {"apn_profiles": "saved"}
    if record.get("schema") == "ApnProfileLibrary.v1" and isinstance(record.get("profiles"), (list, tuple)):
        return {"profiles": "library"}
    # CoreApnSettings has no schema field in its serialized mapping. This
    # permanently defined structural adapter covers the same typed value in
    # targets, transaction params, reconnect choices and History receipts.
    # Never classify a generic username/password by its spelling or value.
    apn = record.get("apn")
    # NGUINetworkPresetStore projects a five-field public APN beneath an exact
    # scoped preset. Its serial/subscription remain private siblings.
    if (set(record) == _RECONNECT_PRESET_FIELDS and isinstance(apn, Mapping)
            and set(apn) == _RECONNECT_APN_FIELDS
            and all(type(child) is str for child in apn.values())
            and type(record["password_known"]) is bool
            and (record["roaming_enabled"] is None or type(record["roaming_enabled"]) is bool)):
        return {"apn": "preset"}
    if (isinstance(apn, Mapping) and set(apn) == _CORE_APN_SERIALIZED_FIELDS
            and all(type(apn[key]) is str for key in _CORE_APN_SERIALIZED_FIELDS)
            and apn["authentication"] in {"none", "pap", "chap", "auto"}
            and apn["ip_protocol"] in {"ipv4", "ipv6", "ipv4v6"}
            and (apn["protected_credentials"] == SCRUB_MASK
                 or apn["protected_credentials"].startswith("dpapi:v1:"))):
        return {"apn": "command"}
    return {}


def _project_apn_child(value: Any, shape: str, *, project: bool,
                       secrets: set[str] | None) -> Any:
    def project_row(row: Any, *, observation: bool = False) -> Any:
        if not isinstance(row, Mapping):
            return _privacy_walk(row, project=project, secrets=secrets)
        profile_fields = {"profile_id", "subscription_key", "network_plmn", "name", "apn", "username",
                          "authentication", "ip_protocol", "password_known"}
        valid = observation or (shape == "command" and set(row) == _CORE_APN_SERIALIZED_FIELDS) or (
            shape == "preset" and set(row) == _RECONNECT_APN_FIELDS) or set(row) in (
            profile_fields, profile_fields | {"protected_credentials"})
        public = {key: child for key, child in row.items()
                  if valid and key in _PUBLIC_APN_FIELDS and (child is None or type(child) in (str, bool))}
        private = {key: child for key, child in row.items() if key not in public}
        clean = _privacy_walk(private, project=project, secrets=secrets)
        return {**clean, **public} if project else None
    if shape in {"observation", "command", "preset"}:
        return project_row(value, observation=shape == "observation")
    if shape == "saved":
        if project:
            return {key: project_row(row) for key, row in value.items()}
        for row in value.values():
            project_row(row)
        return None
    if shape == "library":
        if project:
            return [project_row(row) for row in value]
        for row in value:
            project_row(row)
        return None
    raise ValueError("support_apn_policy_shape_illegal")

# ── Regex patterns for value scrubbing (applied to string values) ────────────
# These fire when the key is not sensitive but the value looks sensitive.

# IPv4 in WAN/public position (private ranges are safe)
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_MAC_RE = re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b")
_PHONE_RE = re.compile(r"(?<![A-Za-z0-9])\+\d{7,15}\b")

# IMEI: 15 digits (sometimes with hyphens)
_IMEI_RE = re.compile(r"\b\d{2}[-\s]?\d{6}[-\s]?\d{6}[-\s]?\d\b")

# IMSI: 14–15 digits starting with MCC+MNC (first 5-6 digits)
_IMSI_RE = re.compile(r"\b\d{14,15}\b")

# SIM ICCID: 18–22 digits
_ICCID_RE = re.compile(r"\b\d{18,22}\b")

_EMAIL_RE = re.compile(
    r"(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![A-Z0-9._%+-])"
)

_WINDOWS_USER_PATH_RE = re.compile(r"(?i)\b([A-Z]:\\+Users\\+)([^\\\s]+)")
_POSIX_USER_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])(/(?:home|Users)/)([^/\s]+)")

# Every value pattern below requires one of these characters. This fast
# negative check retains Unicode digit matching and never caches a value.
_STRING_SCRUB_TRIGGER_RE = re.compile(r"[\d@:=+\\/-]")

# Free-text key/value pairs commonly found in raw logs or support summaries.
# Derive names from the same authority as structured fields. Quoted dictionary
# keys and escaped quoted values occur in PageTruth presentation text.
_ASSIGNMENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)(?<![\w])['\"]?("
            + "|".join(re.escape(key).replace("_", r"[_ -]?")
                       for key in sorted(_SENSITIVE_KEYS, key=lambda key: (-len(key), key)))
            + r")[ '\"]*[:=]\s*"
            + r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;|}\]]+)'''
        ),
        SCRUB_MASK,
    ),
)

_PRESERVED_TEXT_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:firmware|config_version|software_version|hardware_version|webui|software|"
    r"profile_id|display_name)\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


_SUPPORT_TEXT_SUFFIXES: frozenset[str] = frozenset(
    {".json", ".jsonl", ".xml", ".csv", ".txt", ".log", ".md"}
)


@dataclass(frozen=True)
class SupportArtifactScrubResult:
    """One deny-by-default decision for a support-bundle source artifact.

    ``content`` is present only when the artifact may be staged.  An excluded
    result never carries the original bytes, which prevents a caller from
    silently turning an inspection failure into an uninspected raw copy.
    """

    content: bytes | None
    disposition: str
    source_kind: str
    source_size_bytes: int
    staged_size_bytes: int = 0
    staged_sha256: str = ""
    redaction_count: int = 0
    reason: str = ""

    @property
    def included(self) -> bool:
        return self.content is not None

    def manifest_record(self, bundle_path: str, *, origin: str = "source") -> Dict[str, Any]:
        return {
            "path": str(bundle_path or ""),
            "origin": str(origin or "source"),
            "included": self.included,
            "disposition": self.disposition,
            "source_kind": self.source_kind,
            "source_size_bytes": int(self.source_size_bytes),
            "staged_size_bytes": int(self.staged_size_bytes),
            "staged_sha256": str(self.staged_sha256 or ""),
            "redaction_count": int(self.redaction_count),
            "reason": str(self.reason or ""),
        }


@lru_cache(maxsize=4096)
def _normalized_field_key(key: str) -> str:
    """Bounded field-name metadata only; no payload/value memoization."""
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


@lru_cache(maxsize=4096)
def _is_standard_sensitive_key(key: str) -> bool:
    """Return True when a plain or canonical namespaced key is sensitive."""
    raw = str(key or "").strip()
    if "}" in raw:
        raw = raw.rsplit("}", 1)[-1]
    candidates = [raw]
    if "." in raw:
        candidates.append(raw.rsplit(".", 1)[-1])
    for candidate in candidates:
        candidate = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", candidate)
        normalized = _normalized_field_key(candidate)
        if normalized in _SENSITIVE_KEYS:
            return True
    return False


_NATIVE_PRIVATE_KEYS = ContextVar('native_private_field_keys', default=frozenset())
_OMIT_NATIVE_PAYLOAD = ContextVar('omit_native_private_payload', default=False)


@contextmanager
def private_field_alias_scope(keys=(), *, omit_native_payload=False):
    """Private metadata for one native operation; no process-wide mutations."""
    if len(keys)>512 or any(not isinstance(key,str)or len(key)>512 for key in keys):
        raise ValueError('native_private_key_plan_invalid')
    normalized=frozenset(_normalized_field_key(re.sub(r'([a-z0-9])([A-Z])',r'\1_\2',key))for key in keys)
    token=_NATIVE_PRIVATE_KEYS.set(normalized);opaque=_OMIT_NATIVE_PAYLOAD.set(bool(omit_native_payload))
    try:yield
    finally:
        _OMIT_NATIVE_PAYLOAD.reset(opaque);_NATIVE_PRIVATE_KEYS.reset(token)


def native_payload_redaction_required():
    return _OMIT_NATIVE_PAYLOAD.get()


def _is_sensitive_key(key: str) -> bool:
    if _is_standard_sensitive_key(key):return True
    aliases=_NATIVE_PRIVATE_KEYS.get()
    if not aliases:return False
    raw=str(key or '').rsplit('}',1)[-1]
    return any(_normalized_field_key(re.sub(r'([a-z0-9])([A-Z])',r'\1_\2',part))in aliases
               for part in(raw,raw.rsplit('.',1)[-1]))


def _scrub_string_value(value: str, *, key: str = "") -> str:
    """Scrub known sensitive patterns from a string value.

    Applied even when the key is not in _SENSITIVE_KEYS, to catch
    values that happen to contain IMEI, IMSI, or public IP strings.

    Exception: keys in _PRESERVE_FIELDS skip IP pattern matching because
    firmware/config version strings can look like IP addresses.
    """
    if not isinstance(value, str):
        return value
    # For keys we know are sensitive, replace the whole value
    if _is_sensitive_key(key):
        return SCRUB_MASK
    # RouterOS API object paths contain public schema names, not an address
    # assignment. Keep the typed operation only when the entire value is a
    # static API path plus method; payloads/arguments still use normal privacy.
    if (key == "operation" and len(value) <= 256
            and re.fullmatch(r"/(?:[a-z][a-z0-9-]*/)*[a-z][a-z0-9-]*:(?:get|call|set|add|remove)", value)):
        return value
    if not _STRING_SCRUB_TRIGGER_RE.search(value):
        return value
    # For engineering-safe keys, skip the public-IP pattern (firmware strings
    # like "11.0.1.1(H220SP1C11)" would be wrongly masked).
    key_lower = _normalized_field_key(str(key).rsplit(".", 1)[-1])
    skip_ip_pattern = key_lower in {
        "firmware", "config_version", "software_version", "hardware_version",
        "webui", "software", "profile_id", "display_name",
    }
    out = _scrub_subscription_scope(value)
    # These necessary delimiters reject impossible matches before expensive
    # regex scans. Values and privacy decisions are never memoized.
    if ":" in out or "=" in out:
        for pattern, replacement in _ASSIGNMENT_PATTERNS:
            out = pattern.sub(lambda match: f"{match.group(1)}={replacement}", out)
    if len(out) >= 14:
        out = _IMEI_RE.sub(SCRUB_MASK, out)
        out = _ICCID_RE.sub(SCRUB_MASK, out)
        out = _IMSI_RE.sub(SCRUB_MASK, out)
    if "@" in out:
        out = _EMAIL_RE.sub(SCRUB_MASK, out)
    if len(out) >= 17 and (":" in out or "-" in out):
        out = _MAC_RE.sub(SCRUB_MASK, out)
    if "+" in out:
        out = _PHONE_RE.sub(SCRUB_MASK, out)
    if ":\\" in out:
        out = _WINDOWS_USER_PATH_RE.sub(lambda match: f"{match.group(1)}{SCRUB_MASK}", out)
    if "/home/" in out or "/Users/" in out:
        out = _POSIX_USER_PATH_RE.sub(lambda match: f"{match.group(1)}{SCRUB_MASK}", out)
    if not skip_ip_pattern and "." in out:
        out = _IPV4_RE.sub(SCRUB_MASK, out)
    return out


def _public_support_tier_child(key: Any, value: Any) -> Any:
    """Permanent export adapter for the runtime's public support classification.

    Authentication tokens elsewhere retain their ordinary secret semantics.
    The same adapter is used by scrubbing and final-archive secret discovery.
    """
    if key != "support_tier" or not isinstance(value, Mapping):
        return value
    from arch4.runtime_support_tier import _TIER_TOKENS, support_tier_label
    tier = value.get("tier")
    if (type(tier) is int and tier in _TIER_TOKENS
            and value.get("token") == _TIER_TOKENS[tier]
            and value.get("label") == support_tier_label(tier)
            and "classification" not in value):
        result = dict(value)
        result["classification"] = result.pop("token")
        return result
    return value


def _public_explore_support_record(value: Mapping) -> Mapping:
    """Permanent export adapter: Explore IDs are product requests, not logins."""
    schema = value.get("schema")
    container = ""
    if (schema == "NewHorizonExploreEvidence.v1"
            and all(isinstance(value.get(k), str) for k in ("phase", "reason"))):
        target = value
    elif (schema == "NewHorizonPerformanceEvidence.v2"
            and isinstance(value.get("record_type"), str)
            and value.get("record_type") in {"explore_probe", "explore_best_of_three"}
            and isinstance(value.get("details"), Mapping)):
        container = "details"
        target = value["details"]
    elif (schema == "OperationalServiceReceipt.v2"
            and value.get("owner") == "54guc.product.intelligence_matrix"
            and value.get("kind") == "set_intelligence_explore"
            and isinstance(value.get("detail"), Mapping)):
        container = "detail"
        target = value["detail"]
    else:
        return value
    if "session_id" not in target:
        return value
    if "explore_session_id" in target:
        raise ValueError("support_explore_session_alias_collision")
    if not isinstance(target["session_id"], str):
        return value
    projected = dict(target)
    projected["explore_session_id"] = projected.pop("session_id")
    return {**value, container: projected} if container else projected


def scrub_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a recursively scrubbed copy of a mapping record.

    - Keys in _SENSITIVE_KEYS: value replaced with SCRUB_MASK
    - String values with embedded IMEI/IMSI/public-IP: patterns replaced
    - Nested dicts and lists are recursively scrubbed
    - All other fields (RF metrics, timestamps, band info) pass through

    Never modifies the input in-place.
    """
    if not isinstance(record, Mapping):
        return record
    return _privacy_walk(record, project=True, secrets=None)


def scrub_record_with_secrets(record: Mapping[str, Any]) -> tuple[Dict[str, Any], tuple[str, ...]]:
    """One privacy traversal returning export payload and verification receipt.

    Known sensitive values remain transient in memory. Public APN policy,
    provenance and masking are identical to the individual API adapters.
    """
    if not isinstance(record, Mapping):
        raise TypeError("support_privacy_record_mapping_required")
    secrets: set[str] = set()
    clean = _privacy_walk(record, project=True, secrets=secrets)
    return clean, tuple(sorted(secrets))


def _remember_sensitive_values(value: Any, secrets: set[str] | None) -> None:
    if secrets is None:
        return
    values = value if isinstance(value, (list, tuple, set, frozenset)) else (value,)
    for child in values:
        if isinstance(child, (str, Path)) or type(child) is int:
            candidate = str(child)
            if len(candidate) >= 6 and candidate != SCRUB_MASK:
                secrets.add(candidate)


def _privacy_walk(value: Any, *, project: bool, secrets: set[str] | None,
                  key: str = "", masked_fields: frozenset[str] = frozenset()) -> Any:
    """Only recursive structured privacy owner, with explicit output adapters."""
    # Ordinary JSON numeric/null leaves need no container, path or dataclass
    # inspection. Sensitive-key masking is decided by the containing mapping.
    if value is None or type(value) in (int, float, bool):
        return value if project else None
    if isinstance(value, Mapping):
        value = _public_explore_support_record(value)
        if value.get('schema') == 'RouterReadDiagnostic.v2':
            # Permanent encoded diagnostic boundary. The same privacy walk
            # owns projection and secret discovery inside the lossless body.
            from arch4.foundation.read_diagnostic_report import (
                decode_read_diagnostic_data, project_read_diagnostic_data,
            )
            if not project:
                decoded = decode_read_diagnostic_data(value)
                _privacy_walk({name: child for name, child in decoded.items() if name != 'schema'},
                              project=False, secrets=secrets)
                return None
            return project_read_diagnostic_data(value, lambda decoded: _privacy_walk(
                decoded, project=True, secrets=secrets))
        out = {} if project else None
        public_apn = _public_apn_children(value)
        # Permanent stock MiWiFi identity adapter. Its native `id` is a device
        # serial; generic RPC/APN ids remain public. Both documented model
        # representations are supported without depending on the minor FW.
        native_miwifi_identity = 'id' in value and 'romversion' in value and any(
            isinstance(value.get(name), str) and value[name].lower() in
            {'cb0401v2', 'xiaomi.router.cb0401v2'} for name in ('hardware', 'model'))
        # UCI wireless interface replies use an unqualified `key` for the
        # password. Generic `key` fields elsewhere remain public metadata.
        native_uci_wifi = value.get('.type') == 'wifi-iface'
        # Native receive-only DEV2 inbox rows precede canonical SmsMessage
        # translation. Keep ordinary content/from fields public elsewhere.
        native_dev2_sms = {'from', 'content'}.issubset(value) and any(
            name in value for name in ('receivedTime', 'unread'))
        # The trace's XML adapter preserves tag/value/children structure.
        # Reapply semantic masking here, including old captured XML trees;
        # generic value/content fields must not lose engineering evidence.
        native_xml = (isinstance(value.get('tag'), str)
            and isinstance(value.get('attributes'), Mapping)
            and 'value' in value and isinstance(value.get('children'), list))
        if native_xml and _is_sensitive_key(value['tag']):
            masked_fields = masked_fields | frozenset({'value'})
        xml_child_tags = {child.get('tag') for child in value['children']
            if isinstance(child, Mapping) and isinstance(child.get('tag'), str)} if native_xml else set()
        native_xml_sms = (native_xml and value['tag'] == 'Message'
            and 'Content' in xml_child_tags
            and bool(xml_child_tags & {'Phone', 'Date', 'Smstat'}))
        fact_field = str(value.get("field") or "").strip()
        fact_value = bool(fact_field and "disposition" in value
            and isinstance(value.get("value"), Mapping)
            and {"present", "value"}.issubset(value["value"]))
        sensitive_fact = fact_value and _is_sensitive_key(fact_field)
        sms = ("sender" in value and "text" in value and any(
            name in value for name in ("message_id", "timestamp", "received_ts", "protocol")))
        for raw_key, child in value.items():
            child = _public_support_tier_child(raw_key, child)
            key_str = str(raw_key)
            normalized = _normalized_field_key(key_str)
            output_key = _scrub_subscription_scope(raw_key) if project and isinstance(raw_key, str) else raw_key
            if project and output_key in out:
                raise ValueError("support_redacted_mapping_key_collision")
            if key_str in public_apn:
                clean = _project_apn_child(child, public_apn[key_str], project=project, secrets=secrets)
            elif native_xml_sms and key_str == 'children':
                clean_children = [_privacy_walk(node, project=project, secrets=secrets,
                    masked_fields=frozenset({'value'}) if isinstance(node, Mapping)
                    and node.get('tag') in {'Content', 'Phone'} else frozenset()) for node in child]
                clean = clean_children if project else None
            elif normalized in _FIELD_NAME_METADATA_MAP_KEYS and isinstance(child, Mapping):
                # Metadata values are public provenance, never secret field values.
                clean = {k: _privacy_walk(v, project=True, secrets=None) for k, v in child.items()} if project else None
            else:
                sensitive = (_is_sensitive_key(key_str)
                    or normalized in masked_fields
                    or (native_miwifi_identity and key_str == 'id')
                    or (native_uci_wifi and normalized == 'key')
                    or (native_dev2_sms and normalized in {'from', 'content'})
                    or (sms and normalized in {"sender", "text"}))
                if sensitive:
                    _remember_sensitive_values(child, secrets)
                if project and normalized in masked_fields:
                    _privacy_walk(child, project=False, secrets=secrets) if secrets is not None else None
                    # Absence is contract data, not a secret value. Inventing a
                    # string here breaks strict unavailable-fact readback.
                    clean = None if child is None else SCRUB_MASK
                elif sensitive_fact and normalized == "value":
                    clean = _privacy_walk(child, project=project, secrets=secrets,
                        masked_fields=frozenset({"value"}))
                elif sensitive and isinstance(child, Mapping) and "." in key_str and {"value", "text", "source_path"}.issubset(child):
                    clean = _privacy_walk(child, project=project, secrets=secrets,
                        masked_fields=frozenset({"value", "text"}))
                elif sensitive:
                    if secrets is not None:
                        _privacy_walk(child, project=False, secrets=secrets)
                    clean = None if child is None else SCRUB_MASK
                else:
                    semantic_key = key_str
                    if normalized in {"value", "text"}:
                        semantic_key = fact_field if fact_value else (key or key_str)
                    clean = _privacy_walk(child, project=project, secrets=secrets, key=semantic_key)
            if project:
                out[output_key] = clean
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        values = sorted(value, key=str) if project and isinstance(value, (set, frozenset)) else value
        if project:
            return [_privacy_walk(child, project=True, secrets=secrets) for child in values]
        for child in values:
            _privacy_walk(child, project=False, secrets=secrets)
        return None
    if not project:
        return None
    if isinstance(value, (str, Path)):
        return _scrub_string_value(str(value), key=key)
    # Permanent typed-value boundary. The old secret-receipt API accepts JSON
    # mappings/sequences only; keep that contract for these projection adapters.
    if isinstance(value, SimpleNamespace):
        return _privacy_walk(vars(value), project=True, secrets=None)
    if is_dataclass(value) and not isinstance(value, type):
        return _privacy_walk(asdict(value), project=True, secrets=None)
    return value


def _scrub_value(value: Any, *, key: str = "") -> Any:
    return _privacy_walk(value, project=True, secrets=None, key=key)


def scrub_list(items: List[Any]) -> List[Any]:
    """Recursively scrub one JSON-style sequence."""
    return [_scrub_value(item) for item in items]


def scrub_text(text: str) -> str:
    """Scrub a plain-text string (e.g. raw log line) of sensitive patterns.

    Does not parse JSON — use scrub_record for structured data.
    """
    if not isinstance(text, str):
        return text
    out = _scrub_subscription_scope(text)
    for pattern, replacement in _ASSIGNMENT_PATTERNS:
        out = pattern.sub(lambda m: f"{m.group(1)}={replacement}", out)
    out = _IMEI_RE.sub(SCRUB_MASK, out)
    out = _ICCID_RE.sub(SCRUB_MASK, out)
    out = _IMSI_RE.sub(SCRUB_MASK, out)
    out = _EMAIL_RE.sub(SCRUB_MASK, out)
    out = _MAC_RE.sub(SCRUB_MASK, out)
    out = _PHONE_RE.sub(SCRUB_MASK, out)
    out = _WINDOWS_USER_PATH_RE.sub(lambda match: f"{match.group(1)}{SCRUB_MASK}", out)
    out = _POSIX_USER_PATH_RE.sub(lambda match: f"{match.group(1)}{SCRUB_MASK}", out)
    preserved: List[str] = []

    def _protect_engineering_version(match: re.Match[str]) -> str:
        preserved.append(match.group(2))
        return f"{match.group(1)}__GUC_SAFE_VERSION_{len(preserved) - 1}__"

    out = _PRESERVED_TEXT_ASSIGNMENT_RE.sub(_protect_engineering_version, out)
    out = _IPV4_RE.sub(SCRUB_MASK, out)
    for index, value in enumerate(preserved):
        out = out.replace(f"__GUC_SAFE_VERSION_{index}__", value)
    return out


def scrub_jsonl_line(line: str) -> str:
    """Scrub one JSONL line without ever returning malformed source text.

    Parses the JSON, applies scrub_record, and re-serialises.
    Parse failure produces a content-free redaction marker.  This function is
    intentionally deny-by-default because JSONL is exported outside the local
    trust boundary.
    """
    stripped = line.strip()
    if not stripped:
        return line
    try:
        clean = _scrub_json_value(json.loads(stripped))
        return json.dumps(clean, ensure_ascii=False)
    except Exception:
        return json.dumps(
            {
                "privacy_scrub": {
                    "disposition": "redacted_malformed_jsonl_record",
                    "content_retained": False,
                }
            },
            ensure_ascii=False,
        )


def _scrub_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return scrub_record(value)
    return _scrub_value(value)


def _redaction_count(before: Any, after: Any) -> int:
    """Count semantic values changed by the canonical scrub operation."""

    if isinstance(before, Mapping) and isinstance(after, Mapping):
        return sum(
            _redaction_count(value, after.get(key))
            for key, value in before.items()
        )
    if isinstance(before, (list, tuple)) and isinstance(after, list):
        return sum(
            _redaction_count(left, right)
            for left, right in zip(before, after, strict=False)
        )
    return int(before != after)


def support_known_secret_values(value: Any) -> tuple[str, ...]:
    """Return explicit sensitive values for final-archive absence checks.

    Values remain in memory only and are never written to a manifest or
    receipt.  The semantic key authority is the same one used by the scrubber.
    """

    discovered: set[str] = set()

    _privacy_walk(value, project=False, secrets=discovered)
    return tuple(sorted(discovered))


def _encoded_support_result(
    text: str,
    *,
    disposition: str,
    source_kind: str,
    source_size_bytes: int,
    redaction_count: int = 0,
    reason: str = "",
) -> SupportArtifactScrubResult:
    content = str(text).encode("utf-8")
    return SupportArtifactScrubResult(
        content=content,
        disposition=disposition,
        source_kind=source_kind,
        source_size_bytes=int(source_size_bytes),
        staged_size_bytes=len(content),
        staged_sha256=hashlib.sha256(content).hexdigest(),
        redaction_count=int(redaction_count),
        reason=str(reason or ""),
    )


def excluded_support_artifact(
    *,
    source_kind: str,
    source_size_bytes: int = 0,
    disposition: str,
    reason: str,
) -> SupportArtifactScrubResult:
    """Create an explicit exclusion that cannot carry source content."""

    return SupportArtifactScrubResult(
        content=None,
        disposition=str(disposition),
        source_kind=str(source_kind or "unknown"),
        source_size_bytes=int(source_size_bytes),
        reason=str(reason or ""),
    )


def _scrub_json_artifact(text: str, *, source_size_bytes: int) -> SupportArtifactScrubResult:
    try:
        payload = json.loads(text)
    except Exception:
        marker = {
            "privacy_scrub": {
                "disposition": "redacted_malformed_json_artifact",
                "content_retained": False,
            }
        }
        return _encoded_support_result(
            json.dumps(marker, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            disposition="included_structured_redacted",
            source_kind="json",
            source_size_bytes=source_size_bytes,
            redaction_count=1,
            reason="malformed_json",
        )
    clean = _scrub_json_value(payload)
    return _encoded_support_result(
        json.dumps(clean, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        disposition="included_structured_scrubbed",
        source_kind="json",
        source_size_bytes=source_size_bytes,
        redaction_count=_redaction_count(payload, clean),
    )


def _scrub_open_source_manifest_artifact(
    text: str,
    *,
    source_size_bytes: int,
) -> SupportArtifactScrubResult:
    """Scrub the exact public dependency schema without hiding license names."""

    try:
        from arch4.open_source_manifest import (
            OWNER as OPEN_SOURCE_OWNER,
            SCHEMA as OPEN_SOURCE_SCHEMA,
            component_fingerprint,
            validate_open_source_manifest,
        )

        payload = json.loads(text)
        components = validate_open_source_manifest(payload)
        clean_components = []
        for component in components:
            row = component.as_dict()
            clean_components.append(
                {
                    "name": _scrub_string_value(row["name"], key="name"),
                    # This field is public supply-chain information in this
                    # exact validated schema.  Generic license values remain
                    # sensitive everywhere else.
                    "license": scrub_text(row["license"]),
                    "use": _scrub_string_value(row["use"], key="use"),
                    "category": _scrub_string_value(
                        row["category"],
                        key="category",
                    ),
                    "evidence": _scrub_string_value(
                        row["evidence"],
                        key="evidence",
                    ),
                }
            )
        clean = {
            "schema": OPEN_SOURCE_SCHEMA,
            "owner": OPEN_SOURCE_OWNER,
            "catalog_sha256": component_fingerprint(clean_components),
            "generated_from": [
                _scrub_string_value(str(item), key="generated_from")
                for item in payload["generated_from"]
            ],
            "components": clean_components,
        }
        validate_open_source_manifest(clean)
    except Exception as exc:
        return excluded_support_artifact(
            source_kind="json",
            source_size_bytes=source_size_bytes,
            disposition="excluded_invalid_public_manifest",
            reason=f"open_source_manifest_invalid:{type(exc).__name__}",
        )
    return _encoded_support_result(
        json.dumps(clean, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        disposition="included_public_manifest_scrubbed",
        source_kind="json",
        source_size_bytes=source_size_bytes,
        redaction_count=_redaction_count(payload, clean),
    )


def _scrub_jsonl_artifact(text: str, *, source_size_bytes: int) -> SupportArtifactScrubResult:
    output_lines: List[str] = []
    redactions = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            output_lines.append("")
            continue
        try:
            payload = json.loads(line)
            clean = _scrub_json_value(payload)
            redactions += _redaction_count(payload, clean)
            output_lines.append(json.dumps(clean, ensure_ascii=False))
        except Exception:
            redactions += 1
            output_lines.append(
                json.dumps(
                    {
                        "privacy_scrub": {
                            "disposition": "redacted_malformed_jsonl_record",
                            "content_retained": False,
                            "line_number": line_number,
                        }
                    },
                    ensure_ascii=False,
                )
            )
    out = "\n".join(output_lines)
    if text.endswith("\n"):
        out += "\n"
    return _encoded_support_result(
        out,
        disposition=(
            "included_structured_redacted" if redactions else "included_structured_scrubbed"
        ),
        source_kind="jsonl",
        source_size_bytes=source_size_bytes,
        redaction_count=redactions,
        reason="malformed_jsonl_records" if redactions else "",
    )


def _scrub_xml_artifact(text: str, *, source_size_bytes: int) -> SupportArtifactScrubResult:
    try:
        root = ET.fromstring(text)
    except Exception:
        marker = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<privacy_scrub disposition="redacted_malformed_xml_artifact" '
            'content_retained="false" />\n'
        )
        return _encoded_support_result(
            marker,
            disposition="included_structured_redacted",
            source_kind="xml",
            source_size_bytes=source_size_bytes,
            redaction_count=1,
            reason="malformed_xml",
        )
    redactions = 0
    for elem in root.iter():
        for key, value in list(elem.attrib.items()):
            scrubbed = str(scrub_record({str(key): str(value)})[str(key)])
            redactions += int(scrubbed != str(value))
            elem.set(str(key), scrubbed)
        if elem.text is not None:
            original = str(elem.text)
            scrubbed = str(scrub_record({str(elem.tag): original})[str(elem.tag)])
            redactions += int(scrubbed != original)
            elem.text = scrubbed
    ET.indent(root, space="  ")
    out = '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )
    if text.endswith("\n"):
        out += "\n"
    return _encoded_support_result(
        out,
        disposition="included_structured_scrubbed",
        source_kind="xml",
        source_size_bytes=source_size_bytes,
        redaction_count=redactions,
    )


def _scrub_csv_artifact(text: str, *, source_size_bytes: int) -> SupportArtifactScrubResult:
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except Exception:
        return _encoded_support_result(
            "privacy_scrub_disposition,content_retained\nredacted_malformed_csv_artifact,false\n",
            disposition="included_structured_redacted",
            source_kind="csv",
            source_size_bytes=source_size_bytes,
            redaction_count=1,
            reason="malformed_csv",
        )
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    redactions = 0
    if not rows:
        writer.writerows(rows)
    else:
        headers = [str(item) for item in rows[0]]
        writer.writerow(headers)
        for row in rows[1:]:
            output_row: list[str] = []
            for index, value in enumerate(row):
                original = str(value)
                scrubbed = _scrub_string_value(
                    original,
                    key=headers[index] if index < len(headers) else "",
                )
                redactions += int(scrubbed != original)
                output_row.append(scrubbed)
            writer.writerow(output_row)
    return _encoded_support_result(
        output.getvalue(),
        disposition="included_structured_scrubbed",
        source_kind="csv",
        source_size_bytes=source_size_bytes,
        redaction_count=redactions,
    )


def scrub_support_artifact(path: str | Path, raw: bytes) -> SupportArtifactScrubResult:
    """Return the only legal staging decision for one support artifact.

    The allowlist is intentionally textual and explicit.  Unsupported types,
    undecodable payloads, and binary-looking text are excluded with no content
    attached.  Structured parse failures are replaced by content-free markers.
    """

    source_path = Path(path)
    suffix = source_path.suffix.lower()
    source_size = len(raw)
    if suffix == ".gz" and source_path.name.lower().endswith(".jsonl.gz"):
        try:
            decompressed = gzip.decompress(raw).decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return excluded_support_artifact(
                source_kind="jsonl.gz",
                source_size_bytes=source_size,
                disposition="excluded_undecodable",
                reason="invalid_gzip_or_utf8",
            )
        scrubbed = _scrub_jsonl_artifact(
            decompressed,
            source_size_bytes=source_size,
        )
        if scrubbed.content is None:
            return scrubbed
        content = gzip.compress(scrubbed.content, compresslevel=6, mtime=0)
        return SupportArtifactScrubResult(
            content=content,
            disposition=str(scrubbed.disposition),
            source_kind="jsonl.gz",
            source_size_bytes=source_size,
            staged_size_bytes=len(content),
            staged_sha256=hashlib.sha256(content).hexdigest(),
            redaction_count=int(scrubbed.redaction_count),
            reason=str(scrubbed.reason or ""),
        )
    if suffix not in _SUPPORT_TEXT_SUFFIXES:
        return excluded_support_artifact(
            source_kind=suffix.lstrip(".") or "extensionless",
            source_size_bytes=source_size,
            disposition="excluded_not_safe_allowlisted",
            reason="unsupported_artifact_type",
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return excluded_support_artifact(
            source_kind=suffix.lstrip("."),
            source_size_bytes=source_size,
            disposition="excluded_undecodable",
            reason="invalid_utf8",
        )
    if any(ord(char) < 32 and char not in {"\t", "\n", "\r"} for char in text):
        return excluded_support_artifact(
            source_kind=suffix.lstrip("."),
            source_size_bytes=source_size,
            disposition="excluded_binary",
            reason="binary_control_character_detected",
        )
    if source_path.name.casefold() == "open_source_manifest.json":
        return _scrub_open_source_manifest_artifact(
            text,
            source_size_bytes=source_size,
        )
    if suffix == ".json":
        return _scrub_json_artifact(text, source_size_bytes=source_size)
    if suffix == ".jsonl":
        return _scrub_jsonl_artifact(text, source_size_bytes=source_size)
    if suffix == ".xml":
        return _scrub_xml_artifact(text, source_size_bytes=source_size)
    if suffix == ".csv":
        return _scrub_csv_artifact(text, source_size_bytes=source_size)
    scrubbed_text = scrub_text(text)
    return _encoded_support_result(
        scrubbed_text,
        disposition="included_text_scrubbed",
        source_kind=suffix.lstrip("."),
        source_size_bytes=source_size,
        redaction_count=int(scrubbed_text != text),
    )


# ── Engineering-critical fields that must NOT be scrubbed ────────────────────
# Included here for documentation; not used in logic above.
_PRESERVE_FIELDS: frozenset[str] = frozenset({
    "model", "vendor", "family", "firmware", "config_version",
    "hardware_version", "software_version", "profile_id",
    "sinr_db", "rsrp_dbm", "rsrq_db", "rssi_dbm",
    "cq_cell", "cqi", "mcs_dl", "mcs_ul",
    "primary_band", "ca_signature", "carrier_count",
    "congestion_pct", "congestion_display_pct", "rtt_ms", "dl_mbps", "ul_mbps",
    "ts", "iso", "kind", "event", "tick", "seq", "schema_version",
    "state", "action_lever", "action_reason", "suppressed_reason",
    "record_type", "radio_connected",
})
