"""Authenticated HTTP ubus transport for a standard OpenWrt host.

This module owns the remote session and JSON-RPC wire format.  The OpenWrt
modem adapter translates returned router/modem facts; it does not execute
commands on the controller computer.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import requests

from arch4.modem_live.startup_trace import traced_http

from arch4.modem.base import AuthenticationError
from arch4.modem_live.physical_request_authority import (
    ModemLivePhysicalRequestAuthority,
)


log = logging.getLogger("arch4.modem.openwrt_rpcd")

ZERO_SESSION_ID = "0" * 32
UBUS_PERMISSION_DENIED = 6


@dataclass(frozen=True)
class OpenWrtRpcdResult:
    success: bool
    data: Any
    code: int
    reason: str = ""

    @property
    def access_denied(self) -> bool:
        return self.code == UBUS_PERMISSION_DENIED


class OpenWrtRpcdTransport:
    """One serialized rpcd session for one standard OpenWrt router."""

    def __init__(
        self,
        *,
        host: str,
        username: str,
        password: str,
        timeout_s: float = 4.0,
        session: Optional[requests.Session] = None,
        physical_request_authority: Optional[
            ModemLivePhysicalRequestAuthority
        ] = None,
    ) -> None:
        host_text = str(host or "").strip().rstrip("/")
        if not host_text:
            raise ValueError("openwrt_rpcd_host_required")
        if not host_text.startswith(("http://", "https://")):
            host_text = f"http://{host_text}"
        self.base_url = host_text
        self.username = str(username or "root")
        self._password = str(password or "")
        self.timeout_s = max(0.2, float(timeout_s))
        self._session = session or requests.Session()
        self._ubus_session_id = ZERO_SESSION_ID
        self._connected = False
        self._request_id = 0
        self._lock = threading.RLock()
        self._physical_request_authority = physical_request_authority

    def bind_physical_request_authority(
        self,
        authority: ModemLivePhysicalRequestAuthority,
    ) -> None:
        if not isinstance(authority, ModemLivePhysicalRequestAuthority):
            raise TypeError("openwrt_rpcd_physical_request_authority_required")
        current = self._physical_request_authority
        if current is not None and current is not authority:
            raise RuntimeError("openwrt_rpcd_physical_request_authority_conflict")
        self._physical_request_authority = authority

    @property
    def connected(self) -> bool:
        return bool(self._connected and self._ubus_session_id != ZERO_SESSION_ID)

    def connect(self) -> bool:
        with self._lock:
            result = self._call_once(
                object_name="session",
                method="login",
                params={
                    "username": self.username,
                    "password": self._password,
                },
                session_id=ZERO_SESSION_ID,
            )
            data = result.data if isinstance(result.data, Mapping) else {}
            if result.access_denied:
                self._connected = False
                self._ubus_session_id = ZERO_SESSION_ID
                raise AuthenticationError("OpenWrt sign-in rejected; check credentials and access rights")
            token = str(data.get("ubus_rpc_session") or "").strip()
            self._connected = bool(result.success and token)
            self._ubus_session_id = token if self._connected else ZERO_SESSION_ID
            if not self._connected:
                log.warning(
                    "OpenWrt rpcd login failed at %s (%s)",
                    self.base_url,
                    result.reason or result.code,
                )
            return self._connected

    def close(self) -> None:
        with self._lock:
            self._connected = False
            self._ubus_session_id = ZERO_SESSION_ID
            self._session.close()

    def call(
        self,
        object_name: str,
        method: str,
        params: Optional[Mapping[str, Any]] = None,
    ) -> OpenWrtRpcdResult:
        with self._lock:
            if not self.connected:
                return OpenWrtRpcdResult(
                    success=False,
                    data={},
                    code=UBUS_PERMISSION_DENIED,
                    reason="openwrt_rpcd_session_unavailable",
                )
            result = self._call_once(
                object_name=object_name,
                method=method,
                params=params or {},
                session_id=self._ubus_session_id,
            )
            if result.access_denied:
                self._connected = False
                self._ubus_session_id = ZERO_SESSION_ID
            return result

    def _call_once(
        self,
        *,
        object_name: str,
        method: str,
        params: Mapping[str, Any],
        session_id: str,
    ) -> OpenWrtRpcdResult:
        self._request_id += 1
        request_id = self._request_id
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "call",
            "params": [
                str(session_id or ZERO_SESSION_ID),
                str(object_name or ""),
                str(method or ""),
                dict(params or {}),
            ],
        }
        authority = self._physical_request_authority
        if not isinstance(authority, ModemLivePhysicalRequestAuthority):
            raise RuntimeError(
                "openwrt_rpcd_physical_request_authority_unbound"
            )
        lane = authority.current_lane(
            default=("session" if object_name == "session" else "slow_live")
        )
        try:
            with authority.request(
                lane=lane,
                operation=f"openwrt:ubus:{object_name}:{method}",
            ):
                response = traced_http(self._session.post,
                    f"{self.base_url}/ubus",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=self.timeout_s,
                )
            response.raise_for_status()
        except requests.RequestException as exc:
            return OpenWrtRpcdResult(
                success=False,
                data={},
                code=-1,
                reason=f"transport_error:{type(exc).__name__}",
            )
        try:
            body = response.json()
        except requests.exceptions.JSONDecodeError:
            return OpenWrtRpcdResult(
                success=False,
                data={},
                code=-2,
                reason="invalid_json",
            )
        if not isinstance(body, Mapping):
            return OpenWrtRpcdResult(
                success=False,
                data={},
                code=-2,
                reason="invalid_response_shape",
            )
        error = body.get("error")
        if isinstance(error, Mapping):
            try:
                code = int(error.get("code", -3))
            except (TypeError, ValueError):
                code = -3
            return OpenWrtRpcdResult(
                success=False,
                data={},
                code=code,
                reason=str(error.get("message") or "jsonrpc_error"),
            )
        result = body.get("result")
        if not isinstance(result, (list, tuple)) or not result:
            return OpenWrtRpcdResult(
                success=False,
                data={},
                code=-2,
                reason="missing_ubus_result",
            )
        try:
            code = int(result[0])
        except (TypeError, ValueError):
            code = -2
        data = result[1] if len(result) > 1 else {}
        return OpenWrtRpcdResult(
            success=code == 0,
            data=data,
            code=code,
            reason="" if code == 0 else f"ubus_status_{code}",
        )


__all__ = [
    "OpenWrtRpcdResult",
    "OpenWrtRpcdTransport",
    "UBUS_PERMISSION_DENIED",
    "ZERO_SESSION_ID",
]
