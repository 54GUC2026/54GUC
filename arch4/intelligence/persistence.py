"""Independent checkpoint owner for the optional Intelligence customer."""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from arch4.brain.memory_persistence import (
    PreparedMemoryCheckpoint,
    checkpoint_due_reason as memory_checkpoint_due_reason,
    complete_memory_checkpoint,
    fail_memory_checkpoint,
    prepare_memory_checkpoint,
)
from arch4.intelligence.checkpoint_store import (
    IntelligenceCheckpointSelection,
    commit_checkpoint,
    select_checkpoint,
)


OWNER = "54guc.intelligence.persistence"
RECEIPT_SCHEMA = "IntelligencePersistenceReceipt.v1"


class IntelligencePersistenceCoordinator:
    """Commit learning state without participating in Core recovery."""

    def __init__(
        self,
        *,
        memory: Any,
        historical: Any,
        crystal_ball: Any,
        calibration_authority: Any,
        observation: Any,
        outcome: Any,
        clock: Any,
        configuration: Any,
        decision_cooldown_state: Callable[[], Mapping[str, Any]],
        restore_decision_cooldowns: Callable[[Mapping[str, Any]], None],
        publish_projection: Callable[[str, IntelligenceCheckpointSelection], Mapping[str, Any]],
        startup_selection: IntelligenceCheckpointSelection | None,
    ) -> None:
        self._memory = memory
        self._historical = historical
        self._crystal_ball = crystal_ball
        self._calibration_authority = calibration_authority
        self._observation = observation
        self._outcome = outcome
        self._clock = clock
        self._configuration = configuration
        if not callable(decision_cooldown_state):
            raise TypeError("intelligence_persistence_decision_export_required")
        if not callable(restore_decision_cooldowns):
            raise TypeError("intelligence_persistence_decision_restore_required")
        if not callable(publish_projection):
            raise TypeError("intelligence_persistence_projection_port_required")
        self._decision_cooldown_state = decision_cooldown_state
        self._restore_decision_cooldowns = restore_decision_cooldowns
        self._publish_projection = publish_projection
        self._selection = startup_selection
        self._commit_total = 0
        self._failure_total = 0
        self._last_error = ""
        self._last_receipt: dict[str, Any] = {}
        self._restored_cursor_evidence: dict[str, Any] = {}

    @property
    def selection(self) -> IntelligenceCheckpointSelection | None:
        return self._selection

    def _runtime_payload(self) -> dict[str, Any]:
        return {
            "schema": "IntelligenceRuntimeCheckpoint.v1",
            "owner": OWNER,
            "captured_at": float(time.time()),
            "core_cursors": {
                "observation": dict(self._observation.status() or {}),
                "outcome": dict(self._outcome.status() or {}),
                "clock": dict(self._clock.status() or {}),
                "configuration": dict(self._configuration.status() or {}),
            },
            "decision_cooldowns": dict(self._decision_cooldown_state() or {}),
        }

    def maybe_commit(self, *, force: bool = False) -> dict[str, Any]:
        memory_reason = memory_checkpoint_due_reason(self._memory, force=force)
        calibration_reason = self._calibration_authority.checkpoint_due_reason(
            force=force
        )
        reason = str(memory_reason or "")
        if not reason and calibration_reason:
            reason = f"calibration:{calibration_reason}"
        if not reason:
            receipt = {
                "schema": RECEIPT_SCHEMA,
                "owner": OWNER,
                "status": "not_due",
                "saved": False,
                "forced": bool(force),
                "finished_at": float(time.time()),
            }
            self._last_receipt = receipt
            return receipt

        started_at = float(time.time())
        prepared: PreparedMemoryCheckpoint | None = None
        try:
            transaction = getattr(
                self._calibration_authority,
                "state_transaction",
                None,
            )
            if not callable(transaction):
                raise RuntimeError(
                    "intelligence_calibration_state_transaction_required"
                )
            with transaction():
                prepared = prepare_memory_checkpoint(
                    self._memory,
                    checkpoint_reason=reason,
                )
                if prepared.deferred:
                    receipt = {
                        "schema": RECEIPT_SCHEMA,
                        "owner": OWNER,
                        "status": "deferred",
                        "saved": False,
                        "forced": bool(force),
                        "defer_reason": str(
                            prepared.receipt.get("defer_reason")
                            or "identity_scope_not_bound"
                        ),
                        "memory": dict(prepared.receipt),
                        "finished_at": float(time.time()),
                    }
                    self._last_receipt = receipt
                    return receipt
                payloads = {
                    **prepared.payloads,
                    "historical.json": self._historical.get_state_for_save(),
                    "crystal_ball.json": self._crystal_ball.to_dict(),
                    "calibration_authority.json": (
                        self._calibration_authority.checkpoint_payload()
                    ),
                    "intelligence_runtime.json": self._runtime_payload(),
                }
                manifest = commit_checkpoint(
                    self._memory.memory_dir,
                    payloads,
                    reason=reason,
                )
                selected = select_checkpoint(self._memory.memory_dir)
                if not isinstance(selected, IntelligenceCheckpointSelection):
                    raise RuntimeError(
                        "committed_intelligence_checkpoint_not_selectable"
                    )
                memory_receipt = complete_memory_checkpoint(
                    self._memory,
                    prepared,
                    selection=selected,
                )
                self._calibration_authority.note_intelligence_checkpoint_committed(
                    generation=selected.generation,
                    reason=reason,
                )
                self._selection = selected
            projection: dict[str, Any]
            try:
                projection = dict(
                    self._publish_projection(
                        str(self._memory.memory_dir),
                        selected,
                    )
                    or {}
                )
            except BaseException as exc:
                projection = {
                    "status": "degraded",
                    "complete": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            self._commit_total += 1
            self._last_error = ""
            receipt = {
                "schema": RECEIPT_SCHEMA,
                "owner": OWNER,
                "status": (
                    "committed"
                    if bool(projection.get("complete", False))
                    else "committed_projection_degraded"
                ),
                "saved": True,
                "forced": bool(force),
                "reason": reason,
                "generation": str(manifest.get("generation") or ""),
                "memory": memory_receipt,
                "portable_projection": projection,
                "started_at": started_at,
                "finished_at": float(time.time()),
            }
        except BaseException as exc:
            self._failure_total += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            memory_receipt = fail_memory_checkpoint(
                self._memory,
                prepared,
                error=exc,
            )
            receipt = {
                "schema": RECEIPT_SCHEMA,
                "owner": OWNER,
                "status": "failed",
                "saved": False,
                "forced": bool(force),
                "reason": reason,
                "error": self._last_error,
                "memory": memory_receipt,
                "started_at": started_at,
                "finished_at": float(time.time()),
            }
        self._last_receipt = receipt
        return receipt

    def restore_runtime_state(self) -> dict[str, Any]:
        if not isinstance(self._selection, IntelligenceCheckpointSelection):
            return {
                "schema": RECEIPT_SCHEMA,
                "owner": OWNER,
                "status": "not_found",
                "restored": False,
            }
        payload = self._selection.artifact("intelligence_runtime.json")
        if not isinstance(payload, Mapping):
            raise TypeError("intelligence_runtime_checkpoint_mapping_required")
        if payload.get("schema") != "IntelligenceRuntimeCheckpoint.v1":
            raise ValueError("intelligence_runtime_checkpoint_schema_invalid")
        if payload.get("owner") != OWNER:
            raise ValueError("intelligence_runtime_checkpoint_owner_invalid")
        self._restore_decision_cooldowns(
            dict(payload.get("decision_cooldowns") or {})
        )
        self._restored_cursor_evidence = dict(payload.get("core_cursors") or {})
        return {
            "schema": RECEIPT_SCHEMA,
            "owner": OWNER,
            "status": "restored",
            "restored": True,
            "generation": self._selection.generation,
            "core_cursor_evidence": dict(self._restored_cursor_evidence),
            "catch_up": "new_core_stream_epoch_requires_fresh_observation",
        }

    def status(self) -> dict[str, Any]:
        return {
            "schema": "IntelligencePersistenceStatus.v1",
            "owner": OWNER,
            "generation": (
                self._selection.generation
                if isinstance(self._selection, IntelligenceCheckpointSelection)
                else ""
            ),
            "commit_total": self._commit_total,
            "failure_total": self._failure_total,
            "degraded": bool(self._last_error),
            "last_error": self._last_error,
            "last_receipt": dict(self._last_receipt),
            "restored_cursor_evidence": dict(self._restored_cursor_evidence),
        }


__all__ = (
    "OWNER",
    "RECEIPT_SCHEMA",
    "IntelligencePersistenceCoordinator",
)
