"""Single owner for runtime-control suppressed fault state."""

from __future__ import annotations

from typing import Any, Mapping

from arch4.runtime_faults import record_suppressed_fault

_RUNTIME_CONTROL_FAULTS_ATTR = "_runtime_control_faults"
RUNTIME_CONTROL_FAULTS_OWNER = "runtime_control_faults"


def read_runtime_control_faults(engine: Any) -> dict[str, Any]:
    payload = getattr(engine, _RUNTIME_CONTROL_FAULTS_ATTR, None)
    if not isinstance(payload, Mapping) or not payload:
        return {}
    return dict(payload)


def publish_runtime_control_faults(engine: Any, payload: Mapping[str, Any] | None) -> dict[str, Any]:
    out = dict(payload or {}) if isinstance(payload, Mapping) else {}
    try:
        setattr(engine, _RUNTIME_CONTROL_FAULTS_ATTR, dict(out))
    except Exception:
        pass
    return out


def record_runtime_control_fault(
    engine: Any,
    *,
    source: str,
    exc: Exception,
    command: str = "",
    boundary: str = "engine_runtime_control",
) -> dict[str, Any]:
    current = read_runtime_control_faults(engine)
    payload = record_suppressed_fault(
        current,
        category="runtime_control",
        source=source,
        error_type=type(exc).__name__,
        detail=str(exc),
        metadata={
            "command": str(command or ""),
            "boundary": str(boundary or ""),
        },
    )
    return publish_runtime_control_faults(engine, payload)
