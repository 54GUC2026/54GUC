"""Bounded throughput probe used only by an explicit Explore session."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, RLock, Thread
from time import perf_counter, time
from typing import Any, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from arch4.new_horizon.explore import ExploreProbeRequest


DEFAULT_CAPACITY_ENDPOINT = "https://speed.cloudflare.com/__down"


@dataclass(frozen=True, slots=True)
class CapacityProbeConfig:
    endpoint: str = DEFAULT_CAPACITY_ENDPOINT
    max_bytes: int = 32 * 1024 * 1024
    max_duration_s: float = 8.0
    socket_timeout_s: float = 2.5


class ExploreCapacityProbe:
    """Cancellable worker with no policy, history, or modem state."""

    def __init__(self, config: CapacityProbeConfig | None = None) -> None:
        self._config = config or CapacityProbeConfig()
        self._lock = RLock()
        self._cancel = Event()
        self._thread: Thread | None = None
        self._request: ExploreProbeRequest | None = None
        self._result: dict[str, Any] | None = None

    def start(self, request: ExploreProbeRequest) -> bool:
        if not isinstance(request, ExploreProbeRequest):
            raise TypeError("explore_capacity_request_required")
        with self._lock:
            if self._thread is not None:
                return False
            self._cancel.clear()
            self._request = request
            self._result = None
            thread = Thread(
                target=self._run,
                args=(request,),
                name="54guc-explore-capacity",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return True

    def cancel(self) -> None:
        self._cancel.set()

    def stop(self, *, timeout_s: float) -> bool:
        """Cancel and join the probe worker before Product shutdown."""

        self._cancel.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, float(timeout_s)))
        if thread.is_alive():
            return False
        with self._lock:
            if self._thread is thread:
                self._thread = None
                self._request = None
                self._result = None
        return True

    def poll(self) -> tuple[ExploreProbeRequest, Mapping[str, Any]] | None:
        with self._lock:
            thread = self._thread
            request = self._request
            result = self._result
            if thread is None or thread.is_alive() or request is None or result is None:
                return None
            self._thread = None
            self._request = None
            self._result = None
            return request, result

    def _run(self, request: ExploreProbeRequest) -> None:
        config = self._config
        started_wall = time()
        started = perf_counter()
        byte_count = 0
        status = "failed"
        reason = "capacity_probe_failed"
        try:
            separator = "&" if "?" in config.endpoint else "?"
            url = config.endpoint + separator + urlencode({"bytes": config.max_bytes})
            web_request = Request(
                url,
                headers={"User-Agent": "54GUC-Explore-capacity/1.0"},
            )
            with urlopen(web_request, timeout=config.socket_timeout_s) as response:
                while not self._cancel.is_set():
                    if perf_counter() - started >= config.max_duration_s:
                        break
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    byte_count += len(chunk)
                    if byte_count >= config.max_bytes:
                        break
            if self._cancel.is_set():
                status, reason = "cancelled", "explore_stopped"
            elif byte_count > 0:
                status, reason = "measured", "bounded_probe_completed"
            else:
                reason = "capacity_probe_empty_response"
        except Exception as exc:
            reason = f"{type(exc).__name__}:{exc}"
        duration_s = max(0.001, perf_counter() - started)
        result = {
            "status": status,
            "reason": reason,
            "dl_mbps": (float(byte_count) * 8.0) / duration_s / 1_000_000.0,
            "bytes": byte_count,
            "duration_s": duration_s,
            "started_at": started_wall,
            "completed_at": time(),
        }
        with self._lock:
            if self._request == request:
                self._result = result


__all__ = (
    "CapacityProbeConfig",
    "DEFAULT_CAPACITY_ENDPOINT",
    "ExploreCapacityProbe",
)
