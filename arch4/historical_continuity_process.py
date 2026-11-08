"""Process-isolated execution port for non-urgent historical continuity."""

from __future__ import annotations

from dataclasses import dataclass
import json
import multiprocessing
import os
from pathlib import Path
from queue import Empty
from threading import RLock
import time
import traceback
from typing import Any, Callable, Mapping, Protocol

from arch4.external_memory_auto_import import (
    automatic_external_memory_prior_import,
    external_memory_candidate_paths,
)
from arch4.historical_artifact_reader import HistoricalReadCooperation
from arch4.historical_continuity_materialization import (
    materialize_historical_chart_artifact,
)
from arch4.historical_continuity_store import (
    PRIOR_PACK_FILENAME,
    cleanup_superseded_generations,
    prepare_staged_generation_receipt,
)


REQUEST_SCHEMA = "HistoricalContinuityBulkRequest.v1"
RESULT_SCHEMA = "HistoricalContinuityBulkResult.v1"
PORT_OWNER = "arch4.historical_continuity_process"
RAW_YIELD_EVERY_RECORDS = 200
RAW_YIELD_S = 0.001
MAX_CHILD_TRACEBACK_CHARS = 12000

ProgressCallback = Callable[[float, str, str], None]


@dataclass(frozen=True, slots=True)
class HistoricalContinuityBulkRequest:
    transaction_id: str
    attempt: int
    stage_dir: str
    staging_generation_dir: str
    target_identity: Mapping[str, Any]
    preferred_sources: tuple[str, ...]
    include_default_sources: bool
    force: bool
    max_age_days: int
    min_telemetry_rows: int
    confirmed_handoff_receipt: Mapping[str, Any]

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema": REQUEST_SCHEMA,
            "transaction_id": self.transaction_id,
            "attempt": max(1, int(self.attempt)),
            "stage_dir": self.stage_dir,
            "staging_generation_dir": self.staging_generation_dir,
            "target_identity": dict(self.target_identity),
            "preferred_sources": list(self.preferred_sources),
            "include_default_sources": self.include_default_sources,
            "force": bool(self.force),
            "max_age_days": max(1, int(self.max_age_days)),
            "min_telemetry_rows": max(1, int(self.min_telemetry_rows)),
            "confirmed_handoff_receipt": dict(self.confirmed_handoff_receipt),
        }


class HistoricalContinuityBulkExecutionPort(Protocol):
    def execute(
        self,
        request: HistoricalContinuityBulkRequest,
        progress_callback: ProgressCallback,
    ) -> Mapping[str, Any]: ...

    def shutdown(self, *, timeout_s: float) -> Mapping[str, Any]: ...


class HistoricalContinuityChildFault(RuntimeError):
    """Typed child-process fault with bounded diagnostic evidence."""

    def __init__(self, *, error: str, child_traceback: str) -> None:
        self.error = str(error or "historical continuity child failed")
        self.child_traceback = str(child_traceback or "")[
            -MAX_CHILD_TRACEBACK_CHARS:
        ]
        super().__init__(self.error)


def _lower_windows_process_priority(ctypes_api: Any) -> dict[str, Any]:
    """Set and verify BELOW_NORMAL using pointer-width Win32 declarations."""

    below_normal_priority_class = 0x00004000
    kernel32 = ctypes_api.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = ctypes_api.c_void_p
    kernel32.SetPriorityClass.argtypes = (
        ctypes_api.c_void_p,
        ctypes_api.c_uint32,
    )
    kernel32.SetPriorityClass.restype = ctypes_api.c_bool
    kernel32.GetPriorityClass.argtypes = (ctypes_api.c_void_p,)
    kernel32.GetPriorityClass.restype = ctypes_api.c_uint32

    ctypes_api.set_last_error(0)
    handle = kernel32.GetCurrentProcess()
    accepted = bool(
        kernel32.SetPriorityClass(handle, below_normal_priority_class)
    )
    observed = int(kernel32.GetPriorityClass(handle)) if accepted else 0
    error_code = int(ctypes_api.get_last_error())
    verified = bool(accepted and observed == below_normal_priority_class)
    result: dict[str, Any] = {
        "platform": "windows",
        "policy": "below_normal_priority_class",
        "accepted": verified,
        "requested_priority_class": below_normal_priority_class,
        "observed_priority_class": observed,
    }
    if not verified:
        result["error"] = (
            f"win32_priority_rejected:error={error_code}:observed={observed}"
        )
    return result


