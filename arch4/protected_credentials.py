"""Current-user Windows DPAPI adapter for APN command credentials.

Foundation seals before IPC/persistence; the physical modem adapter unseals only
when constructing the request. Unsupported platforms fail, never use plaintext.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import sys


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(raw: bytes, *, decrypt: bool) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("apn_credentials_require_windows_dpapi")
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source = _Blob(len(raw), buffer)
    result = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    operation.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; deliberately not machine-wide protection.
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RuntimeError("apn_credentials_dpapi_failed")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(result.data, 0, result.size)
        kernel32.LocalFree(result.data)


def seal_apn_credentials(username: str, password: str | None) -> str:
    _validate(username, password)
    data = json.dumps({"username": username, "password": password}, ensure_ascii=True).encode()
    return "dpapi:v1:" + base64.b64encode(_crypt(data, decrypt=False)).decode("ascii")


def open_apn_credentials(protected: str) -> tuple[str, str | None]:
    if not isinstance(protected, str) or not protected.startswith("dpapi:v1:"):
        raise ValueError("apn_credentials_protected_value_required")
    try:
        raw = base64.b64decode(protected[9:], validate=True)
        value = json.loads(_crypt(raw, decrypt=True))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("apn_credentials_protected_value_invalid") from exc
    if type(value) is not dict or set(value) != {"username", "password"}:
        raise ValueError("apn_credentials_fields_invalid")
    _validate(value["username"], value["password"])
    return value["username"], value["password"]


def _validate(username: str, password: str | None) -> None:
    for key, value in (("username", username), ("password", password)):
        if key == "password" and value is None:
            continue  # Preserve modem password; distinct from explicitly blank.
        if type(value) is not str or len(value) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError(f"apn_credentials_{key}_invalid")
