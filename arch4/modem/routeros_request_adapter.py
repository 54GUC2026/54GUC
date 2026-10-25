"""Paced RouterOS wire adapter owned by Modem Live.

Only this adapter may hold the raw RouterOS API object.  Resource objects are
wrapped so every get, set and call enters the common physical request authority.
"""

from __future__ import annotations

from typing import Any
from arch4.modem_live.startup_trace import traced_protocol

try:
    from routeros_api.exceptions import (
        RouterOsApiCommunicationError,
        RouterOsApiConnectionError,
        RouterOsApiError,
        RouterOsApiFatalCommunicationError,
        RouterOsApiParsingError,
    )
except ImportError:  # RouterOS support is optional in non-MikroTik installs.
    RouterOsApiError = ()  # type: ignore[assignment,misc]
    RouterOsApiCommunicationError = ()  # type: ignore[assignment,misc]
    _ROUTEROS_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = ()
    _ROUTEROS_PROTOCOL_ERRORS: tuple[type[BaseException], ...] = ()
else:
    _ROUTEROS_TRANSPORT_ERRORS = (
        RouterOsApiConnectionError,
        RouterOsApiFatalCommunicationError,
    )
    _ROUTEROS_PROTOCOL_ERRORS = (
        RouterOsApiCommunicationError,
        RouterOsApiParsingError,
    )

from arch4.modem_live.physical_request_authority import (
    ModemLivePhysicalRequestAuthority,
)


class RouterOsTransportError(ConnectionError):
    """The RouterOS wire or session is unavailable."""


class RouterOsProtocolError(RuntimeError):
    """RouterOS rejected or could not decode an otherwise completed request."""


def raise_typed_routeros_fault(exc: Exception, *, operation: str) -> None:
    if isinstance(exc, (RouterOsTransportError, RouterOsProtocolError)):
        raise exc
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) or (
        _ROUTEROS_TRANSPORT_ERRORS
        and isinstance(exc, _ROUTEROS_TRANSPORT_ERRORS)
    ):
        raise RouterOsTransportError(
            f"routeros_transport_error:{operation}:{type(exc).__name__}"
        ) from exc
    if _ROUTEROS_PROTOCOL_ERRORS and isinstance(exc, _ROUTEROS_PROTOCOL_ERRORS):
        raise RouterOsProtocolError(
            f"routeros_protocol_error:{operation}:{type(exc).__name__}"
        ) from exc
    if RouterOsApiError and isinstance(exc, RouterOsApiError):
        raise RouterOsProtocolError(
            f"routeros_protocol_error:{operation}:{type(exc).__name__}"
        ) from exc
    raise exc


def is_routeros_authentication_fault(exc: Exception) -> bool:
    """Recognize only a RouterOS login trap, never arbitrary exception text."""

    if not RouterOsApiCommunicationError or not isinstance(
        exc, RouterOsApiCommunicationError
    ):
        return False
    message = str(exc or "").strip().lower()
    return "login" in message and any(
        token in message
        for token in (
            "cannot log in",
            "login failure",
            "login failed",
            "bad password",
            "invalid user",
            "invalid username",
            "authentication failed",
            "not permitted",
            "permission denied",
        )
    )


class _RouterOsResource:
    def __init__(
        self,
        *,
        raw: Any,
        path: str,
        authority: ModemLivePhysicalRequestAuthority,
    ) -> None:
        self.__raw = raw
        self.__path = str(path or "").strip()
        self.__authority = authority

    def _run(self, method: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        lane = self.__authority.current_lane(
            default=(
                "event_write"
                if method in {"set", "add", "remove"}
                else "slow_live"
            )
        )
        with self.__authority.request(
            lane=lane,
            operation=f"routeros:{self.__path}:{method}",
            request_kind=("routeros_state_read" if method == "get" else "protected"),
        ):
            try:
                return traced_protocol(fn, operation=f"{self.__path}:{method}", args=args, kwargs=kwargs)
            except Exception as exc:
                raise_typed_routeros_fault(
                    exc,
                    operation=f"{self.__path}:{method}",
                )

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._run("get", self.__raw.get, *args, **kwargs)

    def set(self, *args: Any, **kwargs: Any) -> Any:
        return self._run("set", self.__raw.set, *args, **kwargs)

    def call(self, *args: Any, **kwargs: Any) -> Any:
        return self._run("call", self.__raw.call, *args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> Any:
        return self._run("add", self.__raw.add, *args, **kwargs)

    def remove(self, *args: Any, **kwargs: Any) -> Any:
        return self._run("remove", self.__raw.remove, *args, **kwargs)


class RouterOsRequestAdapter:
    """Expose only paced RouterOS operations to a modem backend."""

    def __init__(
        self,
        raw_api: Any,
        authority: ModemLivePhysicalRequestAuthority,
        *,
        disconnect_fn: Any = None,
    ) -> None:
        if raw_api is None:
            raise ValueError("routeros_raw_api_required")
        if not isinstance(authority, ModemLivePhysicalRequestAuthority):
            raise TypeError("routeros_request_authority_required")
        self.__raw_api = raw_api
        self.__authority = authority
        self.__disconnect_fn = disconnect_fn

    def get_resource(self, path: str) -> _RouterOsResource:
        raw = self.__raw_api.get_resource(path)
        return _RouterOsResource(
            raw=raw,
            path=path,
            authority=self.__authority,
        )

    def get_binary_resource(self, path: str) -> _RouterOsResource:
        raw = self.__raw_api.get_binary_resource(path)
        return _RouterOsResource(
            raw=raw,
            path=path,
            authority=self.__authority,
        )

    def disconnect(self) -> Any:
        disconnect_fn = self.__disconnect_fn
        if not callable(disconnect_fn):
            disconnect_fn = getattr(self.__raw_api, "disconnect", None)
        if not callable(disconnect_fn):
            raise RuntimeError("routeros_disconnect_operation_unavailable")
        lane = self.__authority.current_lane(default="disconnect")
        with self.__authority.request(
            lane=lane,
            operation="routeros:session:disconnect",
        ):
            try:
                return traced_protocol(disconnect_fn, operation="session:disconnect")
            except Exception as exc:
                raise_typed_routeros_fault(
                    exc,
                    operation="session:disconnect",
                )


__all__ = [
    "RouterOsProtocolError",
    "RouterOsRequestAdapter",
    "RouterOsTransportError",
    "is_routeros_authentication_fault",
    "raise_typed_routeros_fault",
]
