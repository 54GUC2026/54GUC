"""Clock-lane runtime authority.

``cycle`` is retained only as an internal sequence counter for deterministic ordering
and tests. Freshness, liveness, and lane health are wall-clock based.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import time
from typing import Any, Callable, Iterable, Mapping

from arch4.modem.base import TargetGuardError
from arch4.architecture_contracts.application_lifecycle import ProductRuntimeWorkerReplacementRequired


MAX_CLOCK_MS = 100
# Latest-only JSON exports are medium-lane GUI truth, not modem liveness.
# Keep them fresh without writing every sub-second clock beat.
EXPORT_SIDECAR_CADENCE_CYCLES = 20
EXPORT_SIDECAR_DRAIN_MAX_ITEMS = 3
CALIBRATION_LEARNING_CADENCE_CYCLES = 300
CALIBRATION_LEARNING_JOB_KEY = "brain:calibration_learning_lane"
BRAIN_EVENT_INBOX_DRAIN_MAX_ITEMS = 32
BRAIN_EVENT_INBOX_JOB_KEY = "brain:event_inbox"
LEARNING_QUALITY_JOB_KEY = "brain:learning_quality"
SELF_LEARNING_STRATEGY_JOB_KEY = "brain:self_learning_strategy"
SMART_LOOP_CAPACITY_PROBE_JOB_KEY = "smart_loop:capacity_probe"
CORE_CLOCK_AUTHORITY_REQUIRED_JOBS: tuple[str, ...] = (
    "core:command_lifecycle",
    "modem_live:refresh_registers",
    "runtime:status_decision_snapshot",
    "core:downstream_diagnostics",
)
CORE_CLOCK_AUTHORITY_REQUIRED_LANES: tuple[str, ...] = (
    "fast_modem_register",
    "medium_status_decision",
    "slow_core_diagnostics",
)


class ClockedSkipPolicy(str, Enum):
    INHERIT_LANE = "inherit_lane"
    SKIP_IF_LATE = "skip_if_late"
    NEVER_SKIP_CACHE_LAST_GOOD = "never_skip_cache_last_good"
    SUPERSEDE_LATEST = "supersede_latest"
    SKIP_IF_BLOCKING_LANE_DUE = "skip_if_blocking_lane_due"
    EVENT_DRIVEN = "event_driven"
    BOUNDED_QUEUE_SUPERSEDE_LATEST = "bounded_queue_supersede_latest"
    CATCH_UP = "catch_up"
    DROP_STALE = "drop_stale"
    RUN_ONCE_IF_LATE = "run_once_if_late"
    BLOCK_UNTIL_ACTION_DONE = "block_until_action_done"


@dataclass(frozen=True)
class ClockedLaneSpec:
    name: str
    owner: str
    purpose: str
    default_cadence_cycles: int
    budget_ms: float
    priority: int
    skip_policy: ClockedSkipPolicy | str
    output_surface: str
    recovery_permission: str = "none"
    interrupt_policy: str = "cooperative"
    blocks_lanes_when_due: tuple[str, ...] = ()
    block_reason: str = ""

    @property
    def default_cadence_s(self) -> float:
        return _cadence_cycles_to_seconds(self.default_cadence_cycles)


@dataclass(frozen=True)
class ClockedJob:
    owner: str
    name: str
    cadence_cycles: int
    budget_ms: float
    lane: str = "normal"
    priority: int = 100
    skip_policy: ClockedSkipPolicy | str = ClockedSkipPolicy.INHERIT_LANE
    output_surface: str = ""
    input_generation_surface: str = "runtime_generation"
    deadline_ms: float | None = None
    recovery_permission: str = "none"
    fault_policy: str = "typed_degraded"
    interrupt_policy: str = "cooperative"
    interruptible: bool = True
    max_catch_up_runs: int = 5

    @property
    def key(self) -> str:
        return f"{self.owner}:{self.name}"

    @property
    def cadence_s(self) -> float:
        return _cadence_cycles_to_seconds(self.cadence_cycles)


@dataclass
class ClockedJobState:
    job: ClockedJob
    last_run_cycle: int = -1
    last_due_cycle: int = -1
    last_start_cycle: int = -1
    last_run_duration_ms: float = 0.0
    last_start_ts: float = 0.0
    last_finish_ts: float = 0.0
    last_due_ts: float = 0.0
    last_lateness_s: float = 0.0
    max_lateness_s: float = 0.0
    last_lateness_cycles: int = 0
    max_lateness_cycles: int = 0
    last_backlog: int = 0
    last_error: str = ""
    last_backpressure_reason: str = ""
    run_count: int = 0
    skipped_count: int = 0
    over_budget_count: int = 0
    pending_event_count: int = 0
    last_fault: "ClockFaultResult | None" = None


@dataclass(frozen=True)
class ClockedSkipDecision:
    skip: bool = False
    reason: str = ""
    backlog: int = 0
    consume_due: bool = False


@dataclass(frozen=True)
class ClockedRunReport:
    job_key: str
    lane: str
    due_cycle: int
    run_cycle: int
    lateness_cycles: int
    duration_ms: float
    budget_ms: float
    over_budget: bool
    due_ts: float = 0.0
    run_ts: float = 0.0
    lateness_s: float = 0.0
    skipped: bool = False
    fault: "ClockFaultResult | None" = None


@dataclass(frozen=True)
class ClockFaultResult:
    owner: str
    operation: str
    generation: int
    severity: str
    retryable: bool
    degraded_behavior: str
    error_type: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "operation": self.operation,
            "generation": self.generation,
            "severity": self.severity,
            "retryable": self.retryable,
            "degraded_behavior": self.degraded_behavior,
            "error_type": self.error_type,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ClockedJobOutcome:
    ran: bool
    backlog: int = 0
    last_error: str = ""
    backpressure_reason: str = ""
    fault: ClockFaultResult | None = None


class ClockedScheduler:
    """Deterministic registration/selection layer for future clocked work."""

    def __init__(
        self,
        *,
        clock_ms: int = MAX_CLOCK_MS,
        lane_specs: Iterable[ClockedLaneSpec] | None = None,
    ):
        clock = int(clock_ms)
        if clock <= 0:
            raise ValueError("clock_ms must be positive")
        if clock > MAX_CLOCK_MS:
            raise ValueError(f"clock_ms must be <= {MAX_CLOCK_MS}")
        self.clock_ms = clock
        if lane_specs is None:
            raise ValueError("clock_scheduler_explicit_lane_catalog_required")
        specs = tuple(lane_specs)
        if not specs:
            raise ValueError("clock_scheduler_lane_catalog_required")
        specs = tuple(_normalize_lane_spec(spec) for spec in specs)
        self._lane_specs: dict[str, ClockedLaneSpec] = {spec.name: spec for spec in specs}
        if len(self._lane_specs) != len(specs):
            raise ValueError("clock lane names must be unique")
        self._validate_lane_specs()
        self._jobs: dict[str, ClockedJobState] = {}
        self._cycle = 0
        self._now_ts = 0.0
        self._started_ts = 0.0
        self.last_run_reports: tuple[ClockedRunReport, ...] = ()

    @property
    def cycle_index(self) -> int:
        return self._cycle

    def register(self, job: ClockedJob) -> None:
        if job.cadence_cycles <= 0:
            raise ValueError("cadence_cycles must be positive")
        if job.budget_ms < 0:
            raise ValueError("budget_ms must be non-negative")
        self._jobs[job.key] = ClockedJobState(job=self._normalize_job(job))

    def trigger(self, job_key: str, *, count: int = 1) -> None:
        state = self._jobs[str(job_key)]
        state.pending_event_count += max(1, int(count or 1))

    def configure_job(self, job_key: str, **changes) -> None:
        state = self._jobs[str(job_key)]
        allowed = {
            "cadence_cycles",
            "budget_ms",
            "lane",
            "priority",
            "skip_policy",
            "output_surface",
            "input_generation_surface",
            "deadline_ms",
            "recovery_permission",
            "fault_policy",
            "interrupt_policy",
            "interruptible",
            "max_catch_up_runs",
        }
        unknown = sorted(str(key) for key in changes if key not in allowed)
        if unknown:
            raise ValueError(f"unsupported clock job configuration fields: {unknown}")
        updated = self._normalize_job(replace(state.job, **changes))
        if updated.cadence_cycles <= 0:
            raise ValueError("cadence_cycles must be positive")
        if updated.budget_ms < 0:
            raise ValueError("budget_ms must be non-negative")
        state.job = updated

    def advance(self, cycles: int = 1) -> int:
        step_count = max(1, int(cycles))
        self._cycle += step_count
        if self._started_ts <= 0.0:
            self._started_ts = time.time()
        base_ts = float(self._now_ts or self._started_ts)
        self._now_ts = base_ts + (_cadence_cycles_to_seconds(step_count, clock_ms=self.clock_ms))
        return self._cycle

    def observe_wall(self, *, now: float | None = None) -> int:
        self._cycle += 1
        now_ts = float(time.time() if now is None else now)
        if self._started_ts <= 0.0:
            self._started_ts = now_ts
        self._now_ts = now_ts
        return self._cycle

    def due_jobs(self) -> list[ClockedJob]:
        due: list[ClockedJob] = []
        for state in self._jobs.values():
            if state.job.skip_policy == ClockedSkipPolicy.EVENT_DRIVEN:
                if state.pending_event_count > 0:
                    due.append(state.job)
                continue
            if self._job_is_due(state):
                due.append(state.job)
        due.sort(key=lambda job: (job.priority, job.owner, job.name))
        return due

    def _normalize_job(self, job: ClockedJob) -> ClockedJob:
        lane_name = str(job.lane or "").strip()
        if not lane_name:
            raise ValueError(f"clock job {job.key!r} must declare a lane")
        spec = self._lane_specs.get(lane_name)
        if spec is None:
            raise ValueError(f"clock job {job.key!r} uses undeclared lane {lane_name!r}")
        policy = _normalize_skip_policy(job.skip_policy)
        if policy == ClockedSkipPolicy.INHERIT_LANE:
            policy = _normalize_skip_policy(spec.skip_policy)
        output_surface = str(job.output_surface or "").strip() or str(spec.output_surface or "").strip()
        if not output_surface:
            raise ValueError(f"clock job {job.key!r} must declare an output surface")
        recovery_permission = str(job.recovery_permission or "").strip() or str(spec.recovery_permission or "none")
        input_generation_surface = str(job.input_generation_surface or "").strip()
        if not input_generation_surface:
            raise ValueError(f"clock job {job.key!r} must declare an input generation surface")
        deadline_ms = float(job.budget_ms if job.deadline_ms is None else job.deadline_ms)
        if deadline_ms < 0.0:
            raise ValueError(f"clock job {job.key!r} must declare a non-negative deadline")
        fault_policy = str(job.fault_policy or "").strip()
        if not fault_policy:
            raise ValueError(f"clock job {job.key!r} must declare a fault policy")
        interrupt_policy = str(job.interrupt_policy or "").strip()
        if not interrupt_policy:
            raise ValueError(f"clock job {job.key!r} must declare an interrupt policy")
        priority = int(job.priority if job.priority is not None else spec.priority)
        return replace(
            job,
            lane=lane_name,
            priority=priority,
            skip_policy=policy,
            output_surface=output_surface,
            input_generation_surface=input_generation_surface,
            deadline_ms=deadline_ms,
            recovery_permission=recovery_permission,
            fault_policy=fault_policy,
            interrupt_policy=interrupt_policy,
        )

    def _validate_lane_specs(self) -> None:
        for spec in self._lane_specs.values():
            if not str(spec.name or "").strip():
                raise ValueError("clock lane must declare a name")
            if not str(spec.owner or "").strip():
                raise ValueError(f"clock lane {spec.name!r} must declare an owner")
            if spec.default_cadence_cycles <= 0:
                raise ValueError(f"clock lane {spec.name!r} must declare a positive default cadence")
            if spec.budget_ms < 0:
                raise ValueError(f"clock lane {spec.name!r} must declare a non-negative budget")
            if not str(spec.output_surface or "").strip():
                raise ValueError(f"clock lane {spec.name!r} must declare an output surface")
            for blocked_lane in spec.blocks_lanes_when_due:
                if blocked_lane not in self._lane_specs:
                    raise ValueError(
                        f"clock lane {spec.name!r} blocks undeclared lane {blocked_lane!r}"
                    )

    def _wall_now(self) -> float:
        if self._now_ts > 0.0:
            return float(self._now_ts)
        return time.time()

    def _job_cadence_s(self, job: ClockedJob) -> float:
        return _cadence_cycles_to_seconds(job.cadence_cycles, clock_ms=self.clock_ms)

    def _job_due_ts(self, state: ClockedJobState) -> float:
        if state.last_run_cycle < 0:
            return float(self._started_ts or self._wall_now())
        anchor = float(state.last_start_ts or state.last_finish_ts)
        return anchor + self._job_cadence_s(state.job)

    def _job_lateness_s(self, state: ClockedJobState) -> float:
        due_ts = self._job_due_ts(state)
        if due_ts <= 0.0:
            return 0.0
        return max(0.0, self._wall_now() - due_ts)

    def _job_late_periods(self, state: ClockedJobState) -> int:
        cadence_s = self._job_cadence_s(state.job)
        if cadence_s <= 0.0:
            return 0
        return int(self._job_lateness_s(state) / cadence_s)

    def _job_is_due(self, state: ClockedJobState) -> bool:
        return state.last_run_cycle < 0 or self._wall_now() >= self._job_due_ts(state)

    def _event_wall_time(self) -> float:
        return self._wall_now()

    def mark_run(
        self,
        job: ClockedJob,
        *,
        duration_ms: float = 0.0,
        start_ts: float = 0.0,
        finish_ts: float = 0.0,
        backlog: int = 0,
        last_error: str = "",
        backpressure_reason: str = "",
        fault: ClockFaultResult | None = None,
    ) -> None:
        state = self._jobs[job.key]
        due_ts = self._job_due_ts(state)
        lateness_s = self._job_lateness_s(state)
        late_periods = self._job_late_periods(state)
        state.last_due_cycle = 0 if state.last_run_cycle < 0 else state.last_run_cycle + state.job.cadence_cycles
        state.last_due_ts = due_ts
        state.last_start_cycle = self._cycle
        state.last_run_cycle = self._cycle
        state.last_run_duration_ms = max(0.0, float(duration_ms or 0.0))
        state.last_start_ts = float(start_ts or 0.0)
        state.last_finish_ts = float(finish_ts or 0.0)
        state.last_lateness_s = round(lateness_s, 3)
        state.max_lateness_s = round(max(state.max_lateness_s, lateness_s), 3)
        state.last_lateness_cycles = late_periods
        state.max_lateness_cycles = max(state.max_lateness_cycles, late_periods)
        state.last_backlog = max(0, int(backlog or 0))
        state.last_error = str(last_error or "")
        state.last_backpressure_reason = str(backpressure_reason or "")
        state.last_fault = fault
        state.run_count += 1
        if state.last_run_duration_ms > state.job.budget_ms:
            state.over_budget_count += 1
        if state.pending_event_count > 0:
            state.pending_event_count = 0

    def mark_skipped(
        self,
        job: ClockedJob,
        *,
        start_ts: float = 0.0,
        finish_ts: float = 0.0,
        backlog: int = 0,
        last_error: str = "",
        backpressure_reason: str = "",
        consume_due: bool = False,
        fault: ClockFaultResult | None = None,
    ) -> None:
        state = self._jobs[job.key]
        due_ts = self._job_due_ts(state)
        lateness_s = self._job_lateness_s(state)
        late_periods = self._job_late_periods(state)
        state.last_due_cycle = 0 if state.last_run_cycle < 0 else state.last_run_cycle + state.job.cadence_cycles
        state.last_due_ts = due_ts
        state.last_start_cycle = self._cycle
        if consume_due:
            state.last_run_cycle = self._cycle
        state.last_start_ts = float(start_ts or 0.0)
        state.last_finish_ts = float(finish_ts or 0.0)
        state.last_run_duration_ms = 0.0
        state.last_lateness_s = round(lateness_s, 3)
        state.max_lateness_s = round(max(state.max_lateness_s, lateness_s), 3)
        state.last_lateness_cycles = late_periods
        state.max_lateness_cycles = max(state.max_lateness_cycles, late_periods)
        state.last_backlog = max(0, int(backlog or 0))
        state.last_error = str(last_error or "")
        state.last_backpressure_reason = str(backpressure_reason or "")
        state.last_fault = fault
        state.skipped_count += 1

    def run_due(self, runner: Callable[[ClockedJob], bool | ClockedJobOutcome]) -> list[str]:
        ran: list[str] = []
        reports: list[ClockedRunReport] = []
        due_jobs = self.due_jobs()
        lane_blockers = self._lane_blockers_for_due_jobs(due_jobs)
        for job in due_jobs:
            state = self._jobs[job.key]
            due_cycle = 0 if state.last_run_cycle < 0 else state.last_run_cycle + job.cadence_cycles
            due_ts = self._job_due_ts(state)
            lateness_s = self._job_lateness_s(state)
            lateness = self._job_late_periods(state)
            skip_decision = self._skip_decision(
                state,
                lateness=lateness,
                blocker=lane_blockers.get(job.lane),
            )
            if skip_decision.skip:
                started_wall = self._event_wall_time()
                self.mark_skipped(
                    job,
                    start_ts=started_wall,
                    finish_ts=started_wall,
                    backlog=skip_decision.backlog,
                    backpressure_reason=skip_decision.reason,
                    consume_due=skip_decision.consume_due,
                )
                reports.append(
                    ClockedRunReport(
                        job_key=job.key,
                        lane=job.lane,
                        due_cycle=due_cycle,
                        run_cycle=self._cycle,
                        lateness_cycles=lateness,
                        duration_ms=0.0,
                        budget_ms=job.budget_ms,
                        over_budget=False,
                        due_ts=due_ts,
                        run_ts=started_wall,
                        lateness_s=round(lateness_s, 3),
                        skipped=True,
                    )
                )
                continue
            run_attempts = self._run_attempt_count(job, lateness=lateness)
            for attempt_index in range(run_attempts):
                started_perf = time.perf_counter()
                started_wall = self._event_wall_time()
                try:
                    outcome = runner(job)
                except (TargetGuardError, ProductRuntimeWorkerReplacementRequired):
                    raise
                except Exception as exc:
                    outcome = ClockedJobOutcome(
                        ran=False,
                        last_error=f"{type(exc).__name__}: {exc}",
                        backpressure_reason="typed_clock_fault",
                        fault=ClockFaultResult(
                            owner=job.owner,
                            operation=job.name,
                            generation=self._cycle,
                            severity="error",
                            retryable=True,
                            degraded_behavior=str(job.recovery_permission or "defer"),
                            error_type=type(exc).__name__,
                            detail=str(exc),
                        ),
                    )
                if isinstance(outcome, ClockedJobOutcome):
                    ran_job = bool(outcome.ran)
                    backlog = int(outcome.backlog or 0)
                    last_error = str(outcome.last_error or "")
                    backpressure_reason = str(outcome.backpressure_reason or "")
                    fault = outcome.fault
                else:
                    ran_job = bool(outcome)
                    backlog = 0
                    last_error = ""
                    backpressure_reason = ""
                    fault = None
                backlog, backpressure_reason = self._apply_run_policy_metadata(
                    job,
                    lateness=lateness,
                    backlog=backlog,
                    backpressure_reason=backpressure_reason,
                    catch_up_remaining=max(0, run_attempts - attempt_index - 1),
                )
                finished_wall = self._event_wall_time()
                if ran_job:
                    duration_ms = (time.perf_counter() - started_perf) * 1000.0
                    self.mark_run(
                        job,
                        duration_ms=duration_ms,
                        start_ts=started_wall,
                        finish_ts=finished_wall,
                        backlog=backlog,
                        last_error=last_error,
                        backpressure_reason=backpressure_reason,
                        fault=fault,
                    )
                    ran.append(job.key)
                    reports.append(
                        ClockedRunReport(
                            job_key=job.key,
                            lane=job.lane,
                            due_cycle=due_cycle,
                            run_cycle=self._cycle,
                            lateness_cycles=lateness,
                            duration_ms=duration_ms,
                            budget_ms=job.budget_ms,
                            over_budget=duration_ms > job.budget_ms,
                            due_ts=due_ts,
                            run_ts=finished_wall,
                            lateness_s=round(lateness_s, 3),
                            fault=fault,
                        )
                    )
                    continue
                self.mark_skipped(
                    job,
                    start_ts=started_wall,
                    finish_ts=finished_wall,
                    backlog=self._declined_run_backlog(job, lateness=lateness, backlog=backlog),
                    last_error=last_error,
                    backpressure_reason=self._declined_run_backpressure_reason(
                        job,
                        backpressure_reason=backpressure_reason,
                    ),
                    consume_due=self._declined_run_consumes_due(job),
                    fault=fault,
                )
                reports.append(
                    ClockedRunReport(
                        job_key=job.key,
                        lane=job.lane,
                        due_cycle=due_cycle,
                        run_cycle=self._cycle,
                        lateness_cycles=lateness,
                        duration_ms=0.0,
                        budget_ms=job.budget_ms,
                        over_budget=False,
                        due_ts=due_ts,
                        run_ts=finished_wall,
                        lateness_s=round(lateness_s, 3),
                        skipped=True,
                        fault=fault,
                    )
                )
                break
        self.last_run_reports = tuple(reports)
        return ran

    def _lane_blockers_for_due_jobs(self, due_jobs: list[ClockedJob]) -> dict[str, ClockedLaneSpec]:
        due_lanes = {str(job.lane or "") for job in due_jobs}
        blockers: dict[str, ClockedLaneSpec] = {}
        for lane_name in due_lanes:
            spec = self._lane_specs.get(lane_name)
            if spec is None:
                continue
            for blocked_lane in spec.blocks_lanes_when_due:
                blockers.setdefault(str(blocked_lane or ""), spec)
        return blockers

    def _skip_decision(
        self,
        state: ClockedJobState,
        *,
        lateness: int,
        blocker: ClockedLaneSpec | None,
    ) -> ClockedSkipDecision:
        policy = _normalize_skip_policy(state.job.skip_policy)
        if blocker is not None and policy in {
            ClockedSkipPolicy.SKIP_IF_BLOCKING_LANE_DUE,
            ClockedSkipPolicy.BOUNDED_QUEUE_SUPERSEDE_LATEST,
            ClockedSkipPolicy.BLOCK_UNTIL_ACTION_DONE,
        }:
            return ClockedSkipDecision(
                skip=True,
                reason=str(blocker.block_reason or f"{blocker.name}_due"),
                backlog=max(1, int(state.pending_event_count or lateness or 1)),
            )
        if policy == ClockedSkipPolicy.DROP_STALE and lateness > 0:
            return ClockedSkipDecision(
                skip=True,
                reason="drop_stale",
                backlog=lateness,
                consume_due=True,
            )
        if policy == ClockedSkipPolicy.SKIP_IF_LATE and lateness > 1:
            return ClockedSkipDecision(
                skip=True,
                reason="late",
                backlog=lateness,
                consume_due=True,
            )
        return ClockedSkipDecision()

    def _run_attempt_count(self, job: ClockedJob, *, lateness: int) -> int:
        policy = _normalize_skip_policy(job.skip_policy)
        if policy == ClockedSkipPolicy.CATCH_UP:
            return max(1, min(int(lateness or 0) + 1, max(1, int(job.max_catch_up_runs or 1))))
        return 1

    def _apply_run_policy_metadata(
        self,
        job: ClockedJob,
        *,
        lateness: int,
        backlog: int,
        backpressure_reason: str,
        catch_up_remaining: int = 0,
    ) -> tuple[int, str]:
        policy = _normalize_skip_policy(job.skip_policy)
        if policy == ClockedSkipPolicy.CATCH_UP:
            backlog_value = max(int(backlog or 0), int(lateness or 0), int(catch_up_remaining or 0))
            return backlog_value, backpressure_reason or ("catch_up" if backlog_value > 0 else "")
        if policy == ClockedSkipPolicy.RUN_ONCE_IF_LATE and lateness > 0:
            return max(int(backlog or 0), int(lateness or 0)), backpressure_reason or "run_once_if_late"
        if policy in {ClockedSkipPolicy.SUPERSEDE_LATEST, ClockedSkipPolicy.BOUNDED_QUEUE_SUPERSEDE_LATEST}:
            if lateness > 0 and backlog <= 0:
                return lateness, backpressure_reason or "superseded_late_periods"
        return backlog, backpressure_reason

    def _declined_run_consumes_due(self, job: ClockedJob) -> bool:
        policy = _normalize_skip_policy(job.skip_policy)
        return policy == ClockedSkipPolicy.NEVER_SKIP_CACHE_LAST_GOOD

    def _declined_run_backlog(self, job: ClockedJob, *, lateness: int, backlog: int) -> int:
        policy = _normalize_skip_policy(job.skip_policy)
        if policy == ClockedSkipPolicy.NEVER_SKIP_CACHE_LAST_GOOD:
            return max(int(backlog or 0), int(lateness or 0))
        return max(0, int(backlog or 0))

    def _declined_run_backpressure_reason(self, job: ClockedJob, *, backpressure_reason: str) -> str:
        policy = _normalize_skip_policy(job.skip_policy)
        if policy == ClockedSkipPolicy.NEVER_SKIP_CACHE_LAST_GOOD:
            reason = str(backpressure_reason or "").strip()
            return f"cache_last_good:{reason}" if reason else "cache_last_good"
        return str(backpressure_reason or "")

    def time_status(self, *, now: float | None = None) -> dict:
        """Return only canonical clock truth, independent of due-job work."""

        now_ts = float(time.time() if now is None else now)
        started_at = float(self._started_ts or 0.0)
        uptime_s = max(0.0, now_ts - started_at) if started_at > 0.0 else 0.0
        return {
            "clock_ms": self.clock_ms,
            "max_clock_ms": MAX_CLOCK_MS,
            "sequence": self._cycle,
            "cycle": self._cycle,
            "clock_authority": "wall_clock",
            "cycle_authority": "debug_sequence_only",
            "sequence_role": "ordering_debug_only",
            "started_at": started_at,
            "uptime_s": round(uptime_s, 3),
            "uptime_source": "runtime_clock",
        }

    def status(self, *, now: float | None = None) -> dict:
        now_ts = float(time.time() if now is None else now)
        time_status = self.time_status(now=now_ts)
        job_rows = {
            key: {
                "owner": state.job.owner,
                "name": state.job.name,
                "lane": state.job.lane,
                "cadence_cycles": state.job.cadence_cycles,
                "cadence_s": round(self._job_cadence_s(state.job), 3),
                "cadence_ms": int(round(self._job_cadence_s(state.job) * 1000.0)),
                "budget_ms": state.job.budget_ms,
                "priority": state.job.priority,
                "skip_policy": _skip_policy_value(state.job.skip_policy),
                "output_surface": state.job.output_surface,
                "input_generation_surface": state.job.input_generation_surface,
                "deadline_ms": state.job.deadline_ms,
                "recovery_permission": state.job.recovery_permission,
                "fault_policy": state.job.fault_policy,
                "interrupt_policy": state.job.interrupt_policy,
                "interruptible": state.job.interruptible,
                "max_catch_up_runs": state.job.max_catch_up_runs,
                "last_due_cycle": state.last_due_cycle,
                "last_due_ts": float(state.last_due_ts),
                "last_start_cycle": state.last_start_cycle,
                "last_run_cycle": state.last_run_cycle,
                "last_run_duration_ms": round(state.last_run_duration_ms, 3),
                "last_start_ts": float(state.last_start_ts),
                "last_finish_ts": float(state.last_finish_ts),
                "last_lateness_cycles": state.last_lateness_cycles,
                "max_lateness_cycles": state.max_lateness_cycles,
                "last_lateness_s": round(float(state.last_lateness_s), 3),
                "max_lateness_s": round(float(state.max_lateness_s), 3),
                "last_backlog": state.last_backlog,
                "last_error": state.last_error,
                "last_backpressure_reason": state.last_backpressure_reason,
                "run_count": state.run_count,
                "skipped_count": state.skipped_count,
                "over_budget_count": state.over_budget_count,
                "pending_event_count": state.pending_event_count,
                "last_fault": state.last_fault.to_dict() if state.last_fault is not None else None,
                "wall_age_s": (
                    round(max(0.0, now_ts - float(state.last_finish_ts)), 3)
                    if float(state.last_finish_ts) > 0.0
                    else None
                ),
                "fresh_by_clock": (
                    bool(now_ts - float(state.last_finish_ts) <= (self._job_cadence_s(state.job) * 3.0))
                    if float(state.last_finish_ts) > 0.0 and state.job.skip_policy != ClockedSkipPolicy.EVENT_DRIVEN
                    else None
                ),
            }
            for key, state in sorted(self._jobs.items())
        }
        return {
            **time_status,
            "lanes": self._lane_status(job_rows, now=now_ts),
            "last_run_reports": tuple(
                {
                    "job_key": report.job_key,
                    "lane": report.lane,
                    "due_cycle": report.due_cycle,
                    "run_cycle": report.run_cycle,
                    "lateness_cycles": report.lateness_cycles,
                    "due_ts": float(report.due_ts),
                    "run_ts": float(report.run_ts),
                    "lateness_s": round(float(report.lateness_s), 3),
                    "duration_ms": round(report.duration_ms, 3),
                    "budget_ms": report.budget_ms,
                    "over_budget": report.over_budget,
                    "skipped": report.skipped,
                    "fault": report.fault.to_dict() if report.fault is not None else None,
                }
                for report in self.last_run_reports
            ),
            "jobs": job_rows,
        }

    def _lane_status(self, job_rows: dict[str, dict], *, now: float | None = None) -> dict[str, dict]:
        now_ts = float(time.time() if now is None else now)
        lanes = {
            name: {
                "owner": spec.owner,
                "purpose": spec.purpose,
                "default_cadence_cycles": spec.default_cadence_cycles,
                "default_cadence_s": round(_cadence_cycles_to_seconds(spec.default_cadence_cycles, clock_ms=self.clock_ms), 3),
                "budget_ms": spec.budget_ms,
                "priority": spec.priority,
                "skip_policy": _skip_policy_value(spec.skip_policy),
                "output_surface": spec.output_surface,
                "recovery_permission": spec.recovery_permission,
                "interrupt_policy": spec.interrupt_policy,
                "blocks_lanes_when_due": list(spec.blocks_lanes_when_due),
                "block_reason": spec.block_reason,
                "job_count": 0,
                "due_count": 0,
                "run_count": 0,
                "skipped_count": 0,
                "over_budget_count": 0,
                "pending_event_count": 0,
                "last_backlog": 0,
                "last_error": "",
                "last_backpressure_reason": "",
                "max_lateness_cycles": 0,
                "max_lateness_s": 0.0,
                "last_start_ts": 0.0,
                "last_finish_ts": 0.0,
                "wall_age_s": None,
                "fresh_by_clock": None,
                "fresh_job_count": 0,
                "stale_job_count": 0,
            }
            for name, spec in sorted(self._lane_specs.items())
        }
        for row in job_rows.values():
            lane_name = str(row.get("lane") or "normal")
            lane = lanes.setdefault(
                lane_name,
                {
                    "owner": "",
                    "purpose": "unregistered lane",
                    "default_cadence_cycles": 0,
                    "default_cadence_s": 0.0,
                    "budget_ms": 0.0,
                    "priority": int(row.get("priority", 100) or 100),
                    "skip_policy": str(row.get("skip_policy") or ""),
                    "output_surface": str(row.get("output_surface") or ""),
                    "recovery_permission": str(row.get("recovery_permission") or ""),
                    "interrupt_policy": "cooperative",
                    "blocks_lanes_when_due": [],
                    "block_reason": "",
                    "job_count": 0,
                    "due_count": 0,
                    "run_count": 0,
                    "skipped_count": 0,
                    "over_budget_count": 0,
                    "pending_event_count": 0,
                    "last_backlog": 0,
                    "last_error": "",
                    "last_backpressure_reason": "",
                    "max_lateness_cycles": 0,
                    "max_lateness_s": 0.0,
                    "last_start_ts": 0.0,
                    "last_finish_ts": 0.0,
                    "wall_age_s": None,
                    "fresh_by_clock": None,
                    "fresh_job_count": 0,
                    "stale_job_count": 0,
                },
            )
            lane["job_count"] += 1
            pending_event_count = int(row.get("pending_event_count", 0) or 0)
            if str(row.get("skip_policy") or "") == ClockedSkipPolicy.EVENT_DRIVEN.value:
                if pending_event_count > 0:
                    lane["due_count"] += 1
            else:
                last_finish_ts = float(row.get("last_finish_ts", 0.0) or 0.0)
                cadence_s = float(row.get("cadence_s", 0.0) or 0.0)
                if last_finish_ts <= 0.0 or (cadence_s > 0.0 and now_ts >= last_finish_ts + cadence_s):
                    lane["due_count"] += 1
            lane["run_count"] += int(row.get("run_count", 0) or 0)
            lane["skipped_count"] += int(row.get("skipped_count", 0) or 0)
            lane["over_budget_count"] += int(row.get("over_budget_count", 0) or 0)
            lane["pending_event_count"] += pending_event_count
            lane["last_backlog"] = max(int(lane.get("last_backlog", 0) or 0), int(row.get("last_backlog", 0) or 0))
            lane["max_lateness_cycles"] = max(
                int(lane.get("max_lateness_cycles", 0) or 0),
                int(row.get("max_lateness_cycles", 0) or 0),
            )
            lane["max_lateness_s"] = round(
                max(
                    float(lane.get("max_lateness_s", 0.0) or 0.0),
                    float(row.get("max_lateness_s", 0.0) or 0.0),
                ),
                3,
            )
            if row.get("last_error"):
                lane["last_error"] = str(row.get("last_error") or "")
            if row.get("last_backpressure_reason"):
                lane["last_backpressure_reason"] = str(row.get("last_backpressure_reason") or "")
            lane["last_start_ts"] = max(float(lane.get("last_start_ts", 0.0) or 0.0), float(row.get("last_start_ts", 0.0) or 0.0))
            lane["last_finish_ts"] = max(float(lane.get("last_finish_ts", 0.0) or 0.0), float(row.get("last_finish_ts", 0.0) or 0.0))
            fresh = row.get("fresh_by_clock")
            if fresh is True:
                lane["fresh_job_count"] += 1
            elif fresh is False:
                lane["stale_job_count"] += 1
        for lane in lanes.values():
            last_finish = float(lane.get("last_finish_ts", 0.0) or 0.0)
            if last_finish > 0.0:
                lane["wall_age_s"] = round(max(0.0, now_ts - last_finish), 3)
            if int(lane.get("job_count", 0) or 0) <= 0:
                lane["fresh_by_clock"] = None
            elif int(lane.get("stale_job_count", 0) or 0) > 0:
                lane["fresh_by_clock"] = False
            elif int(lane.get("fresh_job_count", 0) or 0) > 0:
                lane["fresh_by_clock"] = True
        return lanes


def _normalize_lane_spec(spec: ClockedLaneSpec) -> ClockedLaneSpec:
    return replace(
        spec,
        skip_policy=_normalize_skip_policy(spec.skip_policy),
        blocks_lanes_when_due=tuple(str(lane or "") for lane in spec.blocks_lanes_when_due if str(lane or "")),
        block_reason=str(spec.block_reason or ""),
    )


def _normalize_skip_policy(policy: ClockedSkipPolicy | str) -> ClockedSkipPolicy:
    if isinstance(policy, ClockedSkipPolicy):
        return policy
    text = str(policy or "").strip().lower()
    if not text:
        return ClockedSkipPolicy.INHERIT_LANE
    try:
        return ClockedSkipPolicy(text)
    except ValueError as exc:
        raise ValueError(f"unknown clock skip policy: {policy!r}") from exc


def _skip_policy_value(policy: ClockedSkipPolicy | str) -> str:
    return _normalize_skip_policy(policy).value


def _cadence_cycles_to_seconds(cadence_cycles: int | float, *, clock_ms: int = MAX_CLOCK_MS) -> float:
    cycles = max(1.0, float(cadence_cycles or 1.0))
    clock = max(1.0, float(clock_ms or MAX_CLOCK_MS))
    return cycles * clock / 1000.0


def build_runtime_clock_authority_summary(status: Mapping[str, Any] | None) -> dict[str, Any]:
    """Summarize the permanent Core lanes that are actually clock-owned."""

    data = status if isinstance(status, Mapping) else {}
    jobs = data.get("jobs") if isinstance(data.get("jobs"), Mapping) else {}
    lanes = data.get("lanes") if isinstance(data.get("lanes"), Mapping) else {}
    required_jobs = list(CORE_CLOCK_AUTHORITY_REQUIRED_JOBS)
    required_lanes = list(CORE_CLOCK_AUTHORITY_REQUIRED_LANES)
    present_jobs = [key for key in required_jobs if isinstance(jobs.get(key), Mapping)]
    missing_jobs = [key for key in required_jobs if key not in present_jobs]
    present_lanes = [key for key in required_lanes if isinstance(lanes.get(key), Mapping)]
    missing_lanes = [key for key in required_lanes if key not in present_lanes]
    job_surfaces = {
        key: {
            "lane": str((jobs.get(key) or {}).get("lane") or ""),
            "skip_policy": str((jobs.get(key) or {}).get("skip_policy") or ""),
            "output_surface": str((jobs.get(key) or {}).get("output_surface") or ""),
            "recovery_permission": str((jobs.get(key) or {}).get("recovery_permission") or ""),
            "run_count": int((jobs.get(key) or {}).get("run_count", 0) or 0),
            "pending_event_count": int((jobs.get(key) or {}).get("pending_event_count", 0) or 0),
            "last_backpressure_reason": str((jobs.get(key) or {}).get("last_backpressure_reason") or ""),
            "cadence_s": (jobs.get(key) or {}).get("cadence_s"),
            "last_lateness_s": (jobs.get(key) or {}).get("last_lateness_s"),
            "wall_age_s": (jobs.get(key) or {}).get("wall_age_s"),
            "fresh_by_clock": (jobs.get(key) or {}).get("fresh_by_clock"),
        }
        for key in present_jobs
    }
    stale_jobs = [
        key
        for key, surface in job_surfaces.items()
        if surface.get("fresh_by_clock") is False
    ]
    lane_surfaces = {
        key: {
            "owner": str((lanes.get(key) or {}).get("owner") or ""),
            "output_surface": str((lanes.get(key) or {}).get("output_surface") or ""),
            "wall_age_s": (lanes.get(key) or {}).get("wall_age_s"),
            "fresh_by_clock": (lanes.get(key) or {}).get("fresh_by_clock"),
            "due_count": int((lanes.get(key) or {}).get("due_count", 0) or 0),
            "max_lateness_s": (lanes.get(key) or {}).get("max_lateness_s"),
            "last_backpressure_reason": str((lanes.get(key) or {}).get("last_backpressure_reason") or ""),
        }
        for key in present_lanes
    }
    return {
        "schema_version": "runtime_clock_authority.v2",
        "scope": "permanent_core_runtime_lanes",
        "complete": not missing_jobs and not missing_lanes,
        "clock_authority": str(data.get("clock_authority") or "wall_clock"),
        "cycle_authority": str(data.get("cycle_authority") or "debug_sequence_only"),
        "sequence_role": str(data.get("sequence_role") or "ordering_debug_only"),
        "required_jobs": required_jobs,
        "present_jobs": present_jobs,
        "missing_jobs": missing_jobs,
        "required_lanes": required_lanes,
        "present_lanes": present_lanes,
        "missing_lanes": missing_lanes,
        "job_surfaces": job_surfaces,
        "lane_surfaces": lane_surfaces,
        "stale_jobs": stale_jobs,
    }


class ClockedRuntimeService:
    """Own wall-clock advancement for scheduled runtime lanes.

    The scheduler remains deterministic and testable; this service is the
    runtime boundary that records elapsed wall time and dispatches due jobs.
    Engine code calls the service, not scheduler internals.
    """

    def __init__(self, scheduler: ClockedScheduler):
        if not isinstance(scheduler, ClockedScheduler):
            raise TypeError("scheduler must be ClockedScheduler")
        self.scheduler = scheduler
        self.last_wall_ts = 0.0

    def advance_wall(self, *, now: float | None = None) -> int:
        now_ts = float(time.time() if now is None else now)
        sequence = self.scheduler.observe_wall(now=now_ts)
        self.last_wall_ts = now_ts
        return sequence

    def run_once(
        self,
        runner: Callable[[ClockedJob], bool | ClockedJobOutcome],
        *,
        now: float | None = None,
    ) -> list[str]:
        self.advance_wall(now=now)
        return self.scheduler.run_due(runner)

    def status(self) -> dict:
        status = dict(self.scheduler.status(now=self.last_wall_ts or time.time()))
        status["service_owner"] = "clocked_runtime_service"
        status["last_wall_ts"] = float(self.last_wall_ts)
        return status


def core_runtime_clock_lanes() -> Iterable[ClockedLaneSpec]:
    return (
        ClockedLaneSpec(
            name="normal",
            owner="runtime",
            purpose="explicit internal Core runtime jobs",
            default_cadence_cycles=1,
            budget_ms=100.0,
            priority=100,
            skip_policy="skip_if_late",
            output_surface="runtime_internal",
            recovery_permission="skip",
        ),
        ClockedLaneSpec(
            name="fast_modem_register",
            owner="modem_live",
            purpose="protected live modem register refresh",
            default_cadence_cycles=20,
            budget_ms=2500.0,
            priority=0,
            skip_policy="never_skip_cache_last_good",
            output_surface="live_modem_register",
            recovery_permission="cache_last_good",
            interrupt_policy="preempt_low_priority",
        ),
        ClockedLaneSpec(
            name="medium_status_decision",
            owner="runtime",
            purpose="current status and decision-ready summaries",
            default_cadence_cycles=20,
            budget_ms=50.0,
            priority=10,
            skip_policy="supersede_latest",
            output_surface="runtime_status",
            recovery_permission="skip",
        ),
        ClockedLaneSpec(
            name="slow_core_diagnostics",
            owner="core",
            purpose="bounded downstream LAN diagnostic servicing",
            default_cadence_cycles=10,
            budget_ms=11000.0,
            priority=20,
            skip_policy="supersede_latest",
            output_surface="downstream_health",
            recovery_permission="skip",
        ),
    )


def core_runtime_clock_jobs() -> Iterable[ClockedJob]:
    return (
        ClockedJob(
            owner="core",
            name="command_lifecycle",
            cadence_cycles=1,
            budget_ms=25.0,
            lane="normal",
            priority=0,
            output_surface="command_lifecycle",
            input_generation_surface="command_authority_state",
            recovery_permission="retry_and_report",
        ),
        ClockedJob(
            owner="modem_live",
            name="refresh_registers",
            cadence_cycles=20,
            budget_ms=2500.0,
            lane="fast_modem_register",
            priority=0,
            output_surface="live_modem_register",
            input_generation_surface="modem_live_generation",
            recovery_permission="cache_last_good",
        ),
        ClockedJob(
            owner="runtime",
            name="status_decision_snapshot",
            cadence_cycles=20,
            budget_ms=50.0,
            lane="medium_status_decision",
            priority=10,
            output_surface="runtime_status",
            input_generation_surface="controller_register_generation",
            recovery_permission="skip",
        ),
        ClockedJob(
            owner="core",
            name="downstream_diagnostics",
            cadence_cycles=10,
            budget_ms=11000.0,
            lane="slow_core_diagnostics",
            priority=20,
            output_surface="downstream_health",
            input_generation_surface="core_diagnostics_configuration",
            recovery_permission="skip",
        ),
    )
