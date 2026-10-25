"""Build a read-only Site Report from the current Core snapshot, Foundation health, the
current-session journal, and an optional history snapshot supplied by Product
Intelligence. Foundation never opens or rebuilds the Intelligence store.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import logging
import math
from pathlib import Path
import time
from typing import Any, Mapping, Sequence
import webbrowser

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.atomic_io import atomic_write_text
from arch4.core_contracts.runtime_snapshot import (
    CORE_RUNTIME_SNAPSHOT_OWNER,
    CORE_RUNTIME_SNAPSHOT_SCHEMA,
)
from arch4.core_contracts.current_health import CoreCurrentHealthView
from arch4.core_contracts.accepted_current_facts import CoreAcceptedCurrentFacts
from arch4.privacy_scrub import scrub_record, scrub_text
from arch4.runtime_register_contract_names import (
    REGISTER_CA,
    REGISTER_LATENCY,
    REGISTER_QUALITY,
    REGISTER_INTERNET_REACHABILITY,
)

from .data_budget import FoundationDataBudgetProjection
from .operator_data_budget_reader import (
    FoundationOperatorDataBudgetReader,
    project_operator_data_budget_from_accepted_facts,
)
from .traffic_accounting import resolve_traffic_accounting_root

from .runtime_artifacts import (
    CURRENT_STATE_OWNER,
    CURRENT_STATE_SCHEMA,
    EVENT_OWNER,
    EVENT_SCHEMA,
    RUNTIME_HEALTH_OWNER,
    RUNTIME_HEALTH_SCHEMA,
    core_current_line_values,
)
from .evidence_snapshot import (
    FOUNDATION_EVIDENCE_SNAPSHOT_MAX_EVENTS,
    capture_foundation_evidence_snapshot,
)
from arch4.core_contracts.site_report_history import (
    SITE_REPORT_HISTORY_MAX_HOURS,
    SiteReportHistoryPoint,
    SiteReportHistoryProjection,
)


log = logging.getLogger("arch4.foundation.site_report")

FOUNDATION_SITE_REPORT_SCHEMA = "FoundationSiteReport.v2"
FOUNDATION_SITE_REPORT_OWNER = "arch4.foundation.site_report"
FOUNDATION_SITE_REPORT_RETENTION_DAYS = 14
FOUNDATION_SITE_REPORT_MAX_EVENTS = 200
FOUNDATION_SITE_REPORT_MAX_WINDOW_HOURS = SITE_REPORT_HISTORY_MAX_HOURS


@dataclass(frozen=True, slots=True)
class ReportField:
    label: str
    value: Any
    unit: str = ""
    digits: int | None = None
    explanation: str = ""


@dataclass(frozen=True, slots=True)
class ReportFeature:
    label: str
    status: str
    explanation: str = ""


@dataclass(frozen=True, slots=True)
class ReportSection:
    title: str
    fields: tuple[ReportField, ...]


@dataclass(frozen=True, slots=True)
class ReportEvent:
    ts: float
    sequence: int
    severity: str
    kind: str
    summary: str


@dataclass(frozen=True, slots=True)
class FoundationSiteReportSnapshot:
    generated_at: float
    window_hours: float
    cutoff_ts: float
    current_run_started_at: float
    current_state_sequence: int
    core_sequence: int
    current_state_published_at: float
    runtime_health_sequence: int
    sections: tuple[ReportSection, ...]
    events: tuple[ReportEvent, ...]
    history: SiteReportHistoryProjection
    evidence_snapshot_id: str = ""
    features: tuple[ReportFeature, ...] = ()


# These are accepted-current field names, not device-native aliases.
_SECTION_FIELDS = (
    ("Device and network", "identity", (
        ("manufacturer", "Manufacturer", "", None), ("model", "Model", "", None),
        ("hardware_version", "Hardware version", "", None), ("firmware", "Firmware", "", None),
        ("software_version", "Software", "", None), ("webui", "Router interface version", "", None),
        ("provider_name", "Provider", "", None), ("provider_plmn", "Network code", "", None),
    )),
    ("Radio now", "radio", (
        ("network_type", "Network", "", None), ("serving_band", "Primary band", "", None),
        ("configured_band_pool", "Allowed LTE bands", "", None),
        ("carrier_count", "Bands in the allowed LTE pool", "", None),
        ("ca_active", "Combining LTE carriers now", "", None),
        ("ca_component_band_set", "Reported active carrier bands", "", None),
        ("cell_id", "4G cell", "", None), ("pci", "4G cell code (PCI)", "", None),
        ("earfcn_dl", "4G channel", "", None),
        ("sinr_db", "Signal clarity (SINR)", "dB", 1),
        ("rsrp_dbm", "Signal strength (RSRP)", "dBm", 1),
        ("rsrq_db", "Signal quality (RSRQ)", "dB", 1),
        ("nr5g_active", "5G carrier active", "", None), ("nr_band", "5G band", "", None),
        ("nr_cell_id", "5G cell", "", None), ("nr_pci", "5G cell code (PCI)", "", None),
        ("nr_arfcn", "5G channel", "", None),
        ("nr_sinr_db", "5G signal clarity", "dB", 1),
        ("nr_rsrp_dbm", "5G signal strength", "dBm", 1),
        ("nr_rsrq_db", "5G signal quality", "dB", 1),
        ("nr_mcs_dl", "5G download coding (MCS)", "", None),
        ("nr_mcs_ul", "5G upload coding (MCS)", "", None),
    )),
    ("Connectivity now", "health", (("radio_connected", "Radio connected", "", None),)),
    ("Internet", REGISTER_INTERNET_REACHABILITY, (
        ("disposition", "Internet status", "connection_status", None),
        ("outage_s", "Current Internet interruption", "duration", None),
    )),
    ("Connection quality", REGISTER_QUALITY, (
        ("congestion_display_pct", "Network congestion", "%", 1),
        ("congestion_trend_display", "Congestion trend", "", None),
        ("cq_cell_display", "Connection quality score", "quality_percent", 1),
    )),
    ("Response times", REGISTER_LATENCY, (
        ("rtt_raw_ms", "Internet response time", "ms", 1),
        ("rtt_dl_jitter_trimmed_ms", "Response-time variation", "ms", 1),
    )),
    ("Traffic now", "traffic", (
        ("throughput_dl_mbps", "Download", "Mbps", 3),
        ("throughput_ul_mbps", "Upload", "Mbps", 3),
        ("session_uptime_s", "Mobile connection duration", "duration", None),
        ("traffic_session_download_bytes", "Session downloaded", "bytes", None),
        ("traffic_session_upload_bytes", "Session uploaded", "bytes", None),
        ("traffic_total_download_bytes", "Router total downloaded", "bytes", None),
        ("traffic_total_upload_bytes", "Router total uploaded", "bytes", None),
    )),
    ("Router readings", "slow", (
        ("dl_freq_mhz", "4G download frequency", "MHz", 3),
        ("ul_freq_mhz", "4G upload frequency", "MHz", 3),
        ("nr_dl_freq_mhz", "5G download frequency", "MHz", 3),
        ("nr_ul_freq_mhz", "5G upload frequency", "MHz", 3),
        ("dl_bandwidth_mhz", "4G downlink bandwidth", "MHz", 1),
        ("ul_bandwidth_mhz", "4G uplink bandwidth", "MHz", 1),
        ("nr_bandwidth_mhz", "5G downlink bandwidth", "MHz", 1),
        ("nr_ul_bandwidth_mhz", "5G uplink bandwidth", "MHz", 1),
        ("temperature_cpu_c", "CPU temperature", "°C", 1),
        ("temperature_modem_5g_c", "5G modem temperature", "°C", 1),
        ("router_cpu_load_pct", "Router CPU use", "%", 1),
        ("router_memory_used_pct", "Router memory use", "%", 1),
        ("router_system_uptime_s", "Router uptime", "duration", None),
        ("antenna", "Antenna selection", "", None),
        ("wifi_24ghz_enabled", "2.4 GHz Wi-Fi enabled", "", None),
        ("wifi_5ghz_enabled", "5 GHz Wi-Fi enabled", "", None),
        ("wifi_6ghz_enabled", "6 GHz Wi-Fi enabled", "", None),
    )),
)

_SAFE_EVENT_DATA_KEYS: tuple[str, ...] = (
    "phase",
    "lever",
    "workflow_id",
    "action_origin",
    "success",
    "status",
    "reason",
    "message",
    "error",
    "pband",
    "bands",
    "ca_pool",
    "requested_bands",
    "provider_name",
    "provider_plmn",
)

_REGISTER_FIELD_VALIDITY: Mapping[str, Mapping[str, str]] = {
    REGISTER_CA: {
        "ca_stat_ld_pct": "ca_stat_ld_valid",
    },
    REGISTER_QUALITY: {
        "congestion_display_pct": "congestion_display_valid",
        "cq_cell_display": "cq_cell_display_valid",
        "rsrq_baseline_display_db": "rsrq_baseline_display_valid",
        "rsrq_delta_display_db": "rsrq_delta_display_valid",
    },
    REGISTER_LATENCY: {
        "rtt_raw_ms": "rtt_raw_available",
        "rtt_delta_display_ms": "rtt_delta_display_valid",
        "rtt_dl_jitter_trimmed_ms": "rtt_dl_jitter_valid",
    },
}


def _require_owner(
    payload: Mapping[str, Any],
    *,
    schema: str,
    owner: str,
    reason: str,
) -> None:
    if payload.get("schema") != schema or payload.get("owner") != owner:
        raise RuntimeError(reason)


def _positive_float(value: Any, *, reason: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(reason) from exc
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise RuntimeError(reason)
    return parsed


def _positive_int(value: Any, *, reason: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(reason) from exc
    if parsed <= 0:
        raise RuntimeError(reason)
    return parsed


def _register_value(
    registers: Mapping[str, Any],
    register_name: str,
    *,
    current_facts: CoreAcceptedCurrentFacts,
    core_health: CoreCurrentHealthView,
) -> Mapping[str, Any]:
    if register_name in {"identity", "radio", "traffic", "timing", "slow", "capability"}:
        values = core_current_line_values(current_facts, register_name)
        if register_name == "radio":
            values.update(core_current_line_values(current_facts, "rf"))
        if register_name == "identity":
            card = values.get("resolved_identity_card")
            if not values.get("manufacturer") and isinstance(card, Mapping) and card.get("exact_identity") is True:
                values["manufacturer"] = card.get("vendor")
        return values
    if register_name == "health":
        values = dict(core_health.modem_live_fast_lane.to_mapping())
        values.update(core_health.modem_health.to_mapping())
        values.update(core_current_line_values(current_facts, "health"))
        return values
    envelope = registers.get(register_name)
    if not isinstance(envelope, Mapping):
        return {}
    if envelope.get("valid") is not True or bool(envelope.get("stale", False)):
        return {}
    value = envelope.get("value")
    if not isinstance(value, Mapping):
        return {}
    observed = dict(value)
    for field_name, validity_name in _REGISTER_FIELD_VALIDITY.get(
        register_name,
        {},
    ).items():
        if observed.get(validity_name) is not True:
            observed.pop(field_name, None)
    if (
        register_name == REGISTER_CA
        and str(observed.get("ca_composition_state") or "").lower()
        != "available"
    ):
        observed.pop("ca_component_band_set", None)
        observed.pop("ca_component_count", None)
    return observed


def _field_explanation(current_facts: CoreAcceptedCurrentFacts, line: str, key: str, value: Any) -> str:
    if value is not None and value != "":
        if key == "configured_band_pool":
            return "LTE bands allowed for selection. This is separate from the carriers currently in use."
        if key == "carrier_count":
            return "Counts allowed LTE bands, not active carriers."
        if key in {"nr_mcs_dl", "nr_mcs_ul"}:
            return "The router's reported radio coding index; this is not a speed measurement."
        return ""
    path = f"rf.{key}" if line == "radio" and key in {"cell_id", "pci", "earfcn_dl", "sinr_db", "rsrp_dbm", "rsrq_db"} else f"{line}.{key}"
    try:
        fact = current_facts.exact(path)
    except KeyError:
        fact = None
    if fact is not None and fact.disposition == "unsupported":
        return "54GUC cannot read this from the selected router."
    if key.startswith("nr_") or key == "nr5g_active":
        values = core_current_line_values(current_facts, "capability")
        if values.get("device_radio_class") == "5g":
            radio = core_current_line_values(current_facts, "radio")
            if radio.get("network_type") == "LTE (4G)":
                return "5G is supported but not active in this reading."
            return "5G is supported; this reading is currently unavailable."
    return "Not available in this reading"


def _report_features(current_facts: CoreAcceptedCurrentFacts) -> tuple[ReportFeature, ...]:
    values = core_current_line_values(current_facts, "capability")
    radio_class = values.get("device_radio_class")
    features = [ReportFeature("Mobile radio", {"5g": "4G and 5G", "4g_only": "4G"}.get(radio_class, "Not confirmed"))]
    for key, label in (
        ("band_select", "Choose allowed LTE bands"), ("pband", "Choose a preferred LTE band"),
        ("band_lock", "Lock to an LTE band"), ("nr_band_lock", "Choose 5G bands"),
        ("radio_mode", "Switch between 4G and 5G modes"), ("cell_lock", "Lock to a cell"),
        ("mobile_data", "Connect or disconnect mobile data"), ("network_settings", "Change SIM, network or APN settings"),
        ("wifi_control", "Control router Wi-Fi"), ("restart", "Restart the router"),
    ):
        state, allowed = values.get(f"action_{key}_state"), values.get(f"action_{key}_allowed")
        if state == "unsupported":
            status, detail = "Not supported", ""
        elif state in {"supported", "estimated"}:
            status = "Currently unavailable" if allowed is not True else "Limited support" if state == "estimated" else "Available"
            detail = "Blocked by the current connection state." if allowed is not True else ""
        else:
            status, detail = "Not confirmed", ""
        features.append(ReportFeature(label, status, detail))
    for key, label in (
        ("traffic_volume", "Data usage readings"), ("traffic_presence", "Traffic activity readings"),
        ("neighbors", "Nearby cell readings"), ("ca_details", "Detailed active carriers"),
        ("plmn", "Mobile network identification"), ("ue_capability", "Router radio capabilities"),
        ("band_priority", "Band priority readings"),
    ):
        state = values.get(f"read_{key}_state")
        status = {"supported": "Supported", "estimated": "Limited support", "unsupported": "Not supported"}.get(state, "Not confirmed")
        features.append(ReportFeature(label, status))
    for key, label in (("session_lte_bands", "Hardware-supported LTE bands"), ("session_nr_bands", "Hardware-supported 5G bands")):
        bands = values.get(key)
        prefix = "n" if key == "session_nr_bands" else "B"
        status = ", ".join(prefix + str(b) for b in bands) if isinstance(bands, (list, tuple)) and bands else "Not supported" if key == "session_nr_bands" and radio_class == "4g_only" else "Not confirmed"
        features.append(ReportFeature(label, status,
            "Hardware capability only; this does not describe the provider's allowed LTE pool or active carriers."))
    sms = values.get("sms_state")
    features.append(ReportFeature("SMS support", {"supported": "Supported", "estimated": "Limited support", "unsupported": "Not supported"}.get(sms, "Not confirmed")))
    for label, paths in (
        ("5G signal readings", ("radio.nr_sinr_db", "radio.nr_rsrp_dbm", "radio.nr_rsrq_db")),
        ("5G channel and bandwidth readings", ("radio.nr_band", "radio.nr_arfcn", "slow.nr_bandwidth_mhz", "slow.nr_ul_bandwidth_mhz")),
        ("5G coding readings", ("radio.nr_mcs_dl", "radio.nr_mcs_ul")),
        ("Router CPU, memory and uptime readings", ("slow.router_cpu_load_pct", "slow.router_memory_used_pct", "slow.router_system_uptime_s")),
    ):
        if radio_class == "4g_only" and label.startswith("5G"):
            continue
        facts = []
        for path in paths:
            try:
                facts.append(current_facts.exact(path))
            except KeyError:
                facts.append(None)
        available = sum(fact is not None and fact.disposition == "available" for fact in facts)
        detail = ""
        if available == len(paths):
            status = "Available now"
        elif available:
            status = "Partly available now"
            detail = f"{available} of {len(paths)} readings available."
        elif all(fact is not None and fact.disposition == "unsupported" for fact in facts):
            status = "Not supported"
        elif any(fact is not None for fact in facts):
            status = "Currently unavailable"
        else:
            status = "Not confirmed"
        features.append(ReportFeature(label, status, detail))
    return tuple(features)


def _report_sections(
    registers: Mapping[str, Any],
    health: Mapping[str, Any],
    budget: FoundationDataBudgetProjection | None,
    *,
    current_facts: CoreAcceptedCurrentFacts,
    core_health: CoreCurrentHealthView,
) -> tuple[ReportSection, ...]:
    sections: list[ReportSection] = []
    for title, register_name, specs in _SECTION_FIELDS:
        values = _register_value(
            registers,
            register_name,
            current_facts=current_facts,
            core_health=core_health,
        )
        sections.append(
            ReportSection(
                title=title,
                fields=tuple(
                    ReportField(
                        label=label,
                        value=values.get(key),
                        unit=unit,
                        digits=digits,
                        explanation=_field_explanation(current_facts, register_name, key, values.get(key)),
                    )
                    for key, label, unit, digits in specs
                    if not ((key.startswith("nr_") or key in {"nr5g_active", "temperature_modem_5g_c"})
                        and core_current_line_values(current_facts, "capability").get("device_radio_class") == "4g_only")
                ),
            )
        )
    sections.append(
        ReportSection(
            title="User data budget",
            fields=(
                ReportField("Data plan", "Saved" if budget is not None and budget.plan_configured else "No matching plan saved"),
                ReportField(
                    "Cycle start day",
                    None if budget is None else budget.cycle_start_day,
                ),
                ReportField(
                    "Plan limit",
                    None if budget is None else budget.plan_limit_bytes,
                    "bytes",
                ),
                ReportField(
                    "Current cycle used",
                    None if budget is None else budget.current_cycle_used_bytes,
                    "bytes",
                    explanation="Recorded usage for your selected billing cycle." if budget is not None and budget.period_accounting_qualified else "Usage for this billing cycle is not available from the saved readings.",
                ),
                ReportField(
                    "Budget left",
                    None if budget is None else budget.left_bytes,
                    "bytes",
                ),
                ReportField(
                    "Daily allowance",
                    None if budget is None else budget.daily_allowance_bytes,
                    "bytes",
                ),
                ReportField(
                    "Daily remaining",
                    None if budget is None else budget.daily_remaining_bytes,
                    "bytes",
                ),
                ReportField(
                    "All-time used",
                    None if budget is None else budget.total_used_bytes,
                    "bytes",
                ),
                ReportField("Usage recorded at", None if budget is None else _local_time(budget.observed_at)),
            ),
        )
    )
    sections.append(
        ReportSection(
            title="Application status",
            fields=(
                ReportField("Application health", "Needs attention" if health.get("degraded") else "Normal"),
                ReportField("Last recorded error", health.get("last_error") or "None recorded"),
            ),
        )
    )
    return tuple(sections)


def _event_summary(payload: Mapping[str, Any]) -> str:
    data = payload.get("data")
    data = data if isinstance(data, Mapping) else {}
    evidence = data.get("evidence")
    profile = evidence.get("profile_fields") if isinstance(evidence, Mapping) else None
    kind = str(payload.get("kind") or "")
    if isinstance(profile, Mapping):
        if kind == "profile_field_recovery":
            return "Router readings recovered."
        names = {"traffic.session_uptime_s": "connection duration", "health.radio_connected": "radio connection",
                 "rf.ri": "MIMO rank", "rf.earfcn_dl": "4G channel", "rf.cqi0": "radio signal feedback",
                 "rf.enb_id": "base station", "rf.pci": "cell code", "rf.cell_id": "cell identity",
                 "identity.imei": "router identity", "identity.imsi": "SIM identity", "identity.firmware": "router firmware",
                 "identity.provider_name": "provider", "identity.provider_plmn": "mobile network",
                 "radio.network_type": "mobile connection type", "radio.serving_band": "current LTE band"}
        issues = profile.get("issues") or []
        fields = [names.get(str(v.get("field")), str(v.get("field", "reading")).split(".")[-1].replace("_", " ")) for v in issues if isinstance(v, Mapping)]
        if fields:
            return scrub_text("Some router readings were temporarily unavailable: " + ", ".join(dict.fromkeys(fields)) + ". Other readings continued.")
    if kind == "core_startup_root_published":
        return "Started regular status monitoring."
    if kind == "core_internet_timeline_change":
        status = data.get("current", {})
        if isinstance(status, Mapping):
            return {"reachable": "Internet connection is available.", "unreachable": "Internet connection is unavailable."}.get(status.get("disposition"), "Internet monitoring status changed.")
    parts: list[str] = []
    operation = str(payload.get("operation") or "").strip()
    detail = str(payload.get("detail") or "").strip()
    degraded = str(payload.get("degraded_behavior") or "").strip()
    if detail:
        parts.append(detail)
    data = payload.get("data")
    if isinstance(data, Mapping):
        for key in _SAFE_EVENT_DATA_KEYS:
            value = data.get(key)
            if value is None or value == "" or value == []:
                continue
            if key not in {"message", "reason", "status", "error"}:
                continue
            parts.append(str(value).replace("_", " "))
    if degraded:
        parts.append(degraded.replace("_", " "))
    return scrub_text("; ".join(parts)) or "Status update recorded. Details are available in Log Checker."


def _events_from_evidence_snapshot(
    records: Sequence[Mapping[str, Any]],
    *,
    cutoff_ts: float,
    max_events: int,
) -> tuple[ReportEvent, ...]:
    retained: list[ReportEvent] = []
    for line_number, payload in enumerate(records, start=1):
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"foundation_site_report_snapshot_event_not_object:index={line_number}"
            )
        _require_owner(
            payload,
            schema=EVENT_SCHEMA,
            owner=EVENT_OWNER,
            reason=f"foundation_site_report_event_owner_invalid:line={line_number}",
        )
        ts = _positive_float(
            payload.get("ts"),
            reason=f"foundation_site_report_event_timestamp_invalid:line={line_number}",
        )
        if ts < cutoff_ts:
            continue
        retained.append(
            ReportEvent(
                ts=ts,
                sequence=_positive_int(
                    payload.get("sequence"),
                    reason=f"foundation_site_report_event_sequence_invalid:line={line_number}",
                ),
                severity=scrub_text(str(payload.get("severity") or "INFO").upper()),
                kind=scrub_text(str(payload.get("kind") or "unknown")),
                summary=_event_summary(payload),
            )
        )
    retained.sort(key=lambda item: (item.ts, item.sequence), reverse=True)
    return tuple(retained[:max_events])


def load_foundation_site_report_snapshot(
    runtime_dir: str | Path,
    *,
    hours: float,
    history: SiteReportHistoryProjection,
    now: float | None = None,
    max_events: int = FOUNDATION_SITE_REPORT_MAX_EVENTS,
) -> FoundationSiteReportSnapshot:
    """Load one owner-validated, privacy-bounded Foundation report snapshot."""

    root = Path(runtime_dir)
    if not root.is_dir():
        raise RuntimeError("foundation_site_report_runtime_dir_missing")
    window_hours = _positive_float(hours, reason="foundation_site_report_window_invalid")
    if window_hours > FOUNDATION_SITE_REPORT_MAX_WINDOW_HOURS:
        raise RuntimeError("foundation_site_report_window_invalid")
    if type(history) is not SiteReportHistoryProjection:
        raise TypeError("foundation_site_report_history_projection_required")
    if not math.isclose(
        float(history.requested_hours),
        window_hours,
        rel_tol=0.0,
        abs_tol=0.000001,
    ):
        raise RuntimeError("foundation_site_report_history_window_mismatch")
    event_limit = int(max_events)
    if event_limit <= 0 or event_limit > FOUNDATION_SITE_REPORT_MAX_EVENTS:
        raise RuntimeError("foundation_site_report_event_limit_invalid")
    generated_at = float(time.time() if now is None else now)
    if not math.isfinite(generated_at) or generated_at <= 0.0:
        raise RuntimeError("foundation_site_report_generation_time_invalid")

    evidence = capture_foundation_evidence_snapshot(
        root,
        now=generated_at,
        max_events=FOUNDATION_EVIDENCE_SNAPSHOT_MAX_EVENTS,
        require_complete=False,
    )
    current = evidence.get("current_state")
    if not isinstance(current, dict):
        status = dict(
            (evidence.get("source_status") or {}).get("current_state") or {}
        )
        disposition = str(status.get("disposition") or "missing")
        if disposition == "missing":
            raise RuntimeError("foundation_site_report_current_state_missing")
        if disposition == "unavailable":
            raise RuntimeError("foundation_site_report_current_state_corrupt")
        raise RuntimeError("foundation_site_report_current_state_owner_invalid")
    _require_owner(
        current,
        schema=CURRENT_STATE_SCHEMA,
        owner=CURRENT_STATE_OWNER,
        reason="foundation_site_report_current_state_owner_invalid",
    )
    current_sequence = _positive_int(
        current.get("sequence"),
        reason="foundation_site_report_current_state_sequence_invalid",
    )
    current_published_at = _positive_float(
        current.get("published_at"),
        reason="foundation_site_report_current_state_timestamp_invalid",
    )
    core = current.get("core_snapshot")
    if not isinstance(core, dict):
        raise RuntimeError("foundation_site_report_core_snapshot_missing")
    _require_owner(
        core,
        schema=CORE_RUNTIME_SNAPSHOT_SCHEMA,
        owner=CORE_RUNTIME_SNAPSHOT_OWNER,
        reason="foundation_site_report_core_snapshot_owner_invalid",
    )
    core_sequence = _positive_int(
        core.get("sequence"),
        reason="foundation_site_report_core_sequence_invalid",
    )
    registers = core.get("registers")
    if not isinstance(registers, dict):
        raise RuntimeError("foundation_site_report_core_registers_missing")
    try:
        current_facts = CoreAcceptedCurrentFacts.from_payload(
            core["accepted_current_facts"]
        )
        core_health = CoreCurrentHealthView.from_payload(
            current["core_current_health"]
        )
        core_identity = MatrixGenerationIdentity.from_mapping(
            core["matrix_identity"]
        )
    except Exception as exc:
        raise RuntimeError(
            "foundation_site_report_accepted_current_projection_invalid"
        ) from exc
    if (
        current_facts.matrix_identity != core_identity
        or core_health.namespace != core_identity.namespace
    ):
        raise RuntimeError(
            "foundation_site_report_current_projection_identity_mismatch"
        )

    health = evidence.get("runtime_health")
    if not isinstance(health, dict):
        status = dict(
            (evidence.get("source_status") or {}).get("runtime_health") or {}
        )
        raise RuntimeError(
            "foundation_site_report_runtime_health_unavailable:"
            + str(status.get("disposition") or "missing")
        )
    if not bool(evidence.get("complete")):
        raise RuntimeError(
            "foundation_site_report_evidence_snapshot_incomplete:"
            + ",".join(str(item) for item in evidence.get("omissions") or ())
        )
    _require_owner(
        health,
        schema=RUNTIME_HEALTH_SCHEMA,
        owner=RUNTIME_HEALTH_OWNER,
        reason="foundation_site_report_runtime_health_owner_invalid",
    )
    health_sequence = _positive_int(
        health.get("sequence"),
        reason="foundation_site_report_runtime_health_sequence_invalid",
    )
    cutoff_ts = generated_at - window_hours * 3600.0
    evidence_events = evidence.get("events")
    if not isinstance(evidence_events, list):
        raise RuntimeError("foundation_site_report_events_missing")
    startup_timestamps = [
        _positive_float(
            event.get("ts"),
            reason="foundation_site_report_startup_timestamp_invalid",
        )
        for event in evidence_events
        if isinstance(event, Mapping)
        and event.get("kind") == "core_startup_root_published"
    ]
    current_run_started_at = (
        min(startup_timestamps) if startup_timestamps else 0.0
    )
    events = _events_from_evidence_snapshot(
        evidence_events,
        cutoff_ts=cutoff_ts,
        max_events=event_limit,
    )
    return FoundationSiteReportSnapshot(
        generated_at=generated_at,
        window_hours=window_hours,
        cutoff_ts=cutoff_ts,
        current_run_started_at=current_run_started_at,
        current_state_sequence=current_sequence,
        core_sequence=core_sequence,
        current_state_published_at=current_published_at,
        runtime_health_sequence=health_sequence,
        sections=_report_sections(
            registers,
            health,
            project_operator_data_budget_from_accepted_facts(
                FoundationOperatorDataBudgetReader(root),
                current_facts,
                accounting_read_root=resolve_traffic_accounting_root(root),
                as_of=generated_at,
            ),
            current_facts=current_facts,
            core_health=core_health,
        ),
        events=events,
        history=history,
        evidence_snapshot_id=str(evidence.get("snapshot_id") or ""),
        features=_report_features(current_facts),
    )


def _plain_value(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple)):
        return ", ".join(_plain_value(item) for item in value) or "---"
    return str(value)


def _duration(value: Any) -> str:
    try:
        total = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "---"
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _bytes(value: Any) -> str:
    try:
        size = max(0.0, float(value))
    except (TypeError, ValueError):
        return "---"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    index = 0
    while size >= 1024.0 and index < len(units) - 1:
        size /= 1024.0
        index += 1
    digits = 0 if index == 0 else 2
    return f"{size:.{digits}f} {units[index]}"


def _field_text(field: ReportField) -> str:
    value = field.value
    if value is None or value == "":
        return "Unavailable"
    if field.unit == "connection_status":
        return {"reachable": "Available", "unreachable": "Unavailable", "unknown": "Not confirmed"}.get(str(value), "Not confirmed")
    if field.unit == "quality_percent" and type(value) in (int, float):
        return f"{float(value) * 100:.1f} %"
    if field.unit == "duration":
        return _duration(value)
    if field.unit == "bytes":
        return _bytes(value)
    if field.digits is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
        rendered = f"{float(value):.{field.digits}f}"
    else:
        rendered = _plain_value(value)
    if field.unit:
        rendered = f"{rendered} {field.unit}"
    return str(scrub_record({field.label: rendered})[field.label])


def _html(value: Any) -> str:
    return escape(scrub_text(str(value)), quote=True)


def _local_time(ts: float) -> str:
    if float(ts) <= 0.0:
        return "Unavailable"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))


def _snapshot_field(
    snapshot: FoundationSiteReportSnapshot,
    section_title: str,
    label: str,
) -> ReportField | None:
    for section in snapshot.sections:
        if section.title != section_title:
            continue
        for field in section.fields:
            if field.label == label:
                return field
    return None


def _summary_value(
    snapshot: FoundationSiteReportSnapshot,
    section_title: str,
    label: str,
    *,
    band: bool = False,
    band_pool: bool = False,
) -> str:
    field = _snapshot_field(snapshot, section_title, label)
    if field is None or field.value is None or field.value == "":
        return "---"
    if label == "Radio connected" and type(field.value) is bool:
        return "Connected" if field.value else "Disconnected"
    if band:
        text = _plain_value(field.value).strip()
        return text if text.upper().startswith("B") else f"B{text}"
    if band_pool and isinstance(field.value, (list, tuple)):
        bands = tuple(
            str(item).strip().upper().removeprefix("B")
            for item in field.value
            if str(item).strip()
        )
        return "+".join(f"B{item}" for item in bands) or "---"
    return _field_text(field)


def _history_average(
    points: Sequence[SiteReportHistoryPoint],
    field_name: str,
) -> float | None:
    weighted = 0.0
    weight = 0
    for point in points:
        value = getattr(point, field_name)
        if value is None:
            continue
        weighted += float(value) * point.sample_count
        weight += point.sample_count
    return weighted / weight if weight else None


def _metric_text(value: float | None, unit: str, digits: int = 1) -> str:
    if value is None:
        return "---"
    return f"{value:.{digits}f}{(' ' + unit) if unit else ''}"


def _history_context(point: SiteReportHistoryPoint) -> str:
    if point.network_type == "NR SA (5G)" and point.nr_band is not None:
        return (
            f"n{point.nr_band} · Cell {point.nr_cell_id or '---'} · "
            f"PCI {point.nr_pci if point.nr_pci is not None else '---'}"
        )
    band = f"B{point.serving_band}" if point.serving_band is not None else "---"
    pool = "+".join(f"B{item}" for item in point.ca_pool) or "---"
    return (
        f"{band} / {pool} · Cell {point.cell_id or '---'} · "
        f"PCI {point.pci if point.pci is not None else '---'}"
    )


def _svg_line_chart(
    points: Sequence[SiteReportHistoryPoint],
    *,
    field_name: str,
    title: str,
    unit: str,
    color: str,
) -> str:
    samples = tuple(
        (point.ended_at, float(value))
        for point in points
        if (value := getattr(point, field_name)) is not None
        and math.isfinite(float(value))
    )
    if not samples:
        return (
            f'<div class="chart-card"><h3>{_html(title)}</h3>'
            '<div class="empty">No qualified evidence.</div></div>'
        )
    width, height = 560.0, 170.0
    left, right, top, bottom = 48.0, 12.0, 18.0, 30.0
    values = [value for _stamp, value in samples]
    low, high = min(values), max(values)
    if math.isclose(low, high):
        padding = max(1.0, abs(low) * 0.05)
        low -= padding
        high += padding
    # Padding is presentation only. Keep normalized scores and percentages
    # within their meaningful range, including a single completed reading.
    if field_name in {"communication_quality", "congestion_pct", "internet_availability_pct"}:
        upper = 1.0 if field_name == "communication_quality" else 100.0
        if all(0.0 <= value <= upper for value in values):
            low, high = max(0.0, low), min(upper, high)
    first_time, last_time = samples[0][0], samples[-1][0]
    time_span = max(1.0, last_time - first_time)
    value_span = high - low
    coordinates = []
    for stamp, value in samples:
        x = left + (stamp - first_time) / time_span * (width - left - right)
        y = top + (high - value) / value_span * (height - top - bottom)
        coordinates.append(f"{x:.1f},{y:.1f}")
    latest = samples[-1][1]
    marker = ""
    if len(coordinates) == 1:
        x, y = coordinates[0].split(",")
        marker = f'<circle cx="{x}" cy="{y}" r="3.5" fill="{color}"/>'
    return (
        f'<div class="chart-card"><h3>{_html(title)}</h3>'
        f'<svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
        f'aria-label="{_html(title)} history">'
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" '
        'stroke="var(--border)"/>'
        f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" '
        f'y2="{height-bottom}" stroke="var(--border)"/>'
        f'<polyline fill="none" stroke="{color}" stroke-width="2.5" '
        f'points="{" ".join(coordinates)}"/>'
        f'{marker}'
        f'<text x="4" y="{top+5}" class="svg-label">{_html(_metric_text(high, unit))}</text>'
        f'<text x="4" y="{height-bottom}" class="svg-label">{_html(_metric_text(low, unit))}</text>'
        f'<text x="{width-right-105}" y="{height-7}" class="svg-label">'
        f'Latest {_html(_metric_text(latest, unit))}</text></svg></div>'
    )


def _history_report_html(history: SiteReportHistoryProjection) -> str:
    if history.status != "available":
        state = "History is still collecting" if history.status == "collecting" else "Service history is unavailable"
        return (
            '<section class="history"><h2>2. Service history</h2>'
            f'<div class="notice"><strong>{_html(state)}.</strong> '
            'No completed service measurements are available for this window. '
            'Current readings and recent events are shown below.</div></section>'
        )
    points = history.points
    summary = (
        ("Estimated capacity", _metric_text(_history_average(points, "capacity_points"), "points")),
        ("Latency", _metric_text(_history_average(points, "latency_ms"), "ms")),
        ("Jitter", _metric_text(_history_average(points, "jitter_ms"), "ms")),
        ("Comm quality", _metric_text(_history_average(points, "communication_quality"), "", 2)),
        ("Congestion", _metric_text(_history_average(points, "congestion_pct"), "%")),
        ("Internet available", _metric_text(_history_average(points, "internet_availability_pct"), "%")),
    )
    summary_html = "".join(
        '<div class="kpi"><div class="value">'
        + _html(value)
        + '</div><div class="label">'
        + _html(label)
        + "</div></div>"
        for label, value in summary
    )
    impact = history.intelligence_impact
    impact_accuracy = (
        f"{impact.forecast_correct} of {impact.forecast_assessed}"
        if impact.forecast_assessed
        else "Not proven"
    )
    impact_summary = "".join(
        '<div class="kpi"><div class="value">'
        + _html(str(value))
        + '</div><div class="label">'
        + _html(label)
        + "</div></div>"
        for label, value in (
            ("Intelligence improved", impact.improved),
            ("Neutral", impact.neutral),
            ("Worse", impact.worse),
            ("Not proven", impact.not_proven),
            ("Forecast direction correct", impact_accuracy),
        )
    )
    if impact.latest_kind:
        actor = (
            "Automatic change"
            if impact.latest_kind == "brain_action"
            else "Kept connection unchanged"
        )
        latest_impact = (
            '<h3>Latest attributable outcome</h3><table class="events">'
            '<tr><th>Completed</th><th>Type</th><th>Before</th><th>After</th>'
            '<th>Estimated capacity</th><th>Response time</th><th>Response variation</th>'
            '<th>Communication quality</th><th>Service score change</th></tr><tr>'
            f'<td>{_html(_local_time(impact.latest_completed_at))}</td>'
            f'<td>{_html(actor)}</td>'
            f'<td>{_html(impact.latest_before)}</td>'
            f'<td>{_html(impact.latest_after)}</td>'
            f'<td>{_html(_metric_text(impact.latest_capacity_before, "", 1))} → '
            f'{_html(_metric_text(impact.latest_capacity_after, "", 1))}</td>'
            f'<td>{_html(_metric_text(impact.latest_latency_before, "ms", 0))} → '
            f'{_html(_metric_text(impact.latest_latency_after, "ms", 0))}</td>'
            f'<td>{_html(_metric_text(impact.latest_jitter_before, "ms", 1))} → '
            f'{_html(_metric_text(impact.latest_jitter_after, "ms", 1))}</td>'
            f'<td>{_html(_metric_text(impact.latest_cq_before, "", 2))} → '
            f'{_html(_metric_text(impact.latest_cq_after, "", 2))}</td>'
            f'<td>{_html(_metric_text(impact.latest_service_impact, "", 1))}</td>'
            '</tr></table>'
        )
    else:
        latest_impact = (
            '<div class="empty">No completed comparison of a 54GUC change '
            'in the last 14 days.</div>'
        )
    learned_contexts = ""
    charts = "".join(
        _svg_line_chart(points, **spec)
        for spec in (
            {"field_name": "capacity_points", "title": "Estimated capacity", "unit": "points", "color": "#4e79a7"},
            {"field_name": "latency_ms", "title": "Latency", "unit": "ms", "color": "#f28e2b"},
            {"field_name": "jitter_ms", "title": "Jitter", "unit": "ms", "color": "#e15759"},
            {"field_name": "communication_quality", "title": "Communication quality", "unit": "", "color": "#59a14f"},
            {"field_name": "congestion_pct", "title": "Congestion", "unit": "%", "color": "#b07aa1"},
            {"field_name": "internet_availability_pct", "title": "Internet availability", "unit": "%", "color": "#76b7b2"},
        )
    )
    contexts: list[SiteReportHistoryPoint] = []
    prior = ""
    for point in points:
        current = _history_context(point)
        if current != prior:
            contexts.append(point)
            prior = current
    context_rows = "".join(
        "<tr>"
        f"<td>{_html(_local_time(point.ended_at))}</td>"
        f"<td>{_html(point.network_type or '---')}</td>"
        f"<td>{_html(_history_context(point))}</td>"
        f"<td>{_html(f'{point.service_grade:.1f}' if point.service_grade is not None else '---')}</td>"
        "</tr>"
        for point in contexts[-128:]
    )
    change_rows = "".join(
        "<tr>"
        f"<td>{_html(_local_time(change.observed_at))}</td>"
        f"<td>{_html(change.actor)}</td>"
        f"<td>{_html(change.before)}</td>"
        f"<td>{_html(change.after)}</td>"
        f"<td>{_html(change.outcome)}</td>"
        f"<td>{_html(f'{change.service_grade:.1f}' if change.service_grade is not None else '---')}</td>"
        "</tr>"
        for change in history.changes
    )
    changes = (
        '<table class="events"><tr><th>Completed</th><th>Actor</th><th>Before</th>'
        '<th>After</th><th>Outcome</th><th>Grade</th></tr>'
        + change_rows
        + "</table>"
        if change_rows
        else '<div class="empty">No completed radio-change assessment in this window.</div>'
    )
    return (
        '<section class="history"><h2>2. Service history</h2>'
        f'<div class="subtitle">{history.requested_hours:g} {"hour" if history.requested_hours == 1 else "hours"} · '
        f'{len(points)} summary {"point" if len(points) == 1 else "points"} · {_html(_local_time(history.source_started_at))} '
        f'to {_html(_local_time(history.source_ended_at))}</div>'
        f'<div class="kpi-grid">{summary_html}</div>'
        '<h3>54GUC results — last 14 days</h3>'
        '<p>Service scores run from −3 to +3; higher is better. Improvements below are based on completed comparisons.</p>'
        f'<div class="kpi-grid">{impact_summary}</div>'
        f'{latest_impact}{learned_contexts}'
        f'<div class="chart-grid">{charts}</div>'
        '<h3>Band, cell and CA context</h3>'
        '<table class="events"><tr><th>Observed</th><th>Network</th>'
        f'<th>Configuration</th><th>Grade</th></tr>{context_rows}</table>'
        f'<h3>Assessed radio changes</h3>{changes}</section>'
    )


def build_foundation_site_report_html(snapshot: FoundationSiteReportSnapshot) -> str:
    if type(snapshot) is not FoundationSiteReportSnapshot:
        raise TypeError("foundation_site_report_snapshot_required")
    section_html: list[str] = []
    for section_number, section in enumerate(snapshot.sections, start=3):
        rows = "".join(
            "<tr><th>" + _html(field.label) + "</th><td>"
            + escape(_field_text(field), quote=True)
            + ("<small>" + _html(field.explanation) + "</small>" if field.explanation else "")
            + "</td></tr>"
            for field in section.fields
        )
        section_html.append(
            "<section class=\"detail-card\">"
            f"<h2>{section_number}. {_html(section.title)}</h2>"
            f"<table>{rows}</table></section>"
        )
    summary_specs = (
        ("Router", _summary_value(snapshot, "Device and network", "Model")),
        ("Provider", _summary_value(snapshot, "Device and network", "Provider")),
        ("Mobile connection", _summary_value(snapshot, "Connectivity now", "Radio connected")),
        ("Internet", _summary_value(snapshot, "Internet", "Internet status")),
        ("Network", _summary_value(snapshot, "Radio now", "Network")),
        ("Primary band", _summary_value(snapshot, "Radio now", "Primary band", band=True)),
        ("Allowed LTE bands", _summary_value(snapshot, "Radio now", "Allowed LTE bands", band_pool=True)),
        ("Download", _summary_value(snapshot, "Traffic now", "Download")),
        ("Upload", _summary_value(snapshot, "Traffic now", "Upload")),
        ("Signal clarity", _summary_value(snapshot, "Radio now", "Signal clarity (SINR)")),
        ("Congestion", _summary_value(snapshot, "Connection quality", "Network congestion")),
        ("Response time", _summary_value(snapshot, "Response times", "Internet response time")),
    )
    summary_html = "".join(
        "<div class=\"kpi\"><div class=\"value\">"
        + _html(value)
        + "</div><div class=\"label\">"
        + _html(label)
        + "</div></div>"
        for label, value in summary_specs
    )
    history_html = _history_report_html(snapshot.history)
    feature_rows = "".join(
        f"<tr><th>{_html(f.label)}</th><td>{_html(f.status)}<small>{_html(f.explanation)}</small></td></tr>"
        for f in snapshot.features)
    features_html = ('<section class="detail-card features"><h2>What this router supports</h2>'
        '<p>Features available through 54GUC for this router. This list is built without running Check router.</p>'
        f'<table>{feature_rows}</table></section>')
    if snapshot.events:
        event_rows = "".join(
            "<tr>"
            f"<td>{_html(_local_time(event.ts))}</td>"
            f"<td>{_html(event.severity)}</td>"
            f"<td>{_html(event.summary)}</td>"
            "</tr>"
            for event in snapshot.events
        )
        events_html = (
            "<table class=\"events\"><tr><th>Time</th><th>Status</th>"
            "<th>What happened</th></tr>" + event_rows + "</table>"
        )
    else:
        events_html = (
            "<div class=\"empty\">No current-session diagnostic events were "
            "recorded inside the selected window.</div>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>54GUC Site Report</title>
<style>
 :root {{ --bg:#ffffff; --fg:#1a1a1a; --muted:#666; --accent:#4e79a7;
          --border:#e0e0e0; --section:#fafafa; --table:#f0f0f0; --note:#eef5fb; }}
 @media (prefers-color-scheme: dark) {{ :root {{ --bg:#1a1a1a; --fg:#e0e0e0;
          --muted:#999; --accent:#6fa8dc; --border:#333; --section:#222;
          --table:#2a2a2a; --note:#1f2a34; }} }}
* {{ box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
        color:var(--fg); background:var(--bg); margin:0; padding:24px 16px; line-height:1.45; }}
main {{ max-width:1040px; margin:auto; }}
h1 {{ margin:0 0 4px; color:var(--accent); font-size:1.75rem; }}
.subtitle {{ color:var(--muted); margin-bottom:18px; }}
.notice,.empty {{ background:var(--note); border:1px solid var(--border); padding:12px;
                  margin:12px 0; border-radius:6px; }}
.notice {{ border-left:5px solid var(--accent); }}
.kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr));
             gap:12px; margin:14px 0 22px; }}
.kpi {{ background:var(--section); border:1px solid var(--border); border-radius:6px;
        padding:12px 8px; text-align:center; min-height:78px; }}
.kpi .value {{ color:var(--accent); font-size:1.25rem; font-weight:700;
               overflow-wrap:anywhere; }}
.kpi .label {{ color:var(--muted); font-size:.78rem; margin-top:3px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr)); gap:14px; }}
.chart-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));
               gap:14px; margin:14px 0 20px; }}
.chart-card {{ background:var(--section); border:1px solid var(--border); padding:10px;
               border-radius:6px; break-inside:avoid; }}
.chart-card h3,.history h3 {{ margin:4px 0 8px; font-size:.95rem; }}
.chart-card svg {{ width:100%; height:auto; display:block; }}
.svg-label {{ fill:var(--muted); font-size:10px; }}
.detail-card {{ background:var(--section); border:1px solid var(--border); padding:14px;
                border-radius:6px; break-inside:avoid; }}
h2 {{ font-size:1.08rem; margin:0 0 10px; padding-bottom:5px;
      border-bottom:2px solid var(--accent); }}
table {{ border-collapse:collapse; width:100%; font-size:.9rem; }}
th,td {{ border:1px solid var(--border); padding:7px 9px; text-align:left; vertical-align:top; }}
td,th {{ overflow-wrap:anywhere; }}
small {{ display:block; color:var(--muted); font-size:.8rem; margin-top:4px; }}
.features {{ margin:18px 0; }}
.history {{ overflow-x:auto; }}
th {{ width:48%; color:var(--fg); background:var(--table); font-weight:600; }}
tr:nth-child(even) td {{ background:var(--bg); }}
.events th {{ width:auto; white-space:nowrap; }}
.events td:last-child {{ word-break:break-word; }}
details {{ margin:14px 0; border:1px solid var(--border); border-radius:6px;
           padding:10px 12px; color:var(--muted); }}
summary {{ cursor:pointer; color:var(--fg); font-weight:600; }}
footer {{ color:var(--muted); margin-top:22px; font-size:.82rem; text-align:center;
          border-top:1px solid var(--border); padding-top:12px; }}
@media print {{ body {{ max-width:none; padding:0; font-size:10pt; }}
               .detail-card,.kpi,table {{ break-inside:avoid; }} }}
</style>
</head>
<body><main>
<h1>54GUC Site Report</h1>
<div class="subtitle">Your connection, router features and recent service history · {_html(_local_time(snapshot.generated_at))}</div>
<div class="notice"><strong>About this report:</strong> current readings were saved at {_html(_local_time(snapshot.current_state_published_at))}. Charts cover the selected last {snapshot.window_hours:g} {'hour' if snapshot.window_hours == 1 else 'hours'}. Each unavailable reading has its own explanation. Detailed diagnostic evidence is available in Log Checker.</div>
<h2>1. Your connection at a glance</h2>
<div class="kpi-grid">{summary_html}</div>
{history_html}
{features_html}
<div class="grid">{''.join(section_html)}</div>
<section class="detail-card" style="margin-top:14px"><h2>{len(snapshot.sections) + 3}. Events from this launch</h2>{events_html}</section>
<footer>54GUC Site Report · Times use this PC's local time · Monitoring started {_html(_local_time(snapshot.current_run_started_at))}</footer>
</main></body></html>"""


