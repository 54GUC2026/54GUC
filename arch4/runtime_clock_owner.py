"""Runtime-clock owner diagnostics.

This module is the single resolver for exposing runtime-clock status outside
the scheduler/service boundary. Exporters may present the status; they do not
decide which clock owns it.
"""

from __future__ import annotations

import time
from typing import Any


def _clock_status_provider(obj: Any) -> Any:
    time_status = getattr(obj, "time_status", None)
    return obj if callable(time_status) else None


def _service_scheduler(service: Any) -> Any:
    return getattr(service, "scheduler", None)


def _candidate_status_providers(engine: Any) -> list[tuple[str, Any]]:
    candidates: list[tuple[str, Any]] = []
    subsystem_clock = getattr(engine, "clock", None)
    subsystem_service = getattr(engine, "clock_service", None)

    if subsystem_clock is not None:
        candidates.append(("core.clock", subsystem_clock))

    for name, service in (("core.clock_service", subsystem_service),):
        if service is None:
            continue
        scheduler = _service_scheduler(service)
        if scheduler is not None:
            candidates.append((f"{name}.scheduler", scheduler))
        else:
            provider = _clock_status_provider(service)
            if provider is not None:
                candidates.append((name, provider))
    return candidates


def _first_runtime_clock_provider(
    candidates: list[tuple[str, Any]],
) -> tuple[str, Any] | None:
    provider: tuple[str, Any] | None = None
    for name, identity in candidates:
        if _clock_status_provider(identity) is None:
            continue
        provider = (name, identity)
        break
    return provider


def _runtime_clock_owner_mismatch(candidates: list[tuple[str, Any]]) -> str:
    seen: list[tuple[str, Any]] = []
    for name, identity in candidates:
        if _clock_status_provider(identity) is None:
            continue
        if not seen:
            seen.append((name, identity))
            continue
        if identity is not seen[0][1]:
            return f"runtime clock owner mismatch: {seen[0][0]} and {name} differ"
    return ""


def _unavailable_status(
    *, source: str, observed_ts: float, message: str
) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "owner": "runtime_clock",
        "source": source,
        "observed_ts": observed_ts,
        "message": message,
    }


def runtime_clock_owner_status(
    engine: Any,
    *,
    source: str,
    observed_ts: float | None = None,
    unavailable_message: str = "runtime clock scheduler unavailable",
) -> dict[str, Any]:
    """Return the canonical runtime-clock status or an explicit unavailable row."""

    now = float(time.time() if observed_ts is None else observed_ts)
    candidates = _candidate_status_providers(engine)
    mismatch = _runtime_clock_owner_mismatch(candidates)
    if mismatch:
        return _unavailable_status(source=source, observed_ts=now, message=mismatch)

    provider = _first_runtime_clock_provider(candidates)
    if provider is not None:
        _name, identity = provider
        try:
            time_status = getattr(identity, "time_status", None)
            if not callable(time_status):
                raise RuntimeError("runtime clock bounded time status unavailable")
            status = dict(time_status(now=now) or {})
        except Exception as exc:
            return _unavailable_status(source=source, observed_ts=now, message=str(exc))
        status["owner"] = "runtime_clock"
        status["source"] = source
        status["observed_ts"] = now
        return status

    return _unavailable_status(
        source=source, observed_ts=now, message=unavailable_message
    )
