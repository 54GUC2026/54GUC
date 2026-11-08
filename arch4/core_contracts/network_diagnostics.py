"""Sanitized modem-neutral diagnostic vocabulary; never command confirmation."""
from collections.abc import Mapping
import re

NETWORK_DIAGNOSTIC_SCHEMA = "NetworkConnectionDiagnostic.v1"
NETWORK_DIAGNOSTIC_ISSUES = frozenset({
    "sim_unavailable", "sim_locked", "data_disabled", "data_state_unavailable",
    "manual_connection", "connection_mode_unavailable", "roaming_disabled",
    "roaming_state_unavailable", "apn_unavailable", "apn_read_only",
    "selection_unavailable", "registration_failed", "registration_pending",
    "registration_result_unavailable", "mobile_state_unavailable", "no_mobile_service",
    "registered_without_data", "radio_disabled", "radio_mode_unavailable",
})


def validate_network_diagnostic(value):
    if (not isinstance(value, Mapping) or set(value) != {
            "schema", "subscription_key", "mobile_connected", "issues"}
            or value["schema"] != NETWORK_DIAGNOSTIC_SCHEMA):
        raise ValueError("network_diagnostic_exact_fields_required")
    key = value["subscription_key"]
    if not isinstance(key, str) or key and not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("network_diagnostic_subscription_invalid")
    connected = value["mobile_connected"]
    if connected is not None and type(connected) is not bool:
        raise TypeError("network_diagnostic_connection_invalid")
    issues = value["issues"]
    if (not isinstance(issues, (tuple, list)) or any(
            not isinstance(issue, str) or issue not in NETWORK_DIAGNOSTIC_ISSUES for issue in issues)
            or len(set(issues)) != len(issues) or connected is True and issues):
        raise ValueError("network_diagnostic_issues_invalid")
    return dict(value, issues=list(issues))


def network_diagnostic_from_matrix(value):
    """Permanent adapter for Matrix's named, bounded Scout metadata extension."""
    if not isinstance(value, Mapping):
        raise ValueError("network_diagnostic_matrix_mapping_required")
    payload = dict(value)
    if "unknown_alias_evidence" in payload:
        evidence = payload.pop("unknown_alias_evidence")
        if (not isinstance(evidence, Mapping)
                or evidence.get("schema") != "ModemLiveScoutUnknownTokenReceipt.v1"
                or evidence.get("owner") != "54guc.modem_live.canonical_aliases"
                or evidence.get("consumer") != "54guc.modem_live.scout"
                or evidence.get("bounded") is not True):
            raise ValueError("network_diagnostic_scout_extension_invalid")
    return validate_network_diagnostic(payload)
