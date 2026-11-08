"""Product-hardening status projection.

This module is read-only. It consumes existing owner status surfaces and
publishes one bounded hardening view for runtime health, support bundles, and
fault registers. It does not write sidecars, touch SQLite, call modem I/O, or
render GUI state.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

OWNER = "fault_supervisor.product_hardening"
SCHEMA_VERSION = "product_hardening.v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "degraded", "failed"}
    return bool(value)


def _add_issue(
    issues: list[dict[str, Any]],
    *,
    component: str,
    owner: str,
    status: str,
    detail: str = "",
    severity: str = "warning",
    evidence: Mapping[str, Any] | None = None,
) -> None:
    item: dict[str, Any] = {
        "component": str(component or ""),
        "owner": str(owner or ""),
        "status": str(status or "degraded"),
        "severity": str(severity or "warning"),
    }
    if detail:
        item["detail"] = str(detail)
    if isinstance(evidence, Mapping) and evidence:
        item["evidence"] = dict(evidence)
    issues.append(item)


def _last_failure_detail(payload: Mapping[str, Any]) -> str:
    last = _mapping(payload.get("last_failure"))
    if last:
        for key in ("error", "message", "reason", "stage", "operation"):
            value = str(last.get(key) or "").strip()
            if value:
                return value[:180]
    for key in ("error", "message", "reason"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value[:180]
    return ""


def _calibration_db_health_from_runtime_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    direct = _mapping(payload.get("calibration_db_write_health"))
    if direct:
        return direct
    durable = _mapping(payload.get("durable_memory_runtime_status"))
    if not durable:
        brain = _mapping(payload.get("brain"))
        learning = _mapping(brain.get("learning"))
        durable = _mapping(learning.get("persistence"))
    if not durable:
        return {}
    direct = _mapping(durable.get("calibration_db_write_health"))
    if direct:
        return direct
    io_status = _mapping(durable.get("io"))
    return _mapping(io_status.get("calibration_db_write_health"))


def build_product_hardening_status(
    payload: Mapping[str, Any] | None = None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Build the Chapter 06 hardening projection from owner status surfaces."""

    source = dict(payload or {})
    issues: list[dict[str, Any]] = []

    json_health = _mapping(source.get("json_sidecar_write_health"))
    if json_health and (
        _bool(json_health.get("degraded"))
        or _integer(json_health.get("active_failure_count")) > 0
    ):
        _add_issue(
            issues,
            component="runtime_json_sidecars",
            owner="arch4.runtime_sidecar_health",
            status="degraded",
            detail=_last_failure_detail(json_health),
            evidence={
                "active_failure_count": _integer(json_health.get("active_failure_count")),
                "write_failure_count": _integer(json_health.get("write_failure_count")),
            },
        )

    queue = _mapping(source.get("runtime_sidecar_write_queue"))
    if queue and (
        _integer(queue.get("failures")) > 0
        or _integer(queue.get("enqueue_failures")) > 0
        or _integer(queue.get("dropped")) > 0
        or _integer(queue.get("requeued")) > 0
    ):
        _add_issue(
            issues,
            component="runtime_sidecar_writer_queue",
            owner=str(queue.get("owner") or "runtime_sidecar_writer_queue"),
            status="degraded",
            detail=_last_failure_detail(queue),
            evidence={
                "queued": _integer(queue.get("queued")),
                "failures": _integer(queue.get("failures")),
                "enqueue_failures": _integer(queue.get("enqueue_failures")),
                "requeued": _integer(queue.get("requeued")),
                "dropped": _integer(queue.get("dropped")),
            },
        )

    durable = _mapping(source.get("durable_memory_runtime_status"))
    if durable and (
        _bool(durable.get("load_failed"))
        or _bool(durable.get("checkpoint_failed"))
        or _integer(durable.get("event_journal_replay_error_count")) > 0
    ):
        _add_issue(
            issues,
            component="durable_memory",
            owner="arch4.brain.memory_persistence",
            status="degraded",
            detail=str(
                durable.get("load_failed_reason")
                or durable.get("checkpoint_failed_reason")
                or "memory persistence degraded"
            )[:180],
            evidence={
                "load_failed": _bool(durable.get("load_failed")),
                "checkpoint_failed": _bool(durable.get("checkpoint_failed")),
                "event_journal_replay_error_count": _integer(
                    durable.get("event_journal_replay_error_count")
                ),
            },
        )

    calibration_db = _calibration_db_health_from_runtime_status(source)
    if calibration_db and (
        _bool(calibration_db.get("degraded"))
        or _integer(calibration_db.get("consecutive_failure_count")) > 0
    ):
        _add_issue(
            issues,
            component="calibration_db_writes",
            owner=str(calibration_db.get("owner") or "arch4.brain.memory_calibration_store"),
            status="degraded",
            detail=_last_failure_detail(calibration_db),
            evidence={
                "failure_count": _integer(calibration_db.get("failure_count")),
                "consecutive_failure_count": _integer(
                    calibration_db.get("consecutive_failure_count")
                ),
                "lock_timeout_count": _integer(calibration_db.get("lock_timeout_count")),
                "sqlite_locked_count": _integer(calibration_db.get("sqlite_locked_count")),
                "attempt_count": _integer(calibration_db.get("attempt_count")),
                "success_count": _integer(calibration_db.get("success_count")),
            },
        )

    runtime_faults = _mapping(source.get("runtime_status_faults"))
    if runtime_faults and (
        _bool(runtime_faults.get("degraded"))
        or _integer(runtime_faults.get("active_fault_count")) > 0
    ):
        _add_issue(
            issues,
            component="runtime_status_faults",
            owner="arch4.engine_runtime_status",
            status="degraded",
            detail=_last_failure_detail(runtime_faults),
            evidence={
                "active_fault_count": _integer(runtime_faults.get("active_fault_count")),
            },
        )

    gui_metrics = _mapping(source.get("gui_metrics"))
    gui_lane = _mapping(gui_metrics.get("gui_export_lane"))
    lane_status = str(gui_lane.get("status") or "").strip().lower()
    if gui_lane and (lane_status in {"failed", "fault", "blocked"} or _bool(gui_lane.get("status_error"))):
        _add_issue(
            issues,
            component="gui_export_lane",
            owner=str(gui_lane.get("owner") or "gui_export_lane"),
            status="degraded",
            detail=_last_failure_detail(gui_lane),
            evidence={"status": lane_status or str(gui_lane.get("status") or "")},
        )

    status = "degraded" if issues else "healthy"
    ts = float(time.time() if now is None else now)
    return {
        "schema_version": SCHEMA_VERSION,
        "owner": OWNER,
        "status": status,
        "degraded": bool(issues),
        "issue_count": len(issues),
        "issues": issues[:16],
        "ts": ts,
        "source": OWNER,
        "input_summary": {
            "json_sidecar_health": bool(json_health),
            "runtime_sidecar_queue": bool(queue),
            "durable_memory": bool(durable),
            "calibration_db": bool(calibration_db),
            "runtime_faults": bool(runtime_faults),
            "gui_export_lane": bool(gui_lane),
        },
    }
