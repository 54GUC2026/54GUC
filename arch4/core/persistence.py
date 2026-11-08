"""Independent persistence for Core recovery."""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable, Mapping

from arch4.core.correctness_journal import (
    STATE_SCHEMA,
    CoreCorrectnessJournal,
    canonical_json,
)
from arch4.core_contracts.accepted_generation_root import (
    CoreAcceptedGenerationRoot,
)


OWNER = "54guc.core.persistence"
RECEIPT_SCHEMA = "CorePersistenceReceipt.v1"
DEFAULT_COMMIT_INTERVAL_S = 15.0


def _required_callable(value: Any, reason: str) -> Callable[..., Any]:
    if not callable(value):
        raise TypeError(reason)
    return value


class CorePersistenceCoordinator:
    """Capture and restore Core facts without importing any customer owner."""

    def __init__(
        self,
        *,
        persistence_dir: str,
        bouncer: Any,
        action_source_state: Any,
        export_mutation_state: Callable[[], Mapping[str, Any]],
        latest_accepted_root: Callable[[], Any],
        worker_health_status: Callable[[], Mapping[str, Any]],
        fault_status: Callable[[], Mapping[str, Any]],
        commit_interval_s: float = DEFAULT_COMMIT_INTERVAL_S,
    ) -> None:
        self._journal = CoreCorrectnessJournal(persistence_dir)
        for method_name in (
            "checkpoint_payload",
        ):
            if not callable(getattr(bouncer, method_name, None)):
                raise TypeError("core_persistence_bouncer_port_required")
        for method_name in ("checkpoint_payload",):
            if not callable(getattr(action_source_state, method_name, None)):
                raise TypeError("core_persistence_action_source_port_required")
        self._bouncer = bouncer
        self._action_source_state = action_source_state
        self._export_mutation_state = _required_callable(
            export_mutation_state,
            "core_persistence_export_mutation_required",
        )
        self._latest_accepted_root = _required_callable(
            latest_accepted_root,
            "core_persistence_latest_accepted_root_required",
        )
        self._worker_health_status = _required_callable(
            worker_health_status,
            "core_persistence_worker_health_required",
        )
        self._fault_status = _required_callable(
            fault_status,
            "core_persistence_fault_status_required",
        )
        self._commit_interval_s = max(1.0, float(commit_interval_s))
        self._last_commit_at = 0.0
        self._last_critical_digest = ""
        self._commit_total = 0
        self._failure_total = 0
        self._last_error = ""

    @property
    def path(self):
        return self._journal.path

    def _identity_correlation(self) -> dict[str, Any]:
        root = self._latest_accepted_root()
        if root is None:
            return {}
        if not isinstance(root, CoreAcceptedGenerationRoot):
            raise TypeError("core_persistence_latest_accepted_root_required")
        view = root.runtime_view
        identity = root.matrix_identity
        return {
            "session_scope_key": identity.session_scope_key,
            "identity_code": view.identity_code,
            "identity_status": view.identity_status,
            "access_type": view.access_type,
            "source_epoch": identity.source_epoch,
            "lifecycle_generation": identity.lifecycle_generation,
            "matrix_revision": identity.matrix_revision,
            "source_sequence": identity.matrix_sequence,
            "accepted_at": view.accepted_at,
        }

    def _capture(self, *, now: float) -> dict[str, Any]:
        return {
            "schema": STATE_SCHEMA,
            "owner": OWNER,
            "captured_at": now,
            "identity_correlation": self._identity_correlation(),
            "bouncer_safety": dict(self._bouncer.checkpoint_payload() or {}),
            "action_source_state": dict(
                self._action_source_state.checkpoint_payload() or {}
            ),
            "mutation_state": dict(self._export_mutation_state() or {}),
            "worker_fault_facts": {
                "worker_health": dict(self._worker_health_status() or {}),
                "fault_supervisor": dict(self._fault_status() or {}),
            },
        }

    @staticmethod
    def _critical_digest(state: Mapping[str, Any]) -> str:
        critical = {
            "identity_correlation": dict(state.get("identity_correlation") or {}),
            "bouncer_safety": dict(state.get("bouncer_safety") or {}),
            "action_source_state": dict(state.get("action_source_state") or {}),
            "mutation_state": dict(state.get("mutation_state") or {}),
        }
        return hashlib.sha256(canonical_json(critical).encode("utf-8")).hexdigest()

    def maybe_commit(
        self,
        *,
        force: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        captured_at = float(time.time() if now is None else now)
        started_at = float(time.time())
        try:
            state = self._capture(now=captured_at)
            critical_digest = self._critical_digest(state)
            critical_changed = critical_digest != self._last_critical_digest
            interval_due = (
                self._last_commit_at <= 0.0
                or captured_at - self._last_commit_at >= self._commit_interval_s
            )
            if not force and not critical_changed and not interval_due:
                return {
                    "schema": RECEIPT_SCHEMA,
                    "owner": OWNER,
                    "status": "not_due",
                    "saved": False,
                    "forced": False,
                    "finished_at": float(time.time()),
                }
            envelope = self._journal.commit(state)
            self._last_commit_at = captured_at
            self._last_critical_digest = critical_digest
            self._commit_total += 1
            self._last_error = ""
            return {
                "schema": RECEIPT_SCHEMA,
                "owner": OWNER,
                "status": "committed",
                "saved": True,
                "forced": bool(force),
                "critical_changed": bool(critical_changed),
                "captured_at": captured_at,
                "journal_sha256": str(envelope.get("state_sha256") or ""),
                "started_at": started_at,
                "finished_at": float(time.time()),
            }
        except BaseException as exc:
            self._failure_total += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            return {
                "schema": RECEIPT_SCHEMA,
                "owner": OWNER,
                "status": "failed",
                "saved": False,
                "forced": bool(force),
                "error": self._last_error,
                "started_at": started_at,
                "finished_at": float(time.time()),
            }

    def status(self) -> dict[str, Any]:
        return {
            "schema": "CorePersistenceStatus.v1",
            "owner": OWNER,
            "path": str(self._journal.path),
            "commit_total": self._commit_total,
            "failure_total": self._failure_total,
            "last_commit_at": self._last_commit_at,
            "degraded": bool(self._last_error),
            "last_error": self._last_error,
        }


__all__ = (
    "DEFAULT_COMMIT_INTERVAL_S",
    "OWNER",
    "RECEIPT_SCHEMA",
    "STATE_SCHEMA",
    "CorePersistenceCoordinator",
)
