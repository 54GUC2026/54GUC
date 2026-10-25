"""Runtime service scheduler.

This module is the first shared scheduling boundary for live 54GUC work.
It does not own modem truth; Modem Live Module owns input.  It owns the
question "which consumer work is allowed to run now, in which lane, and how
long did it take?"
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from arch4.runtime_boundary_contracts import FaultEvent


@dataclass(frozen=True, slots=True)
class RuntimeLanePolicy:
    name: str
    budget_ms: float
    priority: int
    can_skip: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeServiceTask:
    label: str
    run: Callable[[], None]
    lane: str = "normal"
    budget_ms: float | None = None
    can_skip: bool | None = None


@dataclass(frozen=True, slots=True)
class RuntimeServiceFailure:
    context: str
    label: str
    lane: str
    error: str
    fault_event: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeServiceTaskReport:
    label: str
    lane: str
    elapsed_ms: float
    budget_ms: float
    over_budget: bool = False
    skipped: bool = False
    error: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeServiceReport:
    context: str
    elapsed_ms: float
    budget_ms: float
    over_budget: bool
    tasks: tuple[RuntimeServiceTaskReport, ...] = field(default_factory=tuple)
    failures: tuple[RuntimeServiceFailure, ...] = field(default_factory=tuple)

    @property
    def skipped_count(self) -> int:
        return sum(1 for task in self.tasks if task.skipped)


DEFAULT_LANE_POLICIES: Mapping[str, RuntimeLanePolicy] = {
    "critical": RuntimeLanePolicy("critical", budget_ms=20.0, priority=0, can_skip=False),
    "fast": RuntimeLanePolicy("fast", budget_ms=40.0, priority=1, can_skip=False),
    "normal": RuntimeLanePolicy("normal", budget_ms=120.0, priority=2, can_skip=False),
    "slow": RuntimeLanePolicy("slow", budget_ms=250.0, priority=3, can_skip=True),
    "background": RuntimeLanePolicy("background", budget_ms=50.0, priority=4, can_skip=True),
}


class RuntimeServiceScheduler:
    """Execute registered work under lane timing and reporting.

    The scheduler intentionally measures synchronous tasks instead of hiding
    them in local try/except blocks.  Future phases can move selected lanes to
    worker queues without changing each consumer module.
    """

    def __init__(
        self,
        *,
        lane_policies: Mapping[str, RuntimeLanePolicy] | None = None,
        context_budget_ms: float = 500.0,
    ) -> None:
        self._lane_policies = dict(lane_policies or DEFAULT_LANE_POLICIES)
        self._context_budget_ms = float(context_budget_ms)
        self.last_report: RuntimeServiceReport | None = None

    def policy_for(self, lane: str) -> RuntimeLanePolicy:
        key = str(lane or "normal").strip().lower()
        return self._lane_policies.get(key) or self._lane_policies["normal"]

    def run(
        self,
        tasks: Iterable[RuntimeServiceTask],
        *,
        context: str,
        context_budget_ms: float | None = None,
        after_task: Callable[[], None] | None = None,
    ) -> RuntimeServiceReport:
        budget_ms = self._context_budget_ms if context_budget_ms is None else float(context_budget_ms)
        started = time.perf_counter()
        task_reports: list[RuntimeServiceTaskReport] = []
        failures: list[RuntimeServiceFailure] = []

        for task in tasks:
            policy = self.policy_for(task.lane)
            task_budget = float(task.budget_ms if task.budget_ms is not None else policy.budget_ms)
            can_skip = policy.can_skip if task.can_skip is None else bool(task.can_skip)
            elapsed_so_far_ms = (time.perf_counter() - started) * 1000.0
            if can_skip and elapsed_so_far_ms >= budget_ms:
                task_reports.append(
                    RuntimeServiceTaskReport(
                        label=str(task.label or ""),
                        lane=policy.name,
                        elapsed_ms=0.0,
                        budget_ms=task_budget,
                        skipped=True,
                    )
                )
                continue

            task_started = time.perf_counter()
            error = ""
            try:
                task.run()
            except Exception as exc:
                error = str(exc or "")
                fault_event = FaultEvent(
                    category="runtime_event",
                    source=f"{str(context or '')}.{str(task.label or '')}",
                    severity="warning",
                    detail=error,
                    recoverable=True,
                    metadata={
                        "boundary": "runtime_service_scheduler",
                        "lane": policy.name,
                        "error_type": type(exc).__name__,
                    },
                ).as_dict()
                failures.append(
                    RuntimeServiceFailure(
                        context=str(context or ""),
                        label=str(task.label or ""),
                        lane=policy.name,
                        error=error,
                        fault_event=fault_event,
                    )
                )
            elapsed_ms = (time.perf_counter() - task_started) * 1000.0
            task_reports.append(
                RuntimeServiceTaskReport(
                    label=str(task.label or ""),
                    lane=policy.name,
                    elapsed_ms=elapsed_ms,
                    budget_ms=task_budget,
                    over_budget=elapsed_ms > task_budget,
                    error=error,
                )
            )
            if after_task is not None:
                try:
                    after_task()
                except Exception as exc:
                    error = str(exc or "")
                    fault_event = FaultEvent(
                        category="runtime_event",
                        source=f"{str(context or '')}.after_task",
                        severity="warning",
                        detail=error,
                        recoverable=True,
                        metadata={
                            "boundary": "runtime_service_scheduler",
                            "lane": "scheduler",
                            "error_type": type(exc).__name__,
                            "after_task_for": str(task.label or ""),
                        },
                    ).as_dict()
                    failures.append(
                        RuntimeServiceFailure(
                            context=str(context or ""),
                            label="after_task",
                            lane="scheduler",
                            error=error,
                            fault_event=fault_event,
                        )
                    )

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        report = RuntimeServiceReport(
            context=str(context or ""),
            elapsed_ms=elapsed_ms,
            budget_ms=budget_ms,
            over_budget=elapsed_ms > budget_ms,
            tasks=tuple(task_reports),
            failures=tuple(failures),
        )
        self.last_report = report
        return report
