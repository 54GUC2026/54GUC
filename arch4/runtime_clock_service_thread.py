"""Shared internal runtime-clock thread for CLI and GUI runners."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Callable, Mapping

from arch4.core.clock_wake_stream import require_core_clock_wake_stream
from arch4.core.extension_hooks import require_core_extension_hook_registry
from arch4.fault_supervisor import (
    FaultBoundaryClass,
    FaultSeverity,
    require_fault_supervisor,
)
from arch4.runtime_clock_owner import runtime_clock_owner_status
from arch4.runtime_registers import require_core_runtime_register_bank
from arch4.worker_health import (
    WorkerExecutionKind,
    require_worker_health_authority,
)


INTERNAL_RUNTIME_CLOCK_INTERVAL_S = 0.1
CORE_CLOCK_WAKE_INTERVAL_S = 0.05
CORE_CLOCK_WAKE_100MS_INTERVAL_S = 0.1
RUNTIME_CLOCK_REGISTER_INTERVAL_S = 1.0
_RUNTIME_CLOCK_WAKE_LISTENER_ATTR = "_runtime_clock_wake_listener"


@dataclass(frozen=True, slots=True)
class InternalRuntimeClockTerminationReceipt:
    survivors: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.survivors, tuple)
            or any(
                type(item) is not str
                or not item
                or item != item.strip()
                for item in self.survivors
            )
        ):
            raise TypeError(
                "internal_runtime_clock_survivors_tuple_required"
            )
        if len(self.survivors) != len(set(self.survivors)):
            raise ValueError(
                "internal_runtime_clock_survivors_duplicate"
            )

    @property
    def terminated(self) -> bool:
        return not self.survivors

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "InternalRuntimeClockTerminationReceipt.v1",
            "owner": "runtime_clock_internal_service",
            "terminated": self.terminated,
            "survivors": list(self.survivors),
        }


def bind_runtime_clock_wake_listener(
    core: Any,
    listener: Callable[[], None] | None,
) -> None:
    """Bind one payload-free wake after a clock register commit."""

    current = getattr(core, _RUNTIME_CLOCK_WAKE_LISTENER_ATTR, None)
    if current is not None and listener is not None and current is not listener:
        raise RuntimeError("runtime clock wake listener is already bound")
    setattr(core, _RUNTIME_CLOCK_WAKE_LISTENER_ATTR, listener)


def bind_runtime_clock_register_listener(
    core: Any,
    listener: Callable[[Mapping[str, Any]], None] | None,
) -> None:
    del core, listener
    raise RuntimeError(
        "runtime clock mapping listener retired; "
        "bind payload-free clock wake listener"
    )


def _publish_runtime_clock_register(
    core: Any,
    status: Mapping[str, Any],
    *,
    now: float,
) -> None:
    payload = dict(status or {})
    if str(payload.get("owner") or "") != "runtime_clock":
        raise RuntimeError("runtime clock register requires canonical owner status")
    available = str(payload.get("status") or "").strip().lower() != "unavailable"
    bank = require_core_runtime_register_bank(core)
    bank.write_runtime_clock(
        value=payload,
        source="runtime_clock_internal_service",
        updated_at=float(now),
        valid=available,
        stale=not available,
        dirty=False,
        trust="live" if available else "stale",
        source_kind="live",
        validity="valid" if available else "stale",
        transition=False,
        error="" if available else str(payload.get("message") or "unavailable"),
        force=True,
    )
    listener = getattr(core, _RUNTIME_CLOCK_WAKE_LISTENER_ATTR, None)
    if callable(listener):
        listener()


def _runtime_clock_status_snapshot(core: Any) -> dict[str, Any]:
    return runtime_clock_owner_status(
        core,
        source="runtime_clock_internal_service",
        observed_ts=time.time(),
        unavailable_message="runtime clock scheduler unavailable in internal service context",
    )


def start_internal_runtime_clock(
    owner: Any,
    *,
    core: Any,
    is_running: Callable[[], bool],
    logger: logging.Logger,
    interval_s: float | None = None,
) -> None:
    if not hasattr(owner, "_internal_runtime_clock_stop"):
        owner._internal_runtime_clock_stop = threading.Event()
    if not hasattr(owner, "_internal_runtime_clock_thread"):
        owner._internal_runtime_clock_thread = None
    if not hasattr(owner, "_internal_runtime_clock_register_thread"):
        owner._internal_runtime_clock_register_thread = None
    if not hasattr(owner, "_internal_core_clock_wake_thread"):
        owner._internal_core_clock_wake_thread = None
    if not hasattr(owner, "_internal_runtime_clock_interval_s"):
        owner._internal_runtime_clock_interval_s = INTERNAL_RUNTIME_CLOCK_INTERVAL_S

    thread = owner._internal_runtime_clock_thread
    register_thread = owner._internal_runtime_clock_register_thread
    wake_thread = owner._internal_core_clock_wake_thread
    if (
        thread is not None
        and thread.is_alive()
        and register_thread is not None
        and register_thread.is_alive()
        and wake_thread is not None
        and wake_thread.is_alive()
    ):
        return
    if (thread is not None and thread.is_alive()) or (
        register_thread is not None and register_thread.is_alive()
    ) or (
        wake_thread is not None and wake_thread.is_alive()
    ):
        raise RuntimeError("internal_runtime_clock_worker_set_incomplete")
    advance = getattr(core, "_advance_runtime_clock", None)
    if not callable(advance):
        raise RuntimeError(
            "internal_runtime_clock_advance_port_unavailable"
        )
    effective_interval_s = max(
        0.1,
        float(
            interval_s
            if interval_s is not None
            else (owner._internal_runtime_clock_interval_s or INTERNAL_RUNTIME_CLOCK_INTERVAL_S)
        ),
    )
    stop_event = owner._internal_runtime_clock_stop
    stop_event.clear()
    worker_health = require_worker_health_authority(core)
    clock_wake_stream = require_core_clock_wake_stream(core)
    extension_hooks = require_core_extension_hook_registry(core)
    worker_id = "internal_runtime_clock"
    register_worker_id = "runtime_clock_register_publisher"
    wake_worker_id = "core_clock_wake_publisher"
    worker_health.register(
        worker_id,
        execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
        required=True,
        retryable=True,
    )
    worker_health.register(
        register_worker_id,
        execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
        required=True,
        retryable=True,
    )
    worker_health.register(
        wake_worker_id,
        execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
        required=True,
        retryable=True,
    )
    worker_health.mark_started(worker_id)
    worker_health.mark_started(register_worker_id)
    worker_health.mark_started(wake_worker_id)
    setattr(core, "_runtime_clock_internal_service_active", True)
    status = {
        "active": True,
        "interval_s": effective_interval_s,
        "started_ts": time.time(),
        "last_run_ts": 0.0,
        "run_count": 0,
        "last_error": "",
        "failure_streak": 0,
        "degraded": False,
        "next_retry_s": 0.0,
    }
    setattr(core, "_runtime_clock_internal_service_status", dict(status))

    def _clock_loop() -> None:
        local_status = dict(status)
        last_health_success_monotonic = 0.0
        setattr(core, "_runtime_clock_internal_service_status", dict(local_status))
        while not stop_event.is_set() and is_running():
            started = time.time()
            try:
                advance()
                monotonic_now = time.monotonic()
                if monotonic_now - last_health_success_monotonic >= 1.0:
                    worker_health.mark_success(worker_id)
                    last_health_success_monotonic = monotonic_now
                local_status["last_error"] = ""
                local_status["failure_streak"] = 0
                local_status["degraded"] = False
                local_status["next_retry_s"] = 0.0
            except Exception as exc:
                local_status["last_error"] = str(exc)
                failure_streak = int(local_status.get("failure_streak", 0) or 0) + 1
                retry_s = min(
                    5.0,
                    max(effective_interval_s, 0.1) * (2 ** min(failure_streak, 6)),
                )
                local_status["failure_streak"] = failure_streak
                local_status["degraded"] = True
                local_status["next_retry_s"] = retry_s
                worker_health.mark_failure(
                    worker_id,
                    exc,
                    next_retry_at=time.time() + retry_s,
                )
                require_fault_supervisor(core).record(
                    boundary=FaultBoundaryClass.WORKER,
                    owner="internal_runtime_clock",
                    operation="advance",
                    generation=int(local_status.get("run_count", 0) or 0) + 1,
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior="clock_retry_with_bounded_backoff",
                    error=exc,
                    suppression_budget=3,
                )
                logger.warning(
                    "Internal runtime clock advance failed; retrying in %.3fs: %s",
                    retry_s,
                    exc,
                )
            local_status["last_run_ts"] = time.time()
            local_status["run_count"] = int(local_status.get("run_count", 0) or 0) + 1
            setattr(core, "_runtime_clock_internal_service_status", dict(local_status))
            retry_s = float(local_status.get("next_retry_s", 0.0) or 0.0)
            wait_s = max(
                retry_s,
                max(0.0, effective_interval_s - (time.time() - started)),
            )
            stop_event.wait(wait_s)
        local_status["active"] = False
        local_status["stopped_ts"] = time.time()
        setattr(core, "_runtime_clock_internal_service_status", dict(local_status))
        setattr(core, "_runtime_clock_internal_service_active", False)
        worker_health.mark_stopped(
            worker_id,
            reason="clock_stop_requested"
            if stop_event.is_set()
            else "runtime_owner_stopped",
        )

    def _supervised_clock_loop() -> None:
        while not stop_event.is_set() and is_running():
            try:
                _clock_loop()
                return
            except Exception as exc:
                current = worker_health.record(worker_id)
                retry_s = min(
                    5.0,
                    max(effective_interval_s, 0.1)
                    * (2 ** min(current.failure_streak + 1, 6)),
                )
                worker_health.mark_failure(
                    worker_id,
                    exc,
                    next_retry_at=time.time() + retry_s,
                )
                require_fault_supervisor(core).record(
                    boundary=FaultBoundaryClass.WORKER,
                    owner=worker_id,
                    operation="clock_loop_uncaught",
                    generation=int(current.success_count + current.failure_count),
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior="clock_worker_restarts_with_bounded_backoff",
                    error=exc,
                    suppression_budget=3,
                )
                logger.exception(
                    "Internal runtime clock worker escaped one iteration; restarting"
                )
                if stop_event.wait(retry_s):
                    return
                worker_health.mark_started(worker_id)

    def _clock_register_loop() -> None:
        register_status = {
            "active": True,
            "interval_s": RUNTIME_CLOCK_REGISTER_INTERVAL_S,
            "poll_s": effective_interval_s,
            "started_ts": time.time(),
            "last_publish_ts": 0.0,
            "run_count": 0,
            "failure_streak": 0,
            "last_error": "",
        }
        setattr(
            core,
            "_runtime_clock_register_service_status",
            dict(register_status),
        )
        next_publish = time.monotonic()
        while not stop_event.is_set() and is_running():
            monotonic_now = time.monotonic()
            wait_s = next_publish - monotonic_now
            if wait_s > 0.0:
                stop_event.wait(min(effective_interval_s, wait_s))
                continue
            wall_now = time.time()
            try:
                clock_status = _runtime_clock_status_snapshot(core)
                _publish_runtime_clock_register(core, clock_status, now=wall_now)
                if str(clock_status.get("status") or "").lower() == "unavailable":
                    raise RuntimeError(
                        str(clock_status.get("message") or "runtime clock unavailable")
                    )
                worker_health.mark_success(register_worker_id)
                register_status["failure_streak"] = 0
                register_status["last_error"] = ""
            except Exception as exc:
                failure_streak = int(register_status.get("failure_streak", 0) or 0) + 1
                register_status["failure_streak"] = failure_streak
                register_status["last_error"] = str(exc)
                worker_health.mark_failure(
                    register_worker_id,
                    exc,
                    next_retry_at=wall_now + RUNTIME_CLOCK_REGISTER_INTERVAL_S,
                )
                require_fault_supervisor(core).record(
                    boundary=FaultBoundaryClass.WORKER,
                    owner="runtime_clock",
                    operation="publish_runtime_clock_register",
                    generation=int(register_status.get("run_count", 0) or 0) + 1,
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior="clock_register_publication_retries",
                    error=exc,
                    suppression_budget=3,
                )
                logger.warning(
                    "Runtime clock register publication failed; retrying: %s",
                    exc,
                )
            register_status["last_publish_ts"] = wall_now
            register_status["run_count"] = int(register_status.get("run_count", 0) or 0) + 1
            setattr(
                core,
                "_runtime_clock_register_service_status",
                dict(register_status),
            )
            next_publish += RUNTIME_CLOCK_REGISTER_INTERVAL_S
            monotonic_now = time.monotonic()
            if next_publish <= monotonic_now:
                missed = int(
                    (monotonic_now - next_publish) // RUNTIME_CLOCK_REGISTER_INTERVAL_S
                ) + 1
                next_publish += missed * RUNTIME_CLOCK_REGISTER_INTERVAL_S
        register_status["active"] = False
        register_status["stopped_ts"] = time.time()
        setattr(
            core,
            "_runtime_clock_register_service_status",
            dict(register_status),
        )
        worker_health.mark_stopped(
            register_worker_id,
            reason=(
                "clock_stop_requested"
                if stop_event.is_set()
                else "runtime_owner_stopped"
            ),
        )

    def _supervised_clock_register_loop() -> None:
        while not stop_event.is_set() and is_running():
            try:
                _clock_register_loop()
                return
            except Exception as exc:
                current = worker_health.record(register_worker_id)
                retry_s = min(
                    5.0,
                    max(effective_interval_s, 0.1)
                    * (2 ** min(current.failure_streak + 1, 6)),
                )
                worker_health.mark_failure(
                    register_worker_id,
                    exc,
                    next_retry_at=time.time() + retry_s,
                )
                require_fault_supervisor(core).record(
                    boundary=FaultBoundaryClass.WORKER,
                    owner=register_worker_id,
                    operation="clock_register_loop_uncaught",
                    generation=int(current.success_count + current.failure_count),
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior="clock_register_worker_restarts_with_bounded_backoff",
                    error=exc,
                    suppression_budget=3,
                )
                logger.exception(
                    "Runtime clock register worker escaped; restarting"
                )
                if stop_event.wait(retry_s):
                    return
                worker_health.mark_started(register_worker_id)

    def _clock_wake_loop() -> None:
        wake_status = {
            "active": True,
            "owner": "54guc.core.clock_wake_stream",
            "interval_50ms_s": CORE_CLOCK_WAKE_INTERVAL_S,
            "interval_100ms_s": CORE_CLOCK_WAKE_100MS_INTERVAL_S,
            "started_ts": time.time(),
            "last_emit_ts": 0.0,
            "emit_50ms_count": 0,
            "emit_100ms_count": 0,
            "missed_50ms_count": 0,
            "missed_100ms_count": 0,
            "failure_streak": 0,
            "last_error": "",
        }
        setattr(core, "_core_clock_wake_service_status", dict(wake_status))
        next_50_monotonic = time.monotonic()
        next_100_monotonic = next_50_monotonic
        next_50_wall = time.time()
        next_100_wall = next_50_wall
        last_health_success_monotonic = 0.0
        while not stop_event.is_set() and is_running():
            monotonic_now = time.monotonic()
            next_due = min(next_50_monotonic, next_100_monotonic)
            if monotonic_now < next_due:
                stop_event.wait(
                    min(CORE_CLOCK_WAKE_INTERVAL_S, next_due - monotonic_now)
                )
                continue
            wall_now = time.time()
            emitted = False
            health_due = False
            try:
                has_50ms_subscriber = bool(
                    extension_hooks.status("clock_wake_50ms").subscribers
                )
                has_100ms_subscriber = bool(
                    extension_hooks.status("clock_wake_100ms").subscribers
                )
                if monotonic_now >= next_50_monotonic:
                    if has_50ms_subscriber:
                        clock_wake_stream.emit(
                            lane="50ms",
                            emitted_at=wall_now,
                            due_at=next_50_wall,
                        )
                        wake_status["emit_50ms_count"] += 1
                        emitted = True
                    missed = max(
                        0,
                        int(
                            (monotonic_now - next_50_monotonic)
                            // CORE_CLOCK_WAKE_INTERVAL_S
                        ),
                    )
                    wake_status["missed_50ms_count"] += missed
                    periods = missed + 1
                    next_50_monotonic += periods * CORE_CLOCK_WAKE_INTERVAL_S
                    next_50_wall += periods * CORE_CLOCK_WAKE_INTERVAL_S
                if monotonic_now >= next_100_monotonic:
                    if has_100ms_subscriber:
                        clock_wake_stream.emit(
                            lane="100ms",
                            emitted_at=wall_now,
                            due_at=next_100_wall,
                        )
                        wake_status["emit_100ms_count"] += 1
                        emitted = True
                    missed = max(
                        0,
                        int(
                            (monotonic_now - next_100_monotonic)
                            // CORE_CLOCK_WAKE_100MS_INTERVAL_S
                        ),
                    )
                    wake_status["missed_100ms_count"] += missed
                    periods = missed + 1
                    next_100_monotonic += periods * CORE_CLOCK_WAKE_100MS_INTERVAL_S
                    next_100_wall += periods * CORE_CLOCK_WAKE_100MS_INTERVAL_S
                if emitted:
                    wake_status["last_emit_ts"] = wall_now
                wake_status["failure_streak"] = 0
                wake_status["last_error"] = ""
                health_due = (
                    monotonic_now - last_health_success_monotonic >= 1.0
                )
                if health_due:
                    worker_health.mark_success(wake_worker_id)
                    last_health_success_monotonic = monotonic_now
            except Exception as exc:
                wake_status["failure_streak"] += 1
                wake_status["last_error"] = f"{type(exc).__name__}: {exc}"
                worker_health.mark_failure(
                    wake_worker_id,
                    exc,
                    next_retry_at=wall_now + CORE_CLOCK_WAKE_INTERVAL_S,
                )
                require_fault_supervisor(core).record(
                    boundary=FaultBoundaryClass.WORKER,
                    owner=wake_worker_id,
                    operation="publish_clock_wakes",
                    generation=(
                        int(wake_status["emit_50ms_count"])
                        + int(wake_status["emit_100ms_count"])
                    ),
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior="clock_wake_publication_retries",
                    error=exc,
                    suppression_budget=3,
                )
                logger.warning("Core clock wake publication failed; retrying: %s", exc)
                next_50_monotonic = time.monotonic() + CORE_CLOCK_WAKE_INTERVAL_S
                next_100_monotonic = time.monotonic() + CORE_CLOCK_WAKE_100MS_INTERVAL_S
                next_50_wall = time.time() + CORE_CLOCK_WAKE_INTERVAL_S
                next_100_wall = time.time() + CORE_CLOCK_WAKE_100MS_INTERVAL_S
            if emitted or health_due:
                setattr(core, "_core_clock_wake_service_status", dict(wake_status))
        wake_status["active"] = False
        wake_status["stopped_ts"] = time.time()
        setattr(core, "_core_clock_wake_service_status", dict(wake_status))
        worker_health.mark_stopped(
            wake_worker_id,
            reason=(
                "clock_stop_requested"
                if stop_event.is_set()
                else "runtime_owner_stopped"
            ),
        )

    def _supervised_clock_wake_loop() -> None:
        while not stop_event.is_set() and is_running():
            try:
                _clock_wake_loop()
                return
            except Exception as exc:
                current = worker_health.record(wake_worker_id)
                retry_s = CORE_CLOCK_WAKE_INTERVAL_S
                worker_health.mark_failure(
                    wake_worker_id,
                    exc,
                    next_retry_at=time.time() + retry_s,
                )
                require_fault_supervisor(core).record(
                    boundary=FaultBoundaryClass.WORKER,
                    owner=wake_worker_id,
                    operation="clock_wake_loop_uncaught",
                    generation=int(current.success_count + current.failure_count),
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior="clock_wake_worker_restarts",
                    error=exc,
                    suppression_budget=3,
                )
                logger.exception("Core clock wake worker escaped; restarting")
                if stop_event.wait(retry_s):
                    return
                worker_health.mark_started(wake_worker_id)

    owner._internal_runtime_clock_thread = threading.Thread(
        target=_supervised_clock_loop,
        name="arch4-internal-runtime-clock",
        daemon=True,
    )
    owner._internal_runtime_clock_register_thread = threading.Thread(
        target=_supervised_clock_register_loop,
        name="arch4-runtime-clock-register",
        daemon=True,
    )
    owner._internal_core_clock_wake_thread = threading.Thread(
        target=_supervised_clock_wake_loop,
        name="arch4-core-clock-wake",
        daemon=True,
    )
    workers = (
        owner._internal_runtime_clock_thread,
        owner._internal_runtime_clock_register_thread,
        owner._internal_core_clock_wake_thread,
    )
    started_workers: list[threading.Thread] = []
    try:
        for worker in workers:
            worker.start()
            started_workers.append(worker)
    except BaseException:
        stop_event.set()
        rollback_deadline = time.monotonic() + 2.0
        for worker in started_workers:
            if worker is not threading.current_thread():
                worker.join(
                    timeout=max(
                        0.0,
                        rollback_deadline - time.monotonic(),
                    )
                )
        survivors = tuple(
            worker.name for worker in started_workers if worker.is_alive()
        )
        for thread_name, worker_id in (
            (
                "_internal_runtime_clock_thread",
                "internal_runtime_clock",
            ),
            (
                "_internal_runtime_clock_register_thread",
                "runtime_clock_register_publisher",
            ),
            (
                "_internal_core_clock_wake_thread",
                "core_clock_wake_publisher",
            ),
        ):
            worker = getattr(owner, thread_name, None)
            if worker is None or not worker.is_alive():
                worker_health.mark_stopped(
                    worker_id,
                    reason="clock_activation_rolled_back",
                )
        setattr(core, "_runtime_clock_internal_service_active", False)
        status["active"] = False
        status["stopped_ts"] = time.time()
        setattr(
            core,
            "_runtime_clock_internal_service_status",
            dict(status),
        )
        # A failed later Thread.start() does not prove that an earlier worker
        # stopped.  Retain every live worker reference so the Product-owned
        # stop boundary can join it and report an exact survivor.  Dead or
        # never-entered workers may be cleared, but they remain represented by
        # the Product's start-attempt evidence.
        for thread_name in (
            "_internal_runtime_clock_thread",
            "_internal_runtime_clock_register_thread",
            "_internal_core_clock_wake_thread",
        ):
            worker = getattr(owner, thread_name, None)
            if worker is None or not worker.is_alive():
                setattr(owner, thread_name, None)
        if survivors:
            raise RuntimeError(
                "internal_runtime_clock_activation_survivor:"
                + ",".join(survivors)
            )
        raise


def stop_internal_runtime_clock(
    owner: Any,
    *,
    core: Any | None = None,
    timeout_s: float = 5.0,
) -> InternalRuntimeClockTerminationReceipt:
    stop_event = getattr(owner, "_internal_runtime_clock_stop", None)
    if stop_event is not None:
        stop_event.set()
    deadline = time.monotonic() + max(0.0, float(timeout_s or 0.0))
    thread_names = (
        "_internal_runtime_clock_thread",
        "_internal_runtime_clock_register_thread",
        "_internal_core_clock_wake_thread",
    )
    for thread_name in thread_names:
        thread = getattr(owner, thread_name, None)
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
    survivors = tuple(
        str(getattr(getattr(owner, thread_name, None), "name", thread_name))
        for thread_name in thread_names
        if (
            getattr(owner, thread_name, None) is not None
            and getattr(owner, thread_name).is_alive()
        )
    )
    if core is not None and not survivors:
        setattr(core, "_runtime_clock_internal_service_active", False)
    return InternalRuntimeClockTerminationReceipt(
        survivors=survivors,
    )


__all__ = (
    "INTERNAL_RUNTIME_CLOCK_INTERVAL_S",
    "CORE_CLOCK_WAKE_INTERVAL_S",
    "CORE_CLOCK_WAKE_100MS_INTERVAL_S",
    "RUNTIME_CLOCK_REGISTER_INTERVAL_S",
    "InternalRuntimeClockTerminationReceipt",
    "bind_runtime_clock_register_listener",
    "start_internal_runtime_clock",
    "stop_internal_runtime_clock",
)