def enforce_foundation_site_report_retention(
    runtime_dir: str | Path,
    *,
    now: float | None = None,
    retention_days: int = FOUNDATION_SITE_REPORT_RETENTION_DAYS,
) -> tuple[Path, ...]:
    root = Path(runtime_dir)
    cutoff = float(time.time() if now is None else now) - max(1, int(retention_days)) * 86400.0
    removed: list[Path] = []
    for path in sorted(root.glob("site_report_*.html")):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError as exc:
            log.warning("Foundation Site Report retention failed for %s: %s", path.name, exc)
    return tuple(removed)


def generate_foundation_site_report(
    *,
    runtime_dir: str | Path,
    hours: float,
    history: SiteReportHistoryProjection,
    open_browser: bool = True,
    now: float | None = None,
) -> Path:
    """Create one current-session Foundation report and return its path."""

    root = Path(runtime_dir)
    generated_at = float(time.time() if now is None else now)
    snapshot = load_foundation_site_report_snapshot(
        root,
        hours=hours,
        history=history,
        now=generated_at,
    )
    enforce_foundation_site_report_retention(root, now=generated_at)
    html = build_foundation_site_report_html(snapshot)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(generated_at))
    milliseconds = int((generated_at - int(generated_at)) * 1000.0)
    report_path = root / f"site_report_{stamp}_{milliseconds:03d}.html"
    try:
        atomic_write_text(report_path, html, encoding="utf-8", logger=log)
    except OSError as exc:
        raise RuntimeError(f"foundation_site_report_write_failed:{type(exc).__name__}") from exc
    if open_browser:
        try:
            webbrowser.open(report_path.as_uri())
        except Exception as exc:
            log.warning("Foundation Site Report browser open failed: %s", exc)
    return report_path


__all__ = [
    "FOUNDATION_SITE_REPORT_MAX_EVENTS",
    "FOUNDATION_SITE_REPORT_OWNER",
    "FOUNDATION_SITE_REPORT_SCHEMA",
    "FOUNDATION_SITE_REPORT_RETENTION_DAYS",
    "FoundationSiteReportSnapshot",
    "build_foundation_site_report_html",
    "enforce_foundation_site_report_retention",
    "generate_foundation_site_report",
    "load_foundation_site_report_snapshot",
]
