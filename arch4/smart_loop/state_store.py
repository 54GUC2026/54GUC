"""Crash-consistent persistence owner for SmartLoop decision state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Any, Mapping

from arch4.atomic_io import atomic_write_text
from arch4.action_shape_legality import validate_action_shape
from arch4.interfaces import LeverType
from arch4.smart_loop.closed_loop import SmartLoopState


SCHEMA = "smart_loop.state_snapshot.v3"
OWNER = "arch4.smart_loop.state_store"
DEFAULT_OBSERVATION_SAVE_INTERVAL_S = 5.0


class SmartLoopStateStore:
    """Persist SmartLoop state before acknowledging its durable input receipts.

    Critical state (proofs, holds and behavior progress) is written immediately.
    Observation-only changes are coalesced for a short declared interval.  The
    current and previous-good snapshots are separately atomic and checksummed.
    """

    def __init__(
        self,
        root_dir: str | Path,
        *,
        observation_save_interval_s: float = DEFAULT_OBSERVATION_SAVE_INTERVAL_S,
    ) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.root_dir / "smart_loop_state.json"
        self.previous_good_path = self.root_dir / "smart_loop_state.previous_good.json"
        self._lock = threading.RLock()
        self._observation_save_interval_s = max(
            0.1,
            float(observation_save_interval_s or DEFAULT_OBSERVATION_SAVE_INTERVAL_S),
        )
        self._sequence = 0
        self._last_saved_ts = 0.0
        self._last_full_fingerprint = ""
        self._last_critical_fingerprint = ""
        self._restore_source = "empty"
        self._restore_error = ""
        self._write_count = 0
        self._coalesced_count = 0
        self._state = self._restore()

    def restore(self) -> SmartLoopState:
        with self._lock:
            return self._state

    def save(
        self,
        state: SmartLoopState,
        *,
        now: float | None = None,
        force: bool = False,
    ) -> bool:
        if not isinstance(state, SmartLoopState):
            raise TypeError("smart_loop_state_required")
        now_ts = float(time.time() if now is None else now)
        state_row = state.as_persistence_dict()
        full_fingerprint = _checksum(state_row)
        critical_fingerprint = _checksum(_critical_state(state_row))
        with self._lock:
            if full_fingerprint == self._last_full_fingerprint:
                self._state = state
                return False
            critical_changed = critical_fingerprint != self._last_critical_fingerprint
            due = (
                self._last_saved_ts <= 0.0
                or now_ts - self._last_saved_ts >= self._observation_save_interval_s
            )
            if not force and not critical_changed and not due:
                self._state = state
                self._coalesced_count += 1
                return False

            payload: dict[str, Any] = {
                "schema": SCHEMA,
                "owner": OWNER,
                "sequence": int(self._sequence) + 1,
                "saved_ts": round(now_ts, 6),
                "state": state_row,
            }
            payload["checksum"] = _checksum(payload)
            encoded = _encode(payload)

            try:
                current = self._read_valid(self.path)
                if current is not None:
                    current_state = SmartLoopState.from_dict(
                        dict(current.get("state") or {})
                    )
                    _validate_state(current_state)
                    atomic_write_text(self.previous_good_path, _encode(current))
            except Exception as exc:
                current = None
                self._restore_error = f"save_ignored_invalid_current:{exc}"
            atomic_write_text(self.path, encoded)

            self._sequence = int(payload["sequence"])
            self._last_saved_ts = now_ts
            self._last_full_fingerprint = full_fingerprint
            self._last_critical_fingerprint = critical_fingerprint
            self._state = state
            self._write_count += 1
            return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": SCHEMA,
                "owner": OWNER,
                "path": str(self.path),
                "previous_good_path": str(self.previous_good_path),
                "sequence": int(self._sequence),
                "last_saved_ts": round(float(self._last_saved_ts or 0.0), 6),
                "restore_source": self._restore_source,
                "restore_error": self._restore_error,
                "write_count": int(self._write_count),
                "coalesced_count": int(self._coalesced_count),
                "observation_save_interval_s": self._observation_save_interval_s,
            }

    def _restore(self) -> SmartLoopState:
        errors: list[str] = []
        for source, path in (
            ("current", self.path),
            ("previous_good", self.previous_good_path),
        ):
            try:
                payload = self._read_valid(path)
                if payload is None:
                    continue
                state = SmartLoopState.from_dict(dict(payload.get("state") or {}))
                _validate_state(state)
            except Exception as exc:
                errors.append(f"{source}:{exc}")
                continue
            self._sequence = int(payload.get("sequence") or 0)
            self._last_saved_ts = float(payload.get("saved_ts") or 0.0)
            state_row = state.as_persistence_dict()
            self._last_full_fingerprint = _checksum(state_row)
            self._last_critical_fingerprint = _checksum(_critical_state(state_row))
            self._restore_source = source
            self._restore_error = ";".join(errors)
            return state
        self._restore_source = "empty"
        self._restore_error = ";".join(errors)
        return SmartLoopState()

    @staticmethod
    def _read_valid(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("snapshot_not_mapping")
        if raw.get("schema") != SCHEMA or raw.get("owner") != OWNER:
            raise ValueError("snapshot_contract_mismatch")
        if str(raw.get("checksum") or "") != _checksum(raw):
            raise ValueError("snapshot_checksum_mismatch")
        if not isinstance(raw.get("state"), Mapping):
            raise ValueError("snapshot_state_missing")
        return raw


def _critical_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "pending_proof": state.get("pending_proof"),
        "objective_holds": dict(state.get("objective_holds") or {}),
        "normal_behavior_progress": dict(
            state.get("normal_behavior_progress") or {}
        ),
    }


def _validate_state(state: SmartLoopState) -> None:
    if state.normal_behavior_progress.as_dict().get("owner") != (
        "arch4.smart_loop.normal_behavior_progress"
    ):
        raise ValueError("smart_loop_normal_behavior_progress_invalid")
    proof = state.pending_proof
    if proof is not None and (
        not proof.command_id
        or not proof.proof_id
        or not proof.context_key
        or not proof.lever
        or not dict(proof.target_shape or {})
    ):
        raise ValueError("smart_loop_pending_proof_invalid")
    if proof is not None:
        _validate_target_shape(
            lever=proof.lever,
            target_shape=proof.target_shape,
            error="smart_loop_pending_proof_invalid",
        )
    for key, hold in state.objective_holds.items():
        if (
            not key
            or not hold.key
            or key != hold.key
            or not hold.lever
            or not dict(hold.target_shape or {})
        ):
            raise ValueError("smart_loop_objective_hold_invalid")
        _validate_target_shape(
            lever=hold.lever,
            target_shape=hold.target_shape,
            error="smart_loop_objective_hold_invalid",
        )
    for observation in state.observations:
        if (
            not observation.context_key
            or observation.ts <= 0.0
            or observation.pband <= 0
        ):
            raise ValueError("smart_loop_observation_context_invalid")


def _validate_target_shape(
    *, lever: str, target_shape: Mapping[str, Any], error: str
) -> None:
    try:
        lever_value = LeverType[str(lever or "").strip().upper()]
    except KeyError as exc:
        raise ValueError(error) from exc
    verdict = validate_action_shape(lever_value, dict(target_shape or {}))
    if not verdict.legal:
        raise ValueError(error)


def _checksum(payload: Mapping[str, Any]) -> str:
    normalized = {
        str(key): value
        for key, value in dict(payload or {}).items()
        if str(key) != "checksum"
    }
    raw = json.dumps(
        normalized,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _encode(payload: Mapping[str, Any]) -> str:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


def require_smart_loop_state_store(owner: object) -> SmartLoopStateStore:
    controller = getattr(owner, "controller", None)
    brain = getattr(controller, "brain", None)
    store = getattr(brain, "smart_loop_state_store", None)
    if not isinstance(store, SmartLoopStateStore):
        raise RuntimeError("smart_loop_state_store_unavailable")
    return store


__all__ = (
    "DEFAULT_OBSERVATION_SAVE_INTERVAL_S",
    "OWNER",
    "SCHEMA",
    "SmartLoopStateStore",
    "require_smart_loop_state_store",
)
