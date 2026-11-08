"""Execute the Core runtime clock."""

from __future__ import annotations

import threading
import time
from typing import Any

from arch4.runtime_clock import (
    ClockedJobOutcome,
    ClockedRuntimeService,
    ClockedScheduler,
    ClockedSkipPolicy,
)
from arch4.runtime_clock_worker_fabric import ClockJobWorkerFabric
from arch4.modem_live.publication import (
    ModemLiveRuntimeSupervisionPort,
    require_modem_live_runtime_supervision_port,
)
from arch4.core.action_status_service import CoreActionStatusService
from arch4.core.command_lifecycle_service import CoreCommandLifecycleService
from arch4.fault_supervisor import (
    FaultBoundaryClass,
    FaultSeverity,
    require_fault_supervisor,
)

INTERNAL_RUNTIME_CLOCK_STALE_MULTIPLIER = 5.0
INTERNAL_RUNTIME_CLOCK_MIN_STALE_S = 1.0
MODEM_LIVE_REFRESH_JOB_KEY = "modem_live:refresh_registers"
RUNTIME_STATUS_DECISION_JOB_KEY = "runtime:status_decision_snapshot"
CORE_DOWNSTREAM_DIAGNOSTICS_JOB_KEY = "core:downstream_diagnostics"
_CLOCK_JOB_WORKER_EXECUTION_TOKEN = object()
CORE_COMMAND_LIFECYCLE_JOB_KEY = "core:command_lifecycle"


def advance_runtime_clock(engine: Any) -> None:
    lock = getattr(engine, "_runtime_clock_advance_lock", None)
    if lock is None:
        lock = threading.RLock()
        setattr(engine, "_runtime_clock_advance_lock", lock)
    acquired = lock.acquire(blocking=False)
    if not acquired:
        engine._runtime_clock_last_skip_reason = "advance_already_running"
        return
    try:
        advance_runtime_clock_locked(engine)
    finally:
        lock.release()


def runtime_clock_internal_service_fresh(engine: Any, *, now: float | None = None) -> bool:
    status = getattr(engine, "_runtime_clock_internal_service_status", None)
    if not isinstance(status, dict) or not status:
        return False
    if not bool(status.get("active", False)):
        return False
    try:
        last_run_ts = float(status.get("last_run_ts", 0.0) or 0.0)
    except Exception:
        last_run_ts = 0.0
    if last_run_ts <= 0.0:
        return False
    try:
        interval_s = float(status.get("interval_s", 0.0) or 0.0)
    except Exception:
        interval_s = 0.0
    stale_after_s = max(
        INTERNAL_RUNTIME_CLOCK_MIN_STALE_S,
        interval_s * INTERNAL_RUNTIME_CLOCK_STALE_MULTIPLIER,
    )
    wall_now = float(now) if now is not None else time.time()
    return (wall_now - last_run_ts) <= stale_after_s


def advance_runtime_clock_locked(
    engine: Any,
    *,
    now: float | None = None,
) -> tuple[str, ...]:
    clock = getattr(engine, "clock", None)
    if not isinstance(clock, ClockedScheduler):
        raise RuntimeError("core_runtime_clock_unavailable")
    wall_now = float(time.time() if now is None else now)
    service = _runtime_clock_service(engine, clock)
    live_module = getattr(engine, "input_module", None)
    supervision_port = require_modem_live_runtime_supervision_port(
        live_module
    )
    supervision_schedule = supervision_port.schedule()
    _configure_live_register_job(
        clock,
        cadence_cycles=supervision_schedule.cadence_cycles,
        budget_ms=supervision_schedule.budget_ms,
        enabled=supervision_schedule.enabled,
    )
    _configure_command_lifecycle_job(clock, engine)
    workers = _runtime_clock_job_workers(engine)
    dispatched = service.run_once(
        lambda job: _dispatch_runtime_clock_job(
            engine,
            workers=workers,
            job=job,
            now=wall_now,
            clock_sequence=clock.cycle_index,
            supervision_port=supervision_port,
            live_register_enabled=supervision_schedule.enabled,
        ),
        now=wall_now,
    )
    for report in clock.last_run_reports:
        fault = report.fault
        if fault is None:
            continue
        require_fault_supervisor(engine).record(
            boundary=FaultBoundaryClass.WORKER,
            owner=fault.owner,
            operation=fault.operation,
            generation=fault.generation,
            severity=FaultSeverity.ERROR,
            retryable=fault.retryable,
            degraded_behavior=fault.degraded_behavior,
            error=RuntimeError(f"{fault.error_type}: {fault.detail}"),
            suppression_budget=3,
        )
    engine._runtime_clock_last_wall_ts = float(service.last_wall_ts)
    return tuple(dispatched)


def _dispatch_runtime_clock_job(
    engine: Any,
    *,
    workers: ClockJobWorkerFabric,
    job: Any,
    now: float,
    clock_sequence: int,
    supervision_port: ModemLiveRuntimeSupervisionPort,
    live_register_enabled: bool,
) -> ClockedJobOutcome:
    return workers.dispatch(
        job,
        clock_sequence=clock_sequence,
        requested_at=now,
        runner=lambda: _run_runtime_clock_job_in_worker(
            engine,
            job,
            now=now,
            clock_sequence=clock_sequence,
            supervision_port=supervision_port,
            live_register_enabled=live_register_enabled,
            execution_token=_CLOCK_JOB_WORKER_EXECUTION_TOKEN,
        ),
    )


