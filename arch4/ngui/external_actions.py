"""Typed external actions scheduled independently for NGUI."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from threading import RLock
from time import monotonic, time
from types import MappingProxyType
from typing import Any, Callable, Mapping

from arch4.fault_supervisor import (
    FaultBoundaryClass,
    FaultSeverity,
    FaultSupervisor,
)
from arch4.foundation.active_job_admission import ActiveJobAdmissionAuthority
from arch4.worker_health import WorkerExecutionKind, WorkerHealthAuthority


class ExternalActionKind(str, Enum):
    OPEN_SPEED_TEST = "open_speed_test"
    OPEN_BUY = "open_buy"
    OPEN_INFO = "open_info"
    CHECK_UPDATES = "check_updates"
    SAVE_TRAFFIC_PLAN = "save_traffic_plan"
    SAVE_LICENSE = "save_license"
    APN_PROFILES = "apn_profiles"
    ROUTER_CHECK_REPORT = "router_check_report"


class ExternalActionState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ExternalActionRequest:
    request_id: str
    sequence: int
    kind: ExternalActionKind
    created_at: float
    payload: Mapping[str, Any]
    runtime_generation: int


@dataclass(frozen=True, slots=True)
class ExternalActionResult:
    request_id: str
    sequence: int
    kind: ExternalActionKind
    state: ExternalActionState
    completed_at: float
    result: Mapping[str, Any]
    message: str = ""


ExternalActionHandler = Callable[[ExternalActionRequest], Mapping[str, Any]]


class NGUIExternalActionRegister:
    """Hold the latest immutable result for each external action."""

    TERMINAL_RETENTION_LIMIT = 512

    def __init__(self) -> None:
        self._lock = RLock()
        self._results: dict[ExternalActionKind, ExternalActionResult] = {}
        self._terminal_receipts: OrderedDict[
            str, ExternalActionResult
        ] = OrderedDict()

    def publish(self, result: ExternalActionResult) -> bool:
        with self._lock:
            if result.state in {
                ExternalActionState.COMPLETED,
                ExternalActionState.FAILED,
                ExternalActionState.REJECTED,
                ExternalActionState.CANCELLED,
            }:
                retained = self._terminal_receipts.get(result.request_id)
                if retained is not None:
                    if retained != result:
                        raise RuntimeError(
                            "external-action terminal receipt replacement"
                        )
                    return False
                self._terminal_receipts[result.request_id] = result
                while (
                    len(self._terminal_receipts)
                    > self.TERMINAL_RETENTION_LIMIT
                ):
                    self._terminal_receipts.popitem(last=False)
                if result.state is ExternalActionState.REJECTED:
                    # Rejection means the lane did not accept the request. Keep
                    # it by request ID, but do not use it as the lane result.
                    return True
            current = self._results.get(result.kind)
            if current is not None and result.sequence < current.sequence:
                return False
            self._results[result.kind] = result
            return True

    def snapshot(self) -> Mapping[ExternalActionKind, ExternalActionResult]:
        with self._lock:
            return MappingProxyType(dict(self._results))

    def terminal_receipt_snapshot(self) -> Mapping[str, ExternalActionResult]:
        with self._lock:
            return MappingProxyType(dict(self._terminal_receipts))

    def reset_runtime_state(self) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        with self._lock:
            prior = {
                "results": {
                    kind.value: value
                    for kind, value in sorted(
                        self._results.items(),
                        key=lambda item: item[0].value,
                    )
                },
                "terminal_receipts": dict(self._terminal_receipts),
            }
            self._results = {}
            self._terminal_receipts = OrderedDict()
            return prior, {"results": {}, "terminal_receipts": {}}


class NGUIExternalActionProcessor:
    """Run each external action on its own lane, never on the GUI thread."""

    def __init__(
        self,
        handlers: Mapping[ExternalActionKind, ExternalActionHandler],
        *,
        admission: ActiveJobAdmissionAuthority,
        worker_health: WorkerHealthAuthority,
        fault_supervisor: FaultSupervisor,
    ) -> None:
        if not isinstance(worker_health, WorkerHealthAuthority):
            raise TypeError("ngui_external_actions_require_worker_health")
        if not isinstance(fault_supervisor, FaultSupervisor):
            raise TypeError("ngui_external_actions_require_fault_supervisor")
        if not isinstance(admission, ActiveJobAdmissionAuthority):
            raise TypeError("ngui_external_actions_require_foundation_admission")
        missing = set(ExternalActionKind) - set(handlers)
        extra = set(handlers) - set(ExternalActionKind)
        if missing or extra:
            raise ValueError(
                "external-action handlers must exactly match declared kinds: "
                f"missing={sorted(kind.value for kind in missing)} "
                f"extra={sorted(str(kind) for kind in extra)}"
            )
        self._handlers = dict(handlers)
        self._worker_health = worker_health
        self._fault_supervisor = fault_supervisor
        self._admission = admission
        expected_lanes = {
            self._lane_id(kind) for kind in ExternalActionKind
        }
        if not expected_lanes.issubset(admission.declared_lanes):
            raise ValueError("external-action admission lanes are incomplete")
        for kind in ExternalActionKind:
            self._worker_health.register(
                self._worker_id(kind),
                execution_kind=WorkerExecutionKind.FINITE_JOB,
                required=False,
                retryable=True,
            )
        self._executors = {
            kind: ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"ngui-external-{kind.value}",
            )
            for kind in ExternalActionKind
        }
        self._lock = RLock()
        self._sequence = 0
        self._runtime_generation = 0
        self._closed = False
        self._futures: dict[
            Future[ExternalActionResult], ExternalActionRequest
        ] = {}
        self.register = NGUIExternalActionRegister()

    def submit(
        self,
        kind: ExternalActionKind,
        *,
        created_at: float,
        payload: Mapping[str, Any] | None = None,
    ) -> Future[ExternalActionResult]:
        if created_at <= 0:
            raise ValueError("external-action created_at must be positive")
        with self._lock:
            if self._closed:
                raise RuntimeError("external-action admission is closed")
            self._sequence += 1
            request = ExternalActionRequest(
                request_id=(
                    f"external-action:{self._runtime_generation}:{kind.value}:{self._sequence}"
                ),
                sequence=self._sequence,
                kind=kind,
                created_at=created_at,
                payload=_freeze_mapping(payload or {}),
                runtime_generation=self._runtime_generation,
            )
            decision = self._admission.admit(
                request_id=request.request_id,
                lane_id=self._lane_id(kind),
                created_at=created_at,
                decided_at=monotonic(),
            )
            if not decision.admitted:
                rejected = ExternalActionResult(
                    request_id=request.request_id,
                    sequence=request.sequence,
                    kind=request.kind,
                    state=ExternalActionState.REJECTED,
                    completed_at=decision.decided_at,
                    result=MappingProxyType({}),
                    message=decision.reason,
                )
                self.register.publish(rejected)
                self._admission.terminalize(
                    request_id=request.request_id,
                    lane_id=self._lane_id(kind),
                    terminal_state=ExternalActionState.REJECTED.value,
                    terminal_at=decision.decided_at,
                )
                future: Future[ExternalActionResult] = Future()
                future.set_result(rejected)
                return future
            self.register.publish(
                ExternalActionResult(
                    request_id=request.request_id,
                    sequence=request.sequence,
                    kind=request.kind,
                    state=ExternalActionState.QUEUED,
                    completed_at=monotonic(),
                    result=MappingProxyType({}),
                )
            )
            worker_id = self._worker_id(kind)
            self._worker_health.mark_started(worker_id)
            try:
                future = self._executors[kind].submit(self._run, request)
            except Exception as exc:
                failed_at = monotonic()
                self.register.publish(
                    ExternalActionResult(
                        request_id=request.request_id,
                        sequence=request.sequence,
                        kind=request.kind,
                        state=ExternalActionState.FAILED,
                        completed_at=failed_at,
                        result=MappingProxyType({}),
                        message=f"{type(exc).__name__}: {exc}",
                    )
                )
                self._admission.terminalize(
                    request_id=request.request_id,
                    lane_id=self._lane_id(kind),
                    terminal_state=ExternalActionState.FAILED.value,
                    terminal_at=failed_at,
                )
                self._worker_health.mark_failure(
                    worker_id,
                    exc,
                    next_retry_at=time(),
                )
                self._fault_supervisor.record(
                    boundary=FaultBoundaryClass.GUI,
                    owner=worker_id,
                    operation="submit",
                    generation=request.sequence,
                    severity=FaultSeverity.ERROR,
                    retryable=True,
                    degraded_behavior="external_action_terminal_failed",
                    error=exc,
                    suppression_budget=3,
                )
                raise
            self._futures[future] = request
            future.add_done_callback(self._observe_future)
        return future

    def reset_runtime_state(
        self,
        *,
        worker_generation: int,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        with self._lock:
            if (
                type(worker_generation) is not int
                or worker_generation != self._runtime_generation + 1
            ):
                raise ValueError(
                    "external_action_runtime_generation_not_successor"
                )
            prior = {
                "runtime_generation": self._runtime_generation,
                "sequence": self._sequence,
                "inflight": tuple(
                    request.request_id
                    for future, request in self._futures.items()
                    if not future.done()
                ),
            }
            for future, request in tuple(self._futures.items()):
                if (
                    request.runtime_generation == self._runtime_generation
                    and not future.done()
                ):
                    future.cancel()
            self._runtime_generation = worker_generation
            self._sequence = 0
            register_prior, register_result = (
                self.register.reset_runtime_state()
            )
            prior = {**prior, "register": register_prior}
            result = {
                "runtime_generation": worker_generation,
                "sequence": 0,
                "inflight": (),
                "register": register_result,
            }
            return prior, result

    def close(self) -> Mapping[str, Any]:
        with self._lock:
            self._closed = True
        for executor in self._executors.values():
            executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            inflight = sum(not future.done() for future in self._futures)
        return MappingProxyType(
            {
                "owner": "ngui_external_actions",
                "admission_closed": True,
                "lanes": len(self._executors),
                "inflight": inflight,
                "terminated": inflight == 0,
            }
        )

    @staticmethod
    def _worker_id(kind: ExternalActionKind) -> str:
        return f"ngui_external_{kind.value}"

    @staticmethod
    def _lane_id(kind: ExternalActionKind) -> str:
        return f"ngui.external.{kind.value}"

    def _observe_future(self, future: Future[ExternalActionResult]) -> None:
        with self._lock:
            request = self._futures.pop(future, None)
        if request is None:
            return
        worker_id = self._worker_id(request.kind)
        if future.cancelled():
            terminal_at = monotonic()
            cancelled = ExternalActionResult(
                request_id=request.request_id,
                sequence=request.sequence,
                kind=request.kind,
                state=ExternalActionState.CANCELLED,
                completed_at=terminal_at,
                result=MappingProxyType({}),
                message="cancelled before execution",
            )
            with self._lock:
                if request.runtime_generation == self._runtime_generation:
                    self.register.publish(cancelled)
            self._admission.terminalize(
                request_id=request.request_id,
                lane_id=self._lane_id(request.kind),
                terminal_state=ExternalActionState.CANCELLED.value,
                terminal_at=terminal_at,
            )
            self._worker_health.mark_failure(
                worker_id,
                RuntimeError("external_action_cancelled_during_shutdown"),
                next_retry_at=0.0,
            )
            return
        try:
            result = future.result()
        except Exception as exc:
            error = exc
        else:
            error = (
                RuntimeError(result.message or "external action failed")
                if result.state is ExternalActionState.FAILED
                else None
            )
        if error is not None:
            self._worker_health.mark_failure(
                worker_id,
                error,
                next_retry_at=time(),
            )
            self._fault_supervisor.record(
                boundary=FaultBoundaryClass.GUI,
                owner=worker_id,
                operation="execute",
                generation=request.sequence,
                severity=FaultSeverity.ERROR,
                retryable=True,
                degraded_behavior="external_action_terminal_failed",
                error=error,
                suppression_budget=3,
            )
        else:
            self._worker_health.mark_success(worker_id)

    def _run(self, request: ExternalActionRequest) -> ExternalActionResult:
        self._publish_for_generation(
            request,
            ExternalActionResult(
                request_id=request.request_id,
                sequence=request.sequence,
                kind=request.kind,
                state=ExternalActionState.RUNNING,
                completed_at=monotonic(),
                result=MappingProxyType({}),
            ),
        )
        try:
            value = _freeze_mapping(self._handlers[request.kind](request))
        except Exception as exc:
            result = ExternalActionResult(
                request_id=request.request_id,
                sequence=request.sequence,
                kind=request.kind,
                state=ExternalActionState.FAILED,
                completed_at=monotonic(),
                result=MappingProxyType({}),
                message=f"{type(exc).__name__}: {exc}",
            )
        else:
            result = ExternalActionResult(
                request_id=request.request_id,
                sequence=request.sequence,
                kind=request.kind,
                state=ExternalActionState.COMPLETED,
                completed_at=monotonic(),
                result=value,
            )
        self._publish_for_generation(request, result)
        self._admission.terminalize(
            request_id=request.request_id,
            lane_id=self._lane_id(request.kind),
            terminal_state=result.state.value,
            terminal_at=result.completed_at,
        )
        return result

    def _publish_for_generation(
        self,
        request: ExternalActionRequest,
        result: ExternalActionResult,
    ) -> bool:
        with self._lock:
            if request.runtime_generation != self._runtime_generation:
                return False
            return self.register.publish(result)


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


__all__ = [
    "ExternalActionKind",
    "ExternalActionRequest",
    "ExternalActionResult",
    "ExternalActionState",
    "NGUIExternalActionProcessor",
    "NGUIExternalActionRegister",
]
