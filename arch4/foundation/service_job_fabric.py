"""Independent Foundation service jobs driven by one product clock pulse."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from enum import StrEnum
import math
from threading import Condition, RLock, get_ident
from contextlib import contextmanager
from time import monotonic
from typing import Any, Callable, Mapping

from arch4.architecture_contracts.runtime_generation import (
    RuntimeGenerationNamespace,
)


FOUNDATION_SERVICE_JOB_FABRIC_OWNER = "54guc.foundation.service_job_fabric"
FOUNDATION_SERVICE_JOB_SHUTDOWN_TIMEOUT_S = 5.0


class FoundationServiceJobDispatchDisposition(StrEnum):
    DISPATCHED = "dispatched"
    INFLIGHT = "inflight"


@dataclass(frozen=True, slots=True)
class FoundationServiceJobSpec:
    job_id: str
    dependency_group: str

    def __post_init__(self) -> None:
        if type(self.job_id) is not str or type(self.dependency_group) is not str:
            raise TypeError("foundation_service_job_identity_string_required")
        job_id = self.job_id.strip()
        group = self.dependency_group.strip()
        if not job_id or not group:
            raise ValueError("foundation_service_job_identity_required")
        object.__setattr__(self, "job_id", job_id)
        object.__setattr__(self, "dependency_group", group)


@dataclass(frozen=True, slots=True)
class FoundationServiceJobAdmission:
    """Active runtime namespace and product service cycle."""

    namespace: RuntimeGenerationNamespace
    service_cycle: int

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, RuntimeGenerationNamespace):
            raise TypeError("foundation_service_job_namespace_required")
        if type(self.service_cycle) is not int:
            raise TypeError("foundation_service_job_cycle_int_required")
        if self.service_cycle < 0:
            raise ValueError("foundation_service_job_cycle_invalid")


def _positive_finite_time(value: Any, reason: str) -> float:
    if type(value) not in {int, float}:
        raise TypeError(reason)
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ValueError(reason)
    return normalized


def _validate_job_admission_identity(
    admission: FoundationServiceJobAdmission,
    *,
    request_sequence: int,
    job_id: str,
    dependency_group: str,
) -> None:
    if not isinstance(admission, FoundationServiceJobAdmission):
        raise TypeError("foundation_service_job_admission_required")
    if type(request_sequence) is not int or request_sequence <= 0:
        raise ValueError("foundation_service_job_request_sequence_invalid")
    if type(job_id) is not str or type(dependency_group) is not str:
        raise TypeError("foundation_service_job_identity_string_required")
    identity = job_id.strip()
    group = dependency_group.strip()
    if not identity or not group:
        raise ValueError("foundation_service_job_identity_required")
    if identity != job_id or group != dependency_group:
        raise ValueError("foundation_service_job_identity_not_canonical")


@dataclass(frozen=True, slots=True)
class FoundationServiceJobCompletion:
    admission: FoundationServiceJobAdmission
    request_sequence: int
    job_id: str
    dependency_group: str
    requested_at: float
    started_at_monotonic: float
    completed_at_monotonic: float
    result: Any = None
    error: Exception | None = None
    quarantined: bool = False

    def __post_init__(self) -> None:
        _validate_job_admission_identity(
            self.admission,
            request_sequence=self.request_sequence,
            job_id=self.job_id,
            dependency_group=self.dependency_group,
        )
        requested_at = _positive_finite_time(
            self.requested_at,
            "foundation_service_job_requested_at_invalid",
        )
        started_at = _positive_finite_time(
            self.started_at_monotonic,
            "foundation_service_job_started_at_invalid",
        )
        completed_at = _positive_finite_time(
            self.completed_at_monotonic,
            "foundation_service_job_completed_at_invalid",
        )
        if completed_at < started_at:
            raise ValueError("foundation_service_job_time_order_invalid")
        if type(self.quarantined) is not bool:
            raise TypeError("foundation_service_job_quarantined_bool_required")
        if self.error is not None and not isinstance(self.error, Exception):
            raise TypeError("foundation_service_job_error_exception_required")
        if self.error is not None and self.result is not None:
            raise ValueError("foundation_service_job_failed_result_forbidden")
        if self.quarantined and self.error is None:
            raise ValueError("foundation_service_job_quarantine_error_required")
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "started_at_monotonic", started_at)
        object.__setattr__(self, "completed_at_monotonic", completed_at)

    @property
    def namespace(self) -> RuntimeGenerationNamespace:
        return self.admission.namespace

    @property
    def succeeded(self) -> bool:
        return self.error is None and not self.quarantined


@dataclass(frozen=True, slots=True)
class FoundationServiceJobDispatch:
    admission: FoundationServiceJobAdmission
    request_sequence: int
    job_id: str
    dependency_group: str
    disposition: FoundationServiceJobDispatchDisposition
    requested_at: float

    def __post_init__(self) -> None:
        _validate_job_admission_identity(
            self.admission,
            request_sequence=self.request_sequence,
            job_id=self.job_id,
            dependency_group=self.dependency_group,
        )
        if not isinstance(
            self.disposition,
            FoundationServiceJobDispatchDisposition,
        ):
            raise TypeError(
                "foundation_service_job_dispatch_disposition_required"
            )
        object.__setattr__(
            self,
            "requested_at",
            _positive_finite_time(
                self.requested_at,
                "foundation_service_job_requested_at_invalid",
            ),
        )

    @property
    def namespace(self) -> RuntimeGenerationNamespace:
        return self.admission.namespace


class FoundationServiceJobContext:
    """Namespace token and final gate for external effects."""

    def __init__(
        self,
        fabric: "FoundationServiceJobFabric",
        *,
        admission: FoundationServiceJobAdmission,
        request_sequence: int,
        job_id: str,
    ) -> None:
        if not isinstance(fabric, FoundationServiceJobFabric):
            raise TypeError("foundation_service_job_fabric_required")
        if type(job_id) is not str:
            raise TypeError("foundation_service_job_identity_string_required")
        catalog = {
            identity: spec.dependency_group
            for identity, spec in fabric._catalog.items()
        }
        identity = job_id.strip()
        if identity != job_id:
            raise ValueError("foundation_service_job_identity_not_canonical")
        if identity not in catalog:
            raise ValueError("foundation_service_job_context_catalog_invalid")
        _validate_job_admission_identity(
            admission,
            request_sequence=request_sequence,
            job_id=identity,
            dependency_group=catalog[identity],
        )
        self._fabric = fabric
        self.admission = admission
        self.request_sequence = request_sequence
        self.job_id = identity

    @property
    def namespace(self) -> RuntimeGenerationNamespace:
        return self.admission.namespace

    def run_effect(self, effect: Callable[[], Any]) -> Any:
        """Execute one command/write only while this namespace is active."""

        return self._fabric._run_effect(self, effect)


class FoundationServiceJobFabric:
    """Run independent Foundation jobs concurrently without overlapping a job."""

    def __init__(
        self,
        specs: tuple[FoundationServiceJobSpec, ...],
    ) -> None:
        if not specs:
            raise ValueError("foundation_service_job_catalog_required")
        catalog: dict[str, FoundationServiceJobSpec] = {}
        for spec in specs:
            if not isinstance(spec, FoundationServiceJobSpec):
                raise TypeError("foundation_service_job_spec_required")
            if spec.job_id in catalog:
                raise ValueError(
                    f"foundation_service_job_duplicate:{spec.job_id}"
                )
            catalog[spec.job_id] = spec
        self._catalog = catalog
        self._executor = ThreadPoolExecutor(
            max_workers=len(catalog),
            thread_name_prefix="54guc-foundation",
        )
        self._lock = RLock()
        self._effects = Condition(self._lock)
        self._group_locks = {spec.dependency_group: RLock() for spec in specs}
        self._entered_effects = 0
        self._effect_threads: dict[int, tuple[str, int]] = {}
        self._cutover_pending = False
        self._drain_deadline: float | None = None
        self._quarantined_requests: set[int] = set()
        self._active_namespace: RuntimeGenerationNamespace | None = None
        self._request_sequence = 0
        self._inflight: dict[
            tuple[RuntimeGenerationNamespace, str],
            tuple[
                int,
                FoundationServiceJobAdmission,
                float,
                Future[FoundationServiceJobCompletion],
            ],
        ] = {}
        self._closed = False

    def _run(
        self,
        admission: FoundationServiceJobAdmission,
        request_sequence: int,
        spec: FoundationServiceJobSpec,
        requested_at: float,
        call: Callable[[FoundationServiceJobContext], Any],
    ) -> FoundationServiceJobCompletion:
        started = monotonic()
        context = FoundationServiceJobContext(
            self,
            admission=admission,
            request_sequence=request_sequence,
            job_id=spec.job_id,
        )
        try:
            result = call(context)
        except Exception as exc:
            return FoundationServiceJobCompletion(
                admission=admission,
                request_sequence=request_sequence,
                job_id=spec.job_id,
                dependency_group=spec.dependency_group,
                requested_at=requested_at,
                started_at_monotonic=started,
                completed_at_monotonic=monotonic(),
                error=exc,
            )
        return FoundationServiceJobCompletion(
            admission=admission,
            request_sequence=request_sequence,
            job_id=spec.job_id,
            dependency_group=spec.dependency_group,
            requested_at=requested_at,
            started_at_monotonic=started,
            completed_at_monotonic=monotonic(),
            result=result,
        )

    def bind_runtime_namespace(
        self,
        namespace: RuntimeGenerationNamespace,
    ) -> bool:
        """Rotate namespace admission, then drain any entered old effect."""

        if not isinstance(namespace, RuntimeGenerationNamespace):
            raise TypeError("foundation_service_job_namespace_required")
        with self._effects:
            if get_ident() in self._effect_threads:
                raise RuntimeError("foundation_namespace_cutover_inside_effect_illegal")
            while self._cutover_pending:
                self._effects.wait()
            self._cutover_pending = True
            try:
                if self._closed:
                    raise RuntimeError("foundation_service_job_fabric_closed")
                if self._active_namespace == namespace:
                    return False
                while self._entered_effects:
                    self._effects.wait()
                if self._closed:
                    raise RuntimeError("foundation_service_job_fabric_closed")
                self._active_namespace = namespace
            finally:
                self._cutover_pending = False
                self._effects.notify_all()
        return True

    def dispatch(
        self,
        job_id: str,
        *,
        admission: FoundationServiceJobAdmission,
        requested_at: float,
        call: Callable[[FoundationServiceJobContext], Any],
    ) -> FoundationServiceJobDispatch:
        if type(job_id) is not str:
            raise TypeError("foundation_service_job_identity_string_required")
        identity = job_id.strip()
        if identity != job_id:
            raise ValueError("foundation_service_job_identity_not_canonical")
        if identity not in self._catalog:
            raise KeyError(f"foundation_service_job_undeclared:{identity}")
        if not callable(call):
            raise TypeError("foundation_service_job_callable_required")
        if not isinstance(admission, FoundationServiceJobAdmission):
            raise TypeError("foundation_service_job_admission_required")
        timestamp = _positive_finite_time(
            requested_at,
            "foundation_service_job_timestamp_invalid",
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("foundation_service_job_fabric_closed")
            if self._active_namespace != admission.namespace:
                raise RuntimeError(
                    "foundation_service_job_namespace_not_active"
                )
            inflight_key = (
                admission.namespace,
                identity,
            )
            prior = self._inflight.get(inflight_key)
            if prior is not None:
                return FoundationServiceJobDispatch(
                    admission=prior[1],
                    request_sequence=prior[0],
                    job_id=identity,
                    dependency_group=self._catalog[
                        identity
                    ].dependency_group,
                    disposition=(
                        FoundationServiceJobDispatchDisposition.INFLIGHT
                    ),
                    requested_at=timestamp,
                )
            spec = self._catalog[identity]
            self._request_sequence += 1
            request_sequence = self._request_sequence
            self._inflight[inflight_key] = (
                request_sequence,
                admission,
                timestamp,
                self._executor.submit(
                    self._run,
                    admission,
                    request_sequence,
                    spec,
                    timestamp,
                    call,
                ),
            )
        return FoundationServiceJobDispatch(
            admission=admission,
            request_sequence=request_sequence,
            job_id=identity,
            dependency_group=spec.dependency_group,
            disposition=FoundationServiceJobDispatchDisposition.DISPATCHED,
            requested_at=timestamp,
        )

    def run_bound_effect(
        self,
        *,
        admission: FoundationServiceJobAdmission,
        dependency_group: str,
        effect: Callable[[], Any],
    ) -> Any:
        """Fence a synchronous command/write at the same final barrier as jobs."""

        if not isinstance(admission, FoundationServiceJobAdmission):
            raise TypeError("foundation_service_job_admission_required")
        if not callable(effect):
            raise TypeError("foundation_service_job_effect_required")
        with self._admit_effect(admission.namespace, dependency_group, context=None):
            return effect()

    def collect(self) -> tuple[FoundationServiceJobCompletion, ...]:
        completed: list[FoundationServiceJobCompletion] = []
        with self._lock:
            active_namespace = self._active_namespace
            for inflight_key, (
                _request_sequence,
                _admission,
                _requested_at,
                future,
            ) in tuple(self._inflight.items()):
                if not future.done():
                    continue
                self._inflight.pop(inflight_key, None)
                if future.cancelled():
                    completion = FoundationServiceJobCompletion(
                        admission=_admission, request_sequence=_request_sequence,
                        job_id=inflight_key[1], dependency_group=self._catalog[inflight_key[1]].dependency_group,
                        requested_at=_requested_at, started_at_monotonic=monotonic(),
                        completed_at_monotonic=monotonic(),
                        error=RuntimeError("foundation_service_job_cancelled"), quarantined=True)
                else:
                    completion = future.result()
                if completion.namespace != active_namespace or _request_sequence in self._quarantined_requests:
                    completion = replace(
                        completion,
                        result=None,
                        error=RuntimeError(
                            "foundation_service_job_completion_quarantined"
                        ),
                        quarantined=True,
                    )
                completed.append(completion)
                self._quarantined_requests.discard(_request_sequence)
        return tuple(
            sorted(completed, key=lambda receipt: receipt.job_id)
        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            inflight = tuple(
                sorted(
                    (
                        admission.namespace.lifecycle_generation,
                        admission.namespace.session_scope_key,
                        admission.namespace.source_epoch,
                        admission.service_cycle,
                        job_id,
                    )
                    for (
                        _namespace,
                        job_id,
                    ), (
                        _request_sequence,
                        admission,
                        _requested_at,
                        _future,
                    ) in self._inflight.items()
                )
            )
            return {
                "owner": FOUNDATION_SERVICE_JOB_FABRIC_OWNER,
                "closed": self._closed,
                "runtime_namespace": (
                    self._active_namespace.as_dict()
                    if self._active_namespace is not None
                    else {}
                ),
                "declared_jobs": tuple(sorted(self._catalog)),
                "inflight_jobs": inflight,
                "active_effects": self._entered_effects,
                "surviving_jobs": tuple(sorted(key[1] for key, row in self._inflight.items()
                                               if self._closed and not row[3].done())),
            }

    def shutdown(
        self,
        *,
        timeout_s: float = FOUNDATION_SERVICE_JOB_SHUTDOWN_TIMEOUT_S,
    ) -> tuple[FoundationServiceJobCompletion, ...]:
        """Close admission and return a terminal result within one deadline."""

        timeout = _positive_finite_time(
            timeout_s,
            "foundation_service_job_shutdown_timeout_invalid",
        )
        deadline = monotonic() + timeout
        with self._lock:
            already_closed = self._closed
            if not already_closed:
                self._closed = True
                self._drain_deadline = deadline
        if already_closed:
            return self.collect()
        with self._lock:
            futures = tuple(row[3] for row in self._inflight.values())
        if futures:
            wait(futures, timeout=max(0.0, deadline - monotonic()))
        with self._effects:
            while self._entered_effects and monotonic() < deadline:
                self._effects.wait(timeout=max(0.0, deadline - monotonic()))
        completions = list(self.collect())
        expired_at = monotonic()
        with self._lock:
            survivors = tuple(self._inflight.items())
            for inflight_key, (
                request_sequence,
                admission,
                requested_at,
                future,
            ) in survivors:
                self._quarantined_requests.add(request_sequence)
                future.cancel()
                job_id = inflight_key[1]
                completions.append(
                    FoundationServiceJobCompletion(
                        admission=admission,
                        request_sequence=request_sequence,
                        job_id=job_id,
                        dependency_group=self._catalog[
                            job_id
                        ].dependency_group,
                        requested_at=requested_at,
                        started_at_monotonic=expired_at,
                        completed_at_monotonic=expired_at,
                        error=RuntimeError(
                            "foundation_service_job_shutdown_deadline_exceeded"
                        ),
                        quarantined=True,
                    )
                )
        self._executor.shutdown(wait=False, cancel_futures=True)
        return tuple(
            sorted(completions, key=lambda receipt: receipt.job_id)
        )

    def _run_effect(
        self,
        context: FoundationServiceJobContext,
        effect: Callable[[], Any],
    ) -> Any:
        if not isinstance(context, FoundationServiceJobContext):
            raise TypeError("foundation_service_job_context_required")
        if not callable(effect):
            raise TypeError("foundation_service_job_effect_required")
        group = self._catalog[context.job_id].dependency_group
        with self._admit_effect(context.namespace, group, context=context):
            return effect()

    @contextmanager
    def _admit_effect(self, namespace, dependency_group: str, *, context):
        """One lifecycle barrier, with exclusion only within a declared domain."""
        if type(dependency_group) is not str or dependency_group not in self._group_locks:
            raise ValueError("foundation_service_job_dependency_group_undeclared")
        thread_id = get_ident()
        with self._lock:
            nested = self._effect_threads.get(thread_id)
            if nested and nested[0] != dependency_group:
                raise RuntimeError("foundation_cross_group_nested_effect_illegal")
        with self._group_locks[dependency_group]:
            with self._effects:
                while self._cutover_pending and thread_id not in self._effect_threads:
                    self._effects.wait()
                if self._active_namespace != namespace:
                    raise RuntimeError("foundation_service_job_namespace_retired_before_effect")
                if context is None:
                    if self._closed:
                        raise RuntimeError("foundation_service_job_fabric_closed")
                else:
                    row = self._inflight.get((context.namespace, context.job_id))
                    if (row is None or row[0] != context.request_sequence or row[1] != context.admission
                            or context.request_sequence in self._quarantined_requests
                            or self._closed and (self._drain_deadline is None or monotonic() >= self._drain_deadline)):
                        raise RuntimeError("foundation_service_job_effect_authority_retired")
                previous_depth = self._effect_threads.get(thread_id, (dependency_group, 0))[1]
                self._effect_threads[thread_id] = (dependency_group, previous_depth + 1)
                self._entered_effects += 1
            try:
                yield
            finally:
                with self._effects:
                    depth = self._effect_threads[thread_id][1] - 1
                    if depth:
                        self._effect_threads[thread_id] = (dependency_group, depth)
                    else:
                        del self._effect_threads[thread_id]
                    self._entered_effects -= 1
                    self._effects.notify_all()


def require_complete_job_calls(
    specs: tuple[FoundationServiceJobSpec, ...],
    calls: Mapping[
        str,
        Callable[[FoundationServiceJobContext], Any],
    ],
) -> dict[str, Callable[[FoundationServiceJobContext], Any]]:
    expected = {spec.job_id for spec in specs}
    actual = set(calls)
    if actual != expected:
        raise ValueError(
            "foundation_service_job_call_catalog_mismatch:"
            f"missing={sorted(expected - actual)};"
            f"extra={sorted(actual - expected)}"
        )
    for job_id, call in calls.items():
        if not callable(call):
            raise TypeError(
                f"foundation_service_job_callable_invalid:{job_id}"
            )
    return dict(calls)


__all__ = (
    "FOUNDATION_SERVICE_JOB_FABRIC_OWNER",
    "FoundationServiceJobCompletion",
    "FoundationServiceJobAdmission",
    "FoundationServiceJobContext",
    "FoundationServiceJobDispatch",
    "FoundationServiceJobDispatchDisposition",
    "FoundationServiceJobFabric",
    "FoundationServiceJobSpec",
    "require_complete_job_calls",
)