def _lower_process_priority() -> dict[str, Any]:
    if os.name == "nt":
        try:
            import ctypes

            return _lower_windows_process_priority(ctypes)
        except (AttributeError, OSError) as exc:
            return {
                "platform": "windows",
                "policy": "below_normal_priority_class",
                "accepted": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    try:
        new_nice = os.nice(10)
        return {
            "platform": os.name,
            "policy": "nice_plus_10",
            "accepted": True,
            "nice": int(new_nice),
        }
    except (AttributeError, OSError) as exc:
        return {
            "platform": os.name,
            "policy": "nice_plus_10",
            "accepted": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _load_staged_pack(staging_dir: Path) -> dict[str, Any]:
    path = staging_dir / PRIOR_PACK_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("staged historical prior pack is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema") != "external_memory_prior_pack_v1":
        raise RuntimeError("staged historical prior pack contract mismatch")
    return payload


def _child_entry(
    request: Mapping[str, Any],
    message_queue: Any,
    cancel_event: Any,
) -> None:
    def emit(kind: str, **payload: Any) -> None:
        message_queue.put(
            {
                "schema": "HistoricalContinuityProcessMessage.v1",
                "kind": kind,
                "child_pid": os.getpid(),
                **payload,
            }
        )

    try:
        if request.get("schema") != REQUEST_SCHEMA:
            raise RuntimeError("historical continuity bulk request schema mismatch")
        priority = _lower_process_priority()
        if not bool(priority.get("accepted")):
            raise RuntimeError(
                "historical continuity child could not establish its declared "
                f"low-priority class: {priority.get('error') or 'unknown_error'}"
            )
        cooperation = HistoricalReadCooperation(
            yield_every_raw_records=RAW_YIELD_EVERY_RECORDS,
            yield_s=RAW_YIELD_S,
            cancel_requested=cancel_event.is_set,
        )
        target = dict(request.get("target_identity") or {})
        staging = Path(str(request.get("staging_generation_dir") or "")).resolve()
        stage = Path(str(request.get("stage_dir") or "")).resolve()
        memory_dir = staging.parent.parent
        removed_generations = cleanup_superseded_generations(memory_dir)
        emit(
            "progress",
            progress_pct=2.0,
            phase="generation_retention",
            detail=f"removed {removed_generations} superseded generations",
            removed_generations=removed_generations,
            priority=priority,
        )
        source_hints = [
            str(value)
            for value in request.get("preferred_sources") or []
            if str(value or "").strip()
        ]
        include_default_sources = request.get("include_default_sources")
        if type(include_default_sources) is not bool:
            raise RuntimeError(
                "historical continuity request requires explicit "
                "include_default_sources boolean"
            )
        handoff = dict(request.get("confirmed_handoff_receipt") or {})
        handoff_path = str(handoff.get("handoff_path") or "").strip()
        if handoff_path:
            source_hints.insert(0, handoff_path)
        candidates = external_memory_candidate_paths(
            stage,
            target,
            preferred_paths=source_hints,
            include_default_sources=include_default_sources,
        )
        emit(
            "progress",
            progress_pct=5.0,
            phase="candidate_inventory",
            detail=f"identified {len(candidates)} bounded source candidates",
            candidate_paths=[str(path) for path in candidates],
            priority=priority,
        )

        def progress(pct: float, phase: str, detail: str) -> None:
            if cancel_event.is_set():
                raise RuntimeError("historical continuity child cancelled")
            emit(
                "progress",
                progress_pct=max(5.0, min(80.0, 5.0 + 0.75 * float(pct))),
                phase=str(phase or "source_scan"),
                detail=str(detail or ""),
            )

        status = automatic_external_memory_prior_import(
            stage_dir=stage,
            memory_dir=staging,
            target_identity=target,
            source_paths=candidates,
            max_age_days=max(1, int(request.get("max_age_days") or 14)),
            min_telemetry_rows=max(
                1,
                int(request.get("min_telemetry_rows") or 100),
            ),
            force=bool(request.get("force", False)),
            confirmed_handoff_receipt=handoff,
            progress_callback=progress,
            reader_cooperation=cooperation,
        )
        if str(status.get("status") or "") != "imported":
            emit(
                "result",
                result={
                    "schema": RESULT_SCHEMA,
                    "owner": PORT_OWNER,
                    "status": str(status.get("status") or "failed"),
                    "auto_import": dict(status),
                    "chart_history": {},
                    "candidate_paths": [str(path) for path in candidates],
                    "priority": priority,
                },
            )
            return
        pack = _load_staged_pack(staging)
        chart_receipt = materialize_historical_chart_artifact(
            staging,
            pack,
            max_age_days=max(1, int(request.get("max_age_days") or 14)),
            transaction_id=str(request.get("transaction_id") or ""),
            cooperation=cooperation,
            progress_callback=lambda pct, phase, detail: emit(
                "progress",
                progress_pct=float(pct),
                phase=phase,
                detail=detail,
            ),
        )
        artifact_receipt = prepare_staged_generation_receipt(
            staging,
            transaction_id=str(request.get("transaction_id") or ""),
        )
        emit(
            "result",
            result={
                "schema": RESULT_SCHEMA,
                "owner": PORT_OWNER,
                "status": "prepared",
                "auto_import": dict(status),
                "chart_history": dict(chart_receipt),
                "artifact_receipt": artifact_receipt,
                "candidate_paths": [str(path) for path in candidates],
                "priority": priority,
            },
        )
    except BaseException as exc:
        emit(
            "fault",
            error=f"{type(exc).__name__}: {exc}",
            traceback="".join(traceback.format_exception(exc))[-12000:],
        )


class SpawnedHistoricalContinuityBulkProcess:
    """One lower-priority spawned child controlled by the Engine owner."""

    def __init__(self, *, process_context: Any | None = None) -> None:
        self._context = process_context or multiprocessing.get_context("spawn")
        self._lock = RLock()
        self._process: Any | None = None
        self._cancel_event: Any | None = None

    def execute(
        self,
        request: HistoricalContinuityBulkRequest,
        progress_callback: ProgressCallback,
    ) -> Mapping[str, Any]:
        queue = self._context.Queue(maxsize=128)
        cancel = self._context.Event()
        process = self._context.Process(
            target=_child_entry,
            args=(request.as_mapping(), queue, cancel),
            name="54guc-history-import",
        )
        with self._lock:
            if self._process is not None and self._process.is_alive():
                raise RuntimeError("historical continuity child already active")
            # Shutdown must never receive a Process whose start is unfinished.
            # The same lock serializes starting, publication and stop admission.
            try:
                process.start()
            except Exception:
                queue.close()
                queue.join_thread()
                raise
            self._process = process
            self._cancel_event = cancel
        terminal: dict[str, Any] | None = None
        fault = ""
        fault_traceback = ""
        try:
            while True:
                try:
                    message = queue.get(timeout=0.2)
                except Empty:
                    if not process.is_alive():
                        break
                    continue
                if not isinstance(message, Mapping):
                    fault = "historical continuity child emitted an invalid message"
                    break
                kind = str(message.get("kind") or "")
                if kind == "progress":
                    progress_callback(
                        float(message.get("progress_pct") or 0.0),
                        str(message.get("phase") or "background"),
                        str(message.get("detail") or ""),
                    )
                elif kind == "result":
                    terminal = dict(message.get("result") or {})
                    terminal["child_pid"] = int(message.get("child_pid") or 0)
                    break
                elif kind == "fault":
                    fault = str(message.get("error") or "historical continuity child failed")
                    fault_traceback = str(message.get("traceback") or "")
                    break
                else:
                    fault = "historical continuity child emitted an unknown message"
                    break
            process.join(timeout=2.0)
            if process.is_alive():
                cancel.set()
                process.terminate()
                process.join(timeout=1.0)
                raise RuntimeError("historical continuity child did not terminate")
            if fault:
                raise HistoricalContinuityChildFault(
                    error=fault,
                    child_traceback=fault_traceback,
                )
            if terminal is None:
                raise RuntimeError(
                    "historical continuity child exited without terminal receipt "
                    f"(exitcode={process.exitcode})"
                )
            terminal["child_exitcode"] = int(process.exitcode or 0)
            terminal["parent_pid"] = os.getpid()
            return terminal
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
                    self._cancel_event = None
            try:
                queue.close()
                queue.join_thread()
            except Exception:
                pass

    def shutdown(self, *, timeout_s: float) -> Mapping[str, Any]:
        started = time.monotonic()
        with self._lock:
            process = self._process
            cancel = self._cancel_event
        if process is None:
            return {
                "owner": PORT_OWNER,
                "status": "idle",
                "terminated": False,
            }
        if cancel is not None:
            cancel.set()
        process.join(timeout=max(0.0, float(timeout_s)))
        terminated = False
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.5)
            terminated = True
        return {
            "owner": PORT_OWNER,
            "status": "terminated" if terminated else "stopped",
            "terminated": terminated,
            "elapsed_s": max(0.0, time.monotonic() - started),
            "exitcode": process.exitcode,
        }


__all__ = [
    "HistoricalContinuityBulkExecutionPort",
    "HistoricalContinuityBulkRequest",
    "HistoricalContinuityChildFault",
    "MAX_CHILD_TRACEBACK_CHARS",
    "PORT_OWNER",
    "RAW_YIELD_EVERY_RECORDS",
    "RAW_YIELD_S",
    "REQUEST_SCHEMA",
    "RESULT_SCHEMA",
    "SpawnedHistoricalContinuityBulkProcess",
]
