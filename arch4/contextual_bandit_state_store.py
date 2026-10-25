"""Crash-consistent owner for Contextual Bandit posterior state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Any, Mapping

from arch4.atomic_io import atomic_write_text
from arch4.contextual_bandit import ContextualBanditState


SCHEMA = "ContextualBanditStateSnapshot.v1"
OWNER = "arch4.contextual_bandit_state_store"


class ContextualBanditStateStore:
    """Persist each admitted RewardLedger update before acknowledging it."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.root_dir / "contextual_bandit_state.json"
        self.previous_good_path = (
            self.root_dir / "contextual_bandit_state.previous_good.json"
        )
        self._lock = threading.RLock()
        self._sequence = 0
        self._restore_source = "empty"
        self._restore_error = ""
        self._state = self._restore()

    def state(self) -> ContextualBanditState:
        with self._lock:
            return ContextualBanditState.from_dict(self._state.as_dict())

    def consume_terminal_reward(
        self,
        entry: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            working = ContextualBanditState.from_dict(self._state.as_dict())
            receipt = working.consume_terminal_reward(entry, now=now)
            if not bool(receipt.get("admitted", False)):
                return receipt
            self._persist_working_state(working, now=now)
            return receipt

    def record_selection_policy_effects(
        self,
        selection_receipt: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            working = ContextualBanditState.from_dict(self._state.as_dict())
            receipt = working.admit_policy_effects(
                selection_receipt,
                now=now,
            )
            if bool(receipt.get("duplicate", False)):
                return receipt
            self._persist_working_state(working, now=now)
            return receipt

    def _persist_working_state(
        self,
        working: ContextualBanditState,
        *,
        now: float | None,
    ) -> None:
        payload = {
            "schema": SCHEMA,
            "owner": OWNER,
            "sequence": self._sequence + 1,
            "saved_ts": round(
                float(time.time() if now is None else now),
                6,
            ),
            "state": working.as_dict(),
        }
        payload["checksum"] = _checksum(payload)
        try:
            current = self._read_valid(self.path)
        except Exception as exc:
            # Startup may have recovered from previous-good while the
            # current snapshot remains torn/corrupt. Never copy that corrupt
            # file over the recovery point; repair current from admitted
            # in-memory truth.
            current = None
            current_error = f"current:{exc}"
            if current_error not in self._restore_error:
                self._restore_error = ";".join(
                    value
                    for value in (self._restore_error, current_error)
                    if value
                )
        if current is not None:
            atomic_write_text(self.previous_good_path, _encode(current))
        atomic_write_text(self.path, _encode(payload))
        self._state = working
        self._sequence = int(payload["sequence"])

    def has_consumed(self, ledger_key: str) -> bool:
        key = str(ledger_key or "")
        if not key:
            return False
        with self._lock:
            return key in set(
                self._state.as_dict().get("consumed_reward_ledger_keys") or []
            )

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": SCHEMA,
                "owner": OWNER,
                "sequence": self._sequence,
                "restore_source": self._restore_source,
                "restore_error": self._restore_error,
                "path": str(self.path),
            }

    def _restore(self) -> ContextualBanditState:
        errors: list[str] = []
        for source, path in (
            ("current", self.path),
            ("previous_good", self.previous_good_path),
        ):
            try:
                payload = self._read_valid(path)
                if payload is None:
                    continue
                state_payload = dict(payload.get("state") or {})
                if state_payload.get("schema") != "ContextualBanditState.v3":
                    raise ValueError("bandit_state_contract_invalid")
                state = ContextualBanditState.from_dict(state_payload)
            except Exception as exc:
                errors.append(f"{source}:{exc}")
                continue
            self._sequence = int(payload.get("sequence", 0) or 0)
            self._restore_source = source
            self._restore_error = ";".join(errors)
            return state
        self._restore_error = ";".join(errors)
        return ContextualBanditState()

    @staticmethod
    def _read_valid(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("bandit_snapshot_not_mapping")
        if payload.get("schema") != SCHEMA or payload.get("owner") != OWNER:
            raise ValueError("bandit_snapshot_contract_invalid")
        if str(payload.get("checksum") or "") != _checksum(payload):
            raise ValueError("bandit_snapshot_checksum_invalid")
        if not isinstance(payload.get("state"), Mapping):
            raise ValueError("bandit_snapshot_state_missing")
        return payload


def _checksum(payload: Mapping[str, Any]) -> str:
    content = {
        str(key): value
        for key, value in dict(payload or {}).items()
        if str(key) != "checksum"
    }
    return hashlib.sha256(
        json.dumps(
            content,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _encode(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(payload),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"


__all__ = ["ContextualBanditStateStore", "OWNER", "SCHEMA"]
