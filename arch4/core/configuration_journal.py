"""Durable Core-only journal for active and request-specific configuration truth."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from arch4.atomic_io import atomic_write_text
from arch4.privacy_scrub import scrub_record


SCHEMA = "CoreConfigurationJournal.v2"
OWNER = "54guc.core.configuration_journal"


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _receipt_digest(receipt: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(receipt).encode("utf-8")).hexdigest()


def _envelope(
    receipt: Mapping[str, Any],
    history: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    payload = dict(receipt or {})
    if not payload:
        raise ValueError("core_configuration_journal_receipt_required")
    generation_id = str(payload.get("generation_id") or "").strip()
    if not generation_id:
        raise ValueError("core_configuration_journal_generation_id_required")
    retained = {
        str(key): dict(value)
        for key, value in history.items()
    }
    retained[generation_id] = payload
    content = {
        "schema": SCHEMA,
        "owner": OWNER,
        "current_generation_id": generation_id,
        "receipts": retained,
    }
    content["content_sha256"] = _receipt_digest(content)
    return content


def _evidence_envelope(receipt: Mapping[str, Any]) -> dict[str, Any]:
    payload = scrub_record(dict(receipt or {}))
    content = {
        "schema": SCHEMA,
        "owner": OWNER,
        "current_generation_id": str(payload.get("generation_id") or ""),
        "receipts": {
            str(payload.get("generation_id") or ""): payload,
        },
        "privacy_scrubbed": True,
    }
    content["content_sha256"] = _receipt_digest(content)
    return content


class CoreConfigurationJournal:
    """Atomically persists only Core active-configuration correctness state."""

    def __init__(
        self,
        persistence_dir: str | Path,
        *,
        evidence_dir: str | Path | None = None,
    ) -> None:
        self.path = Path(persistence_dir) / "core_active_configuration.json"
        self.evidence_path = (
            Path(evidence_dir) / "core_active_configuration.json"
            if evidence_dir is not None
            else None
        )

    def commit(self, receipt: Mapping[str, Any]) -> bool:
        current = self._load_envelope(required=False)
        history = (
            dict(current.get("receipts") or {})
            if current
            else {}
        )
        envelope = _envelope(receipt, history)
        self._write_envelope(self.path, envelope)
        if self.evidence_path is not None and self.evidence_path != self.path:
            self._write_envelope(self.evidence_path, _evidence_envelope(receipt))
        return True

    def load(self) -> dict[str, Any]:
        envelope = self._load_envelope(required=False)
        if not envelope:
            return {}
        generation_id = str(
            envelope.get("current_generation_id") or ""
        )
        receipts = dict(envelope.get("receipts") or {})
        receipt = receipts.get(generation_id)
        if not isinstance(receipt, Mapping) or not receipt:
            raise ValueError(
                "core_configuration_journal_current_receipt_missing"
            )
        return dict(receipt)

    def find_by_operational_request_id(
        self,
        request_id: str,
    ) -> dict[str, Any]:
        identity = str(request_id or "").strip()
        if not identity:
            raise ValueError(
                "core_configuration_journal_request_id_required"
            )
        envelope = self._load_envelope(required=False)
        if not envelope:
            return {}
        matches: list[dict[str, Any]] = []
        for raw in dict(envelope.get("receipts") or {}).values():
            if not isinstance(raw, Mapping):
                raise TypeError(
                    "core_configuration_journal_history_receipt_invalid"
                )
            receipt = dict(raw)
            provenance = dict(receipt.get("provenance") or {})
            if identity in {
                str(provenance.get("operational_request_id") or ""),
                str(provenance.get("client_request_id") or ""),
            }:
                matches.append(receipt)
        if len(matches) > 1:
            raise RuntimeError(
                "core_configuration_journal_request_id_ambiguous"
            )
        return matches[0] if matches else {}

    def _load_envelope(self, *, required: bool) -> dict[str, Any]:
        if not self.path.exists():
            if required:
                raise FileNotFoundError(self.path)
            return {}
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("core_configuration_journal_invalid_json") from exc
        if not isinstance(envelope, dict):
            raise TypeError("core_configuration_journal_object_required")
        if set(envelope) != {
            "schema",
            "owner",
            "current_generation_id",
            "receipts",
            "content_sha256",
        }:
            raise ValueError("core_configuration_journal_contract_invalid")
        checksum = str(envelope.get("content_sha256") or "")
        content = dict(envelope)
        content.pop("content_sha256")
        if (
            envelope.get("schema") != SCHEMA
            or envelope.get("owner") != OWNER
            or checksum != _receipt_digest(content)
        ):
            raise ValueError("core_configuration_journal_checksum_mismatch")
        generation_id = str(
            envelope.get("current_generation_id") or ""
        ).strip()
        receipts = envelope.get("receipts")
        if (
            not generation_id
            or not isinstance(receipts, Mapping)
            or generation_id not in receipts
        ):
            raise ValueError(
                "core_configuration_journal_current_generation_invalid"
            )
        for key, receipt in receipts.items():
            if (
                not isinstance(key, str)
                or not key
                or not isinstance(receipt, Mapping)
                or str(receipt.get("generation_id") or "") != key
            ):
                raise ValueError(
                    "core_configuration_journal_history_invalid"
                )
        return dict(envelope)

    def export_current_run_evidence(self) -> bool:
        """Publish the current durable receipt into the run evidence directory."""

        if self.evidence_path is None:
            return False
        receipt = self.load()
        if not receipt:
            return False
        self._write_envelope(self.evidence_path, _evidence_envelope(receipt))
        return True

    def publish_evidence_receipt(self, receipt: Mapping[str, Any]) -> bool:
        if self.evidence_path is None:
            return False
        self._write_envelope(self.evidence_path, _evidence_envelope(receipt))
        return True

    @staticmethod
    def _write_envelope(path: Path, envelope: Mapping[str, Any]) -> None:
        atomic_write_text(path, _canonical(envelope))


__all__ = ("CoreConfigurationJournal", "OWNER", "SCHEMA")
