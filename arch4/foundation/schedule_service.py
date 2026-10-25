"""Foundation-owned typed schedule planning and cancellation service."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from threading import RLock
import time as time_module
from time import time
from typing import Any, Callable, Mapping

from arch4.atomic_io import atomic_write_json
from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
    RuntimeGenerationNamespace,
)
from arch4.core.command_context import require_core_command_ingress_context
from arch4.core.band_action_policy import OPERATOR_BAND_COOLDOWN_S
from arch4.core.command_gateway import require_core_command_gateway
from arch4.core.accepted_root_outbox import (
    require_core_accepted_root_outbox,
)
from arch4.core.command_request_builder import (
    build_core_command_request,
    build_core_command_target,
)
from arch4.core.lever_action_adapter import lever_action_authority
from arch4.core_contracts.clients import CoreClientRole
from arch4.command_transactions import (
    CommandRootState,
    CommandTerminalReceipt,
    CommandTerminalStatus,
)
from arch4.core_contracts.commands import CoreCommandOrigin, CoreCommandRequest
from arch4.core_contracts.accepted_generation_root import (
    CoreAcceptedGenerationRoot,
)
from arch4.core_contracts.payload import CoreFrozenPayload
from arch4.core_contracts.runtime import CoreRuntimePort
from arch4.operational_service_contracts import (
    BandScheduleCancelRequest,
    BandSchedulePlanRequest,
    BandSequenceCancelRequest,
    BandSequencePlanRequest,
    FoundationOperatorRequestContext,
    OperationalDisposition,
    OperationalKind,
    OperationalServiceReceipt,
    ProductOperationalEffectPermit,
    operational_request_fingerprint,
    operational_request_kind,
    require_product_operational_effect_permit,
)
from arch4.trajectory_contract import (
    BAND_SEQUENCE_INTERVAL_REJECT_MESSAGE,
    MIN_BAND_SEQUENCE_INTERVAL_S,
)


OWNER = "54guc.foundation.schedule"
FOUNDATION_SCHEDULE_CLIENT_ID = "54guc.foundation.schedule.observation"
FOUNDATION_SCHEDULE_OUTCOME_CLIENT_ID = "54guc.foundation.schedule.outcome"
STATE_FILENAME = "foundation_schedule_state.json"
BAND_DELTA_SCHEDULE_PLAN_TYPE = "band_delta_schedule"
MAX_BAND_DELTA_SCHEDULE_RULES = 24
DAILY_SCHEDULE_RETRY_DELAY_S = 60.0
DAILY_SCHEDULE_MAX_ATTEMPTS = 2
_SCHEDULE_TRANSIENT_EXECUTION_FIELDS = frozenset(
    {
        "active_target",
        "active_rule_id",
        "active_occurrence_id",
        "active_due_ts",
        "executing_since_ts",
        "core_command_request_id",
        "core_command_request",
        "core_command_root_id",
        "core_command_matrix_identity",
        "dispatch_started_ts",
    }
)
log = logging.getLogger("arch4.foundation.schedule_service")


class FoundationScheduleService:
    def __init__(
        self,
        core: CoreRuntimePort,
        *,
        persistence_dir: str | Path,
        fault_reporter: Callable[[str, str, BaseException, str], None],
    ) -> None:
        if core is None:
            raise TypeError("foundation_schedule_core_port_required")
        if not callable(fault_reporter):
            raise TypeError("foundation_schedule_fault_reporter_required")
        self._core = core
        self._fault_reporter = fault_reporter
        self._accepted_root_outbox = require_core_accepted_root_outbox(core)
        outcome_stream = getattr(core, "command_outcome_stream", None)
        if not all(
            callable(getattr(outcome_stream, name, None))
            for name in ("claim_next", "acknowledge", "release")
        ):
            raise TypeError("foundation_schedule_core_command_outcome_port_required")
        self._outcome_stream = outcome_stream
        self._latest_accepted_root: CoreAcceptedGenerationRoot | None = None
        self._state_lock = RLock()
        self._observation_lock = RLock()
        self._state_path = Path(persistence_dir).resolve() / STATE_FILENAME
        self._state: dict[str, Any] = self._restore_state()

    def service_once(self, *, now: float | None = None) -> dict[str, Any]:
        """Advance schedule lifecycle from Core observations and outcomes once."""

        observed_at = float(time() if now is None else now)
        self._drain_observations()
        state = self.state()
        if not state:
            self._drain_command_outcomes()
            return {"owner": OWNER, "status": "idle", "submitted": False}
        lifecycle = _text(state.get("lifecycle_state")).lower()
        if lifecycle == "armed" and _is_band_delta_schedule_state(state):
            state = self._rebind_armed_schedule(state)
            lifecycle = _text(state.get("lifecycle_state")).lower()
        submitted = False
        reason = lifecycle
        if lifecycle in {"dispatching", "executing"}:
            result = self._recover_inflight_dispatch(
                state,
                now=observed_at,
            )
            submitted = bool(result.get("submitted", False))
            reason = str(result.get("reason") or reason)
        # A restored dispatch must bind its saved Core root before the schedule
        # can claim or acknowledge its final outcome.
        self._drain_command_outcomes()
        state = self.state()
        lifecycle = _text(state.get("lifecycle_state")).lower()
        if lifecycle in {"armed", "due"}:
            result = self._submit_if_due(state, now=observed_at)
            submitted = bool(result.get("submitted", False))
            reason = str(result.get("reason") or reason)
        return {
            "owner": OWNER,
            "status": str(self.state().get("lifecycle_state") or "idle"),
            "submitted": submitted,
            "reason": reason,
        }

    def plan_band_schedule(
        self,
        request: BandSchedulePlanRequest,
        *,
        permit: ProductOperationalEffectPermit,
    ) -> OperationalServiceReceipt:
        if not isinstance(request, BandSchedulePlanRequest):
            raise TypeError("foundation_band_schedule_typed_request_required")
        require_product_operational_effect_permit(
            permit,
            request,
            expected_kind=OperationalKind.PLAN_BAND_SCHEDULE,
        )
        rejection, expected_revision = self._precheck(
            request.context.request_id,
            request.interval_s,
        )
        if rejection:
            return self._rejected(
                request.context,
                OperationalKind.PLAN_BAND_SCHEDULE,
                rejection,
            )
        current = self.state()
        current_lifecycle = _text(current.get("lifecycle_state")).lower()
        if (
            current
            and _text(current.get("operation"))
            == OperationalKind.PLAN_BAND_SEQUENCE.value
            and current_lifecycle not in {
                "verified",
                "failed",
                "expired",
                "canceled",
            }
        ):
            return self._rejected(
                request.context,
                OperationalKind.PLAN_BAND_SCHEDULE,
                "foundation_band_sequence_active",
            )
        try:
            rule = _band_delta_schedule_rule(
                request,
                created_at=time(),
            )
        except (TypeError, ValueError) as exc:
            return self._rejected(
                request.context,
                OperationalKind.PLAN_BAND_SCHEDULE,
                str(exc),
            )
        existing_rules: list[dict[str, Any]] = []
        existing_progress: dict[str, Any] = {}
        if _is_band_delta_schedule_state(current):
            existing_rules = [
                dict(item)
                for item in _schedule_rules(_mapping(current.get("plan")))
            ]
            existing_progress = _mapping(current.get("rule_progress"))
            existing_kinds = {
                _text(item.get("recurrence")) for item in existing_rules
            }
            if existing_kinds and rule["recurrence"] not in existing_kinds:
                return self._rejected(
                    request.context,
                    OperationalKind.PLAN_BAND_SCHEDULE,
                    "band_schedule_daily_and_once_cannot_be_mixed",
                )
        rules = list(existing_rules)
        if all(
            _text(item.get("rule_id")) != rule["rule_id"]
            for item in rules
        ):
            rules.append(rule)
        rules = sorted(rules, key=_schedule_rule_sort_key)
        if len(rules) > MAX_BAND_DELTA_SCHEDULE_RULES:
            return self._rejected(
                request.context,
                OperationalKind.PLAN_BAND_SCHEDULE,
                "band_schedule_rule_limit_reached",
            )
        progress = {
            key: dict(value)
            for key, value in existing_progress.items()
            if key in {_text(item.get("rule_id")) for item in rules}
            and isinstance(value, Mapping)
        }
        if rule["recurrence"] == "daily":
            active = _current_daily_occurrence(rule, now=time())
            progress.setdefault(
                rule["rule_id"],
                {
                    "last_completed_occurrence": active["occurrence_id"],
                    "last_outcome": "definition_armed_without_immediate_execution",
                    "completed_at": time(),
                    "attempt_count": 0,
                },
            )
        plan = _schedule_plan(
            rules=rules,
            approval=request.requires_user_approval,
        )
        return self._publish(
            request.context,
            OperationalKind.PLAN_BAND_SCHEDULE,
            plan,
            request_fingerprint=operational_request_fingerprint(request),
            lifecycle_state="armed",
            message=(
                f"Daily band {request.operation} rule added."
                if rule["recurrence"] == "daily"
                else f"One-time band {request.operation} rule armed."
            ),
            expected_revision=expected_revision,
            state_extras={"rule_progress": progress},
        )

    def plan_band_sequence(
        self,
        request: BandSequencePlanRequest,
        *,
        permit: ProductOperationalEffectPermit,
    ) -> OperationalServiceReceipt:
        if not isinstance(request, BandSequencePlanRequest):
            raise TypeError("foundation_band_sequence_typed_request_required")
        require_product_operational_effect_permit(
            permit,
            request,
            expected_kind=OperationalKind.PLAN_BAND_SEQUENCE,
        )
        rejection, expected_revision = self._precheck(
            request.context.request_id,
            request.interval_s,
            sequence=True,
        )
        if rejection:
            return self._rejected(
                request.context,
                OperationalKind.PLAN_BAND_SEQUENCE,
                rejection,
            )
        current = self.state()
        if (
            _is_band_delta_schedule_state(current)
            and _text(current.get("lifecycle_state")).lower() == "armed"
            and _schedule_rules(_mapping(current.get("plan")))
        ):
            return self._rejected(
                request.context,
                OperationalKind.PLAN_BAND_SEQUENCE,
                "foundation_daily_band_schedule_active",
            )
        serving, current_pool = self._current_radio(
            request.context.matrix_identity
        )
        bands = list(request.bands) or list(current_pool)
        plan = _sequence_plan(
            serving=serving,
            current_pool=current_pool,
            bands=bands,
            ca_bands=list(request.ca_bands),
            sequence_mode=request.sequence_mode,
            interval_s=request.interval_s,
            approval=request.requires_user_approval,
        )
        if not plan.get("steps"):
            return self._rejected(
                request.context,
                OperationalKind.PLAN_BAND_SEQUENCE,
                "band_sequence_steps_unavailable",
            )
        return self._publish(
            request.context,
            OperationalKind.PLAN_BAND_SEQUENCE,
            plan,
            request_fingerprint=operational_request_fingerprint(request),
            lifecycle_state="armed",
            message="Band sequence armed.",
            expected_revision=expected_revision,
        )

    def cancel_band_schedule(
        self,
        request: BandScheduleCancelRequest,
        *,
        permit: ProductOperationalEffectPermit,
    ) -> OperationalServiceReceipt:
        if not isinstance(request, BandScheduleCancelRequest):
            raise TypeError("foundation_band_schedule_cancel_typed_request_required")
        require_product_operational_effect_permit(
            permit,
            request,
            expected_kind=OperationalKind.CANCEL_BAND_SCHEDULE,
        )
        rejection, expected_revision = self._precheck(
            request.context.request_id,
            None,
        )
        if rejection:
            return self._rejected(
                request.context,
                OperationalKind.CANCEL_BAND_SCHEDULE,
                rejection,
            )
        return self._cancel(
            request.context,
            OperationalKind.CANCEL_BAND_SCHEDULE,
            request_fingerprint=operational_request_fingerprint(request),
            expected_operation=OperationalKind.PLAN_BAND_SCHEDULE,
            reason=request.reason,
            expected_revision=expected_revision,
        )

    def cancel_band_sequence(
        self,
        request: BandSequenceCancelRequest,
        *,
        permit: ProductOperationalEffectPermit,
    ) -> OperationalServiceReceipt:
        if not isinstance(request, BandSequenceCancelRequest):
            raise TypeError("foundation_band_sequence_cancel_typed_request_required")
        require_product_operational_effect_permit(
            permit,
            request,
            expected_kind=OperationalKind.CANCEL_BAND_SEQUENCE,
        )
        rejection, expected_revision = self._precheck(
            request.context.request_id,
            None,
        )
        if rejection:
            return self._rejected(
                request.context,
                OperationalKind.CANCEL_BAND_SEQUENCE,
                rejection,
            )
        return self._cancel(
            request.context,
            OperationalKind.CANCEL_BAND_SEQUENCE,
            request_fingerprint=operational_request_fingerprint(request),
            expected_operation=OperationalKind.PLAN_BAND_SEQUENCE,
            reason=request.reason,
            expected_revision=expected_revision,
        )

    def reconcile_operational_request(
        self,
        request: (
            BandSchedulePlanRequest
            | BandSequencePlanRequest
            | BandScheduleCancelRequest
            | BandSequenceCancelRequest
        ),
    ) -> OperationalServiceReceipt | None:
        state = self.state()
        if not state:
            return None
        matrix = state.get("matrix_identity")
        if (
            not isinstance(matrix, Mapping)
            or MatrixGenerationIdentity.from_mapping(matrix)
            != request.context.matrix_identity
        ):
            return None
        kind = operational_request_kind(request)
        if kind in {
            OperationalKind.PLAN_BAND_SCHEDULE,
            OperationalKind.PLAN_BAND_SEQUENCE,
        }:
            matched = (
                _text(state.get("request_id"))
                == request.context.request_id
                and _text(state.get("operation")) == kind.value
                and _text(state.get("operational_request_fingerprint"))
                == operational_request_fingerprint(request)
            )
        else:
            expected_operation = (
                OperationalKind.PLAN_BAND_SCHEDULE
                if kind is OperationalKind.CANCEL_BAND_SCHEDULE
                else OperationalKind.PLAN_BAND_SEQUENCE
            )
            matched = (
                _text(state.get("cancellation_request_id"))
                == request.context.request_id
                and _text(state.get("operation"))
                == expected_operation.value
                and _text(state.get("lifecycle_state"))
                == "canceled"
                and _text(
                    state.get("cancellation_request_fingerprint")
                )
                == operational_request_fingerprint(request)
            )
        if not matched:
            return None
        return OperationalServiceReceipt(
            request_id=request.context.request_id,
            kind=kind,
            owner=OWNER,
            disposition=OperationalDisposition.CONFIRMED,
            message={
                OperationalKind.PLAN_BAND_SCHEDULE: (
                    "Band schedule recovered."
                ),
                OperationalKind.PLAN_BAND_SEQUENCE: (
                    "Band sequence recovered."
                ),
                OperationalKind.CANCEL_BAND_SCHEDULE: (
                    "Band schedule cancellation recovered."
                ),
                OperationalKind.CANCEL_BAND_SEQUENCE: (
                    "Band sequence cancellation recovered."
                ),
            }[kind],
            occurred_at=time(),
            runtime_namespace=request.context.runtime_namespace,
            matrix_identity=request.context.matrix_identity,
            detail=CoreFrozenPayload.from_mapping(state),
        )

    def _restore_state(self) -> dict[str, Any]:
        if not self._state_path.is_file():
            return {}
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("foundation_schedule_state_mapping_required")
            if payload.get("artifact") != STATE_FILENAME:
                raise ValueError("foundation_schedule_state_artifact_invalid")
            if "schema_version" in payload:
                raise ValueError(
                    "foundation_schedule_state_versioned_contract_forbidden"
                )
            if payload.get("owner") != OWNER:
                raise ValueError("foundation_schedule_state_owner_invalid")
            return _validate_schedule_state(payload)
        except Exception as exc:
            try:
                self._fault_reporter(
                    OWNER,
                    "restore_schedule_state",
                    exc,
                    "schedule_disabled_until_operator_replans",
                )
            except Exception as report_error:
                log.error(
                    "Foundation schedule fault reporting failed: %s: %s",
                    type(report_error).__name__,
                    report_error,
                )
            return {}

    def _commit_state(
        self,
        state: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """CAS one complete schedule aggregate and its durable image."""

        out = dict(state)
        if "schema_version" in out:
            raise ValueError(
                "foundation_schedule_state_versioned_contract_forbidden"
            )
        out["artifact"] = STATE_FILENAME
        out["owner"] = OWNER
        with self._state_lock:
            current = dict(self._state)
            current_revision = int(current.get("revision", 0) or 0)
            expected = (
                int(out.get("revision", 0) or 0)
                if expected_revision is None
                else int(expected_revision)
            )
            if expected != current_revision:
                raise RuntimeError(
                    "foundation_schedule_state_revision_conflict:"
                    f"expected={expected};current={current_revision}"
                )
            if (
                current
                and _text(current.get("request_id"))
                == _text(out.get("request_id"))
                and _mapping(current.get("plan"))
                != _mapping(out.get("plan"))
                and not _text(out.get("cancellation_request_id"))
            ):
                raise RuntimeError(
                    "foundation_schedule_plan_is_immutable"
                )
            out["revision"] = current_revision + 1
            out = _validate_schedule_state(out)
            comparable_current = {
                key: value
                for key, value in current.items()
                if key not in {"ts", "revision"}
            }
            comparable_out = {
                key: value
                for key, value in out.items()
                if key not in {"ts", "revision"}
            }
            if comparable_current == comparable_out:
                return current
            out["ts"] = time()
            atomic_write_json(
                self._state_path,
                out,
                sort_keys=True,
                trailing_newline=True,
            )
            self._state = dict(out)
            return dict(out)

    def _drain_observations(self) -> None:
        with self._observation_lock:
            while True:
                record = self._accepted_root_outbox.claim_next(
                    FOUNDATION_SCHEDULE_CLIENT_ID
                )
                if record is None:
                    return
                try:
                    self._latest_accepted_root = record.root
                    self._accepted_root_outbox.acknowledge(
                        FOUNDATION_SCHEDULE_CLIENT_ID,
                        key=record.key,
                    )
                except Exception as exc:
                    self._accepted_root_outbox.release(
                        FOUNDATION_SCHEDULE_CLIENT_ID,
                        key=record.key,
                        error=exc,
                    )
                    raise

    def _drain_command_outcomes(self) -> None:
        state = self.state()
        matrix_raw = state.get("core_command_matrix_identity")
        runtime_namespace = (
            MatrixGenerationIdentity.from_mapping(matrix_raw).namespace
            if isinstance(matrix_raw, Mapping)
            else require_core_command_ingress_context(
                self._core
            ).matrix_identity.namespace
        )
        while True:
            record = self._outcome_stream.claim_next(
                FOUNDATION_SCHEDULE_OUTCOME_CLIENT_ID,
                runtime_namespace=runtime_namespace,
            )
            if record is None:
                return
            try:
                self._apply_command_outcome(record.receipt)
                self._outcome_stream.acknowledge(
                    FOUNDATION_SCHEDULE_OUTCOME_CLIENT_ID,
                    runtime_namespace=runtime_namespace,
                    key=record.key,
                )
            except Exception as exc:
                self._outcome_stream.release(
                    FOUNDATION_SCHEDULE_OUTCOME_CLIENT_ID,
                    runtime_namespace=runtime_namespace,
                    key=record.key,
                    error=exc,
                )
                raise

    def _apply_command_outcome(self, receipt: CommandTerminalReceipt) -> None:
        if not isinstance(receipt, CommandTerminalReceipt):
            raise TypeError("foundation_schedule_typed_terminal_receipt_required")
        state = self.state()
        if not state or _text(state.get("core_command_request_id")) != _text(
            receipt.transaction.command_id
        ):
            return
        lifecycle = receipt.terminal_status
        event_root = _text(receipt.transaction.public_command_root_id)
        correlated_root = _text(state.get("core_command_root_id"))
        if not correlated_root or correlated_root != event_root:
            return
        if MatrixGenerationIdentity.from_mapping(
            _mapping(state.get("core_command_matrix_identity"))
        ) != receipt.transaction.matrix_identity:
            return
        updated = dict(state)
        updated["last_command_outcome"] = lifecycle.value
        updated["last_command_reason"] = str(receipt.reason or "")
        outcome_ts = float(receipt.terminal_ts or 0.0)
        if not math.isfinite(outcome_ts) or outcome_ts <= 0.0:
            raise ValueError("foundation_schedule_core_outcome_time_invalid")
        updated["last_command_outcome_ts"] = outcome_ts
        if lifecycle in {
            CommandTerminalStatus.FAILED,
            CommandTerminalStatus.REJECTED,
            CommandTerminalStatus.TIMED_OUT,
            CommandTerminalStatus.CANCELLED,
        }:
            if _is_band_delta_schedule_state(updated):
                reason = updated["last_command_reason"] or lifecycle.value
                if _is_daily_band_delta_schedule_state(updated):
                    self._mark_daily_occurrence_failure(
                        updated,
                        reason=reason,
                        now=outcome_ts,
                    )
                else:
                    self._mark_schedule_occurrence_terminal(
                        updated,
                        occurrence=_active_band_delta_occurrence(updated),
                        reason=reason,
                        now=outcome_ts,
                        failed=True,
                    )
                return
            updated["lifecycle_state"] = "failed"
            updated["execution_proof"] = updated["last_command_reason"] or lifecycle.value
            updated["failure_reason"] = updated["execution_proof"]
            updated["terminal_reason"] = updated["execution_proof"]
            self._commit_state(updated)
            return
        self._mark_verified_or_next(
            updated,
            reason=f"core_command_{lifecycle.value}",
            now=outcome_ts,
        )

    def _current_radio(
        self,
        matrix_identity: MatrixGenerationIdentity,
    ) -> tuple[int, list[int]]:
        if not isinstance(matrix_identity, MatrixGenerationIdentity):
            raise TypeError(
                "foundation_schedule_matrix_identity_required"
            )
        with self._observation_lock:
            self._drain_observations()
            root = self._latest_accepted_root
        if not isinstance(root, CoreAcceptedGenerationRoot):
            raise RuntimeError("foundation_schedule_accepted_generation_unavailable")
        if root.matrix_identity.namespace != matrix_identity.namespace:
            raise RuntimeError(
                "foundation_schedule_matrix_identity_not_active"
            )
        frame = root.observation_frame
        derived = frame.derived_facts.to_mapping()
        radio = (
            dict(derived.get("radio_target_facts") or {})
            if isinstance(derived.get("radio_target_facts"), Mapping)
            else {}
        )
        serving = max(0, int(radio.get("primary_band", 0) or 0))
        pool = _unique_bands(radio.get("selected_bands") or ())
        if serving > 0 and serving not in pool:
            pool.insert(0, serving)
        return serving, pool

    def _rebind_armed_schedule(
        self,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Bind an armed recurring schedule to the current run. Work already in progress
        keeps its original Core request. Only the operator's armed schedule definition
        may survive a process restart.
        """

        if not _is_band_delta_schedule_state(state):
            return dict(state)
        if _text(state.get("lifecycle_state")).lower() != "armed":
            return dict(state)
        context = require_core_command_ingress_context(self._core)
        retained_namespace = RuntimeGenerationNamespace.from_mapping(
            _mapping(state.get("runtime_namespace"))
        )
        if retained_namespace == context.matrix_identity.namespace:
            return dict(state)
        updated = dict(state)
        updated.update(
            runtime_namespace=context.matrix_identity.namespace.as_dict(),
            matrix_identity=context.matrix_identity.as_dict(),
            execution_proof="recurring_schedule_rebound_to_active_runtime",
        )
        for key in _SCHEDULE_TRANSIENT_EXECUTION_FIELDS:
            updated.pop(key, None)
        return self._commit_state(updated)

    def _precheck(
        self,
        request_id: str,
        interval_s: int | None,
        *,
        sequence: bool = False,
    ) -> tuple[str, int]:
        minimum = (
            MIN_BAND_SEQUENCE_INTERVAL_S
            if sequence else int(OPERATOR_BAND_COOLDOWN_S) + 1
        )
        if interval_s is not None and interval_s < minimum:
            message = (
                BAND_SEQUENCE_INTERVAL_REJECT_MESSAGE if sequence else
                f"Band schedule interval must be at least {minimum} seconds."
            )
            return message, 0
        with self._state_lock:
            current = dict(self._state)
        lifecycle = _text(current.get("lifecycle_state")).lower()
        if lifecycle in {"dispatching", "executing"}:
            return "foundation_schedule_command_inflight", int(
                current.get("revision", 0) or 0
            )
        return "", int(current.get("revision", 0) or 0)

    def _publish(
        self,
        context: FoundationOperatorRequestContext,
        kind: OperationalKind,
        plan: Mapping[str, Any],
        *,
        request_fingerprint: str,
        lifecycle_state: str,
        message: str,
        expected_revision: int,
        state_extras: Mapping[str, Any] | None = None,
    ) -> OperationalServiceReceipt:
        if not isinstance(context, FoundationOperatorRequestContext):
            raise TypeError("foundation_schedule_typed_context_required")
        request_id = context.request_id
        now = time()
        state = {
            **dict(state_extras or {}),
            "created_ts": now,
            "request_id": request_id,
            "command_id": request_id,
            "operation": kind.value,
            "operational_request_fingerprint": request_fingerprint,
            "lifecycle_state": str(lifecycle_state),
            "plan": dict(plan),
            "active_step_index": 0,
            "execution_proof": (
                "canceled_before_execution"
                if lifecycle_state == "canceled"
                else "armed_for_scheduled_execution"
            ),
            **(
                {"terminal_reason": str(plan.get("reason") or "operator_canceled")}
                if lifecycle_state == "canceled"
                else {}
            ),
            "artifact": STATE_FILENAME,
            "ts": now,
            "owner": OWNER,
            "runtime_namespace": context.runtime_namespace.as_dict(),
            "matrix_identity": context.matrix_identity.as_dict(),
        }
        try:
            state = self._commit_state(
                state,
                expected_revision=expected_revision,
            )
        except RuntimeError as exc:
            if str(exc).startswith(
                "foundation_schedule_state_revision_conflict:"
            ):
                return self._rejected(
                    context,
                    kind,
                    "foundation_schedule_state_changed_retry",
                )
            raise
        return OperationalServiceReceipt(
            request_id=request_id,
            kind=kind,
            owner=OWNER,
            disposition=OperationalDisposition.CONFIRMED,
            message=message,
            occurred_at=time(),
            runtime_namespace=context.runtime_namespace,
            matrix_identity=context.matrix_identity,
            detail=CoreFrozenPayload.from_mapping(state),
        )

    def _cancel(
        self,
        context: FoundationOperatorRequestContext,
        kind: OperationalKind,
        *,
        request_fingerprint: str,
        expected_operation: OperationalKind,
        reason: str,
        expected_revision: int,
    ) -> OperationalServiceReceipt:
        if not isinstance(context, FoundationOperatorRequestContext):
            raise TypeError("foundation_schedule_typed_context_required")
        request_id = context.request_id
        with self._state_lock:
            current = dict(self._state)
        if not current:
            return self._rejected(
                context,
                kind,
                "foundation_schedule_no_active_plan",
            )
        if _text(current.get("operation")) != expected_operation.value:
            return self._rejected(
                context,
                kind,
                "foundation_schedule_plan_kind_mismatch",
            )
        lifecycle = _text(current.get("lifecycle_state")).lower()
        if lifecycle in {"dispatching", "executing"}:
            return self._rejected(
                context,
                kind,
                "foundation_schedule_cannot_cancel_inflight",
            )
        terminal_delta_table = expected_operation is OperationalKind.PLAN_BAND_SCHEDULE and _is_band_delta_schedule_state(current)
        if lifecycle in {"verified", "failed", "expired", "canceled"} and not terminal_delta_table:
            return self._rejected(
                context,
                kind,
                "foundation_schedule_plan_already_terminal",
            )
        plan_namespace = RuntimeGenerationNamespace.from_mapping(
            _mapping(current.get("runtime_namespace"))
        )
        if plan_namespace != context.runtime_namespace:
            return self._rejected(
                context,
                kind,
                "foundation_schedule_runtime_namespace_mismatch",
            )
        updated = dict(current)
        updated.update(
            lifecycle_state="canceled",
            execution_proof="canceled_before_core_admission",
            terminal_reason=str(reason or "operator_canceled"),
            cancellation_request_id=request_id,
            cancellation_request_fingerprint=request_fingerprint,
            cancellation_reason=str(reason or "operator_canceled"),
            cancellation_ts=time(),
        )
        if (
            expected_operation is OperationalKind.PLAN_BAND_SCHEDULE
            and _is_band_delta_schedule_state(updated)
        ):
            updated["plan"] = {
                **_mapping(updated.get("plan")),
                "rules": [],
            }
            updated["rule_progress"] = {}
        try:
            committed = self._commit_state(
                updated,
                expected_revision=expected_revision,
            )
        except RuntimeError as exc:
            if str(exc).startswith(
                "foundation_schedule_state_revision_conflict:"
            ):
                return self._rejected(
                    context,
                    kind,
                    "foundation_schedule_state_changed_retry",
                )
            raise
        return OperationalServiceReceipt(
            request_id=request_id,
            kind=kind,
            owner=OWNER,
            disposition=OperationalDisposition.CONFIRMED,
            message=(
                "Band schedule canceled."
                if kind is OperationalKind.CANCEL_BAND_SCHEDULE
                else "Band sequence canceled."
            ),
            occurred_at=time(),
            runtime_namespace=context.runtime_namespace,
            matrix_identity=context.matrix_identity,
            detail=CoreFrozenPayload.from_mapping(committed),
        )

    def _submit_if_due(
        self,
        state: Mapping[str, Any],
        *,
        now: float,
    ) -> dict[str, Any]:
        if _is_band_delta_schedule_state(state):
            return self._submit_band_delta_if_due(state, now=now)
        plan = _mapping(state.get("plan"))
        due_ts = _activation_due_ts(state, plan, now=now)
        if due_ts <= 0.0:
            if _text(state.get("execution_proof")) != "armed_waiting_for_activation_time":
                updated = dict(state)
                updated["execution_proof"] = "armed_waiting_for_activation_time"
                self._commit_state(updated)
            return {"submitted": False, "reason": "activation_time_unavailable"}
        if now < due_ts:
            if float(state.get("next_due_ts", 0.0) or 0.0) != due_ts:
                updated = dict(state)
                updated["next_due_ts"] = due_ts
                self._commit_state(updated)
            return {"submitted": False, "reason": "schedule_not_due"}
        due_grace_s = max(60.0, float(state.get("due_grace_s", 300.0) or 300.0))
        if now > due_ts + due_grace_s:
            updated = dict(state)
            updated.update(
                lifecycle_state="expired",
                execution_proof="scheduled_band_plan_due_window_expired",
                failure_reason="scheduled_band_plan_due_window_expired",
                terminal_reason="scheduled_band_plan_due_window_expired",
            )
            self._commit_state(updated)
            return {"submitted": False, "reason": updated["failure_reason"]}
        context = require_core_command_ingress_context(self._core)
        operation_namespace = RuntimeGenerationNamespace.from_mapping(
            _mapping(state.get("runtime_namespace"))
        )
        if context.matrix_identity.namespace != operation_namespace:
            updated = dict(state)
            updated.update(
                lifecycle_state="failed",
                execution_proof=(
                    "scheduled_band_plan_runtime_namespace_replaced"
                ),
                failure_reason=(
                    "scheduled_band_plan_runtime_namespace_replaced"
                ),
                terminal_reason=(
                    "scheduled_band_plan_runtime_namespace_replaced"
                ),
            )
            self._commit_state(updated)
            return {"submitted": False, "reason": updated["failure_reason"]}
        serving, current_pool = self._current_radio(
            context.matrix_identity
        )
        target = _target_from_state(
            state,
            current_pband=serving,
            current_pool=current_pool,
        )
        lever, params = _command_target(target)
        if not lever:
            updated = dict(state)
            updated.update(
                lifecycle_state="failed",
                execution_proof="scheduled_band_plan_target_unavailable",
                failure_reason="scheduled_band_plan_target_unavailable",
                terminal_reason="scheduled_band_plan_target_unavailable",
            )
            self._commit_state(updated)
            return {"submitted": False, "reason": updated["failure_reason"]}
        command_id = _text(state.get("command_id"))
        step_index = int(state.get("active_step_index", 0) or 0)
        request_id = f"foundation-band-plan:{command_id}:step:{step_index}"
        typed_target = build_core_command_target(lever, params)
        typed_target_keys = set(typed_target.to_mapping()) - {"lever"}
        target_extras = {
            key: value for key, value in params.items()
            if key not in typed_target_keys
        }
        request = build_core_command_request(
            context,
            request_id=request_id,
            correlation_id=request_id,
            client_id="foundation.band_schedule",
            client_role=CoreClientRole.OPERATOR_AUTOMATION,
            origin=CoreCommandOrigin.OPERATOR_AUTOMATION,
            command_kind=lever,
            target=typed_target,
            parameters={
                "automation_source": "schedule",
                "intent": "scheduled_band_plan",
                "reason": "operator-approved scheduled band plan",
                "objective_id": f"scheduled_band_plan:{command_id}",
                "action_parameters": {
                    "_scheduled_band_plan": True,
                    "_band_plan_command_id": command_id,
                    **target_extras,
                },
            },
            capability_reference=lever_action_authority(lever).capability_key,
            created_at=now,
            expires_at=max(now + 0.1, due_ts + due_grace_s),
            confirmed=True,
        )
        updated = dict(state)
        updated.update(
            lifecycle_state="dispatching",
            execution_proof="core_command_ingress_submitting",
            active_target=target,
            dispatch_started_ts=now,
            core_command_request_id=request_id,
            core_command_request=request.to_payload(),
            core_command_matrix_identity=context.matrix_identity.as_dict(),
        )
        self._commit_state(updated)
        try:
            result = dict(
                require_core_command_gateway(self._core).submit(request, now=now)
            )
        except Exception as exc:
            failed = dict(self.state())
            reason = f"core_command_dispatch_failed:{type(exc).__name__}"
            failed.update(
                lifecycle_state="failed",
                execution_proof=reason,
                failure_reason=reason,
                terminal_reason=reason,
            )
            self._commit_state(failed)
            raise
        updated = dict(self.state())
        if bool(result.get("submitted", False)):
            updated.update(
                lifecycle_state="executing",
                execution_proof="core_command_authority_pending_terminal_receipt",
                executing_since_ts=now,
                core_command_root_id=_text(result.get("root_id")),
            )
        else:
            reason = _text(result.get("reason")) or "core_command_rejected"
            updated.update(
                lifecycle_state="failed",
                execution_proof=reason,
                failure_reason=reason,
                terminal_reason=reason,
            )
        self._commit_state(updated)
        return result

    def _submit_band_delta_if_due(
        self,
        state: Mapping[str, Any],
        *,
        now: float,
    ) -> dict[str, Any]:
        occurrence = _due_band_delta_occurrence(state, now=now)
        if occurrence is None:
            next_due = _next_band_delta_due(state, now=now)
            updated = dict(state)
            changed = False
            if next_due > 0.0 and float(updated.get("next_due_ts", 0.0) or 0.0) != next_due:
                updated["next_due_ts"] = next_due
                changed = True
            if _text(updated.get("execution_proof")) != "armed_waiting_for_next_rule":
                updated["execution_proof"] = "armed_waiting_for_next_rule"
                changed = True
            if changed:
                self._commit_state(updated)
            return {"submitted": False, "reason": "schedule_not_due"}

        rule = occurrence["rule"]
        recurrence = _text(rule.get("recurrence"))
        rule_id = _text(rule.get("rule_id"))
        occurrence_id = _text(occurrence.get("occurrence_id"))
        due_ts = float(occurrence.get("due_ts", 0.0) or 0.0)
        progress = _mapping(state.get("rule_progress"))
        row = _mapping(progress.get(rule_id))
        retry_not_before = float(row.get("retry_not_before", 0.0) or 0.0)
        if retry_not_before > now:
            updated = dict(state)
            updated["next_due_ts"] = retry_not_before
            if _text(updated.get("execution_proof")) != "armed_waiting_for_bounded_retry":
                updated["execution_proof"] = "armed_waiting_for_bounded_retry"
                self._commit_state(updated)
            return {"submitted": False, "reason": "schedule_retry_not_due"}

        if recurrence == "once":
            due_grace_s = max(
                60.0,
                float(state.get("due_grace_s", 300.0) or 300.0),
            )
            if now > due_ts + due_grace_s:
                self._mark_schedule_occurrence_terminal(
                    state,
                    occurrence=occurrence,
                    reason="scheduled_band_plan_due_window_expired",
                    now=now,
                    failed=True,
                )
                return {
                    "submitted": False,
                    "reason": "scheduled_band_plan_due_window_expired",
                }

        context = require_core_command_ingress_context(self._core)
        operation_namespace = RuntimeGenerationNamespace.from_mapping(
            _mapping(state.get("runtime_namespace"))
        )
        if context.matrix_identity.namespace != operation_namespace:
            raise RuntimeError("foundation_schedule_runtime_namespace_not_rebound")
        _serving, current_pool = self._current_radio(context.matrix_identity)
        target = _target_from_schedule_rule(rule, current_pool=current_pool)
        target_pool = _unique_bands(target.get("bands") or ())
        if target_pool and set(current_pool) == set(target_pool):
            self._mark_schedule_occurrence_terminal(
                state,
                occurrence=occurrence,
                reason="scheduled_target_already_active",
                now=now,
            )
            return {"submitted": False, "reason": "scheduled_target_already_active"}

        lever, params = _command_target(target)
        if not lever:
            return self._mark_schedule_dispatch_failure(
                state,
                occurrence=occurrence,
                reason="scheduled_band_plan_target_unavailable",
                now=now,
            )
        attempt_count = int(row.get("attempt_count", 0) or 0) + 1
        request_id = (
            "foundation-band-schedule:"
            + rule_id.replace(":", "-")
            + ":"
            + occurrence_id.replace(":", "-")
            + f":attempt-{attempt_count}"
        )
        typed_target = build_core_command_target(lever, params)
        typed_target_keys = set(typed_target.to_mapping()) - {"lever"}
        target_extras = {
            key: value
            for key, value in params.items()
            if key not in typed_target_keys
        }
        request = build_core_command_request(
            context,
            request_id=request_id,
            correlation_id=request_id,
            client_id="foundation.band_schedule",
            client_role=CoreClientRole.OPERATOR_AUTOMATION,
            origin=CoreCommandOrigin.OPERATOR_AUTOMATION,
            command_kind=lever,
            target=typed_target,
            parameters={
                "automation_source": "schedule",
                "intent": "scheduled_band_plan",
                "reason": "operator-approved scheduled band addition or removal",
                "objective_id": f"band_delta_schedule:{occurrence_id}",
                "action_parameters": {
                    "_scheduled_band_plan": True,
                    "_band_schedule_rule_id": rule_id,
                    "_band_schedule_occurrence_id": occurrence_id,
                    **target_extras,
                },
            },
            capability_reference=lever_action_authority(lever).capability_key,
            created_at=now,
            expires_at=now + 300.0,
            confirmed=True,
        )
        progress[rule_id] = {
            **row,
            "active_occurrence": occurrence_id,
            "attempt_count": attempt_count,
        }
        updated = dict(state)
        updated.update(
            lifecycle_state="dispatching",
            execution_proof="core_command_ingress_submitting",
            active_target=target,
            active_rule_id=rule_id,
            active_occurrence_id=occurrence_id,
            active_due_ts=due_ts,
            dispatch_started_ts=now,
            core_command_request_id=request_id,
            core_command_request=request.to_payload(),
            core_command_matrix_identity=context.matrix_identity.as_dict(),
            rule_progress=progress,
        )
        self._commit_state(updated)
        try:
            result = dict(
                require_core_command_gateway(self._core).submit(request, now=now)
            )
        except Exception as exc:
            reason = f"core_command_dispatch_failed:{type(exc).__name__}"
            self._mark_schedule_dispatch_failure(
                self.state(),
                occurrence=occurrence,
                reason=reason,
                now=now,
            )
            raise
        updated = dict(self.state())
        if bool(result.get("submitted", False)):
            updated.update(
                lifecycle_state="executing",
                execution_proof="core_command_authority_pending_terminal_receipt",
                executing_since_ts=now,
                core_command_root_id=_text(result.get("root_id")),
            )
            self._commit_state(updated)
            return result
        reason = _text(result.get("reason")) or "core_command_rejected"
        return self._mark_schedule_dispatch_failure(
            updated,
            occurrence=occurrence,
            reason=reason,
            now=now,
            result=result,
        )

    def _mark_schedule_dispatch_failure(
        self,
        state: Mapping[str, Any],
        *,
        occurrence: Mapping[str, Any],
        reason: str,
        now: float,
        result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if _text(_mapping(occurrence.get("rule")).get("recurrence")) == "daily":
            rule = _mapping(occurrence.get("rule"))
            prepared = dict(state)
            rule_id = _text(rule.get("rule_id"))
            occurrence_id = _text(occurrence.get("occurrence_id"))
            progress = _mapping(prepared.get("rule_progress"))
            row = _mapping(progress.get(rule_id))
            if not _text(prepared.get("active_rule_id")):
                row["attempt_count"] = int(row.get("attempt_count", 0) or 0) + 1
                progress[rule_id] = row
                prepared.update(
                    active_rule_id=rule_id,
                    active_occurrence_id=occurrence_id,
                    active_due_ts=float(occurrence.get("due_ts", 0.0) or 0.0),
                    rule_progress=progress,
                )
            committed = self._mark_daily_occurrence_failure(
                prepared,
                reason=reason,
                now=now,
            )
            return {
                **dict(result or {}),
                "submitted": False,
                "reason": reason,
                "schedule_state": committed["lifecycle_state"],
            }
        committed = self._mark_schedule_occurrence_terminal(
            state,
            occurrence=occurrence,
            reason=reason,
            now=now,
            failed=True,
        )
        return {
            **dict(result or {}),
            "submitted": False,
            "reason": reason,
            "schedule_state": committed["lifecycle_state"],
        }

    def _mark_daily_occurrence_failure(
        self,
        state: Mapping[str, Any],
        *,
        reason: str,
        now: float,
    ) -> dict[str, Any]:
        updated = dict(state)
        rule_id = _text(updated.get("active_rule_id"))
        occurrence_id = _text(updated.get("active_occurrence_id"))
        progress = _mapping(updated.get("rule_progress"))
        row = _mapping(progress.get(rule_id))
        attempts = int(row.get("attempt_count", 0) or 0)
        row.update(
            last_failure_reason=reason,
            last_failure_at=now,
        )
        if attempts < DAILY_SCHEDULE_MAX_ATTEMPTS:
            row["retry_not_before"] = now + DAILY_SCHEDULE_RETRY_DELAY_S
            proof = "daily_rule_waiting_for_bounded_retry"
        else:
            row.update(
                last_completed_occurrence=occurrence_id,
                last_outcome="failed_after_bounded_attempts",
                completed_at=now,
                retry_not_before=0.0,
            )
            proof = "daily_rule_failed_occurrence_retained_for_next_rule"
        progress[rule_id] = row
        updated.update(
            lifecycle_state="armed",
            execution_proof=proof,
            rule_progress=progress,
            last_rule_failure_reason=reason,
            last_rule_failure_at=now,
        )
        for key in _SCHEDULE_TRANSIENT_EXECUTION_FIELDS:
            updated.pop(key, None)
        updated["next_due_ts"] = _next_band_delta_due(updated, now=now)
        return self._commit_state(updated)

    def _mark_schedule_occurrence_terminal(
        self,
        state: Mapping[str, Any],
        *,
        occurrence: Mapping[str, Any],
        reason: str,
        now: float,
        failed: bool = False,
    ) -> dict[str, Any]:
        updated = dict(state)
        rule = _mapping(occurrence.get("rule"))
        rule_id = _text(rule.get("rule_id"))
        occurrence_id = _text(occurrence.get("occurrence_id"))
        progress = _mapping(updated.get("rule_progress"))
        row = _mapping(progress.get(rule_id))
        row.update(
            last_completed_occurrence=occurrence_id,
            last_outcome=reason,
            completed_at=now,
            retry_not_before=0.0,
        )
        if failed:
            row.update(
                last_failure_reason=reason,
                last_failure_at=now,
            )
        progress[rule_id] = row
        updated["rule_progress"] = progress
        for key in _SCHEDULE_TRANSIENT_EXECUTION_FIELDS:
            updated.pop(key, None)
        if _text(rule.get("recurrence")) == "daily":
            updated.update(
                lifecycle_state="armed",
                execution_proof="daily_rule_applied_waiting_for_next_rule",
            )
            updated.pop("terminal_reason", None)
            updated.pop("failure_reason", None)
            updated["next_due_ts"] = _next_band_delta_due(updated, now=now)
        elif _next_band_delta_due(updated, now=now) > 0.0:
            updated.update(
                lifecycle_state="armed",
                execution_proof=(
                    "one_time_rule_failed_waiting_for_next_rule"
                    if failed
                    else "one_time_rule_applied_waiting_for_next_rule"
                ),
                next_due_ts=_next_band_delta_due(updated, now=now),
            )
            updated.pop("terminal_reason", None)
            updated.pop("failure_reason", None)
        else:
            updated.update(
                lifecycle_state="failed" if failed else "verified",
                execution_proof=reason,
                terminal_reason=reason,
            )
            if failed:
                updated["failure_reason"] = reason
            else:
                updated.pop("failure_reason", None)
            updated["next_due_ts"] = 0.0
        return self._commit_state(updated)

    def _recover_inflight_dispatch(
        self,
        state: Mapping[str, Any],
        *,
        now: float,
    ) -> dict[str, Any]:
        """Recover the saved Core request without creating a new one."""

        request_payload = state.get("core_command_request")
        if not isinstance(request_payload, Mapping):
            raise RuntimeError(
                "foundation_schedule_dispatch_request_missing"
            )
        request = CoreCommandRequest.from_payload(request_payload)
        if (
            request.request_id
            != _text(state.get("core_command_request_id"))
            or request.matrix_identity
            != MatrixGenerationIdentity.from_mapping(
                _mapping(state.get("core_command_matrix_identity"))
            )
        ):
            raise RuntimeError(
                "foundation_schedule_dispatch_request_identity_mismatch"
            )
        gateway = require_core_command_gateway(self._core)
        root = gateway.request_root(request.request_id)
        if root is not None and root.request != request:
            raise RuntimeError(
                "foundation_schedule_dispatch_command_conflict"
            )
        if root is not None and root.state is CommandRootState.TERMINAL:
            # A final request root only proves that Core finished the command.
            # The outcome stream says whether it succeeded, failed, timed out,
            # or was rejected. Waiting here also handles the race where the
            # root finishes just before its receipt is read.
            return {
                "submitted": False,
                "reason": "core_command_terminal_pending_typed_receipt",
            }
        if root is None or root.state is not CommandRootState.DISPATCHED:
            result = dict(
                gateway.submit(
                    request,
                    now=now,
                )
            )
        else:
            result = {
                "submitted": True,
                "reason": "core_command_dispatch_already_retained",
                "root_id": root.request_root_id,
                "physical_action_root_id": (
                    root.physical_action_root_id
                ),
                "command_state": root.state.value,
            }
        updated = dict(self.state())
        if bool(result.get("submitted", False)):
            updated.update(
                lifecycle_state="executing",
                execution_proof=(
                    "core_command_authority_pending_terminal_receipt"
                ),
                executing_since_ts=float(
                    updated.get("executing_since_ts", 0.0) or now
                ),
                core_command_root_id=_text(result.get("root_id")),
            )
        else:
            reason = _text(result.get("reason")) or "core_command_rejected"
            if _is_daily_band_delta_schedule_state(updated):
                self._mark_daily_occurrence_failure(
                    updated,
                    reason=reason,
                    now=now,
                )
                return result
            if _is_band_delta_schedule_state(updated):
                self._mark_schedule_occurrence_terminal(
                    updated,
                    occurrence=_active_band_delta_occurrence(updated),
                    reason=reason,
                    now=now,
                    failed=True,
                )
                return result
            updated.update(
                lifecycle_state="failed",
                execution_proof=reason,
                failure_reason=reason,
                terminal_reason=reason,
            )
        self._commit_state(updated)
        return result

    def _mark_verified_or_next(
        self,
        state: Mapping[str, Any],
        *,
        reason: str,
        now: float,
    ) -> dict[str, Any]:
        updated = dict(state)
        plan = _mapping(updated.get("plan"))
        if _is_band_delta_schedule_state(updated):
            return self._mark_schedule_occurrence_terminal(
                updated,
                occurrence=_active_band_delta_occurrence(updated),
                reason=reason,
                now=now,
            )
        if _text(updated.get("operation")).lower() == OperationalKind.PLAN_BAND_SEQUENCE.value:
            steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
            index = int(updated.get("active_step_index", 0) or 0)
            next_index = index + 1
            if next_index < len(steps):
                interval_s = float(plan["interval_s"])
                updated.update(
                    lifecycle_state="armed",
                    execution_proof="step_verified_core_command_completed",
                    last_verified_step_index=index,
                    active_step_index=next_index,
                    next_due_ts=now + interval_s,
                )
                for key in (
                    "active_target",
                    "executing_since_ts",
                    "core_command_request_id",
                    "core_command_request",
                    "core_command_root_id",
                    "core_command_matrix_identity",
                    "dispatch_started_ts",
                ):
                    updated.pop(key, None)
                return self._commit_state(updated)
        updated.update(
            lifecycle_state="verified",
            execution_proof=reason,
            terminal_reason=reason,
        )
        for key in (
            "active_target",
            "executing_since_ts",
        ):
            updated.pop(key, None)
        return self._commit_state(updated)

    def state(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._state)

    @staticmethod
    def _rejected(
        context: FoundationOperatorRequestContext,
        kind: OperationalKind,
        reason: str,
    ) -> OperationalServiceReceipt:
        if not isinstance(context, FoundationOperatorRequestContext):
            raise TypeError("foundation_schedule_typed_context_required")
        return OperationalServiceReceipt(
            request_id=context.request_id,
            kind=kind,
            owner=OWNER,
            disposition=OperationalDisposition.REJECTED,
            message=f"Schedule request rejected: {reason}",
            occurred_at=time(),
            runtime_namespace=context.runtime_namespace,
            matrix_identity=context.matrix_identity,
            detail=CoreFrozenPayload.from_mapping({"reason": reason}),
        )


def _unique_bands(values: Any) -> list[int]:
    out: list[int] = []
    for value in values or ():
        band = int(value or 0)
        if band > 0 and band not in out:
            out.append(band)
    return out


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _schedule_rules(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = plan.get("rules")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _is_band_delta_schedule_state(state: Mapping[str, Any]) -> bool:
    return (
        _text(state.get("operation"))
        == OperationalKind.PLAN_BAND_SCHEDULE.value
        and _text(_mapping(state.get("plan")).get("plan_type"))
        == BAND_DELTA_SCHEDULE_PLAN_TYPE
    )


def _is_daily_band_delta_schedule_state(state: Mapping[str, Any]) -> bool:
    if not _is_band_delta_schedule_state(state):
        return False
    rules = _schedule_rules(_mapping(state.get("plan")))
    return bool(rules) and all(
        _text(rule.get("recurrence")) == "daily" for rule in rules
    )


def _relative_delay_s(value: str) -> float:
    lower = _text(value).lower()
    if not lower.startswith("+"):
        raise ValueError("band_schedule_relative_time_required")
    number = lower[1:]
    if ":" in number:
        try:
            pieces = [float(piece) for piece in number.split(":")]
        except ValueError as exc:
            raise ValueError("band_schedule_relative_time_invalid") from exc
        if len(pieces) == 2:
            return max(0.0, pieces[0] * 60.0 + pieces[1])
        if len(pieces) == 3:
            return max(
                0.0,
                pieces[0] * 3600.0 + pieces[1] * 60.0 + pieces[2],
            )
        raise ValueError("band_schedule_relative_time_invalid")
    multiplier = 1.0
    if number.endswith("ms"):
        multiplier = 0.001
        number = number[:-2]
    elif number.endswith("s"):
        number = number[:-1]
    elif number.endswith("m"):
        multiplier = 60.0
        number = number[:-1]
    elif number.endswith("h"):
        multiplier = 3600.0
        number = number[:-1]
    try:
        return max(0.0, float(number) * multiplier)
    except ValueError as exc:
        raise ValueError("band_schedule_relative_time_invalid") from exc


def _daily_minute(value: str) -> int:
    pieces = _text(value).split(":")
    if len(pieces) != 2:
        raise ValueError("band_schedule_daily_time_hh_mm_required")
    try:
        hour = int(pieces[0])
        minute = int(pieces[1])
    except ValueError as exc:
        raise ValueError("band_schedule_daily_time_hh_mm_required") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("band_schedule_daily_time_invalid")
    return hour * 60 + minute


def _band_delta_schedule_rule(
    request: BandSchedulePlanRequest,
    *,
    created_at: float,
) -> dict[str, Any]:
    bands = _unique_bands(request.bands)
    if not bands:
        raise ValueError("band_schedule_bands_required")
    operation = _text(request.operation).lower()
    if operation not in {"add", "remove"}:
        raise ValueError("band_schedule_operation_invalid")
    band_key = "+".join(f"B{band}" for band in bands)
    activate_at = _text(request.activate_at)
    if not activate_at:
        raise ValueError("band_schedule_activation_time_required")
    if activate_at.startswith("+"):
        due_ts = created_at + _relative_delay_s(activate_at)
        return {
            "rule_id": (
                f"once:{int(created_at * 1_000_000)}:{operation}:{band_key}"
            ),
            "recurrence": "once",
            "activate_at": activate_at,
            "due_ts": due_ts,
            "operation": operation,
            "bands": bands,
        }
    minute_of_day = _daily_minute(activate_at)
    normalized = f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"
    return {
        "rule_id": f"daily:{normalized}:{operation}:{band_key}",
        "recurrence": "daily",
        "activate_at": normalized,
        "minute_of_day": minute_of_day,
        "operation": operation,
        "bands": bands,
    }


def _schedule_rule_sort_key(rule: Mapping[str, Any]) -> tuple[int, float, str]:
    if _text(rule.get("recurrence")) == "daily":
        return (0, float(rule.get("minute_of_day", 0) or 0), _text(rule.get("rule_id")))
    return (1, float(rule.get("due_ts", 0.0) or 0.0), _text(rule.get("rule_id")))


def _daily_rule_due_ts(rule: Mapping[str, Any], *, now: float, day_offset: int = 0) -> float:
    minute_of_day = int(rule.get("minute_of_day", -1) or 0)
    if minute_of_day < 0 or minute_of_day >= 24 * 60:
        raise ValueError("band_schedule_daily_rule_minute_invalid")
    local = time_module.localtime(now)
    return float(
        time_module.mktime(
            (
                local.tm_year,
                local.tm_mon,
                local.tm_mday + day_offset,
                minute_of_day // 60,
                minute_of_day % 60,
                0,
                0,
                0,
                -1,
            )
        )
    )


def _occurrence(rule: Mapping[str, Any], *, due_ts: float) -> dict[str, Any]:
    rule_id = _text(rule.get("rule_id"))
    return {
        "rule": dict(rule),
        "due_ts": due_ts,
        "occurrence_id": f"{rule_id}:{int(due_ts)}",
    }


def _current_daily_occurrence(
    rule: Mapping[str, Any],
    *,
    now: float,
) -> dict[str, Any]:
    due_ts = _daily_rule_due_ts(rule, now=now)
    if due_ts > now:
        due_ts = _daily_rule_due_ts(rule, now=now, day_offset=-1)
    return _occurrence(rule, due_ts=due_ts)


def _due_band_delta_occurrence(
    state: Mapping[str, Any],
    *,
    now: float,
) -> dict[str, Any] | None:
    rules = _schedule_rules(_mapping(state.get("plan")))
    progress = _mapping(state.get("rule_progress"))
    if not rules:
        return None
    due: list[dict[str, Any]] = []
    for rule in rules:
        if _text(rule.get("recurrence")) == "daily":
            occurrence = _current_daily_occurrence(rule, now=now)
        else:
            due_ts = float(rule.get("due_ts", 0.0) or 0.0)
            if due_ts > now:
                continue
            occurrence = _occurrence(rule, due_ts=due_ts)
        row = _mapping(progress.get(_text(rule.get("rule_id"))))
        if _text(row.get("last_completed_occurrence")) != _text(
            occurrence.get("occurrence_id")
        ):
            due.append(occurrence)
    return min(
        due,
        key=lambda item: (
            float(item.get("due_ts", 0.0) or 0.0),
            _text(_mapping(item.get("rule")).get("rule_id")),
        ),
        default=None,
    )


def _next_band_delta_occurrence(
    state: Mapping[str, Any],
    *,
    now: float,
) -> dict[str, Any] | None:
    rules = _schedule_rules(_mapping(state.get("plan")))
    progress = _mapping(state.get("rule_progress"))
    candidates: list[dict[str, Any]] = []
    for rule in rules:
        recurrence = _text(rule.get("recurrence"))
        if recurrence == "daily":
            current = _current_daily_occurrence(rule, now=now)
            row = _mapping(progress.get(_text(rule.get("rule_id"))))
            if _text(row.get("last_completed_occurrence")) != _text(
                current.get("occurrence_id")
            ):
                candidates.append(current)
                continue
            due_ts = _daily_rule_due_ts(rule, now=now)
            if due_ts <= now:
                due_ts = _daily_rule_due_ts(rule, now=now, day_offset=1)
            candidates.append(_occurrence(rule, due_ts=due_ts))
            continue
        due_ts = float(rule.get("due_ts", 0.0) or 0.0)
        occurrence_id = _text(_occurrence(rule, due_ts=due_ts).get("occurrence_id"))
        row = _mapping(progress.get(_text(rule.get("rule_id"))))
        if _text(row.get("last_completed_occurrence")) != occurrence_id:
            candidates.append(_occurrence(rule, due_ts=due_ts))
    return min(
        candidates,
        key=lambda item: (
            float(item.get("due_ts", 0.0) or 0.0),
            _text(_mapping(item.get("rule")).get("rule_id")),
        ),
        default=None,
    )


def _next_band_delta_due(state: Mapping[str, Any], *, now: float) -> float:
    occurrence = _next_band_delta_occurrence(state, now=now)
    return (
        float(occurrence.get("due_ts", 0.0) or 0.0)
        if occurrence is not None
        else 0.0
    )


def _active_band_delta_occurrence(
    state: Mapping[str, Any],
) -> dict[str, Any]:
    rule_id = _text(state.get("active_rule_id"))
    occurrence_id = _text(state.get("active_occurrence_id"))
    rule = next(
        (
            item
            for item in _schedule_rules(_mapping(state.get("plan")))
            if _text(item.get("rule_id")) == rule_id
        ),
        None,
    )
    if rule is None or not occurrence_id:
        raise RuntimeError("foundation_schedule_active_occurrence_missing")
    return {
        "rule": rule,
        "occurrence_id": occurrence_id,
        "due_ts": float(state.get("active_due_ts", 0.0) or 0.0),
    }


def _target_from_schedule_rule(
    rule: Mapping[str, Any],
    *,
    current_pool: list[int],
) -> dict[str, Any]:
    operation = _text(rule.get("operation")).lower()
    bands = _unique_bands(rule.get("bands") or ())
    if operation == "add":
        target_pool = _unique_bands((*current_pool, *bands))
    elif operation == "remove":
        removed = set(bands)
        target_pool = [band for band in current_pool if band not in removed]
    else:
        return {}
    if not target_pool:
        return {}
    return {
        "kind": "band_selection",
        "bands": target_pool,
        "scheduled_operation": operation,
        "scheduled_bands": bands,
    }


def schedule_presentation_status(
    state: Mapping[str, Any],
) -> str:
    lifecycle = _text(state.get("lifecycle_state")).lower()
    if not _is_band_delta_schedule_state(state):
        return lifecycle or "OFF"
    rules = _schedule_rules(_mapping(state.get("plan")))
    if lifecycle == "canceled" or not rules:
        return "OFF - no scheduled rules"
    if lifecycle in {"dispatching", "executing"}:
        target = _mapping(state.get("active_target"))
        bands = "+".join(
            f"B{band}"
            for band in _unique_bands(target.get("scheduled_bands") or ())
        )
        return f"Applying {_text(target.get('scheduled_operation')).upper()} {bands}"
    recurrence = _text(rules[0].get("recurrence"))
    kind = "daily" if recurrence == "daily" else "one-time"
    summaries: list[str] = []
    for rule in rules:
        bands = "+".join(
            f"B{band}" for band in _unique_bands(rule.get("bands") or ())
        )
        if _text(rule.get("recurrence")) == "daily":
            when = _text(rule.get("activate_at"))
        else:
            due_ts = float(rule.get("due_ts", 0.0) or 0.0)
            when = time_module.strftime(
                "%H:%M", time_module.localtime(due_ts)
            )
        summaries.append(
            f"{when} {_text(rule.get('operation')).upper()} {bands}"
        )
    summary = f"{len(rules)} {kind}: " + "; ".join(summaries)
    if lifecycle == "failed":
        return "FAILED - " + summary
    if lifecycle == "verified":
        return "COMPLETED - " + summary
    return summary


def _validate_schedule_state(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("foundation_schedule_state_mapping_required")
    state = dict(value)
    if state.get("artifact") != STATE_FILENAME:
        raise ValueError("foundation_schedule_state_artifact_invalid")
    if "schema_version" in state:
        raise ValueError("foundation_schedule_state_versioned_contract_forbidden")
    if state.get("owner") != OWNER:
        raise ValueError("foundation_schedule_state_owner_invalid")
    runtime_namespace = RuntimeGenerationNamespace.from_mapping(
        _mapping(state.get("runtime_namespace"))
    )
    matrix_identity = MatrixGenerationIdentity.from_mapping(
        _mapping(state.get("matrix_identity"))
    )
    if matrix_identity.namespace != runtime_namespace:
        raise ValueError("foundation_schedule_matrix_namespace_mismatch")
    command_matrix = state.get("core_command_matrix_identity")
    if command_matrix is not None:
        execution_identity = MatrixGenerationIdentity.from_mapping(
            _mapping(command_matrix)
        )
        if execution_identity.namespace != runtime_namespace:
            raise ValueError(
                "foundation_schedule_command_namespace_mismatch"
            )
    if {"radio_stability_deferred_until_ts", "radio_stability_deferred_occurrence_id"}.intersection(state):
        raise ValueError("foundation_schedule_obsolete_stability_deferral_replan_required")
    forbidden = {"status", "next_lifecycle_state", "workflow"}.intersection(state)
    if forbidden:
        raise ValueError(
            "foundation_schedule_duplicate_lifecycle_fields_forbidden:"
            + ",".join(sorted(forbidden))
        )
    for field in ("request_id", "command_id", "operation", "lifecycle_state"):
        if not _text(state.get(field)):
            raise ValueError(f"foundation_schedule_{field}_required")
    revision = state.get("revision")
    if type(revision) is not int or revision <= 0:
        raise ValueError("foundation_schedule_revision_required")
    lifecycle = _text(state["lifecycle_state"]).lower()
    if lifecycle not in {
        "armed",
        "dispatching",
        "executing",
        "verified",
        "failed",
        "expired",
        "canceled",
    }:
        raise ValueError("foundation_schedule_lifecycle_invalid")
    plan = state.get("plan")
    if not isinstance(plan, Mapping):
        raise TypeError("foundation_schedule_plan_mapping_required")
    nested_forbidden = {
        "status",
        "lifecycle_state",
        "next_lifecycle_state",
    }.intersection(plan)
    if nested_forbidden:
        raise ValueError(
            "foundation_schedule_plan_lifecycle_forbidden:"
            + ",".join(sorted(nested_forbidden))
        )
    operation = _text(state.get("operation"))
    if operation == OperationalKind.PLAN_BAND_SCHEDULE.value:
        if _text(plan.get("plan_type")) != BAND_DELTA_SCHEDULE_PLAN_TYPE:
            raise ValueError("foundation_schedule_legacy_plan_forbidden")
        rules = _schedule_rules(plan)
        if not rules and lifecycle != "canceled":
            raise ValueError("foundation_schedule_band_delta_rules_required")
        if len(rules) > MAX_BAND_DELTA_SCHEDULE_RULES:
            raise ValueError("foundation_schedule_rule_limit_exceeded")
        recurrences: set[str] = set()
        rule_ids: set[str] = set()
        for rule in rules:
            rule_id = _text(rule.get("rule_id"))
            recurrence = _text(rule.get("recurrence"))
            if not rule_id or rule_id in rule_ids:
                raise ValueError("foundation_schedule_rule_identity_invalid")
            if recurrence not in {"daily", "once"}:
                raise ValueError("foundation_schedule_recurrence_invalid")
            rule_ids.add(rule_id)
            recurrences.add(recurrence)
            scheduled_operation = _text(rule.get("operation")).lower()
            scheduled_bands = _unique_bands(rule.get("bands") or ())
            if scheduled_operation not in {"add", "remove"} or not scheduled_bands:
                raise ValueError("foundation_schedule_band_delta_invalid")
            band_key = "+".join(f"B{band}" for band in scheduled_bands)
            if recurrence == "daily":
                minute = int(rule.get("minute_of_day", -1) or 0)
                if minute < 0 or minute >= 24 * 60:
                    raise ValueError("foundation_schedule_daily_time_invalid")
                expected_identity = (
                    f"daily:{minute // 60:02d}:{minute % 60:02d}:"
                    f"{scheduled_operation}:{band_key}"
                )
                if rule_id != expected_identity:
                    raise ValueError("foundation_schedule_daily_identity_invalid")
            else:
                if float(rule.get("due_ts", 0.0) or 0.0) <= 0.0:
                    raise ValueError("foundation_schedule_once_due_invalid")
                if not (
                    rule_id.startswith("once:")
                    and rule_id.endswith(
                        f":{scheduled_operation}:{band_key}"
                    )
                ):
                    raise ValueError(
                        "foundation_schedule_once_identity_invalid"
                    )
        if len(recurrences) > 1:
            raise ValueError("foundation_schedule_recurrence_mix_forbidden")
        progress = state.get("rule_progress", {})
        if not isinstance(progress, Mapping):
            raise TypeError("foundation_schedule_progress_mapping_required")
        if set(progress) - rule_ids:
            raise ValueError("foundation_schedule_progress_rule_unknown")
    elif operation == OperationalKind.PLAN_BAND_SEQUENCE.value:
        if _text(plan.get("plan_type")) != "scheduled_band_sequence":
            raise ValueError("foundation_sequence_plan_type_invalid")
        interval_s = plan.get("interval_s")
        if type(interval_s) is not int or interval_s < MIN_BAND_SEQUENCE_INTERVAL_S:
            raise ValueError("foundation_sequence_interval_below_minimum_or_invalid")
    else:
        raise ValueError("foundation_schedule_operation_invalid")
    active_step_index = int(state.get("active_step_index", 0) or 0)
    if active_step_index < 0:
        raise ValueError("foundation_schedule_active_step_negative")
    state["active_step_index"] = active_step_index
    if lifecycle in {"verified", "failed", "expired", "canceled"} and not _text(
        state.get("terminal_reason")
    ):
        raise ValueError("foundation_schedule_terminal_reason_required")
    if lifecycle in {"dispatching", "executing"}:
        request_payload = state.get("core_command_request")
        if not isinstance(request_payload, Mapping):
            raise ValueError(
                "foundation_schedule_dispatch_request_required"
            )
        request = CoreCommandRequest.from_payload(request_payload)
        if (
            request.request_id
            != _text(state.get("core_command_request_id"))
            or request.matrix_identity
            != MatrixGenerationIdentity.from_mapping(
                _mapping(state.get("core_command_matrix_identity"))
            )
        ):
            raise ValueError(
                "foundation_schedule_dispatch_request_mismatch"
            )
    return state


def _activation_due_ts(
    state: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    now: float,
) -> float:
    if _text(state.get("operation")).lower() != OperationalKind.PLAN_BAND_SEQUENCE.value:
        raise RuntimeError(
            "foundation_schedule_band_delta_occurrence_route_required"
        )
    existing = float(state.get("next_due_ts", 0.0) or 0.0)
    if existing > 0.0:
        return existing
    created = float(state.get("created_ts", 0.0) or now)
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    index = int(state.get("active_step_index", 0) or 0)
    if index < 0 or index >= len(steps):
        return 0.0
    step = _mapping(steps[index])
    return created + max(0.0, float(step.get("offset_s", 0.0) or 0.0))


def _target_from_state(
    state: Mapping[str, Any],
    *,
    current_pband: int,
    current_pool: list[int],
) -> dict[str, Any]:
    plan = _mapping(state.get("plan"))
    if _text(state.get("operation")).lower() == OperationalKind.PLAN_BAND_SEQUENCE.value:
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        index = int(state.get("active_step_index", 0) or 0)
        if index < 0 or index >= len(steps):
            return {}
        step = _mapping(steps[index])
        mode = _text(step.get("mode")).lower()
        target_pband = int(step.get("target_pband", 0) or 0)
        pool = _unique_bands(step.get("target_ca_pool") or ())
        if mode == "scheduled_pband_sequence_step":
            if target_pband > 0 and target_pband not in pool:
                pool.insert(0, target_pband)
            return {
                "kind": "pband_ca_pool",
                "pband": target_pband,
                "ca_pool": pool,
                "candidate_band": int(step.get("candidate_band", 0) or 0),
                "step_index": index,
                "sequence_mode": "pband_sequence",
            } if target_pband > 0 and pool else {}
        candidate = int(step.get("candidate_band", 0) or 0)
        for band in (candidate, *current_pool, current_pband):
            if band > 0 and band not in pool:
                pool.append(band)
        return {
            "kind": "band_sequence_ca_pool_step",
            "pband": current_pband,
            "ca_pool": pool,
            "preserve_pband": True,
            "candidate_band": candidate,
            "step_index": index,
        } if current_pband > 0 and pool else {}
    raise RuntimeError("foundation_schedule_band_delta_rule_route_required")


def _command_target(target: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    kind = _text(target.get("kind")).lower()
    if kind == "pband_ca_pool":
        pband = int(target.get("pband", 0) or 0)
        pool = _unique_bands(target.get("ca_pool") or ())
        if pband <= 0 or not pool:
            return "", {}
        return "PBAND", {"pband": pband, "ca_pool": pool}
    if kind == "band_sequence_ca_pool_step":
        pool = _unique_bands(target.get("ca_pool") or ())
        return (
            "BAND_SELECTION",
            {"bands": pool, "sequential": False},
        ) if pool else ("", {})
    bands = _unique_bands(target.get("bands") or ())
    return ("BAND_SELECTION", {"bands": bands}) if bands else ("", {})


def _schedule_plan(
    *,
    rules: list[Mapping[str, Any]],
    approval: bool,
) -> dict[str, Any]:
    return {
        "plan_type": BAND_DELTA_SCHEDULE_PLAN_TYPE,
        "approval_required": approval,
        "actuation_enabled": True,
        "rules": [dict(rule) for rule in sorted(rules, key=_schedule_rule_sort_key)],
    }


def _sequence_plan(*, serving: int, current_pool: list[int], bands: list[int], ca_bands: list[int], sequence_mode: str, interval_s: int, approval: bool) -> dict[str, Any]:
    mode = sequence_mode or "ca_pool"
    steps: list[dict[str, Any]] = []
    if mode in {"pband_sequence", "pband", "anchor_sequence"}:
        base_pool = ca_bands or bands or current_pool
        for index, pband in enumerate(bands):
            steps.append(
                {
                    "step": index + 1,
                    "candidate_band": pband,
                    "target_pband": pband,
                    "target_ca_pool": _unique_bands((pband, *base_pool)),
                    "mode": "scheduled_pband_sequence_step",
                    "offset_s": (index + 1) * interval_s,
                    "duration_s": interval_s,
                    "precheck": ["radio_connected", "same_provider", "cooldown_clear"],
                    "rollback": "return_to_previous_pool_on_contact_loss_or_rf_cliff",
                }
            )
    else:
        cumulative = list(current_pool)
        candidates = [band for band in bands if band not in cumulative]
        for index, band in enumerate(candidates):
            cumulative = _unique_bands((*cumulative, band))
            steps.append(
                {
                    "step": index + 1,
                    "candidate_band": band,
                    "target_pband": serving,
                    "target_ca_pool": list(cumulative),
                    "mode": "scheduled_ca_pool_add",
                    "offset_s": (index + 1) * interval_s,
                    "duration_s": interval_s,
                    "precheck": [
                        "radio_connected",
                        "same_provider",
                        "same_cell_or_verified_handover",
                        "cooldown_clear",
                    ],
                    "rollback": "return_to_previous_pool_on_contact_loss_or_rf_cliff",
                }
            )
        if not steps and serving > 0 and current_pool:
            steps.append(
                {
                    "step": 1,
                    "candidate_band": serving,
                    "target_pband": serving,
                    "target_ca_pool": list(current_pool),
                    "mode": "scheduled_ca_pool_hold",
                    "offset_s": interval_s,
                    "duration_s": interval_s,
                    "precheck": ["radio_connected", "same_provider", "cooldown_clear"],
                    "rollback": "no_change_required",
                }
            )
    return {
        "plan_type": "scheduled_band_sequence",
        "sequence_mode": mode,
        "approval_required": approval,
        "actuation_enabled": True,
        "interval_s": interval_s,
        "cooldown_s": interval_s,
        "steps": steps,
    }


__all__ = (
    "FOUNDATION_SCHEDULE_CLIENT_ID",
    "OWNER",
    "STATE_FILENAME",
    "FoundationScheduleService",
    "schedule_presentation_status",
)