def _run_runtime_clock_job_in_worker(
    engine: Any,
    job: Any,
    *,
    now: float,
    clock_sequence: int,
    supervision_port: ModemLiveRuntimeSupervisionPort,
    live_register_enabled: bool,
    execution_token: object,
) -> bool | ClockedJobOutcome:
    if execution_token is not _CLOCK_JOB_WORKER_EXECUTION_TOKEN:
        raise RuntimeError("runtime_clock_domain_execution_requires_worker_fabric")
    if job.key == CORE_COMMAND_LIFECYCLE_JOB_KEY:
        service = getattr(engine, "command_lifecycle", None)
        if not isinstance(service, CoreCommandLifecycleService):
            raise RuntimeError("core_command_lifecycle_service_unavailable")
        result = dict(service.service_once(now=now) or {})
        return ClockedJobOutcome(
            ran=True,
            backlog=max(0, int(result.get("projected_count", 0) or 0)),
            backpressure_reason=(
                "command_terminal_receipts_projected"
                if int(result.get("projected_count", 0) or 0) > 0
                else "command_lifecycle_current"
            ),
        )
    if job.key == MODEM_LIVE_REFRESH_JOB_KEY:
        return _run_modem_live_register_job(
            supervision_port,
            live_register_enabled=live_register_enabled,
        )
    if job.key == RUNTIME_STATUS_DECISION_JOB_KEY:
        health_view = engine.current_health_view()
        action_status = getattr(engine, "action_status", None)
        if not isinstance(action_status, CoreActionStatusService):
            raise RuntimeError("core_action_status_service_unavailable")
        action_status.publish(
            now=now,
            modem_health=health_view.modem_health.to_mapping(),
        )
        engine._runtime_status_decision_lane_last_cycle = {
            "owner": "runtime",
            "lane": "medium_status_decision",
            "job_key": job.key,
            "observed_ts": float(now),
            "source": "runtime_clock",
        }
        return ClockedJobOutcome(ran=True)
    if job.key == CORE_DOWNSTREAM_DIAGNOSTICS_JOB_KEY:
        service = getattr(engine, "core_diagnostics", None)
        runner = getattr(service, "service_once", None)
        if not callable(runner):
            raise RuntimeError("core_downstream_diagnostics_service_unavailable")
        result = dict(runner(now=now) or {})
        return ClockedJobOutcome(
            ran=True,
            backpressure_reason=(
                "downstream_health_published"
                if bool(result.get("published", False))
                else "downstream_health_unchanged"
            ),
        )
    return False


def _runtime_clock_service(engine: Any, clock: ClockedScheduler) -> ClockedRuntimeService:
    service = getattr(engine, "clock_service", None)
    if not isinstance(service, ClockedRuntimeService) or service.scheduler is not clock:
        raise RuntimeError("core_runtime_clock_service_mismatch")
    return service


def _runtime_clock_job_workers(engine: Any) -> ClockJobWorkerFabric:
    workers = getattr(engine, "clock_job_workers", None)
    if not isinstance(workers, ClockJobWorkerFabric):
        raise RuntimeError("core_runtime_clock_job_worker_fabric_unavailable")
    return workers


def _configure_live_register_job(
    clock: ClockedScheduler,
    *,
    cadence_cycles: int,
    budget_ms: float,
    enabled: bool,
) -> None:
    jobs = clock.status().get("jobs", {})
    current = jobs.get(MODEM_LIVE_REFRESH_JOB_KEY)
    if not isinstance(current, dict):
        return
    skip_policy = (
        ClockedSkipPolicy.NEVER_SKIP_CACHE_LAST_GOOD
        if enabled
        else ClockedSkipPolicy.EVENT_DRIVEN
    )
    if (
        int(current.get("cadence_cycles", 0) or 0) == int(cadence_cycles)
        and float(current.get("budget_ms", 0.0) or 0.0) == float(budget_ms)
        and str(current.get("skip_policy") or "") == skip_policy.value
    ):
        return
    clock.configure_job(
        MODEM_LIVE_REFRESH_JOB_KEY,
        cadence_cycles=cadence_cycles,
        budget_ms=budget_ms,
        skip_policy=skip_policy,
    )


def _configure_command_lifecycle_job(
    clock: ClockedScheduler,
    engine: Any,
) -> None:
    jobs = clock.status().get("jobs", {})
    current = jobs.get(CORE_COMMAND_LIFECYCLE_JOB_KEY)
    if not isinstance(current, dict):
        return
    service = getattr(engine, "command_lifecycle", None)
    if not isinstance(service, CoreCommandLifecycleService):
        raise RuntimeError("core_command_lifecycle_service_unavailable")
    status = service.status()
    active = bool(status.get("active") or status.get("projection_pending"))
    cadence_cycles = 1 if active else 10
    if int(current.get("cadence_cycles", 0) or 0) == cadence_cycles:
        return
    clock.configure_job(
        CORE_COMMAND_LIFECYCLE_JOB_KEY,
        cadence_cycles=cadence_cycles,
    )


def _run_modem_live_register_job(
    supervision_port: ModemLiveRuntimeSupervisionPort,
    *,
    live_register_enabled: bool,
) -> ClockedJobOutcome:
    if not live_register_enabled:
        return ClockedJobOutcome(
            ran=False,
            backpressure_reason="live_register_disabled",
        )
    try:
        receipt = supervision_port.service_once()
    except Exception as exc:
        return ClockedJobOutcome(
            ran=False,
            last_error=str(exc or ""),
            backpressure_reason="live_register_status_failed",
        )
    return ClockedJobOutcome(
        ran=receipt.accepted,
        backlog=0,
        last_error="",
        backpressure_reason=(
            "modem_live_health_published"
        ),
    )


__all__ = [
    "advance_runtime_clock",
    "advance_runtime_clock_locked",
    "runtime_clock_internal_service_fresh",
]
