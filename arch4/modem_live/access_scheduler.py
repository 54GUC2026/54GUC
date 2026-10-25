"""Schedule Modem Live access lanes and record each decision.

This module has no ``arch4.*`` imports. Acquisition supplies timing and state.
This module defines lane names, priorities, and run, skip, or defer records.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ModemLiveLaneSpec:
    name: str
    priority: int
    purpose: str
    output_surface: str
    serial_access: bool = True


LANE_SPECS: tuple[ModemLiveLaneSpec, ...] = (
    ModemLiveLaneSpec("recovery", 0, "restore modem access or radio service", "recovery_state"),
    ModemLiveLaneSpec("disconnect", 0, "close a failed or replaced session", "session_state"),
    ModemLiveLaneSpec("event_write", 5, "operator or brain modem mutation", "action_result"),
    ModemLiveLaneSpec("session", 10, "open or renew one modem session", "session_state"),
    ModemLiveLaneSpec("readback", 15, "post-write confirmation readback", "action_result"),
    ModemLiveLaneSpec("startup", 20, "first required session truth", "runtime_identity"),
    ModemLiveLaneSpec("fast_live", 25, "RF/traffic/RTT live sample", "live_modem_register"),
    ModemLiveLaneSpec("sms", 35, "SMS inbox read or typed SMS action", "sms_state"),
    ModemLiveLaneSpec("slow_live", 45, "slow live enrichment", "live_modem_register"),
    ModemLiveLaneSpec("maintenance", 55, "identity, neighbors, and slow maintenance", "live_modem_register"),
    ModemLiveLaneSpec("boot_static", 65, "startup/static capability discovery", "runtime_identity"),
    ModemLiveLaneSpec("scout", 75, "bounded profile discovery", "scout_state"),
    ModemLiveLaneSpec("export", 90, "report/support/status snapshot projection", "support_bundle"),
)

_LANE_BY_NAME = {spec.name: spec for spec in LANE_SPECS}
MODEM_LIVE_ACCESS_LANES = frozenset(_LANE_BY_NAME)

_SOURCE_LANE = {
    "accepted_generation": "slow_live",
    "action": "event_write",
    "action_readback": "readback",
    "band_status": "slow_live",
    "bootstrap": "boot_static",
    "capabilities": "boot_static",
    "capability": "slow_live",
    "clocked_modem_register": "slow_live",
    "connectivity": "slow_live",
    "current_state": "export",
    "device_identity": "maintenance",
    "execute_lever": "slow_live",
    "export": "export",
    "health_probe": "slow_live",
    "input_snapshot": "export",
    "internet_emergency_verification": "fast_live",
    "live_register": "fast_live",
    "maintenance": "maintenance",
    "modem_live.set_wireless_radio": "slow_live",
    "modem_live_pband_release": "slow_live",
    "modem_live_pband_sequence": "slow_live",
    "operator_connect": "session",
    "operator_disconnect": "disconnect",
    "operator_status": "slow_live",
    "pband_sequence": "event_write",
    "protected_live_lane_status": "export",
    "provider_identity": "slow_live",
    "read_live_modem_snapshot": "slow_live",
    "readback": "readback",
    "recovery": "recovery",
    "report": "export",
    "routeros_at": "slow_live",
    "routeros_watchdog": "maintenance",
    "rtt_cadence": "fast_live",
    "runtime_action": "event_write",
    "sms": "sms",
    "startup": "boot_static",
    "startup_config": "boot_static",
    "startup_connect": "session",
    "startup_live_phase": "fast_live",
    "startup_live_snapshot": "slow_live",
    "status_snapshot": "slow_live",
    "support_bundle": "export",
    "threaded_read": "slow_live",
    "toolbox_rtt": "slow_live",
    "traffic_presence": "slow_live",
    "wireless_radio": "slow_live",
    "zte_goform": "slow_live",
}


def lane_priority(lane: Any) -> int:
    lane_key = str(lane or "").strip()
    spec = _LANE_BY_NAME.get(lane_key)
    return int(spec.priority if spec is not None else 100)


def classify_access_lane(*, source: Any, operation: Any = "") -> str:
    source_key = str(source or "").strip().lower()
    operation_key = str(operation or "").strip().lower()
    lane = _SOURCE_LANE.get(source_key)
    if lane is None:
        raise ValueError(f"modem_live_access_source_unclassified:{source_key or '<empty>'}")
    if source_key == "internet_emergency_verification" and operation_key != "verify_internet_reachability":
        raise ValueError("modem_live_internet_verification_operation_required")
    if source_key in {"live_register", "rtt_cadence"}:
        if operation_key.startswith("recover_session"):
            return "readback"
    return lane


def classify_endpoint_class(*, source: Any, operation: Any = "") -> str:
    source_key = str(source or "").strip().lower()
    operation_key = str(operation or "").strip().lower()
    if source_key == "internet_emergency_verification":
        classify_access_lane(source=source, operation=operation)
        return "fast_live_rtt_read"
    if source_key in {"live_register", "rtt_cadence"}:
        if operation_key.startswith("recover_session"):
            return "session_recovery_readback"
        if source_key == "rtt_cadence":
            return "fast_live_rtt_read"
        if operation_key in {"read_actionable_modem_input", "read_modem_input"}:
            return "fast_live_sample_read"
        return "fast_live_read"
    if source_key in {"runtime_action", "action", "pband_sequence"}:
        if operation_key.startswith(("get_", "read_")):
            return "event_write_support_read"
        return "event_write_mutation"
    if source_key in {"readback", "action_readback"} or "readback" in operation_key:
        return "post_write_readback"
    if source_key in {"maintenance", "device_identity"}:
        if "neighbor" in operation_key:
            return "slow_live_neighbors_read"
        if "rtt" in operation_key:
            return "slow_live_rtt_read"
        if "identity" in operation_key or "info" in operation_key:
            return "slow_live_identity_read"
        return "slow_live_maintenance_read"
    if source_key in {"startup", "bootstrap", "capabilities", "startup_config"}:
        return "boot_static_read"
    if source_key in {"input_snapshot"}:
        return "input_snapshot_aux_read"
    if source_key in {
        "protected_live_lane_status",
        "input_snapshot",
        "support_bundle",
        "current_state",
        "report",
        "export",
    }:
        return "export_snapshot"
    return f"{classify_access_lane(source=source, operation=operation)}_access"


class ModemLiveAccessScheduler:
    """Store Modem Live lane definitions and make scheduling decisions."""

    def __init__(self, *, history_limit: int = 80) -> None:
        self.history_limit = max(1, int(history_limit or 80))
        self._sequence = 0
        self._history: list[dict[str, Any]] = []
        self._lanes: Dict[str, Dict[str, Any]] = {
            spec.name: {
                "name": spec.name,
                "priority": int(spec.priority),
                "purpose": spec.purpose,
                "output_surface": spec.output_surface,
                "serial_access": bool(spec.serial_access),
                "run_count": 0,
                "skip_count": 0,
                "defer_count": 0,
                "supersede_count": 0,
                "last_decision": "",
                "last_reason": "",
                "last_ts": 0.0,
                "last_run_ts": 0.0,
                "last_skip_ts": 0.0,
                "last_defer_ts": 0.0,
            }
            for spec in LANE_SPECS
        }

    @staticmethod
    def classify(*, source: Any, operation: Any = "") -> str:
        return classify_access_lane(source=source, operation=operation)

    @staticmethod
    def endpoint_class(*, source: Any, operation: Any = "") -> str:
        return classify_endpoint_class(source=source, operation=operation)

    @staticmethod
    def priority_for(lane: Any) -> int:
        return lane_priority(lane)

    def _record(
        self,
        *,
        lane: str,
        decision: str,
        reason: str,
        now: float,
        requested: bool = False,
        due: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any]:
        lane_name = str(lane or "").strip()
        if lane_name not in self._lanes:
            raise ValueError(
                f"modem_live_access_lane_unclassified:{lane_name or '<empty>'}"
            )
        self._sequence += 1
        row = self._lanes[lane_name]
        row["last_decision"] = str(decision or "")
        row["last_reason"] = str(reason or "")
        row["last_ts"] = float(now or 0.0)
        if decision == "run":
            row["run_count"] = int(row.get("run_count", 0) or 0) + 1
            row["last_run_ts"] = float(now or 0.0)
        elif decision == "skip":
            row["skip_count"] = int(row.get("skip_count", 0) or 0) + 1
            row["last_skip_ts"] = float(now or 0.0)
        elif decision == "defer":
            row["defer_count"] = int(row.get("defer_count", 0) or 0) + 1
            row["last_defer_ts"] = float(now or 0.0)
        elif decision == "supersede":
            row["supersede_count"] = int(row.get("supersede_count", 0) or 0) + 1
            row["last_supersede_ts"] = float(now or 0.0)
        event = {
            "seq": int(self._sequence),
            "ts": float(now or 0.0),
            "lane": lane_name,
            "priority": int(100 if row.get("priority") is None else row.get("priority")),
            "decision": str(decision or ""),
            "reason": str(reason or ""),
            "requested": bool(requested),
            "due": bool(due),
        }
        if metadata:
            event["metadata"] = dict(metadata)
        self._history.append(dict(event))
        self._history = self._history[-self.history_limit :]
        return dict(event)

    def record_run(
        self,
        lane: str,
        *,
        now: float,
        reason: str = "due",
        requested: bool = False,
        due: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self._record(
            lane=lane,
            decision="run",
            reason=reason,
            now=now,
            requested=requested,
            due=due,
            metadata=metadata,
        )

    def record_skip(
        self,
        lane: str,
        *,
        now: float,
        reason: str,
        requested: bool = False,
        due: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self._record(
            lane=lane,
            decision="skip",
            reason=reason,
            now=now,
            requested=requested,
            due=due,
            metadata=metadata,
        )

    def record_defer(
        self,
        lane: str,
        *,
        now: float,
        reason: str,
        requested: bool = False,
        due: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self._record(
            lane=lane,
            decision="defer",
            reason=reason,
            now=now,
            requested=requested,
            due=due,
            metadata=metadata,
        )

    def record_supersede(
        self,
        lane: str,
        *,
        now: float,
        reason: str,
        requested: bool = False,
        due: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self._record(
            lane=lane,
            decision="supersede",
            reason=reason,
            now=now,
            requested=requested,
            due=due,
            metadata=metadata,
        )

    def decide_fast_live(self, *, now: float) -> dict[str, Any]:
        return self.record_run("fast_live", now=now, reason="due", due=True)

    def decide_maintenance(
        self,
        *,
        now: float,
        requested: bool,
        due_by_interval: bool,
        fast_margin_ok: bool,
        already_running: bool,
        event_write_pending: bool,
        interval_s: float,
        age_s: float,
    ) -> dict[str, Any]:
        due = bool(requested or due_by_interval)
        metadata = {
            "interval_s": round(float(interval_s or 0.0), 3),
            "age_s": round(max(0.0, float(age_s or 0.0)), 3),
        }
        if not due:
            return self.record_defer(
                "maintenance",
                now=now,
                reason="not_due",
                requested=requested,
                due=False,
                metadata=metadata,
            )
        if event_write_pending:
            return self.record_defer(
                "maintenance",
                now=now,
                reason="event_write_pending",
                requested=requested,
                due=True,
                metadata=metadata,
            )
        if not fast_margin_ok:
            return self.record_skip(
                "maintenance",
                now=now,
                reason="fast_live_priority_margin",
                requested=requested,
                due=True,
                metadata=metadata,
            )
        if already_running:
            return self.record_skip(
                "maintenance",
                now=now,
                reason="maintenance_already_running",
                requested=requested,
                due=True,
                metadata=metadata,
            )
        return self.record_run(
            "maintenance",
            now=now,
            reason="requested" if requested else "interval_due",
            requested=requested,
            due=True,
            metadata=metadata,
        )

    def status(self) -> dict[str, Any]:
        return {
            "schema_version": "modem_live_access_scheduler.v1",
            "owner": "modem_live",
            "sequence": int(self._sequence),
            "lanes": {name: dict(value) for name, value in sorted(self._lanes.items())},
            "recent": [dict(item) for item in self._history[-20:]],
        }
