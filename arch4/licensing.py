"""Canonical modem-bound license generation and verification.

V0.1 retains the established vB5.3 offline contract: a 16/18-character
nonce and a 20-character uppercase HMAC-SHA256 key bound to the confirmed
modem identity.  This module is the sole algorithm owner used by both the
product verifier and the standalone license generator.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass


KNOWN_LICENSE_TIERS = ("tester_basic", "tester_trusted", "internal_full")
_LICENSE_TIER_CODES = {
    "tester_basic": "TB",
    "tester_trusted": "TT",
    "internal_full": "IF",
}
_LICENSE_CODE_TIERS = {code: tier for tier, code in _LICENSE_TIER_CODES.items()}

# This exact vB5.3 secret and derivation are the approved V0.1 contract.
_LICENSE_SECRET = b"LUC_v9_modem_license_2026"


@dataclass(frozen=True, slots=True)
class LicenseVerificationReceipt:
    valid: bool
    reason: str
    tier: str
    scheme: str = "hmac-sha256-v1"


def generate_token(modem_serial: str) -> str:
    """Produce the stable 16-character purchase token for a modem identity."""

    digest = hashlib.sha256(str(modem_serial or "").strip().encode("utf-8")).hexdigest()
    return digest[:16].upper()


def normalize_license_tier(tier: str, *, default: str = "") -> str:
    raw = str(tier or "").strip().lower()
    if raw in KNOWN_LICENSE_TIERS:
        return raw
    return str(default or "").strip().lower()


def encode_tiered_nonce(raw_nonce: str, *, tier: str = "") -> str:
    body = str(raw_nonce or "").strip().upper()
    normalized_tier = normalize_license_tier(tier)
    if not normalized_tier:
        return body
    return f"{_LICENSE_TIER_CODES[normalized_tier]}{body}"


def decode_license_nonce(nonce: str) -> tuple[str, str]:
    raw = str(nonce or "").strip().upper()
    if len(raw) == 16 and all(c in "0123456789ABCDEF" for c in raw):
        return "", raw
    if len(raw) == 18:
        code = raw[:2]
        body = raw[2:]
        tier = _LICENSE_CODE_TIERS.get(code, "")
        if tier and all(c in "0123456789ABCDEF" for c in body):
            return tier, body
    return "", ""


def license_tier_from_nonce(nonce: str) -> str:
    tier, _body = decode_license_nonce(nonce)
    return tier


def parse_license_entry(raw: str) -> tuple[str, str]:
    """Extract ``(nonce, key)`` from every supported operator paste format.

    The permanent operator contract accepts either the compact ``NONCE-KEY``
    string or the standalone generator clipboard block containing ``Nonce:``
    and ``Key:`` lines.  The cryptographic contract remains singular: callers
    must validate the extracted nonce/key through :func:`verify_license`.
    """

    text = str(raw or "").strip()
    if not text:
        return "", ""
    labeled_nonce = re.search(
        r"(?im)\bnonce\s*:\s*((?:TB|TT|IF)?[0-9A-F]{16})\b",
        text,
    )
    labeled_key = re.search(
        r"(?im)\b(?:license\s+key|key)\s*:\s*([0-9A-F]{20})\b",
        text,
    )
    if labeled_nonce and labeled_key:
        nonce = labeled_nonce.group(1).upper()
        if decode_license_nonce(nonce)[1]:
            return nonce, labeled_key.group(1).upper()
    compact = re.search(
        r"(?i)\b((?:TB|TT|IF)?[0-9A-F]{16})\s*-\s*([0-9A-F]{20})\b",
        text,
    )
    if compact:
        nonce = compact.group(1).upper()
        if decode_license_nonce(nonce)[1]:
            return nonce, compact.group(2).upper()
    return "", ""


def resolve_presentation_tier(*, licensed: bool, stored_tier: str = "") -> str:
    if not licensed:
        return "tester_basic"
    return (
        normalize_license_tier(stored_tier, default="internal_full") or "internal_full"
    )


def _derive_key(serial: str, nonce: str) -> str:
    """Apply the exact vB5.3 HMAC derivation to one identity and nonce."""

    normalized_serial = str(serial or "").strip()
    tier, nonce_body = decode_license_nonce(nonce)
    if nonce_body:
        payload = (
            f"{normalized_serial}:{nonce_body}:{tier}"
            if tier
            else f"{normalized_serial}:{nonce_body}"
        )
    else:
        payload = f"{normalized_serial}:{str(nonce or '').strip()}"
    return (
        hmac.new(
            _LICENSE_SECRET,
            payload.encode("utf-8"),
            hashlib.sha256,
        )
        .hexdigest()[:20]
        .upper()
    )


def generate_license(modem_serial: str, tier: str = "") -> tuple[str, str]:
    """Generate the established nonce/key pair for one modem identity."""

    serial = str(modem_serial or "").strip()
    if not serial:
        raise ValueError("license_identity_required")
    raw_nonce = secrets.token_hex(8).upper()
    nonce = encode_tiered_nonce(raw_nonce, tier=tier)
    return nonce, _derive_key(serial, nonce)


def verify_license(
    modem_serial: str,
    nonce: str,
    key: str,
) -> LicenseVerificationReceipt:
    """Return a typed receipt for the sole V0.1 HMAC license contract."""

    serial = str(modem_serial or "").strip()
    tier, nonce_body = decode_license_nonce(nonce)
    candidate = str(key or "").strip().upper()
    if not serial:
        return LicenseVerificationReceipt(False, "modem_identity_required", tier)
    if not nonce_body:
        return LicenseVerificationReceipt(False, "license_nonce_invalid", tier)
    if len(candidate) != 20 or any(c not in "0123456789ABCDEF" for c in candidate):
        return LicenseVerificationReceipt(False, "license_key_format_invalid", tier)
    expected = _derive_key(serial, nonce)
    if not hmac.compare_digest(candidate, expected):
        return LicenseVerificationReceipt(False, "license_key_invalid", tier)
    return LicenseVerificationReceipt(True, "verified", tier)


def validate_license(modem_serial: str, nonce: str, key: str) -> bool:
    return verify_license(modem_serial, nonce, key).valid


__all__ = [
    "KNOWN_LICENSE_TIERS",
    "LicenseVerificationReceipt",
    "decode_license_nonce",
    "encode_tiered_nonce",
    "generate_license",
    "generate_token",
    "license_tier_from_nonce",
    "normalize_license_tier",
    "parse_license_entry",
    "resolve_presentation_tier",
    "validate_license",
    "verify_license",
]
