"""Run modem I/O through one lock and record each call.

This module has no ``arch4.*`` imports. ``ModemLiveModule`` supplies the lock,
success check, and fault callback. Every modem call uses the same record format.
"""

from __future__ import annotations

import threading
import time
from contextlib import nullcontext
from typing import Any, Callable, ContextManager, Dict, Optional, Tuple


ContextFactory = Callable[[str], Tuple[ContextManager[Any], bool]]
SemanticSuccess = Callable[..., bool]
FaultRecorder = Callable[[str, Exception], None]


class ModemIoGate:
    """Execute and record modem calls through one gate."""

    def __init__(self, *, owner: str = "modem_live", history_limit: int = 120, recent_limit: int = 30) -> None:
        self.owner = str(owner or "modem_live")
        self.history_limit = max(1, int(history_limit or 120))
        self.recent_limit = max(1, int(recent_limit or 30))
        self._lock = threading.Lock()
        self._sequence = 0
        self._history: list[dict[str, Any]] = []
        self._counts: dict[str, dict[str, Any]] = {}
        self._thread_evidence = threading.local()

    def record(
        self,
        *,
        source: str,
        operation: str,
        lock_policy: str,
        locked: bool,
        started_ts: float,
        success: bool,
        call_completed: bool = False,
        result_present: bool = False,
        result_type: str = "",
        error: Optional[BaseException] = None,
        caller_id: str = "",
        lane: str = "",
        endpoint_class: str = "",
        priority: Optional[int] = None,
        cooldown_s: float = 0.0,
        result: str = "",
        queue_wait_ms: float = 0.0,
    ) -> None:
        finished_ts = time.time()
        source_key = str(source or "unknown")
        operation_key = str(operation or "unknown")
        caller_key = str(caller_id or source_key or "unknown")
        lane_key = str(lane or "").strip()
        endpoint_key = str(endpoint_class or "").strip()
        result_key = str(result or ("ok" if success else "fault")).strip()
        key = f"{source_key}:{operation_key}"
        event: dict[str, Any] = {
            "seq": 0,
            "ts": finished_ts,
            "source": source_key,
            "operation": operation_key,
            "caller_id": caller_key,
            "lane": lane_key,
            "endpoint_class": endpoint_key,
            "priority": int(priority if priority is not None else 100),
            "cooldown_s": round(max(0.0, float(cooldown_s or 0.0)), 3),
            "lock_policy": str(lock_policy or "backend"),
            "locked": bool(locked),
            "thread": threading.current_thread().name,
            "duration_ms": round(max(0.0, finished_ts - float(started_ts or finished_ts)) * 1000.0, 3),
            "queue_wait_ms": round(max(0.0, float(queue_wait_ms or 0.0)), 3),
            "success": bool(success),
            "result": result_key,
            "call_completed": bool(call_completed),
            "result_present": bool(result_present),
        }
        if result_type:
            event["result_type"] = str(result_type)
        if error is not None:
            event["error_type"] = type(error).__name__
            event["error"] = str(error)

        with self._lock:
            self._sequence += 1
            event["seq"] = int(self._sequence)
            self._history.append(dict(event))
            self._history = self._history[-self.history_limit :]
            current = dict(self._counts.get(key) or {})
            current["count"] = int(current.get("count", 0) or 0) + 1
            current["last_ts"] = finished_ts
            current["last_duration_ms"] = event["duration_ms"]
            current["last_queue_wait_ms"] = event["queue_wait_ms"]
            current["last_success"] = bool(success)
            current["last_result"] = result_key
            current["last_call_completed"] = bool(call_completed)
            current["last_result_present"] = bool(result_present)
            current["last_caller_id"] = caller_key
            if lane_key:
                current["last_lane"] = lane_key
            if endpoint_key:
                current["last_endpoint_class"] = endpoint_key
            current["last_priority"] = int(priority if priority is not None else 100)
            current["last_cooldown_s"] = round(max(0.0, float(cooldown_s or 0.0)), 3)
            if result_type:
                current["last_result_type"] = str(result_type)
            current["locked"] = bool(locked)
            current["lock_policy"] = str(lock_policy or "backend")
            if error is not None:
                current["fail_count"] = int(current.get("fail_count", 0) or 0) + 1
                current["last_error_type"] = type(error).__name__
                current["last_error"] = str(error)
            else:
                current.pop("last_error_type", None)
                current.pop("last_error", None)
            self._counts[key] = current
        self._thread_evidence.last_event = dict(event)

    def last_call_event(self) -> dict[str, Any]:
        """Return the current thread's most recent gated-call record."""

        event = getattr(self._thread_evidence, "last_event", None)
        return dict(event) if isinstance(event, dict) else {}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "owner": self.owner,
                "sequence": int(self._sequence),
                "source_count": len(self._counts),
                "counts": {key: dict(value) for key, value in sorted(self._counts.items())},
                "recent": [dict(item) for item in self._history[-self.recent_limit :]],
            }

    def call(
        self,
        *,
        source: str,
        operation: str,
        caller_id: str = "",
        lane: str = "",
        endpoint_class: str = "",
        priority: Optional[int] = None,
        cooldown_s: float = 0.0,
        fn: Callable[[], Any],
        context_factory: ContextFactory,
        semantic_success: SemanticSuccess,
        fault_recorder: FaultRecorder,
        lock_policy: str = "backend",
    ) -> Any:
        queued_ts = time.time()
        execution_started_ts = queued_ts
        context, locked = context_factory(lock_policy)
        if context is None:
            context = nullcontext()
        try:
            with context:
                execution_started_ts = time.time()
                result = fn()
        except Exception as exc:
            self.record(
                source=source,
                operation=operation,
                caller_id=caller_id or source,
                lane=lane,
                lock_policy=lock_policy,
                locked=locked,
                started_ts=execution_started_ts,
                success=False,
                call_completed=False,
                result_present=False,
                error=exc,
                endpoint_class=endpoint_class,
                priority=priority,
                cooldown_s=cooldown_s,
                result="exception",
                queue_wait_ms=(execution_started_ts - queued_ts) * 1000.0,
            )
            fault_recorder(operation, exc)
            raise

        success = bool(semantic_success(source=source, operation=operation, result=result))
        result_label = "ok" if success else "semantic_failure"
        self.record(
            source=source,
            operation=operation,
            caller_id=caller_id or source,
            lane=lane,
            lock_policy=lock_policy,
            locked=locked,
            started_ts=execution_started_ts,
            success=success,
            call_completed=True,
            result_present=result is not None,
            result_type=type(result).__name__ if result is not None else "",
            endpoint_class=endpoint_class,
            priority=priority,
            cooldown_s=cooldown_s,
            result=result_label,
            queue_wait_ms=(execution_started_ts - queued_ts) * 1000.0,
        )
        return result
