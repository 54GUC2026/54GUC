"""
arch4/runtime_state_contract.py
═══════════════════════════════════════════════════════════════
Explicit runtime connection state contract.

Defines the canonical set of runtime connection states that the
entire app shares — runner, engine, GUI, sidecar files. No
vendor-specific semantics leak into these states.

States:
    OFFLINE         — no network/device contact has been attempted
    PROBING         — actively probing for a device
    DISCONNECTED    — live telemetry is currently unavailable
    RECONNECTING    — attempting to re-establish contact
    STARTUP_BLOCKED — initial connection failed, waiting to retry
    CONNECTING      — first connection attempt in progress
    LIVE            — connected and receiving telemetry
    SHUTDOWN        — clean shutdown in progress or completed

Transitions:
    OFFLINE → CONNECTING → LIVE
    OFFLINE → PROBING → CONNECTING → LIVE
    OFFLINE → CONNECTING → STARTUP_BLOCKED → RECONNECTING → LIVE
    LIVE → DISCONNECTED → RECONNECTING → LIVE
    any → SHUTDOWN
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class RuntimeConnectionState(enum.Enum):
    """Canonical runtime connection states."""
    OFFLINE = "offline"
    PROBING = "probing"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"
    STARTUP_BLOCKED = "startup_blocked"
    CONNECTING = "connecting"
    LIVE = "live"
    SHUTDOWN = "shutdown"


# ── Human-readable labels (no vendor jargon) ────────────────────────────

_STATE_LABELS: dict[RuntimeConnectionState, str] = {
    RuntimeConnectionState.OFFLINE: "Offline",
    RuntimeConnectionState.PROBING: "Searching for device",
    RuntimeConnectionState.DISCONNECTED: "Live telemetry unavailable",
    RuntimeConnectionState.RECONNECTING: "Reconnecting",
    RuntimeConnectionState.STARTUP_BLOCKED: "Startup blocked",
    RuntimeConnectionState.CONNECTING: "Connecting",
    RuntimeConnectionState.LIVE: "Connected",
    RuntimeConnectionState.SHUTDOWN: "Shutting down",
}

# ── State properties ────────────────────────────────────────────────────

_ACTUATION_ALLOWED: set[RuntimeConnectionState] = {
    RuntimeConnectionState.LIVE,
}

_CONTROLS_INTERLOCKED: set[RuntimeConnectionState] = {
    RuntimeConnectionState.OFFLINE,
    RuntimeConnectionState.PROBING,
    RuntimeConnectionState.DISCONNECTED,
    RuntimeConnectionState.RECONNECTING,
    RuntimeConnectionState.STARTUP_BLOCKED,
    RuntimeConnectionState.CONNECTING,
    RuntimeConnectionState.SHUTDOWN,
}

_BANNER_SEVERITY: dict[RuntimeConnectionState, str] = {
    RuntimeConnectionState.OFFLINE: "info",
    RuntimeConnectionState.PROBING: "info",
    RuntimeConnectionState.DISCONNECTED: "warning",
    RuntimeConnectionState.RECONNECTING: "warning",
    RuntimeConnectionState.STARTUP_BLOCKED: "warning",
    RuntimeConnectionState.CONNECTING: "info",
    RuntimeConnectionState.LIVE: "ok",
    RuntimeConnectionState.SHUTDOWN: "info",
}


@dataclass(frozen=True, slots=True)
class RuntimeConnectionStatus:
    """Compact, immutable snapshot of runtime connection state.

    This is the single status object the runtime shares.
    Runner health, sidecar files, and declared NGUI projection inputs consume it.
    """
    state: RuntimeConnectionState = RuntimeConnectionState.OFFLINE
    label: str = ""
    detail: str = ""
    reconnect_attempt: int = 0
    reconnect_delay_s: float = 0.0

    @property
    def actuation_allowed(self) -> bool:
        """True only when connected and receiving live data."""
        return self.state in _ACTUATION_ALLOWED

    @property
    def controls_interlocked(self) -> bool:
        """True when manual controls should be disabled."""
        return self.state in _CONTROLS_INTERLOCKED

    @property
    def banner_severity(self) -> str:
        """Banner color hint: ok, info, warning, error."""
        return _BANNER_SEVERITY.get(self.state, "info")

    @property
    def display_label(self) -> str:
        """Human-readable label for this state."""
        if self.label:
            return self.label
        return _STATE_LABELS.get(self.state, str(self.state.value))

    @property
    def is_connected(self) -> bool:
        return self.state == RuntimeConnectionState.LIVE

    @property
    def is_degraded(self) -> bool:
        return self.state in {
            RuntimeConnectionState.DISCONNECTED,
            RuntimeConnectionState.RECONNECTING,
            RuntimeConnectionState.STARTUP_BLOCKED,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "label": self.display_label,
            "detail": self.detail,
            "actuation_allowed": self.actuation_allowed,
            "controls_interlocked": self.controls_interlocked,
            "banner_severity": self.banner_severity,
            "reconnect_attempt": self.reconnect_attempt,
            "reconnect_delay_s": self.reconnect_delay_s,
            "is_connected": self.is_connected,
            "is_degraded": self.is_degraded,
        }


# ── Factory helpers ─────────────────────────────────────────────────────

def from_runtime_health_phase(phase: str, **kwargs: Any) -> RuntimeConnectionStatus:
    """Map a raw runtime_health phase string to a RuntimeConnectionStatus.

    This bridges the existing runner/sidecar phase strings to the new
    canonical state model without requiring runner changes.
    """
    phase = str(phase or "").strip().lower()

    if phase in {"init", "startup_begin"}:
        return RuntimeConnectionStatus(
            state=RuntimeConnectionState.CONNECTING,
            detail=str(kwargs.get("detail", "") or ""),
        )
    elif phase == "startup_ok":
        return RuntimeConnectionStatus(state=RuntimeConnectionState.LIVE)
    elif phase == "startup_blocked":
        detail = str(kwargs.get("error", "") or "")
        reason = normalize_startup_blocked_reason(
            kwargs.get("startup_blocked_reason", ""),
            detail=detail,
        )
        return RuntimeConnectionStatus(
            state=RuntimeConnectionState.STARTUP_BLOCKED,
            label=startup_blocked_display_label(reason=reason, detail=detail),
            detail=detail,
        )
    elif phase == "reconnecting":
        return RuntimeConnectionStatus(
            state=RuntimeConnectionState.RECONNECTING,
            reconnect_attempt=int(kwargs.get("reconnect_attempt", 0) or 0),
            reconnect_delay_s=float(kwargs.get("reconnect_delay_s", 0.0) or 0.0),
            detail=str(kwargs.get("error", "") or ""),
        )
    elif phase in {"cycle_ok", "cycle_started", "modem_read_missed"}:
        return RuntimeConnectionStatus(state=RuntimeConnectionState.LIVE)
    elif phase == "connection_lost":
        return RuntimeConnectionStatus(
            state=RuntimeConnectionState.DISCONNECTED,
        )
    elif phase in {"fatal_cycle", "fatal_runner", "fatal_startup"}:
        return RuntimeConnectionStatus(
            state=RuntimeConnectionState.DISCONNECTED,
            detail=str(kwargs.get("error", "") or ""),
        )
    elif phase == "shutdown":
        return RuntimeConnectionStatus(state=RuntimeConnectionState.SHUTDOWN)
    elif phase == "check_complete":
        return RuntimeConnectionStatus(state=RuntimeConnectionState.SHUTDOWN)
    else:
        return RuntimeConnectionStatus(
            state=RuntimeConnectionState.OFFLINE,
            detail=f"unknown phase: {phase}" if phase else "",
        )


def normalize_startup_blocked_reason(reason: Any = "", *, detail: Any = "") -> str:
    """Collapse startup-blocked causes into canonical user-facing buckets.

    Returns one of:
      "authentication_failed" — wrong credentials
      "wrong_target"         — wrong device (IMEI/serial/model mismatch)
      "modem_unreachable"    — device not reachable (network, power, etc.)
      "waiting_for_live_tick" — session established but no telemetry yet
      "unknown"              — unrecognised cause
    """
    raw_reason = str(reason or "").strip().lower()
    raw_detail = str(detail or "").strip().lower()
    combined = f"{raw_reason} {raw_detail}".strip()
    if not combined:
        return "unknown"
    if any(
        token in combined
        for token in (
            "authentication_failed",
            "authentication failed",
            "check password",
            "wrong username/password",
            "wrong password",
            "requires a password",
            "login failed",
        )
    ):
        return "authentication_failed"
    if any(
        token in combined
        for token in (
            "target_guard",
            "runtime_session_changed",
            "session changed",
            "identity changed",
            "hot swap",
            "wrong target",
            "wrong_target",
            "mismatch_serial",
            "mismatch_imei",
            "mismatch_model",
        )
    ):
        return "wrong_target"
    if any(
        token in combined
        for token in (
            "waiting_for_live_tick",
            "waiting for live",
            "waiting for first live",
            "no live tick",
            "no telemetry yet",
        )
    ):
        return "waiting_for_live_tick"
    if any(
        token in combined
        for token in (
            "modem_unreachable",
            "unreachable",
            "cannot connect",
            "connection refused",
            "timed out",
            "timeout",
            "refused",
            "offline",
            "no route to host",
            "host is down",
        )
    ):
        return "modem_unreachable"
    return "unknown"


def startup_blocked_display_label(*, reason: Any = "", detail: Any = "") -> str:
    """Return the canonical short label for startup-blocked user surfaces."""
    code = normalize_startup_blocked_reason(reason, detail=detail)
    if code == "authentication_failed":
        return "Authentication failed"
    if code == "wrong_target":
        return "Wrong device"
    if code == "waiting_for_live_tick":
        return "Waiting for live data"
    if code == "modem_unreachable":
        return "Device unreachable"
    return "Startup blocked"


# ── Valid transitions (for documentation and optional validation) ───────

VALID_TRANSITIONS: dict[RuntimeConnectionState, set[RuntimeConnectionState]] = {
    RuntimeConnectionState.OFFLINE: {
        RuntimeConnectionState.PROBING,
        RuntimeConnectionState.CONNECTING,
        RuntimeConnectionState.SHUTDOWN,
    },
    RuntimeConnectionState.PROBING: {
        RuntimeConnectionState.CONNECTING,
        RuntimeConnectionState.OFFLINE,
        RuntimeConnectionState.SHUTDOWN,
    },
    RuntimeConnectionState.CONNECTING: {
        RuntimeConnectionState.LIVE,
        RuntimeConnectionState.STARTUP_BLOCKED,
        RuntimeConnectionState.DISCONNECTED,
        RuntimeConnectionState.SHUTDOWN,
    },
    RuntimeConnectionState.LIVE: {
        RuntimeConnectionState.DISCONNECTED,
        RuntimeConnectionState.SHUTDOWN,
    },
    RuntimeConnectionState.DISCONNECTED: {
        RuntimeConnectionState.RECONNECTING,
        RuntimeConnectionState.SHUTDOWN,
    },
    RuntimeConnectionState.RECONNECTING: {
        RuntimeConnectionState.LIVE,
        RuntimeConnectionState.STARTUP_BLOCKED,
        RuntimeConnectionState.DISCONNECTED,
        RuntimeConnectionState.SHUTDOWN,
    },
    RuntimeConnectionState.STARTUP_BLOCKED: {
        RuntimeConnectionState.RECONNECTING,
        RuntimeConnectionState.SHUTDOWN,
    },
    RuntimeConnectionState.SHUTDOWN: set(),  # Terminal state
}


def is_valid_transition(
    from_state: RuntimeConnectionState,
    to_state: RuntimeConnectionState,
) -> bool:
    """Check whether a state transition is valid."""
    # Any state can transition to SHUTDOWN
    if to_state == RuntimeConnectionState.SHUTDOWN:
        return True
    return to_state in VALID_TRANSITIONS.get(from_state, set())
