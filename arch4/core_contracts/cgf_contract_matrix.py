"""Static CGF runtime register contract matrix."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


CGF_REGISTER_CONTRACT_MATRIX: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "modem_live_matrix": MappingProxyType(
            {
                "owner": "54guc.modem_live.truth_matrix",
                "producer": "Modem Live accepted immutable truth generation",
                "consumer": "Core accepted-root transaction and future accepted-root customers",
                "clock_event_source": "Modem Live accepted session/sample/slow event",
                "stale_invalid_policy": "invalid matrix rejected atomically; missing cells remain explicit",
                "evidence_artifact": "current_state.json; ngui_page_truth.json",
            }
        ),
        "latency": MappingProxyType(
            {
                "owner": "signal_processing_matrix",
                "producer": "Signal Processing Matrix from raw RTT observations",
                "consumer": "quality display and congestion",
                "clock_event_source": "accepted sample plus RTT probe event",
                "stale_invalid_policy": "RTT state reports stale/invalid without blocking other registers",
                "evidence_artifact": "current_state.json; ngui_page_truth.json",
            }
        ),
        "quality": MappingProxyType(
            {
                "owner": "signal_processing_matrix",
                "producer": "Signal Processing Matrix",
                "consumer": "congestion, footer, charts",
                "clock_event_source": "accepted sample event",
                "stale_invalid_policy": "one owner for RTT, jitter, and congestion state",
                "evidence_artifact": "current_state.json; ngui_page_truth.json",
            }
        ),
        "ca": MappingProxyType(
            {
                "owner": "signal_processing_matrix",
                "producer": "Signal Processing Matrix",
                "consumer": "CA/CART/CAST display and charts",
                "clock_event_source": "accepted sample event",
                "stale_invalid_policy": "sentinels cannot be presented as real B0/PCI0",
                "evidence_artifact": "current_state.json; ngui_page_truth.json",
            }
        ),
        "health": MappingProxyType(
            {
                "owner": "54guc.modem_live.current_health",
                "producer": "Modem Live typed current-health publication port",
                "consumer": "Core typed decisions, Foundation evidence, NGUI accepted projection",
                "clock_event_source": "Modem Live supervision event",
                "stale_invalid_policy": "owner observation timestamp retained; malformed or foreign namespace facts rejected",
                "evidence_artifact": "ModemLiveCurrentHealthPublication.v1; current_state.json",
            }
        ),
        "runtime_clock": MappingProxyType(
            {
                "owner": "runtime_clock",
                "producer": "runtime clock service",
                "consumer": "Foundation jobs, Core, NGUI uptime",
                "clock_event_source": "100 ms runtime clock",
                "stale_invalid_policy": "clock stall reported while uptime holds last value",
                "evidence_artifact": "runtime_health.json; ngui_page_truth.json",
            }
        ),
        "mutation": MappingProxyType(
            {
                "owner": "action_runtime",
                "producer": "Core mutation policy",
                "consumer": "command admission and controls",
                "clock_event_source": "command event and current sample",
                "stale_invalid_policy": "active mutation protects only conflicting controls",
                "evidence_artifact": "core command journal; current_state.json",
            }
        ),
        "action": MappingProxyType(
            {
                "owner": "command_authority_projection",
                "producer": "Core Command Authority projection",
                "consumer": "NGUI manual-action panel and future consumers",
                "clock_event_source": "command lifecycle events",
                "stale_invalid_policy": "only terminal receipt can produce success/failure",
                "evidence_artifact": "core command journal; ngui_page_truth.json",
            }
        ),
        "action_auxiliary": MappingProxyType(
            {
                "owner": "action_runtime_auxiliary",
                "producer": "Core action helper state",
                "consumer": "NGUI controls and diagnostics",
                "clock_event_source": "command/runtime events",
                "stale_invalid_policy": "cannot override action truth",
                "evidence_artifact": "current_state.json",
            }
        ),
        "operational_status": MappingProxyType(
            {
                "owner": "operational_service_status",
                "producer": "Foundation operational services",
                "consumer": "device/tools buttons",
                "clock_event_source": "operational event",
                "stale_invalid_policy": "unsupported or rejected state remains visible",
                "evidence_artifact": "cgf_evidence.sqlite3; current_state.json",
            }
        ),
        "downstream_health": MappingProxyType(
            {
                "owner": "downstream_health",
                "producer": "Foundation downstream diagnostics",
                "consumer": "status page and safe start",
                "clock_event_source": "runtime diagnostic event",
                "stale_invalid_policy": "advisory unless directly blocking required runtime",
                "evidence_artifact": "runtime_health.json; ngui_safe_start_advisory.json",
            }
        ),
        "internet_reachability": MappingProxyType(
            {
                "owner": "54guc.core.action_reachability",
                "producer": "Core continuous reachability timeline",
                "consumer": "Core emergency handling, Referee intervals, NGUI graph",
                "clock_event_source": "Modem Live RTT register revision",
                "stale_invalid_policy": "missing or stale probes are unavailable and never invented as success",
                "evidence_artifact": "CoreInternetReachabilityRegister.v3; CoreReachabilityIntervalReceipt.v2",
            }
        ),
    }
)


CGF_MODEM_LIVE_LINE_CONTRACT_MATRIX: Mapping[
    str, Mapping[str, str]
] = MappingProxyType(
    {
        "identity": MappingProxyType(
            {
                "owner": "54guc.modem_live.truth_matrix",
                "producer": "Modem Live accepted identity event",
                "consumer": "Core, Foundation status, NGUI, licensing, future Intelligence",
                "clock_event_source": "accepted modem/session identity event",
                "stale_invalid_policy": "missing exact identity blocks control login but not monitoring",
                "evidence_artifact": "current_state.json; ngui_page_truth.json",
            }
        ),
        "capability": MappingProxyType(
            {
                "owner": "54guc.modem_live.truth_matrix",
                "producer": "Modem Live effective session capability",
                "consumer": "Core admission, NGUI, Scout evidence",
                "clock_event_source": "session capability/profile event",
                "stale_invalid_policy": "probe unavailability is explicit and cannot erase effective capability",
                "evidence_artifact": "current_state.json; capability discrepancy evidence",
            }
        ),
        "radio": MappingProxyType(
            {
                "owner": "54guc.modem_live.truth_matrix",
                "producer": "Modem Live accepted cellular state",
                "consumer": "Core target verification, NGUI, future Intelligence",
                "clock_event_source": "accepted fast modem sample",
                "stale_invalid_policy": "invalid cells cannot become target confirmation",
                "evidence_artifact": "current_state.json; ngui_page_truth.json",
            }
        ),
        "rf": MappingProxyType(
            {
                "owner": "54guc.modem_live.truth_matrix",
                "producer": "Modem Live accepted RF and scheduler state",
                "consumer": "Signal Compute, NGUI charts, future Intelligence",
                "clock_event_source": "accepted fast modem sample",
                "stale_invalid_policy": "missing or invalid RF cells are not reused as new samples",
                "evidence_artifact": "current_state.json; ngui_page_truth.json",
            }
        ),
        "traffic": MappingProxyType(
            {
                "owner": "54guc.modem_live.truth_matrix",
                "producer": "Modem Live accepted traffic state",
                "consumer": "NGUI graphs, Foundation budget, future Intelligence",
                "clock_event_source": "accepted fast or traffic-volume event",
                "stale_invalid_policy": "validity, reason, rate and counters remain one generation",
                "evidence_artifact": "current_state.json; ngui_page_truth.json",
            }
        ),
        "slow": MappingProxyType(
            {
                "owner": "54guc.modem_live.truth_matrix",
                "producer": "Modem Live accepted slow/maintenance state",
                "consumer": "NGUI Device/Dashboard and diagnostics",
                "clock_event_source": "accepted slow/maintenance event",
                "stale_invalid_policy": "missing fields render unknown and cannot imply action outcome",
                "evidence_artifact": "current_state.json; ngui_page_truth.json",
            }
        ),
        "timing": MappingProxyType(
            {
                "owner": "54guc.modem_live.truth_matrix",
                "producer": "Modem Live accepted sample timing line",
                "consumer": "diagnostics, site report and source-soak proof",
                "clock_event_source": "accepted fast modem sample",
                "stale_invalid_policy": "missing timing fails timing proof without blocking unrelated live fields",
                "evidence_artifact": "current_state.json; source-soak manifest",
            }
        ),
        "health": MappingProxyType(
            {
                "owner": "54guc.modem_live.truth_matrix",
                "producer": "Modem Live communication and quiet-window state",
                "consumer": "Core, Foundation health and NGUI",
                "clock_event_source": "accepted sample or Modem Live state event",
                "stale_invalid_policy": "communication faults are explicit and isolated from valid retained lines",
                "evidence_artifact": "current_state.json; runtime_health.json",
            }
        ),
    }
)


CGF_REGISTER_CONTRACT_REQUIRED_FIELDS: tuple[str, ...] = (
    "owner",
    "producer",
    "consumer",
    "clock_event_source",
    "stale_invalid_policy",
    "evidence_artifact",
)


__all__ = [
    "CGF_MODEM_LIVE_LINE_CONTRACT_MATRIX",
    "CGF_REGISTER_CONTRACT_MATRIX",
    "CGF_REGISTER_CONTRACT_REQUIRED_FIELDS",
]
