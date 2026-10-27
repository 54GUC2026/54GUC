"""Run NGUI heavy work on independent lanes outside the GUI toolkit."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
from enum import Enum
import hashlib
import os
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

from arch4.command_transactions import (
    TRANSACTION_POLICIES,
    TransactionFamily,
    TransactionPolicy,
)


class HeavyWorkKind(str, Enum):
    SITE_REPORT = "site_report"
    SUPPORT_BUNDLE = "support_bundle"
    SCAN = "scan"


@contextmanager
def _background_support_resources(kind: HeavyWorkKind, *, platform: str | None = None):
    """Lower only this export worker's CPU/I/O/memory scheduling on Windows.

    Large History collection must yield to the GUI and the independent live
    runtime. Never lower the process, modem worker or GUI thread. Restore the
    existing executor thread even when collection or final privacy fails.
    """
    if kind is not HeavyWorkKind.SUPPORT_BUNDLE or (os.name if platform is None else platform) != "nt":
        yield
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentThread.argtypes = ()
    kernel.GetCurrentThread.restype = ctypes.c_void_p
    kernel.SetThreadPriority.argtypes = (ctypes.c_void_p, ctypes.c_int)
    kernel.SetThreadPriority.restype = ctypes.c_int
    thread = kernel.GetCurrentThread()
    if not kernel.SetThreadPriority(thread, 0x00010000):  # THREAD_MODE_BACKGROUND_BEGIN
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        if not kernel.SetThreadPriority(thread, 0x00020000):  # THREAD_MODE_BACKGROUND_END
            raise ctypes.WinError(ctypes.get_last_error())


class HeavyWorkState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class HeavyWorkRequest:
    request_id: str
    sequence: int
    kind: HeavyWorkKind
    created_at: float
    payload: Mapping[str, Any]
    runtime_generation: int

    @property
    def transaction_family(self) -> TransactionFamily:
        """Use the typed export and read-model transaction family."""

        return TransactionFamily.EXPORT

    @property
    def policy(self) -> TransactionPolicy:
        return TRANSACTION_POLICIES[self.transaction_family]


@dataclass(frozen=True, slots=True)
class HeavyWorkProgress:
    sequence: int
    kind: HeavyWorkKind
    state: HeavyWorkState
    progress_pct: float
    updated_at: float
    message: str = ""


@dataclass(frozen=True, slots=True)
class HeavyWorkResult:
    request_id: str
    sequence: int
    kind: HeavyWorkKind
    completed_at: float
    result: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HeavyWorkTerminalReceipt:
    request_id: str
    sequence: int
    kind: HeavyWorkKind
    state: HeavyWorkState
    terminal_at: float
    result: Mapping[str, Any]
    message: str = ""

    def __post_init__(self) -> None:
        if self.state not in {
            HeavyWorkState.COMPLETED,
            HeavyWorkState.FAILED,
            HeavyWorkState.CANCELLED,
            HeavyWorkState.REJECTED,
        }:
            raise ValueError("heavy-work terminal state required")
        if not str(self.request_id or "").strip():
            raise ValueError("heavy-work request root required")

    def as_dict(self) -> Mapping[str, Any]:
        content = {
            "schema": "ngui.heavy_work_terminal.v1",
            "owner": "ngui_heavy_work",
            "request_id": self.request_id,
            "sequence": int(self.sequence),
            "kind": self.kind.value,
            "state": self.state.value,
            "terminal_at": float(self.terminal_at),
            "result": dict(self.result),
            "message": str(self.message or ""),
        }
        return MappingProxyType(
            {**content, "checksum": _checksum(content)}
        )


@dataclass(frozen=True, slots=True)
class HeavyWorkRegisterSnapshot:
    progress: Mapping[HeavyWorkKind, HeavyWorkProgress]
    results: Mapping[HeavyWorkKind, HeavyWorkResult]
    terminals: Mapping[HeavyWorkKind, HeavyWorkTerminalReceipt]
    terminal_receipts: Mapping[str, HeavyWorkTerminalReceipt]


ProgressCallback = Callable[[float, float, str], None]
HeavyWorkHandler = Callable[[HeavyWorkRequest, ProgressCallback], Mapping[str, Any]]


class NGUIHeavyWorkRegister:
    """Keep immutable progress and results for each work lane."""

    TERMINAL_RETENTION_LIMIT = 512

    def __init__(self) -> None:
        self._lock = RLock()
        self._progress: dict[HeavyWorkKind, HeavyWorkProgress] = {}
        self._results: dict[HeavyWorkKind, HeavyWorkResult] = {}
        self._terminals: dict[
            HeavyWorkKind, HeavyWorkTerminalReceipt
        ] = {}
        self._terminal_receipts: OrderedDict[
            str, HeavyWorkTerminalReceipt
        ] = OrderedDict()

    def publish_progress(self, progress: HeavyWorkProgress) -> bool:
        with self._lock:
            current = self._progress.get(progress.kind)
            if current is not None and progress.sequence < current.sequence:
                return False
            self._progress[progress.kind] = progress
            return True

    def complete(self, progress: HeavyWorkProgress, result: HeavyWorkResult) -> bool:
        if progress.state is not HeavyWorkState.COMPLETED:
            raise ValueError("heavy-work completion progress must be completed")
        if progress.sequence != result.sequence or progress.kind is not result.kind:
            raise ValueError(
                "heavy-work progress and result must identify the same request"
            )
        terminal = HeavyWorkTerminalReceipt(
            request_id=result.request_id,
            sequence=result.sequence,
            kind=result.kind,
            state=HeavyWorkState.COMPLETED,
            terminal_at=result.completed_at,
            result=result.result,
        )
        return self.terminalize(progress, terminal, result=result)

    def terminalize(
        self,
        progress: HeavyWorkProgress,
        terminal: HeavyWorkTerminalReceipt,
        *,
        result: HeavyWorkResult | None = None,
    ) -> bool:
        if progress.state is not terminal.state:
            raise ValueError("heavy-work progress terminal mismatch")
        if (
            progress.sequence != terminal.sequence
            or progress.kind is not terminal.kind
        ):
            raise ValueError("heavy-work terminal identity mismatch")
        with self._lock:
            retained = self._terminal_receipts.get(
                terminal.request_id
            )
            if retained is not None:
                if retained != terminal:
                    raise RuntimeError(
                        "heavy-work terminal receipt replacement"
                    )
                return False
            self._terminal_receipts[terminal.request_id] = terminal
            while (
                len(self._terminal_receipts)
                > self.TERMINAL_RETENTION_LIMIT
            ):
                self._terminal_receipts.popitem(last=False)
            if terminal.state is HeavyWorkState.REJECTED:
                # A rejected request never owned this job lane. Keep its final
                # receipt, but do not let its sequence replace or block the
                # accepted owner's result.
                return True
            current = self._progress.get(progress.kind)
            if current is not None and progress.sequence < current.sequence:
                return False
            current_terminal = self._terminals.get(terminal.kind)
            if current_terminal is not None:
                if terminal.sequence < current_terminal.sequence:
                    return False
                if terminal.sequence == current_terminal.sequence:
                    return False
            self._progress[progress.kind] = progress
            self._terminals[terminal.kind] = terminal
            if result is not None:
                if result.request_id != terminal.request_id:
                    raise ValueError("heavy-work result request mismatch")
                self._results[result.kind] = result
            return True

    def snapshot(self) -> Mapping[HeavyWorkKind, HeavyWorkProgress]:
        return self.progress_snapshot()

    def progress_snapshot(self) -> Mapping[HeavyWorkKind, HeavyWorkProgress]:
        with self._lock:
            return MappingProxyType(dict(self._progress))

    def result_snapshot(self) -> Mapping[HeavyWorkKind, HeavyWorkResult]:
        with self._lock:
            return MappingProxyType(dict(self._results))

    def terminal_snapshot(
        self,
    ) -> Mapping[HeavyWorkKind, HeavyWorkTerminalReceipt]:
        with self._lock:
            return MappingProxyType(dict(self._terminals))

    def terminal_receipt_snapshot(
        self,
    ) -> Mapping[str, HeavyWorkTerminalReceipt]:
        with self._lock:
            return MappingProxyType(dict(self._terminal_receipts))

    def readback(self) -> HeavyWorkRegisterSnapshot:
        with self._lock:
            return HeavyWorkRegisterSnapshot(
                progress=MappingProxyType(dict(self._progress)),
                results=MappingProxyType(dict(self._results)),
                terminals=MappingProxyType(dict(self._terminals)),
                terminal_receipts=MappingProxyType(
                    dict(self._terminal_receipts)
                ),
            )

    def reset_runtime_state(self) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        with self._lock:
            prior = self._reset_evidence_unlocked()
            self._progress = {}
            self._results = {}
            self._terminals = {}
            self._terminal_receipts = OrderedDict()
            return prior, self._reset_evidence_unlocked()

    def _reset_evidence_unlocked(self) -> dict[str, Any]:
        return {
            "progress": {
                kind.value: value
                for kind, value in sorted(
                    self._progress.items(), key=lambda item: item[0].value
                )
            },
            "results": {
                kind.value: value
                for kind, value in sorted(
                    self._results.items(), key=lambda item: item[0].value
                )
            },
            "terminals": {
                kind.value: value
                for kind, value in sorted(
                    self._terminals.items(), key=lambda item: item[0].value
                )
            },
            "terminal_receipts": dict(self._terminal_receipts),
        }


class NGUIHeavyWorkProcessor:
    """Run each heavy-work type on its own lane, never on the GUI thread."""

    def __init__(
        self,
        handlers: Mapping[HeavyWorkKind, HeavyWorkHandler],
        *,
        admission: ActiveJobAdmissionAuthority,
        worker_health: WorkerHealthAuthority,
        fault_supervisor: FaultSupervisor,
    ) -> None:
        if not isinstance(worker_health, WorkerHealthAuthority):
            raise TypeError("ngui_heavy_work_requires_worker_health")
        if not isinstance(fault_supervisor, FaultSupervisor):
            raise TypeError("ngui_heavy_work_requires_fault_supervisor")
        if not isinstance(admission, ActiveJobAdmissionAuthority):
            raise TypeError("ngui_heavy_work_requires_foundation_admission")
        missing = set(HeavyWorkKind) - set(handlers)
        extra = set(handlers) - set(HeavyWorkKind)
        if missing or extra:
            raise ValueError(
                "heavy-work handlers must exactly match declared kinds: "
                f"missing={sorted(kind.value for kind in missing)} "
                f"extra={sorted(str(kind) for kind in extra)}"
            )
        self._handlers = dict(handlers)
        self._worker_health = worker_health
        self._fault_supervisor = fault_supervisor
        self._admission = admission
        expected_lanes = {self._lane_id(kind) for kind in HeavyWorkKind}
        if not expected_lanes.issubset(admission.declared_lanes):
            raise ValueError("heavy-work admission lanes are incomplete")
        for kind in HeavyWorkKind:
            self._worker_health.register(
                self._worker_id(kind),
                execution_kind=WorkerExecutionKind.FINITE_JOB,
                required=False,
                retryable=True,
            )
        self._executors = {
            kind: ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"ngui-{kind.value}"
            )
            for kind in HeavyWorkKind
        }
        self._lock = RLock()
        self._sequence = 0
        self._runtime_generation = 0
        self._closed = False
        self._futures: dict[
            Future[Mapping[str, Any]], HeavyWorkRequest
        ] = {}
        self.register = NGUIHeavyWorkRegister()

    def submit(
        self,
        kind: HeavyWorkKind,
        *,
        created_at: float,
        payload: Mapping[str, Any] | None = None,
    ) -> Future[Mapping[str, Any]]:
        if created_at <= 0:
            raise ValueError("heavy-work created_at must be positive")
        with self._lock:
            if self._closed:
                raise RuntimeError("heavy-work admission is closed")
            self._sequence += 1
            canonical_payload = canonical_json_value(payload or {})
            if not isinstance(canonical_payload, dict):
                raise TypeError("heavy-work payload must be a mapping")
            frozen_payload = _freeze_mapping(canonical_payload)
            request = HeavyWorkRequest(
                request_id=_request_id(
                    kind=kind,
                    sequence=self._sequence,
                    created_at=created_at,
                    payload=frozen_payload,
                ),
                sequence=self._sequence,
                kind=kind,
                created_at=created_at,
                payload=frozen_payload,
                runtime_generation=self._runtime_generation,
            )
            decision = self._admission.admit(
                request_id=request.request_id,
                lane_id=self._lane_id(kind),
                created_at=created_at,
                decided_at=monotonic(),
            )
            if not decision.admitted:
                self.register.terminalize(
                    HeavyWorkProgress(
                        sequence=request.sequence,
                        kind=request.kind,
                        state=HeavyWorkState.REJECTED,
                        progress_pct=100.0,
                        updated_at=decision.decided_at,
                        message=decision.reason,
                    ),
                    HeavyWorkTerminalReceipt(
                        request_id=request.request_id,
                        sequence=request.sequence,
                        kind=request.kind,
                        state=HeavyWorkState.REJECTED,
                        terminal_at=decision.decided_at,
                        result=MappingProxyType({}),
                        message=decision.reason,
                    ),
                )
                self._admission.terminalize(
                    request_id=request.request_id,
                    lane_id=self._lane_id(kind),
                    terminal_state=HeavyWorkState.REJECTED.value,
                    terminal_at=decision.decided_at,
                )
                future: Future[Mapping[str, Any]] = Future()
                future.set_exception(
                    RuntimeError(
                        decision.reason or "heavy_work_admission_rejected"
                    )
                )
                return future
            self._publish(request, HeavyWorkState.QUEUED, 0.0, created_at)
            worker_id = self._worker_id(kind)
            self._worker_health.mark_started(worker_id)
            try:
                future = self._executors[kind].submit(self._run, request)
            except Exception as exc:
                failed_at = monotonic()
                message = f"{type(exc).__name__}: {exc}"
                self.register.terminalize(
                    HeavyWorkProgress(
                        sequence=request.sequence,
                        kind=request.kind,
                        state=HeavyWorkState.FAILED,
                        progress_pct=100.0,
                        updated_at=failed_at,
                        message=message,
                    ),
                    HeavyWorkTerminalReceipt(
                        request_id=request.request_id,
                        sequence=request.sequence,
                        kind=request.kind,
                        state=HeavyWorkState.FAILED,
                        terminal_at=failed_at,
                        result=MappingProxyType({}),
                        message=message,
                    ),
                )
                self._admission.terminalize(
                    request_id=request.request_id,
                    lane_id=self._lane_id(kind),
                    terminal_state=HeavyWorkState.FAILED.value,
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
                    degraded_behavior="utility_terminal_failed",
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
                raise ValueError("heavy_work_runtime_generation_not_successor")
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
            return {**prior, "register": register_prior}, {
                "runtime_generation": worker_generation,
                "sequence": 0,
                "inflight": (),
                "register": register_result,
            }

    def close(self) -> Mapping[str, Any]:
        with self._lock:
            self._closed = True
        for executor in self._executors.values():
            executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            inflight = sum(
                not future.done() for future in self._futures
            )
        return MappingProxyType(
            {
                "owner": "ngui_heavy_work",
                "admission_closed": True,
                "lanes": len(self._executors),
                "inflight": inflight,
                "terminated": inflight == 0,
            }
        )

    @staticmethod
    def _worker_id(kind: HeavyWorkKind) -> str:
        return f"ngui_heavy_{kind.value}"

    @staticmethod
    def _lane_id(kind: HeavyWorkKind) -> str:
        return f"ngui.heavy.{kind.value}"

    def _observe_future(self, future: Future[Mapping[str, Any]]) -> None:
        with self._lock:
            request = self._futures.pop(future, None)
        if request is None:
            return
        worker_id = self._worker_id(request.kind)
        if future.cancelled():
            terminal_at = monotonic()
            with self._lock:
                if request.runtime_generation == self._runtime_generation:
                    self.register.terminalize(
                        HeavyWorkProgress(
                            sequence=request.sequence,
                            kind=request.kind,
                            state=HeavyWorkState.CANCELLED,
                            progress_pct=100.0,
                            updated_at=terminal_at,
                            message="cancelled before execution",
                        ),
                        HeavyWorkTerminalReceipt(
                            request_id=request.request_id,
                            sequence=request.sequence,
                            kind=request.kind,
                            state=HeavyWorkState.CANCELLED,
                            terminal_at=terminal_at,
                            result=MappingProxyType({}),
                            message="cancelled before execution",
                        ),
                    )
            self._admission.terminalize(
                request_id=request.request_id,
                lane_id=self._lane_id(request.kind),
                terminal_state=HeavyWorkState.CANCELLED.value,
                terminal_at=terminal_at,
            )
            self._worker_health.mark_failure(
                worker_id,
                RuntimeError("heavy_work_cancelled_during_shutdown"),
                next_retry_at=0.0,
            )
            return
        try:
            future.result()
        except Exception as exc:
            self._worker_health.mark_failure(
                worker_id,
                exc,
                next_retry_at=time(),
            )
            self._fault_supervisor.record(
                boundary=FaultBoundaryClass.GUI,
                owner=worker_id,
                operation="execute",
                generation=request.sequence,
                severity=FaultSeverity.ERROR,
                retryable=True,
                degraded_behavior="utility_terminal_failed",
                error=exc,
                suppression_budget=3,
            )
        else:
            self._worker_health.mark_success(worker_id)

    def _run(self, request: HeavyWorkRequest) -> Mapping[str, Any]:
        self._publish(request, HeavyWorkState.RUNNING, 0.0, monotonic())

        def progress(progress_pct: float, updated_at: float, message: str = "") -> None:
            if not 0.0 <= progress_pct <= 100.0:
                raise ValueError("heavy-work progress must be within 0..100")
            if updated_at <= 0:
                raise ValueError("heavy-work progress timestamp must be positive")
            self._publish(
                request,
                HeavyWorkState.RUNNING,
                progress_pct,
                updated_at,
                message=message,
            )

        try:
            with _background_support_resources(request.kind):
                result = _freeze_mapping(self._handlers[request.kind](request, progress))
        except Exception as exc:
            failed_at = monotonic()
            message = f"{type(exc).__name__}: {exc}"
            with self._lock:
                if request.runtime_generation == self._runtime_generation:
                    self.register.terminalize(
                        HeavyWorkProgress(
                            sequence=request.sequence,
                            kind=request.kind,
                            state=HeavyWorkState.FAILED,
                            progress_pct=100.0,
                            updated_at=failed_at,
                            message=message,
                        ),
                        HeavyWorkTerminalReceipt(
                            request_id=request.request_id,
                            sequence=request.sequence,
                            kind=request.kind,
                            state=HeavyWorkState.FAILED,
                            terminal_at=failed_at,
                            result=MappingProxyType({}),
                            message=message,
                        ),
                    )
            self._admission.terminalize(
                request_id=request.request_id,
                lane_id=self._lane_id(request.kind),
                terminal_state=HeavyWorkState.FAILED.value,
                terminal_at=failed_at,
            )
            raise
        completed_at = monotonic()
        with self._lock:
            if request.runtime_generation == self._runtime_generation:
                self.register.complete(
                    HeavyWorkProgress(
                        sequence=request.sequence,
                        kind=request.kind,
                        state=HeavyWorkState.COMPLETED,
                        progress_pct=100.0,
                        updated_at=completed_at,
                    ),
                    HeavyWorkResult(
                        request_id=request.request_id,
                        sequence=request.sequence,
                        kind=request.kind,
                        completed_at=completed_at,
                        result=result,
                    ),
                )
        self._admission.terminalize(
            request_id=request.request_id,
            lane_id=self._lane_id(request.kind),
            terminal_state=HeavyWorkState.COMPLETED.value,
            terminal_at=completed_at,
        )
        return result

    def _publish(
        self,
        request: HeavyWorkRequest,
        state: HeavyWorkState,
        progress_pct: float,
        updated_at: float,
        *,
        message: str = "",
    ) -> None:
        with self._lock:
            if request.runtime_generation != self._runtime_generation:
                return
            self.register.publish_progress(
                HeavyWorkProgress(
                    sequence=request.sequence,
                    kind=request.kind,
                    state=state,
                    progress_pct=progress_pct,
                    updated_at=updated_at,
                    message=message,
                )
            )


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


def _request_id(
    *,
    kind: HeavyWorkKind,
    sequence: int,
    created_at: float,
    payload: Mapping[str, Any],
) -> str:
    content = {
        "kind": kind.value,
        "sequence": int(sequence),
        "created_at": float(created_at),
        "payload": dict(payload),
    }
    return "heavy-work:" + _checksum(content)[:32]


def _checksum(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


__all__ = [
    "HeavyWorkKind",
    "HeavyWorkProgress",
    "HeavyWorkRequest",
    "HeavyWorkResult",
    "HeavyWorkState",
    "HeavyWorkTerminalReceipt",
    "NGUIHeavyWorkProcessor",
    "NGUIHeavyWorkRegister",
]
from arch4.canonical_json import canonical_json_bytes, canonical_json_value
