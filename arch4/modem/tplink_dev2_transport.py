"""NX520v EU1 260408 encrypted DEV2 boundary; no legacy dialect fallback.

Wire access is supplied by the ModemLive backend's single paced HTTP adapter.
Reference: retained official proxy.js/tpEncrypt.js and NX510v issue #135.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from typing import Callable

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from arch4.modem.base import SessionExpiredError

ZERO_STACK = "0,0,0,0,0,0"
MAX_REPLY = 1_048_576


class Dev2ProtocolError(ConnectionError):
    """The selected firmware returned an invalid protocol response."""


class Dev2AuthenticationError(ConnectionError):
    pass


class Dev2Rejected(ConnectionError):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"tplink_command_rejected:{code}")


@dataclass(repr=False)
class Dev2Cipher:
    modulus: int
    exponent: int
    sequence: int
    credential_hash: str = field(repr=False)
    key: bytes = field(default_factory=lambda: secrets.token_hex(8).encode(), repr=False)
    iv: bytes = field(default_factory=lambda: secrets.token_hex(8).encode(), repr=False)

    def __post_init__(self):
        if self.modulus.bit_length() != 512 or self.exponent != 65537 or self.sequence < 0:
            raise Dev2ProtocolError("tplink_cbc_512_key_contract_mismatch")
        if len(self.key) != 16 or len(self.iv) != 16:
            raise Dev2ProtocolError("tplink_aes_key_contract_mismatch")

    def encrypt(self, text: str) -> str:
        padder = padding.PKCS7(128).padder()
        raw = padder.update(text.encode("utf-8")) + padder.finalize()
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).encryptor()
        return base64.b64encode(cipher.update(raw) + cipher.finalize()).decode("ascii")

    def decrypt(self, encoded: str) -> str:
        try:
            raw = base64.b64decode(encoded.strip(), validate=True)
            decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).decryptor()
            padded = decryptor.update(raw) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise Dev2ProtocolError("tplink_encrypted_response_invalid") from exc

    def envelope(self, text: str, *, login: bool = False) -> str:
        encoded = self.encrypt(text)
        signature_text = f"h={self.credential_hash}&s={self.sequence + len(encoded)}"
        if login:
            signature_text = f"key={self.key.decode('ascii')}&iv={self.iv.decode('ascii')}&" + signature_text
        raw = signature_text.encode("ascii")
        # Official rsa.encrypt(..., 512, 0): 64-byte chunks, RIGHT zero padding.
        signature = "".join(
            format(pow(int.from_bytes(raw[i:i+64].ljust(64, b"\0"), "big"), self.exponent, self.modulus), "0128x")
            for i in range(0, len(raw), 64)
        )
        return f"sign={signature}\r\ndata={encoded}\r\n"


def _js_scalar(text: str, name: str) -> str:
    found = re.findall(r"\bvar\s+" + re.escape(name) + r"\s*=\s*(['\"])([^'\"\r\n]*)\1\s*;", text)
    if len(found) != 1:
        raise Dev2ProtocolError(f"tplink_{name}_missing_or_ambiguous")
    return found[0][1]


def _return_code(text: str) -> int:
    matches = re.findall(r"\$\.ret\s*=\s*(-?\d+)\s*;", text)
    if len(matches) != 1:
        raise Dev2ProtocolError("tplink_return_code_missing_or_ambiguous")
    return int(matches[0])


class Dev2Session:
    """One authenticated session; recovery/retries belong to ModemLive.

    wire(method, path, body, headers, lane, operation) returns bounded text and
    owns HTTP status, redirects, cookies and the physical request authority.
    """
    def __init__(self, wire: Callable, username: str, password: str):
        self._wire = wire
        self._username = username
        self._password = password
        self._cipher: Dev2Cipher | None = None
        self._token = ""
        self._lock = threading.RLock()

    @property
    def authenticated(self) -> bool:
        return bool(self._cipher is not None and self._token)

    def clear(self):
        self._token = ""
        self._cipher = None

    def login(self):
        with self._lock:
            self.clear()
            text = self._wire("POST", "/cgi/getGDPRParm", "", {}, "session", "public_key")
            if _return_code(text) != 0:
                raise Dev2AuthenticationError("tplink_login_key_request_refused")
            try:
                self._cipher = Dev2Cipher(int(_js_scalar(text, "nn"), 16), int(_js_scalar(text, "ee"), 16),
                    int(_js_scalar(text, "seq")), hashlib.md5((self._username + self._password).encode()).hexdigest())
            except ValueError as exc:
                raise Dev2ProtocolError("tplink_key_response_invalid") from exc
            data = {"UserName": base64.b64encode(self._username.encode()).decode(),
                    "Passwd": base64.b64encode(self._password.encode()).decode(), "Action": "1"}
            try:
                result = self._exchange("cgi", "/cgi/login", data, ZERO_STACK, "session", login=True)
                code = _return_code(result)
                if code != 0:
                    raise Dev2AuthenticationError(f"tplink_login_refused:{code}")
                root = self._wire("GET", "/", None, {}, "session", "session_token")
                self._token = _js_scalar(root, "token")
                if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", self._token):
                    raise Dev2ProtocolError("tplink_session_token_invalid")
            except Exception:
                self.clear()
                raise

    def logout(self):
        with self._lock:
            try:
                if self.authenticated:
                    self._exchange("cgi", "/cgi/logout", {}, ZERO_STACK, "session")
            finally:
                self.clear()

    def request(self, operation: str, oid: str, data: dict | None = None, *, stack=ZERO_STACK, lane="fast_live"):
        object_operation = operation in {"go", "gl", "so"} and re.fullmatch(r"DEV2_[A-Z0-9_]+", oid)
        reboot_operation = (operation == "op" and oid == "ACT_REBOOT"
                            and data in (None, {}) and stack == ZERO_STACK)
        if not (object_operation or reboot_operation):
            raise ValueError("tplink_explicit_dev2_operation_required")
        if not re.fullmatch(r"\d+,\d+,\d+,\d+,\d+,\d+", stack):
            raise ValueError("tplink_stack_invalid")
        with self._lock:
            if not self.authenticated:
                raise SessionExpiredError("tplink_authenticated_session_required", confirmed=False)
            raw = self._exchange(operation, oid, data or {}, stack, lane)
            try:
                result = json.loads(raw)
            except ValueError as exc:
                raise Dev2ProtocolError("tplink_dev2_response_not_json") from exc
            if not isinstance(result, dict) or type(result.get("success")) is not bool:
                raise Dev2ProtocolError("tplink_dev2_response_invalid")
            if not result["success"]:
                code = result.get("errorcode")
                if type(code) is not int:
                    raise Dev2ProtocolError("tplink_dev2_error_code_invalid")
                raise Dev2Rejected(code)
            return result.get("data")

    def _exchange(self, operation, oid, data, stack, lane, *, login=False):
        if self._cipher is None:
            raise SessionExpiredError("tplink_cipher_required", confirmed=False)
        if not isinstance(data, dict) or any(k in data for k in ("stack", "pstack")):
            raise ValueError("tplink_object_data_invalid")
        # Native DEV2 idle accounting is activity-based, not request-based.
        # An actively running management client must declare that activity.
        payload = {"data": {"stack": stack, "pstack": ZERO_STACK, **data},
                   "operation": operation, "oid": oid, "isuseractive": True}
        body = self._cipher.envelope(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\r\n", login=login)
        encoded = self._wire("POST", "/cgi_gdpr?9", body, {"TokenID": self._token or "0"}, lane, f"{operation}:{oid}")
        if len(encoded) > MAX_REPLY:
            raise Dev2ProtocolError("tplink_response_too_large")
        try:
            return self._cipher.decrypt(encoded)
        except Dev2ProtocolError:
            # No unencrypted response fallback and no automatic replay of writes.
            self.clear()
            raise
