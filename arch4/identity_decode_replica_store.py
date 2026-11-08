"""Durable upper read replica for Modem Live identity publications.

This store never derives or edits identity meaning.  It persists only a fully
validated atomic Modem Live catalog/access/identity publication and revalidates
the entire envelope on restart.  A corrupt replacement is quarantined while
the in-memory previous-good replica remains installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from arch4.atomic_io import atomic_write_json
from arch4.modem_live.compatibility_identity import (
    DecodeCatalogReplica,
    IdentityContractError,
    validated_identity_publication_copy,
)


SCHEMA = "IdentityDecodeReplicaStore.v1"
OWNER = "arch4.identity_decode_replica_store"


def _retained_atomic_publication(validated: Mapping[str, Any]) -> dict[str, Any]:
    retained = {
        key: json.loads(json.dumps(validated[key]))
        for key in (
            "decode_catalog_snapshot",
            "access_type_contract",
            "modem_identity_key",
            "identity_publication_order",
            "schema",
            "owner",
            "code_namespace",
            "identity_code",
            "identity_status",
            "catalog_version",
            "catalog_checksum",
            "combination_guard",
            "access_type",
            "access_type_id",
            "access_type_fingerprint",
        )
        if key in validated
    }
    if isinstance(validated.get("modem_live_access_profile"), Mapping):
        retained["modem_live_access_profile"] = json.loads(
            json.dumps(validated["modem_live_access_profile"])
        )
    return retained


class IdentityDecodeReplicaStore:
    """Validate, atomically persist, and restart an immutable read replica."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._replica = DecodeCatalogReplica()
        self._current_publication: dict[str, Any] = {}
        self._last_error = ""
        self._lock = RLock()

    @property
    def replica(self) -> DecodeCatalogReplica:
        return self._replica

    @property
    def current_publication(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._current_publication))

    @property
    def last_error(self) -> str:
        with self._lock:
            return str(self._last_error or self._replica.last_error)

    def accept(self, publication: Mapping[str, Any], *, persist: bool = True) -> dict[str, Any]:
        """Install one validated Modem Live publication and optionally persist it."""

        with self._lock:
            try:
                retained: dict[str, Any] = {}
                def persist_validated(candidate: Mapping[str, Any]) -> None:
                    nonlocal retained
                    retained = _retained_atomic_publication(candidate)
                    catalog = candidate["decode_catalog_snapshot"]
                    envelope = {
                        "schema": SCHEMA,
                        "owner": OWNER,
                        "catalog_version": int(catalog["catalog_version"]),
                        "catalog_checksum": str(catalog["catalog_checksum"]),
                        "publication": retained,
                    }
                    if persist and (
                        retained != self._current_publication or not self.path.exists()
                    ):
                        atomic_write_json(self.path, envelope, indent=2, sort_keys=True,
                            ensure_ascii=False, trailing_newline=True)
                validated_identity_publication_copy(publication, replica=self._replica,
                    before_install=persist_validated)
                self._current_publication = retained
                self._last_error = ""
                return json.loads(json.dumps(retained))
            except (OSError, IdentityContractError, KeyError, TypeError, ValueError) as exc:
                self._last_error = str(exc)
                raise

    def load(self) -> dict[str, Any]:
        """Load and revalidate the durable replica, retaining previous-good on failure."""

        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, Mapping):
                    raise IdentityContractError("identity replica envelope must be a mapping")
                if raw.get("schema") != SCHEMA or raw.get("owner") != OWNER:
                    raise IdentityContractError("identity replica envelope schema or owner mismatch")
                publication = raw.get("publication")
                if not isinstance(publication, Mapping):
                    raise IdentityContractError("identity replica publication is missing")
                raw_catalog = publication.get("decode_catalog_snapshot")
                if not isinstance(raw_catalog, Mapping):
                    raise IdentityContractError("identity replica catalog is missing")
                if int(raw.get("catalog_version", 0) or 0) != int(
                    raw_catalog.get("catalog_version", 0) or 0
                ):
                    raise IdentityContractError("identity replica catalog version reference mismatch")
                if str(raw.get("catalog_checksum") or "") != str(
                    raw_catalog.get("catalog_checksum") or ""
                ):
                    raise IdentityContractError("identity replica catalog checksum reference mismatch")
                validated = self.accept(publication, persist=False)
                self._last_error = ""
                return validated
            except (OSError, json.JSONDecodeError, IdentityContractError, KeyError, TypeError, ValueError) as exc:
                self._last_error = str(exc)
                raise IdentityContractError(f"identity decode replica load rejected: {exc}") from exc


__all__ = ["IdentityDecodeReplicaStore", "OWNER", "SCHEMA"]
