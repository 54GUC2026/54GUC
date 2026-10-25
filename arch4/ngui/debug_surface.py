"""Serialize the NGUI Front and PageTruth for read-only diagnostics."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
from threading import Condition, RLock, Thread
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping

from arch4.atomic_io import atomic_write_text, cleanup_abandoned_atomic_temps
from arch4.architecture_contracts.application_lifecycle import (
    ApplicationLeaseIdentity,
)
from arch4.fault_supervisor import (
    FaultBoundaryClass,
    FaultSeverity,
    FaultSupervisor,
)
from arch4.worker_health import WorkerExecutionKind, WorkerHealthAuthority

from .chart_model import chart_front_cursor
from .front_register import (
    FrontRegisterSnapshot,
    PresentationValue,
    accepted_modem_source_generation,
    accepted_modem_source_sampled_at,
)
from .safe_start_advisory import NGUISafeStartAdvisor


NGUI_PAGE_TRUTH_SCHEMA = "NGUIPageTruth.v1"
NGUI_PAGE_TRUTH_OWNER = "arch4.ngui.debug_surface.NGUIPageTruthLogWriter"
NGUI_PAGE_TRUTH_MAX_BYTES = 2_097_152
NGUI_PAGE_TRUTH_CLOSE_BUDGET_S = 2.0
PAGE_TRUTH_WRITER_TERMINATION_SCHEMA = (
    "NGUIPageTruthWriterTerminationReceipt.v1"
)
PAGE_TRUTH_WRITER_OWNER = "ngui_page_truth_writer"
PAGE_TRUTH_GENERATION_ROTATION_SCHEMA = (
    "NGUIPageTruthGenerationRotationReceipt.v1"
)
PAGE_TRUTH_GENERATION_MANAGER_OWNER = (
    "arch4.ngui.debug_surface.NGUIPageTruthGenerationManager"
)
_APPLICATION_LEASE_KEYS = frozenset(
    {
        "lease_id",
        "generation",
        "process_id",
        "process_started_at_unix_ms",
        "profile",
        "executable_path",
        "stage_dir",
    }
)
_PAGE_TRUTH_WRITER_TERMINATION_KEYS = frozenset(
    {
        "schema",
        "owner",
        "admission_closed",
        "terminated",
        "status",
        "active_sequence",
        "pending_sequence",
        "last_written_sequence",
        "rejected_sequence",
        "deadline_exceeded",
    }
)
_PAGE_TRUTH_GENERATION_ROTATION_KEYS = frozenset(
    {
        "schema",
        "owner",
        "application_lease",
        "previous_generation",
        "generation",
        "reset_transaction_digest",
        "prior_termination",
        "rotated_at",
    }
)


def _deep_freeze_evidence(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _deep_freeze_evidence(item)
                for key, item in copy.deepcopy(dict(value)).items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze_evidence(item) for item in value)
    if isinstance(value, set):
        return frozenset(_deep_freeze_evidence(item) for item in value)
    return value


def _deep_thaw_evidence(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _deep_thaw_evidence(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_deep_thaw_evidence(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_deep_thaw_evidence(item) for item in value),
            key=repr,
        )
    return copy.deepcopy(value)


def _canonical_reset_transaction_digest(value: str) -> str:
    if type(value) is not str:
        raise TypeError(
            "page_truth_rotation_reset_transaction_digest_string_required"
        )
    if (
        value != value.strip().lower()
        or len(value) != 64
        or any(item not in "0123456789abcdef" for item in value)
    ):
        raise ValueError(
            "page_truth_rotation_reset_transaction_digest_invalid"
        )
    return value


@dataclass(frozen=True, slots=True)
class PageTruthWriterTerminationReceipt:
    """Final state of the PageTruth output owner."""

    owner: str
    admission_closed: bool
    terminated: bool
    status: str
    active_sequence: int
    pending_sequence: int
    last_written_sequence: int
    rejected_sequence: int
    deadline_exceeded: bool

    def __post_init__(self) -> None:
        if type(self.owner) is not str:
            raise TypeError("page_truth_termination_owner_string_required")
        if self.owner != PAGE_TRUTH_WRITER_OWNER:
            raise ValueError("page_truth_termination_owner_mismatch")
        if type(self.admission_closed) is not bool:
            raise TypeError(
                "page_truth_termination_admission_closed_bool_required"
            )
        if type(self.terminated) is not bool:
            raise TypeError("page_truth_termination_terminated_bool_required")
        if type(self.status) is not str:
            raise TypeError("page_truth_termination_status_string_required")
        if self.status not in {"complete", "degraded"}:
            raise ValueError("page_truth_termination_status_invalid")
        for name in (
            "active_sequence",
            "pending_sequence",
            "last_written_sequence",
            "rejected_sequence",
        ):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(
                    f"page_truth_termination_{name}_int_required"
                )
            if value < 0:
                raise ValueError(
                    f"page_truth_termination_{name}_invalid"
                )
        if type(self.deadline_exceeded) is not bool:
            raise TypeError(
                "page_truth_termination_deadline_exceeded_bool_required"
            )
        if self.status == "complete":
            if (
                self.admission_closed is not True
                or self.terminated is not True
                or self.deadline_exceeded is not False
                or self.pending_sequence > self.last_written_sequence
                or self.rejected_sequence > self.last_written_sequence
            ):
                raise ValueError(
                    "page_truth_termination_complete_state_mismatch"
                )

    @classmethod
    def from_payload(
        cls,
        value: Mapping[str, Any],
    ) -> "PageTruthWriterTerminationReceipt":
        if not isinstance(value, Mapping):
            raise TypeError("page_truth_termination_mapping_required")
        if set(value) != _PAGE_TRUTH_WRITER_TERMINATION_KEYS:
            raise ValueError("page_truth_termination_keys_mismatch")
        if (
            type(value["schema"]) is not str
            or value["schema"] != PAGE_TRUTH_WRITER_TERMINATION_SCHEMA
        ):
            raise ValueError("page_truth_termination_schema_mismatch")
        return cls(
            owner=value["owner"],
            admission_closed=value["admission_closed"],
            terminated=value["terminated"],
            status=value["status"],
            active_sequence=value["active_sequence"],
            pending_sequence=value["pending_sequence"],
            last_written_sequence=value["last_written_sequence"],
            rejected_sequence=value["rejected_sequence"],
            deadline_exceeded=value["deadline_exceeded"],
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": PAGE_TRUTH_WRITER_TERMINATION_SCHEMA,
            "owner": self.owner,
            "admission_closed": self.admission_closed,
            "terminated": self.terminated,
            "status": self.status,
            "active_sequence": self.active_sequence,
            "pending_sequence": self.pending_sequence,
            "last_written_sequence": self.last_written_sequence,
            "rejected_sequence": self.rejected_sequence,
            "deadline_exceeded": self.deadline_exceeded,
        }


@dataclass(frozen=True, slots=True)
class PageTruthGenerationRotationReceipt:
    """Proof that a writer stopped before the next generation opened."""

    application_lease: ApplicationLeaseIdentity
    previous_generation: int
    generation: int
    reset_transaction_digest: str
    prior_termination: PageTruthWriterTerminationReceipt
    rotated_at: float

    def __post_init__(self) -> None:
        if not isinstance(
            self.application_lease,
            ApplicationLeaseIdentity,
        ):
            raise TypeError("page_truth_rotation_application_lease_required")
        if (
            type(self.previous_generation) is not int
            or self.previous_generation < 0
            or type(self.generation) is not int
            or self.generation != self.previous_generation + 1
        ):
            raise ValueError("page_truth_rotation_generation_not_contiguous")
        transaction_digest = _canonical_reset_transaction_digest(
            self.reset_transaction_digest
        )
        object.__setattr__(
            self,
            "reset_transaction_digest",
            transaction_digest,
        )
        if not isinstance(
            self.prior_termination,
            PageTruthWriterTerminationReceipt,
        ):
            raise TypeError(
                "page_truth_rotation_prior_termination_receipt_required"
            )
        if (
            self.prior_termination.admission_closed is not True
            or self.prior_termination.terminated is not True
            or self.prior_termination.status != "complete"
        ):
            raise RuntimeError("page_truth_rotation_prior_writer_not_drained")
        if type(self.rotated_at) is not float:
            raise TypeError("page_truth_rotation_timestamp_float_required")
        if not math.isfinite(self.rotated_at) or self.rotated_at <= 0.0:
            raise ValueError("page_truth_rotation_timestamp_required")

    @classmethod
    def from_payload(
        cls,
        value: Mapping[str, Any],
    ) -> "PageTruthGenerationRotationReceipt":
        if not isinstance(value, Mapping):
            raise TypeError("page_truth_rotation_mapping_required")
        if set(value) != _PAGE_TRUTH_GENERATION_ROTATION_KEYS:
            raise ValueError("page_truth_rotation_keys_mismatch")
        if (
            type(value["schema"]) is not str
            or value["schema"] != PAGE_TRUTH_GENERATION_ROTATION_SCHEMA
        ):
            raise ValueError("page_truth_rotation_schema_mismatch")
        if (
            type(value["owner"]) is not str
            or value["owner"] != PAGE_TRUTH_GENERATION_MANAGER_OWNER
        ):
            raise ValueError("page_truth_rotation_owner_mismatch")
        lease_payload = value["application_lease"]
        if not isinstance(lease_payload, Mapping):
            raise TypeError(
                "page_truth_rotation_application_lease_mapping_required"
            )
        if set(lease_payload) != _APPLICATION_LEASE_KEYS:
            raise ValueError(
                "page_truth_rotation_application_lease_keys_mismatch"
            )
        return cls(
            application_lease=ApplicationLeaseIdentity(**dict(lease_payload)),
            previous_generation=value["previous_generation"],
            generation=value["generation"],
            reset_transaction_digest=value["reset_transaction_digest"],
            prior_termination=(
                PageTruthWriterTerminationReceipt.from_payload(
                    value["prior_termination"]
                )
            ),
            rotated_at=value["rotated_at"],
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": PAGE_TRUTH_GENERATION_ROTATION_SCHEMA,
            "owner": PAGE_TRUTH_GENERATION_MANAGER_OWNER,
            "application_lease": self.application_lease.to_payload(),
            "previous_generation": self.previous_generation,
            "generation": self.generation,
            "reset_transaction_digest": self.reset_transaction_digest,
            "prior_termination": self.prior_termination.to_payload(),
            "rotated_at": self.rotated_at,
        }


class NGUIPageTruthGenerationManager:
    """Own PageTruth writer generations that cannot be reopened."""

    def __init__(
        self,
        *,
        application_lease: ApplicationLeaseIdentity,
        writer_factory: Callable[[], "NGUIPageTruthLogWriter"],
    ) -> None:
        if not isinstance(application_lease, ApplicationLeaseIdentity):
            raise TypeError("page_truth_manager_application_lease_required")
        if not callable(writer_factory):
            raise TypeError("page_truth_manager_writer_factory_required")
        self._application_lease = application_lease
        self._writer_factory = writer_factory
        self._lock = RLock()
        self._generation = 0
        self._writer: NGUIPageTruthLogWriter | None = None
        self._writer_closed = True
        self._closed_writer_termination: Mapping[str, Any] = {}
        self._last_rotation: Mapping[str, Any] = {}

    def publish(
        self,
        snapshot: FrontRegisterSnapshot,
        *,
        worker_generation: int,
    ) -> None:
        with self._lock:
            if (
                type(worker_generation) is not int
                or worker_generation != self._generation
                or worker_generation <= 0
                or self._writer_closed
                or self._writer is None
            ):
                raise RuntimeError(
                    "page_truth_publish_generation_not_active:"
                    f"candidate={worker_generation};active={self._generation}"
                )
            self._writer.publish(snapshot)

    def rotate_generation(
        self,
        *,
        worker_generation: int,
        reset_transaction_digest: str,
        timeout_s: float = NGUI_PAGE_TRUTH_CLOSE_BUDGET_S,
    ) -> Mapping[str, Any]:
        with self._lock:
            transaction_digest = _canonical_reset_transaction_digest(
                reset_transaction_digest
            )
            if worker_generation == self._generation:
                previous = dict(self._last_rotation)
                if (
                    previous.get("generation") == worker_generation
                    and previous.get("reset_transaction_digest")
                    == transaction_digest
                ):
                    return _deep_thaw_evidence(previous)
                raise ValueError(
                    "page_truth_rotation_generation_transaction_conflict"
                )
            if (
                type(worker_generation) is not int
                or worker_generation != self._generation + 1
            ):
                raise ValueError("page_truth_rotation_generation_not_successor")
            previous_generation = self._generation
            if self._writer is None:
                termination = {
                    "schema": "NGUIPageTruthWriterTerminationReceipt.v1",
                    "owner": "ngui_page_truth_writer",
                    "admission_closed": True,
                    "terminated": True,
                    "status": "complete",
                    "active_sequence": 0,
                    "pending_sequence": 0,
                    "last_written_sequence": 0,
                    "rejected_sequence": 0,
                    "deadline_exceeded": False,
                }
            elif not self._writer_closed:
                termination = dict(
                    self._writer.close(timeout_s=timeout_s)
                )
                self._writer_closed = True
                self._closed_writer_termination = termination
            else:
                termination = dict(self._closed_writer_termination)
            if (
                termination.get("admission_closed") is not True
                or termination.get("terminated") is not True
                or termination.get("status") != "complete"
            ):
                raise RuntimeError("page_truth_rotation_prior_writer_not_drained")
            successor = self._writer_factory()
            if not isinstance(successor, NGUIPageTruthLogWriter):
                raise TypeError("page_truth_manager_typed_writer_required")
            receipt = PageTruthGenerationRotationReceipt(
                application_lease=self._application_lease,
                previous_generation=previous_generation,
                generation=worker_generation,
                reset_transaction_digest=transaction_digest,
                prior_termination=(
                    PageTruthWriterTerminationReceipt.from_payload(
                        termination
                    )
                ),
                rotated_at=time.time(),
            ).to_payload()
            self._writer = successor
            self._writer_closed = False
            self._closed_writer_termination = {}
            self._generation = worker_generation
            self._last_rotation = _deep_freeze_evidence(receipt)
            return _deep_thaw_evidence(receipt)

    def status(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                "owner": (
                    "arch4.ngui.debug_surface."
                    "NGUIPageTruthGenerationManager"
                ),
                "generation": self._generation,
                "writer_closed": self._writer_closed,
                "writer": (
                    dict(self._writer.status())
                    if self._writer is not None
                    else {}
                ),
                "last_rotation": _deep_thaw_evidence(
                    self._last_rotation
                ),
            }

    def close(
        self,
        *,
        timeout_s: float = NGUI_PAGE_TRUTH_CLOSE_BUDGET_S,
    ) -> Mapping[str, Any]:
        with self._lock:
            if self._writer is None:
                return {
                    "schema": "NGUIPageTruthWriterTerminationReceipt.v1",
                    "owner": "ngui_page_truth_writer",
                    "admission_closed": True,
                    "terminated": True,
                    "status": "complete",
                    "active_sequence": 0,
                    "pending_sequence": 0,
                    "last_written_sequence": 0,
                    "rejected_sequence": 0,
                    "deadline_exceeded": False,
                }
            if not self._writer_closed:
                receipt = dict(self._writer.close(timeout_s=timeout_s))
                self._writer_closed = True
                self._closed_writer_termination = receipt
                return receipt
            return dict(self._closed_writer_termination)


def front_register_payload(
    snapshot: FrontRegisterSnapshot,
    *,
    application_lease: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    chart_snapshot = snapshot.chart_snapshot
    chart_status = dict(snapshot.chart_status)
    chart_timestamps = tuple(
        float(value)
        for value in (getattr(chart_snapshot, "timestamps", ()) or ())
    )
    chart_outages = tuple(
        getattr(chart_snapshot, "outages", ()) or ()
    )
    return {
        "schema": NGUI_PAGE_TRUTH_SCHEMA,
        "owner": NGUI_PAGE_TRUTH_OWNER,
        "application_lease": dict(application_lease or {}),
        "sequence": snapshot.sequence,
        "input_generation": snapshot.input_generation,
        "runtime_session_scope_key": snapshot.runtime_session_scope_key,
        "runtime_source_epoch": snapshot.runtime_source_epoch,
        "published_at": snapshot.published_at,
        "fields": {
            field_id: _field_payload(field)
            for field_id, field in snapshot.fields.items()
        },
        "pages": {
            page.value: {
                field_id: _field_payload(field)
                for field_id, field in truth.fields.items()
            }
            for page, truth in snapshot.pages.items()
        },
        "chart_request_sequence": (
            snapshot.chart_snapshot.request_sequence
            if snapshot.chart_snapshot is not None
            else 0
        ),
        "chart_delivery": {
            "front_session_scope_key": snapshot.runtime_session_scope_key,
            "front_source_epoch": snapshot.runtime_source_epoch,
            "front_source_sequence": accepted_modem_source_generation(snapshot),
            "front_source_cursor": chart_front_cursor(snapshot),
            "front_sampled_at": accepted_modem_source_sampled_at(snapshot),
            "front_published_at": snapshot.published_at,
            "output_session_scope_key": str(
                getattr(chart_snapshot, "session_scope_key", "") or ""
            ),
            "output_source_epoch": str(
                getattr(chart_snapshot, "source_epoch", "") or ""
            ),
            "output_request_sequence": int(
                getattr(chart_snapshot, "request_sequence", 0) or 0
            ),
            "output_source_sequence": int(
                getattr(chart_snapshot, "source_sequence", 0) or 0
            ),
            "output_source_append_ts": float(
                getattr(chart_snapshot, "source_append_ts", 0.0) or 0.0
            ),
            "output_published_at": float(
                getattr(chart_snapshot, "published_at", 0.0) or 0.0
            ),
            "output_sample_count": len(chart_timestamps),
            "output_window_seconds": int(
                getattr(chart_snapshot, "window_seconds", 0) or 0
            ),
            "output_first_timestamp": (
                chart_timestamps[0] if chart_timestamps else 0.0
            ),
            "output_last_timestamp": (
                chart_timestamps[-1] if chart_timestamps else 0.0
            ),
            "output_outage_count": len(chart_outages),
            "output_outages": [
                {
                    "started_at": float(outage.started_at),
                    "restored_at": float(outage.restored_at),
                    "duration_s": float(outage.duration_s),
                    "label": str(outage.label),
                    "lane": int(outage.lane),
                }
                for outage in chart_outages
            ],
            "history_source_sequence": int(
                chart_status.get("history_source_sequence", 0) or 0
            ),
            "history_source_cursor": tuple(
                int(value)
                for value in chart_status.get("history_source_cursor", (0, 0))
            ),
            "history_append_ts": float(
                chart_status.get("history_append_ts", 0.0) or 0.0
            ),
            "render_active": bool(chart_status.get("render_active", False)),
            "render_pending": bool(
                chart_status.get("worker_pending", False)
            ),
            "render_coalesced": int(
                chart_status.get("coalesced", 0) or 0
            ),
        },
    }


def _field_payload(field: PresentationValue) -> dict[str, Any]:
    return {
        "value": _json_output_value(field.value),
        "text": field.text,
        "state": field.state.value,
        "source_sequence": field.source_sequence,
        "sampled_at": field.sampled_at,
        "published_at": field.published_at,
        "owner": field.owner.value,
        "source_path": field.source_path,
        "freshness": field.freshness.value,
        "reason": field.reason,
    }


def _json_output_value(value: Any) -> Any:
    """Convert immutable presentation values for the strict JSON boundary."""

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Enum):
        return _json_output_value(value.value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_output_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_output_value(item) for item in value]
    raise TypeError(
        "unsupported NGUI page-truth value type: "
        f"{type(value).__name__}"
    )


def _page_truth_change_digest(payload: Mapping[str, Any]) -> str:
    """Hash visible meaning and ignore publication clock changes."""

    def field_meaning(raw: Any) -> Any:
        if not isinstance(raw, Mapping):
            return raw
        return {
            str(key): value
            for key, value in raw.items()
            if str(key) != "published_at"
        }

    fields = payload.get("fields")
    pages = payload.get("pages")
    chart_delivery = payload.get("chart_delivery")
    meaning = {
        "schema": payload.get("schema"),
        "owner": payload.get("owner"),
        "fields": {
            str(field_id): field_meaning(value)
            for field_id, value in dict(fields or {}).items()
        },
        "pages": {
            str(page): {
                str(field_id): field_meaning(value)
                for field_id, value in dict(page_fields or {}).items()
            }
            for page, page_fields in dict(pages or {}).items()
        },
        "chart_request_sequence": payload.get("chart_request_sequence"),
        "chart_delivery": {
            str(key): value
            for key, value in dict(chart_delivery or {}).items()
            if str(key) not in {"front_published_at", "output_published_at"}
        },
    }
    encoded = json.dumps(
        meaning,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_start_advisory_change_digest(payload: Mapping[str, Any]) -> str:
    """Hash advisory meaning and ignore live progress counters."""

    volatile = frozenset(
        {
            "connected_elapsed_s",
            "front_sequence",
            "input_generation",
            "published_at",
            "runtime_source_sequence",
            "source_sequence",
        }
    )

    def meaning(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): meaning(item)
                for key, item in value.items()
                if str(key) not in volatile
            }
        if isinstance(value, (list, tuple)):
            return [meaning(item) for item in value]
        return value

    encoded = json.dumps(
        meaning(payload),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class NGUIPageTruthLogWriter:
    """Write a coalesced output mirror that is never used as NGUI input."""

    def __init__(
        self,
        path: str | Path,
        *,
        report_error: Callable[[str], None],
        worker_health: WorkerHealthAuthority,
        fault_supervisor: FaultSupervisor,
        safe_start_advisory_path: str | Path | None = None,
        max_payload_bytes: int = NGUI_PAGE_TRUTH_MAX_BYTES,
        application_lease: Mapping[str, Any] | None = None,
        minimum_interval_s: float = 1.0,
    ) -> None:
        if not isinstance(worker_health, WorkerHealthAuthority):
            raise TypeError("page_truth_writer_requires_worker_health")
        if not isinstance(fault_supervisor, FaultSupervisor):
            raise TypeError("page_truth_writer_requires_fault_supervisor")
        self.path = Path(path)
        self.safe_start_advisory_path = (
            Path(safe_start_advisory_path)
            if safe_start_advisory_path is not None
            else None
        )
        self._safe_start_advisor = NGUISafeStartAdvisor()
        self._max_payload_bytes = max(1024, int(max_payload_bytes))
        self._application_lease = dict(application_lease or {})
        self._minimum_interval_s = max(0.05, float(minimum_interval_s))
        self._report_error = report_error
        self._worker_health = worker_health
        self._fault_supervisor = fault_supervisor
        self._worker_id = "ngui_page_truth_writer"
        self._worker_health.register(
            self._worker_id,
            execution_kind=WorkerExecutionKind.CONTINUOUS_SERVICE,
            required=True,
            retryable=True,
        )
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._latest: FrontRegisterSnapshot | None = None
        self._active = False
        self._active_sequence = 0
        self._closed = False
        self._close_deadline = 0.0
        self._retry_not_before = 0.0
        self._retry_delay_s = 0.05
        self._last_written_digest = ""
        self._last_write_monotonic = 0.0
        self._last_safe_start_digest = ""
        self._last_written_sequence = 0
        self._rejected_sequence = 0
        self._last_failure = ""
        self._reported_failure_key: tuple[str, str] | None = None
        self._terminal_worker_failure = ""
        self._cleanup_owned_temps()
        self._worker = Thread(
            target=self._run,
            name="ngui-page-truth-log",
            daemon=True,
        )
        self._worker_health.mark_started(self._worker_id)
        self._worker.start()

    def _cleanup_owned_temps(self) -> None:
        paths = [self.path]
        if self.safe_start_advisory_path is not None:
            paths.append(self.safe_start_advisory_path)
        for path in paths:
            try:
                cleanup_abandoned_atomic_temps(path)
            except Exception as exc:
                self._safe_diagnostic_call(
                    lambda exc=exc: self._worker_health.mark_failure(
                        self._worker_id,
                        exc,
                        next_retry_at=time.time(),
                    )
                )
                self._safe_diagnostic_call(
                    lambda exc=exc: self._fault_supervisor.record(
                        boundary=FaultBoundaryClass.GUI,
                        owner=self._worker_id,
                        operation="cleanup_abandoned_temp",
                        generation=0,
                        severity=FaultSeverity.WARNING,
                        retryable=True,
                        degraded_behavior=(
                            "page_truth_output_continues_without_temp_cleanup"
                        ),
                        error=exc,
                        suppression_budget=3,
                    )
                )
                self._safe_diagnostic_call(
                    lambda exc=exc: self._report_error(
                        "NGUI page-truth temp cleanup failed; output will "
                        f"continue: {type(exc).__name__}: {exc}"
                    )
                )

    def publish(self, snapshot: FrontRegisterSnapshot) -> None:
        with self._changed:
            if self._closed:
                raise RuntimeError("NGUI page-truth log writer is closed")
            if self._terminal_worker_failure or not self._worker.is_alive():
                raise RuntimeError(
                    "NGUI page-truth log writer is not servicing admission: "
                    f"{self._terminal_worker_failure or 'worker_not_alive'}"
                )
            newest_sequence = max(
                self._last_written_sequence,
                self._active_sequence,
                int(getattr(self._latest, "sequence", 0) or 0),
            )
            if snapshot.sequence < newest_sequence:
                raise ValueError(
                    "NGUI page-truth sequence regression: "
                    f"candidate={snapshot.sequence};newest={newest_sequence}"
                )
            self._latest = snapshot
            self._changed.notify()

    def close(
        self,
        *,
        timeout_s: float = NGUI_PAGE_TRUTH_CLOSE_BUDGET_S,
    ) -> Mapping[str, Any]:
        budget_s = max(0.0, float(timeout_s))
        close_started = time.monotonic()
        termination_margin_s = min(0.05, budget_s * 0.2)
        with self._changed:
            self._closed = True
            if self._close_deadline <= 0.0:
                self._close_deadline = (
                    close_started + max(0.0, budget_s - termination_margin_s)
                )
            self._changed.notify_all()
        self._worker.join(timeout=budget_s)
        with self._lock:
            terminated = not self._worker.is_alive()
            active_sequence = self._active_sequence
            pending_sequence = max(
                active_sequence,
                int(getattr(self._latest, "sequence", 0) or 0),
            )
            deadline_exceeded = bool(
                not terminated
                or (
                    pending_sequence > self._last_written_sequence
                    and time.monotonic() >= self._close_deadline
                )
            )
            status = (
                "complete"
                if terminated
                and pending_sequence <= self._last_written_sequence
                and self._rejected_sequence <= self._last_written_sequence
                and not self._terminal_worker_failure
                else "degraded"
            )
            return PageTruthWriterTerminationReceipt(
                owner=self._worker_id,
                admission_closed=True,
                terminated=terminated,
                status=status,
                active_sequence=active_sequence,
                pending_sequence=pending_sequence,
                last_written_sequence=self._last_written_sequence,
                rejected_sequence=self._rejected_sequence,
                deadline_exceeded=deadline_exceeded,
            ).to_payload()

    def status(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                "owner": self._worker_id,
                "closed": self._closed,
                "active": self._active,
                "active_sequence": self._active_sequence,
                "pending_sequence": int(
                    getattr(self._latest, "sequence", 0) or 0
                ),
                "retry_not_before": self._retry_not_before,
                "last_written_sequence": self._last_written_sequence,
                "rejected_sequence": self._rejected_sequence,
                "last_failure": self._last_failure,
                "terminal_worker_failure": self._terminal_worker_failure,
            }

    def _run(self) -> None:
        try:
            while True:
                with self._changed:
                    while self._latest is None and not self._closed:
                        self._changed.wait()
                    if self._latest is None and self._closed:
                        return
                    if (
                        self._closed
                        and self._close_deadline > 0.0
                        and time.monotonic() >= self._close_deadline
                    ):
                        return
                    retry_wait_s = max(
                        0.0,
                        self._retry_not_before - time.monotonic(),
                    )
                    if retry_wait_s > 0.0:
                        if self._closed and self._close_deadline > 0.0:
                            retry_wait_s = min(
                                retry_wait_s,
                                max(
                                    0.0,
                                    self._close_deadline - time.monotonic(),
                                ),
                            )
                        self._changed.wait(timeout=retry_wait_s)
                        continue
                    cadence_wait_s = max(
                        0.0,
                        self._last_write_monotonic
                        + self._minimum_interval_s
                        - time.monotonic(),
                    )
                    if cadence_wait_s > 0.0 and not self._closed:
                        self._changed.wait(timeout=cadence_wait_s)
                        continue
                    snapshot = self._latest
                    self._latest = None
                    self._active = True
                    self._active_sequence = int(snapshot.sequence)
                if snapshot is None:
                    continue
                failure: Exception | None = None
                retry_delay_s = 0.0
                try:
                    wrote_snapshot = self._write_snapshot(snapshot)
                    self._write_safe_start_advisory(snapshot)
                    if wrote_snapshot:
                        with self._lock:
                            self._last_write_monotonic = time.monotonic()
                except Exception as exc:
                    failure = exc
                if failure is not None:
                    with self._changed:
                        retryable = isinstance(failure, OSError)
                        if retryable:
                            if (
                                self._latest is None
                                or self._latest.sequence <= snapshot.sequence
                            ):
                                self._latest = snapshot
                        else:
                            self._rejected_sequence = max(
                                self._rejected_sequence,
                                int(snapshot.sequence),
                            )
                        if retryable:
                            retry_delay_s = self._retry_delay_s
                            self._retry_not_before = (
                                time.monotonic() + retry_delay_s
                            )
                            self._retry_delay_s = min(
                                1.0,
                                self._retry_delay_s * 2.0,
                            )
                        else:
                            # A deterministic candidate rejection is not an
                            # internal retry.  Wait for a newer coalesced
                            # snapshot and keep the normal output cadence so a
                            # changing Front cannot create a tight fault loop.
                            retry_delay_s = self._minimum_interval_s
                            self._retry_not_before = (
                                time.monotonic() + retry_delay_s
                            )
                        self._active = False
                        self._active_sequence = 0
                        self._last_failure = (
                            f"{type(failure).__name__}: {failure}"
                        )
                    self._report_write_failure(
                        snapshot=snapshot,
                        failure=failure,
                        retry_delay_s=retry_delay_s,
                    )
                    if (
                        self._closed
                        and self._close_deadline > 0.0
                        and time.monotonic() >= self._close_deadline
                    ):
                        return
                    continue
                with self._changed:
                    self._retry_not_before = 0.0
                    self._retry_delay_s = 0.05
                    self._active = False
                    self._active_sequence = 0
                    self._last_failure = ""
                    self._reported_failure_key = None
                    self._last_written_sequence = max(
                        self._last_written_sequence,
                        int(snapshot.sequence),
                    )
                self._safe_diagnostic_call(
                    lambda: self._worker_health.mark_success(self._worker_id)
                )
        except BaseException as exc:
            with self._changed:
                self._terminal_worker_failure = (
                    f"{type(exc).__name__}: {exc}"
                )
                self._active = False
                self._active_sequence = 0
                self._changed.notify_all()
            self._safe_diagnostic_call(
                lambda exc=exc: self._report_error(
                    "NGUI page-truth writer terminated unexpectedly: "
                    f"{type(exc).__name__}: {exc}"
                )
            )
        finally:
            with self._lock:
                has_pending = self._latest is not None
            self._safe_diagnostic_call(
                lambda: self._worker_health.mark_stopped(
                    self._worker_id,
                    reason=(
                        "page_truth_writer_closed_with_pending_snapshot"
                        if has_pending
                        else "page_truth_writer_closed"
                    ),
                )
            )

    def _report_write_failure(
        self,
        *,
        snapshot: FrontRegisterSnapshot,
        failure: Exception,
        retry_delay_s: float,
    ) -> None:
        detail = str(failure)
        failure_key = (
            type(failure).__name__,
            (
                "ngui_page_truth_payload_exceeds_budget"
                if isinstance(failure, ValueError)
                and detail.startswith(
                    "ngui_page_truth_payload_exceeds_budget:"
                )
                else detail
            ),
        )
        with self._lock:
            if self._reported_failure_key == failure_key:
                return
            self._reported_failure_key = failure_key
        self._safe_diagnostic_call(
            lambda: self._worker_health.mark_failure(
                self._worker_id,
                failure,
                next_retry_at=time.time() + retry_delay_s,
            )
        )
        retryable = isinstance(failure, OSError)
        self._safe_diagnostic_call(
            lambda: self._fault_supervisor.record(
                boundary=FaultBoundaryClass.GUI,
                owner=self._worker_id,
                operation="write_snapshot",
                generation=int(snapshot.sequence),
                severity=FaultSeverity.ERROR,
                retryable=retryable,
                degraded_behavior=(
                    "page_truth_output_retained_for_internal_retry"
                    if retryable
                    else "page_truth_snapshot_rejected_newer_generation_required"
                ),
                error=failure,
                suppression_budget=3,
            )
        )
        self._safe_diagnostic_call(
            lambda: self._report_error(
                (
                    "NGUI page-truth output failed and is retained for retry: "
                    if retryable
                    else "NGUI page-truth snapshot rejected; a newer snapshot "
                    "will be tried after the output interval: "
                )
                + f"{type(failure).__name__}: {failure}"
            )
        )

    @staticmethod
    def _safe_diagnostic_call(fn: Callable[[], Any]) -> None:
        try:
            fn()
        except BaseException:
            # A diagnostic port failure must not stop the output owner.
            return

    def _write_snapshot(self, snapshot: FrontRegisterSnapshot) -> bool:
        payload = front_register_payload(
            snapshot,
            application_lease=self._application_lease,
        )
        encoded = json.dumps(payload, indent=2, sort_keys=True)
        encoded_bytes = len(encoded.encode("utf-8"))
        if encoded_bytes > self._max_payload_bytes:
            raise ValueError(
                "ngui_page_truth_payload_exceeds_budget:"
                f"bytes={encoded_bytes};limit={self._max_payload_bytes}"
            )
        digest = _page_truth_change_digest(payload)
        with self._lock:
            if digest == self._last_written_digest:
                return False
        atomic_write_text(
            self.path,
            encoded,
            encoding="utf-8",
        )
        with self._lock:
            self._last_written_digest = digest
        return True

    def _write_safe_start_advisory(
        self,
        snapshot: FrontRegisterSnapshot,
    ) -> None:
        path = self.safe_start_advisory_path
        if path is None:
            return
        payload = self._safe_start_advisor.evaluate(snapshot)
        encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        digest = _safe_start_advisory_change_digest(payload)
        with self._lock:
            if digest == self._last_safe_start_digest:
                return
        atomic_write_text(
            path,
            encoded,
        )
        with self._lock:
            self._last_safe_start_digest = digest


__all__ = [
    "NGUI_PAGE_TRUTH_MAX_BYTES",
    "NGUI_PAGE_TRUTH_OWNER",
    "NGUI_PAGE_TRUTH_SCHEMA",
    "NGUIPageTruthGenerationManager",
    "NGUIPageTruthLogWriter",
    "PageTruthGenerationRotationReceipt",
    "front_register_payload",
]
