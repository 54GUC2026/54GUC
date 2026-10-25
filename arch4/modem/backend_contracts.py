"""Backend capability contracts for gradual modem API tightening."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from arch4.modem.base import LiveAcquisitionContract
from arch4.core_contracts.physical_commands import (
    PhysicalCommandDispatchEvidence,
)
from arch4.interfaces import LeverAction, LeverResult, ModemInput, NeighborCell


BACKEND_FEATURE_STATUSES: frozenset[str] = frozenset(
    {
        "ok",
        "empty_valid",
        "timeout",
        "auth_failed",
        "transport_error",
        "malformed_response",
        "unsupported",
        "stale_cache",
        "failed",
    }
)
BACKEND_FEATURE_STATUS_ALIASES: dict[str, str] = {
    "empty": "empty_valid",
    "fail": "failed",
    "failure": "failed",
    "error": "failed",
    "stale": "stale_cache",
    "transport": "transport_error",
    "malformed": "malformed_response",
}
BACKEND_ERROR_KINDS: frozenset[str] = frozenset(
    {
        "none",
        "empty_valid",
        "timeout",
        "auth_failed",
        "transport_error",
        "malformed_response",
        "unsupported",
        "stale_cache",
        "failed",
    }
)
BACKEND_STATUS_ERROR_KIND: dict[str, str] = {
    "ok": "none",
    "empty_valid": "empty_valid",
    "timeout": "timeout",
    "auth_failed": "auth_failed",
    "transport_error": "transport_error",
    "malformed_response": "malformed_response",
    "unsupported": "unsupported",
    "stale_cache": "stale_cache",
    "failed": "failed",
}


@dataclass(frozen=True)
class BackendIoContract:
    io_serial_required: bool = True
    startup_parallel_allowed: bool = False
    runtime_action_requires_io_lock: bool = True

    def as_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True)
class BackendFeatureResult:
    status: str
    value: Any = None
    reason: str = ""
    source: str = ""
    error_kind: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        status_text = str(self.status or "").strip().lower()
        status_text = BACKEND_FEATURE_STATUS_ALIASES.get(status_text, status_text)
        if status_text not in BACKEND_FEATURE_STATUSES:
            raise ValueError(f"unsupported backend feature result status: {self.status!r}")
        error_kind = str(self.error_kind or BACKEND_STATUS_ERROR_KIND.get(status_text, "failed")).strip().lower()
        error_kind = BACKEND_FEATURE_STATUS_ALIASES.get(error_kind, error_kind)
        if error_kind not in BACKEND_ERROR_KINDS:
            raise ValueError(f"unsupported backend feature error kind: {self.error_kind!r}")
        object.__setattr__(self, "status", status_text)
        object.__setattr__(self, "reason", str(self.reason or ""))
        object.__setattr__(self, "source", str(self.source or ""))
        object.__setattr__(self, "error_kind", error_kind)
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @property
    def supported(self) -> bool:
        return self.status != "unsupported"

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "empty_valid"}

    @property
    def failed(self) -> bool:
        return self.status in {
            "timeout",
            "auth_failed",
            "transport_error",
            "malformed_response",
            "stale_cache",
            "failed",
        }

    @property
    def empty(self) -> bool:
        return self.status == "empty_valid"

    @property
    def unsupported(self) -> bool:
        return self.status == "unsupported"

    @property
    def retryable(self) -> bool:
        return self.status in {"timeout", "transport_error", "stale_cache"}

    @property
    def terminal_error(self) -> bool:
        return self.status in {"auth_failed", "unsupported", "malformed_response"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "value": self.value,
            "reason": self.reason,
            "source": self.source,
            "error_kind": self.error_kind,
            "metadata": dict(self.metadata or {}),
        }


def backend_ok(value: Any, *, source: str = "", metadata: Mapping[str, Any] | None = None) -> BackendFeatureResult:
    return BackendFeatureResult(status="ok", value=value, source=source, error_kind="none", metadata=dict(metadata or {}))


def backend_empty(*, reason: str = "", source: str = "", metadata: Mapping[str, Any] | None = None) -> BackendFeatureResult:
    return BackendFeatureResult(
        status="empty_valid",
        value=None,
        reason=reason,
        source=source,
        error_kind="empty_valid",
        metadata=dict(metadata or {}),
    )


def backend_failed(
    *,
    reason: str,
    source: str = "",
    status: str = "failed",
    error_kind: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> BackendFeatureResult:
    return BackendFeatureResult(
        status=status,
        value=None,
        reason=reason,
        source=source,
        error_kind=error_kind or BACKEND_STATUS_ERROR_KIND.get(str(status or "").strip().lower(), "failed"),
        metadata=dict(metadata or {}),
    )


def backend_failed_from_exception(
    exc: Exception,
    *,
    source: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> BackendFeatureResult:
    status, error_kind = classify_backend_exception(exc)
    return backend_failed(
        reason=str(exc),
        source=source,
        status=status,
        error_kind=error_kind,
        metadata=dict(metadata or {}),
    )


def backend_unsupported(*, reason: str, source: str = "", metadata: Mapping[str, Any] | None = None) -> BackendFeatureResult:
    return BackendFeatureResult(
        status="unsupported",
        value=None,
        reason=reason,
        source=source,
        error_kind="unsupported",
        metadata=dict(metadata or {}),
    )


def coerce_backend_feature_result(
    value: Any,
    *,
    source: str = "",
    empty_reason: str = "empty",
    failed_reason: str = "failed",
    empty_values: tuple[Any, ...] = (None, "", (), [], {}),
) -> BackendFeatureResult:
    """Normalize legacy backend returns into explicit feature states.

    This is the Phase 10 bridge: old APIs can keep returning bool/dict/list
    while new callers stop confusing unsupported, empty, and failed states.
    """
    if isinstance(value, BackendFeatureResult):
        return value
    if isinstance(value, Mapping):
        status = str(value.get("status") or "").strip().lower()
        status = BACKEND_FEATURE_STATUS_ALIASES.get(status, status)
        if status in BACKEND_FEATURE_STATUSES:
            return BackendFeatureResult(
                status=status,
                value=value.get("value"),
                reason=str(value.get("reason") or value.get("error") or ""),
                source=str(value.get("source") or source),
                error_kind=str(value.get("error_kind") or ""),
                metadata=value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {},
            )
        if "ok" in value:
            if bool(value.get("ok")):
                return backend_ok(dict(value), source=source)
            return backend_failed(
                reason=str(value.get("reason") or value.get("error") or failed_reason),
                source=source,
                error_kind=str(value.get("error_kind") or ""),
                metadata={"legacy": dict(value)},
            )
        if "error" in value:
            reason = str(value.get("error") or value.get("reason") or failed_reason)
            status, error_kind = classify_backend_error_text(reason)
            return backend_failed(
                reason=reason,
                source=source,
                status=status,
                error_kind=error_kind,
                metadata={"legacy": dict(value)},
            )
    for empty_value in empty_values:
        if value == empty_value:
            return backend_empty(reason=empty_reason, source=source)
    return backend_ok(value, source=source)


def call_backend_feature(
    target: Any,
    method: str,
    *args: Any,
    source: str = "",
    unsupported_reason: str = "",
    empty_reason: str = "empty",
    failed_reason: str = "failed",
    empty_values: tuple[Any, ...] = (None, "", (), [], {}),
    fatal_exception_names: tuple[str, ...] = ("TargetGuardError",),
    **kwargs: Any,
) -> BackendFeatureResult:
    fn = getattr(target, method, None)
    feature_source = source or f"{target.__class__.__name__}.{method}"
    if not callable(fn):
        return backend_unsupported(
            reason=unsupported_reason or f"{method}_missing",
            source=feature_source,
        )
    try:
        return coerce_backend_feature_result(
            fn(*args, **kwargs),
            source=feature_source,
            empty_reason=empty_reason,
            failed_reason=failed_reason,
            empty_values=empty_values,
        )
    except Exception as exc:
        if exc.__class__.__name__ in set(fatal_exception_names or ()):
            raise
        return backend_failed_from_exception(exc, source=feature_source)


def classify_backend_exception(exc: Exception) -> tuple[str, str]:
    """Classify backend exceptions into the explicit I/O taxonomy."""
    name = exc.__class__.__name__.lower()
    text = str(exc or "").strip().lower()
    return classify_backend_error_text(f"{name} {text}", exc=exc)


def classify_backend_error_text(text: str, *, exc: Exception | None = None) -> tuple[str, str]:
    """Classify backend error text into the explicit I/O taxonomy."""
    haystack = str(text or "").strip().lower()
    if isinstance(exc, TimeoutError) or "timeout" in haystack or "timed out" in haystack:
        return "timeout", "timeout"
    if isinstance(exc, PermissionError) or "auth" in haystack or "login" in haystack or "permission" in haystack:
        return "auth_failed", "auth_failed"
    if "json" in haystack or "parse" in haystack or "malformed" in haystack or "decode" in haystack:
        return "malformed_response", "malformed_response"
    if isinstance(exc, OSError) or "transport" in haystack or "connection" in haystack or "network" in haystack:
        return "transport_error", "transport_error"
    return "failed", "failed"


def capability_result(capabilities: Mapping[str, Any], flag: str, *, value: Any = None, source: str = "capability_matrix") -> BackendFeatureResult:
    if not bool(capabilities.get(flag, False)):
        return backend_unsupported(reason=f"{flag}=false", source=source)
    if value in (None, [], {}, ""):
        return backend_empty(reason="supported_no_value", source=source)
    return backend_ok(value, source=source)


@runtime_checkable
class ModemSession(Protocol):
    def connect(self) -> bool: ...
    def disconnect(self) -> None: ...


@runtime_checkable
class ModemSnapshotReader(Protocol):
    def read_modem_input(self) -> ModemInput | None: ...
    def read_actionable_modem_input(self) -> ModemInput | None: ...
    def read_post_write_modem_input(
        self,
        stage: PhysicalCommandDispatchEvidence,
    ) -> ModemInput | None: ...
    def refresh_live_maintenance(self) -> None: ...


@runtime_checkable
class ModemActionExecutor(Protocol):
    def execute_lever(self, action: LeverAction) -> LeverResult: ...


@runtime_checkable
class CapabilityProvider(Protocol):
    def get_live_acquisition_contract(self) -> LiveAcquisitionContract: ...
    def verify_static_config(self) -> Mapping[str, bool]: ...
    def verify_ue_capability(self) -> Mapping[str, Any]: ...


@runtime_checkable
class NeighborScanner(Protocol):
    def read_neighbors(self) -> list[NeighborCell]: ...


@runtime_checkable
class NeighborScannerResult(Protocol):
    def read_neighbors_result(self) -> BackendFeatureResult: ...


@runtime_checkable
class WifiProvider(Protocol):
    def get_wifi_status(self) -> Mapping[str, Any]: ...


@runtime_checkable
class WifiProviderResult(Protocol):
    def get_wifi_status_result(self) -> BackendFeatureResult: ...
    def wifi_set_radio_result(self, radio: str, enabled: bool) -> BackendFeatureResult: ...


@runtime_checkable
class SmsProvider(Protocol):
    def read_sms(self) -> BackendFeatureResult: ...


@runtime_checkable
class RouterNativeFeatureProvider(Protocol):
    def restart_modem(self) -> bool: ...
    def read_band_priority(self) -> list[int]: ...
    def set_band_priority(self, bands: list[int]) -> bool: ...


@runtime_checkable
class RouterNativeFeatureProviderResult(Protocol):
    def read_band_priority_result(self) -> BackendFeatureResult: ...
    def set_band_priority_result(self, bands: list[int]) -> BackendFeatureResult: ...
