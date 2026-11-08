"""Atomic owner for validated credentials and permanent boundary projections."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arch4.atomic_io import atomic_write_text
from arch4.licensing import normalize_license_tier, verify_license


SCHEMA = "LicenseCredentialRecord.v1"
OWNER = "arch4.license_credential_store"
STATE_FILENAME = "license_credentials.json"
PREVIOUS_FILENAME = "license_credentials.previous.json"
LICENSE_DAT_FILENAME = "license.dat"


class LicenseCredentialStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LicenseCredentialRecord:
    identity: str
    nonce: str
    key: str
    email: str = ""
    tier: str = ""
    updated_at_ts: float = 0.0

    def payload(self) -> dict[str, Any]:
        content = {
            "schema": SCHEMA,
            "owner": OWNER,
            "identity": self.identity.strip(),
            "nonce": self.nonce.strip(),
            "key": self.key.strip(),
            "email": self.email.strip(),
            "tier": normalize_license_tier(self.tier),
            "updated_at_ts": float(self.updated_at_ts),
        }
        return {**content, "content_sha256": _checksum(content)}


def _checksum(content: dict[str, Any]) -> str:
    encoded = json.dumps(content, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise LicenseCredentialStoreError("license credential state must be an object")
    allowed = {
        "schema",
        "owner",
        "identity",
        "nonce",
        "key",
        "email",
        "tier",
        "updated_at_ts",
        "content_sha256",
    }
    if set(payload) != allowed:
        raise LicenseCredentialStoreError("license credential state fields mismatch")
    if payload.get("schema") != SCHEMA or payload.get("owner") != OWNER:
        raise LicenseCredentialStoreError("license credential state owner mismatch")
    content = {name: payload[name] for name in allowed if name != "content_sha256"}
    for name in ("identity", "nonce", "key", "email", "tier"):
        if not isinstance(content[name], str):
            raise LicenseCredentialStoreError(
                "license credential text field is invalid"
            )
    if (
        not content["identity"].strip()
        or not content["nonce"].strip()
        or not content["key"].strip()
    ):
        raise LicenseCredentialStoreError(
            "license credential identity, nonce and key are required"
        )
    try:
        content["updated_at_ts"] = float(content["updated_at_ts"])
    except Exception as exc:
        raise LicenseCredentialStoreError(
            "license credential timestamp is invalid"
        ) from exc
    content["tier"] = normalize_license_tier(content["tier"])
    if str(payload.get("content_sha256") or "") != _checksum(content):
        raise LicenseCredentialStoreError("license credential checksum mismatch")
    verification = verify_license(
        content["identity"],
        content["nonce"],
        content["key"],
    )
    if not verification.valid:
        raise LicenseCredentialStoreError(
            f"license credential verification failed:{verification.reason}"
        )
    return {**content, "content_sha256": _checksum(content)}


def _read_valid(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return _validated_payload(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        mode=0o600,
    )


def _project_license_dat(root_dir: Path, payload: dict[str, Any]) -> None:
    lines = [payload["identity"], payload["nonce"], payload["key"]]
    if payload["tier"]:
        lines.append(payload["tier"])
    atomic_write_text(
        root_dir / LICENSE_DAT_FILENAME,
        "\n".join(lines) + "\n",
        mode=0o600,
    )


def reconcile_license_credentials(config_dir: str | Path) -> dict[str, Any] | None:
    """Restore canonical credentials and the named license.dat output adapter."""

    root_dir = Path(config_dir)
    current = _read_valid(root_dir / STATE_FILENAME)
    recovered_previous = False
    if current is None:
        current = _read_valid(root_dir / PREVIOUS_FILENAME)
        if current is None:
            return None
        _write_private_json(root_dir / STATE_FILENAME, current)
        recovered_previous = True
    _project_license_dat(root_dir, current)
    return {
        "schema": "LicenseCredentialReconciliation.v1",
        "owner": OWNER,
        "reconciled": True,
        "recovered_previous": recovered_previous,
        "content_sha256": current["content_sha256"],
    }


def save_license_credentials(
    *,
    identity: str,
    nonce: str,
    key: str,
    email: str = "",
    tier: str = "",
    config_dir: str | Path,
    now: float | None = None,
) -> dict[str, Any]:
    """Commit canonical credentials first, then repair license.dat output."""

    root_dir = Path(config_dir)
    record = LicenseCredentialRecord(
        identity=str(identity or "").strip(),
        nonce=str(nonce or "").strip(),
        key=str(key or "").strip(),
        email=str(email or "").strip(),
        tier=str(tier or "").strip(),
        updated_at_ts=float(time.time() if now is None else now),
    )
    payload = _validated_payload(record.payload())
    root_dir.mkdir(parents=True, exist_ok=True)
    current_path = root_dir / STATE_FILENAME
    previous = _read_valid(current_path)
    if previous is not None:
        _write_private_json(root_dir / PREVIOUS_FILENAME, previous)
    _write_private_json(current_path, payload)
    _project_license_dat(root_dir, payload)
    return {
        "schema": "LicenseCredentialCommit.v1",
        "owner": OWNER,
        "saved": True,
        "content_sha256": payload["content_sha256"],
        "projections": {"license.dat": "reconciled"},
    }


def load_license_credentials(config_dir: str | Path) -> dict[str, Any] | None:
    payload = _read_valid(Path(config_dir) / STATE_FILENAME)
    if payload is None:
        payload = _read_valid(Path(config_dir) / PREVIOUS_FILENAME)
    return dict(payload) if payload is not None else None
