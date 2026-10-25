"""Checksummed atomic journal for deterministic Core recovery facts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from arch4.atomic_io import atomic_write_text


SCHEMA = "CoreCorrectnessJournal.v1"
OWNER = "54guc.core.correctness_journal"
FILENAME = "core_correctness_journal.json"
STATE_SCHEMA = "CoreCorrectnessState.v2"
STATE_OWNER = "54guc.core.persistence"


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


class CoreCorrectnessJournal:
    def __init__(self, persistence_dir: str | Path) -> None:
        self.path = Path(persistence_dir) / FILENAME

    def commit(self, state: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(state or {})
        if payload.get("schema") != STATE_SCHEMA:
            raise ValueError("core_correctness_state_schema_required")
        if payload.get("owner") != STATE_OWNER:
            raise ValueError("core_correctness_state_owner_required")
        canonical = canonical_json(payload)
        envelope = {
            "schema": SCHEMA,
            "owner": OWNER,
            "state": payload,
            "state_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }
        atomic_write_text(self.path, canonical_json(envelope))
        return envelope

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("core_correctness_journal_invalid_json") from exc
        if not isinstance(envelope, dict):
            raise TypeError("core_correctness_journal_object_required")
        if envelope.get("schema") != SCHEMA or envelope.get("owner") != OWNER:
            raise ValueError("core_correctness_journal_contract_invalid")
        state = envelope.get("state")
        if not isinstance(state, dict):
            raise TypeError("core_correctness_journal_state_required")
        digest = hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()
        if str(envelope.get("state_sha256") or "") != digest:
            raise ValueError("core_correctness_journal_checksum_mismatch")
        if state.get("schema") != STATE_SCHEMA:
            raise ValueError("core_correctness_state_schema_invalid")
        if state.get("owner") != STATE_OWNER:
            raise ValueError("core_correctness_state_owner_invalid")
        return dict(state)


__all__ = (
    "FILENAME",
    "OWNER",
    "SCHEMA",
    "STATE_OWNER",
    "STATE_SCHEMA",
    "CoreCorrectnessJournal",
    "canonical_json",
)
