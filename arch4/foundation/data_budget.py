"""Foundation owner for user data-budget display."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from typing import Any

from arch4.core_contracts.runtime_snapshot import (
    FOUNDATION_DATA_BUDGET_NGUI_SOURCE_IDS,
)
from arch4.customer_contracts.settings import TrafficPlan


FOUNDATION_DATA_BUDGET_SCHEMA = "FoundationDataBudgetProjection.v1"
FOUNDATION_DATA_BUDGET_OWNER = "54guc.foundation.operator_data_budget"
FOUNDATION_DATA_BUDGET_CLIENT_ID = "foundation.operator_data_budget"
BYTES_PER_GIB = 1024 ** 3


def _nonnegative_int(value: Any, *, field: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_integer_required") from exc
    if normalized < 0:
        raise ValueError(f"{field}_cannot_be_negative")
    return normalized


def _optional_nonnegative_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, field=field)


@dataclass(frozen=True, slots=True)
class TrafficCounterObservation:
    """Read-only Core traffic counters used by the budget calculation."""

    day_used_bytes: int | None
    current_cycle_download_bytes: int | None
    current_cycle_upload_bytes: int | None
    total_download_bytes: int | None
    total_upload_bytes: int | None
    router_quota_limit_bytes: int | None
    router_quota_left_bytes: int | None
    observed_at: float
    sample_sequence: int
    period_accounting_qualified: bool = True
    period_accounting_status: str = ""

    def __post_init__(self) -> None:
        for field in (
            "day_used_bytes",
            "current_cycle_download_bytes",
            "current_cycle_upload_bytes",
        ):
            object.__setattr__(
                self,
                field,
                _optional_nonnegative_int(getattr(self, field), field=field),
            )
        for field in ("total_download_bytes", "total_upload_bytes"):
            object.__setattr__(
                self,
                field,
                _optional_nonnegative_int(getattr(self, field), field=field),
            )
        for field in ("router_quota_limit_bytes", "router_quota_left_bytes"):
            object.__setattr__(
                self,
                field,
                _optional_nonnegative_int(getattr(self, field), field=field),
            )
        try:
            observed_at = float(self.observed_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("traffic_observation_timestamp_required") from exc
        if not isfinite(observed_at) or observed_at <= 0.0:
            raise ValueError("traffic_observation_positive_timestamp_required")
        sequence = _nonnegative_int(
            self.sample_sequence,
            field="traffic_observation_sample_sequence",
        )
        if sequence <= 0:
            raise ValueError("traffic_observation_positive_sample_sequence_required")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "sample_sequence", sequence)
        object.__setattr__(
            self,
            "period_accounting_status",
            str(self.period_accounting_status or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class FoundationDataBudgetProjection:
    """One display-ready budget fact set; router quota remains diagnostic only."""

    scope_id: str
    plan_configured: bool
    plan_enabled: bool
    plan_limit_bytes: int
    cycle_start_day: int
    cycle_length_days: int
    day_used_bytes: int | None
    current_cycle_used_bytes: int | None
    total_used_bytes: int | None
    left_bytes: int | None
    daily_allowance_bytes: int
    daily_remaining_bytes: int | None
    router_quota_limit_bytes: int | None
    router_quota_left_bytes: int | None
    observed_at: float
    sample_sequence: int
    period_accounting_qualified: bool = True
    period_accounting_status: str = ""
    schema: str = FOUNDATION_DATA_BUDGET_SCHEMA
    owner: str = FOUNDATION_DATA_BUDGET_OWNER

    def __post_init__(self) -> None:
        if self.schema != FOUNDATION_DATA_BUDGET_SCHEMA:
            raise ValueError("foundation_data_budget_schema_invalid")
        if self.owner != FOUNDATION_DATA_BUDGET_OWNER:
            raise ValueError("foundation_data_budget_owner_invalid")
        if not str(self.scope_id or "").strip():
            raise ValueError("foundation_data_budget_exact_scope_required")
        if not 1 <= int(self.cycle_start_day) <= 31:
            raise ValueError("foundation_data_budget_cycle_day_invalid")
        if int(self.cycle_length_days) <= 0:
            raise ValueError("foundation_data_budget_cycle_length_invalid")
        for field in (
            "plan_limit_bytes",
            "daily_allowance_bytes",
        ):
            _nonnegative_int(getattr(self, field), field=field)
        for field in (
            "day_used_bytes",
            "current_cycle_used_bytes",
            "total_used_bytes",
            "left_bytes",
            "daily_remaining_bytes",
        ):
            _optional_nonnegative_int(getattr(self, field), field=field)
        if self.current_cycle_used_bytes is None:
            if self.left_bytes is not None:
                raise ValueError("foundation_data_budget_left_requires_period_usage")
        elif self.left_bytes != max(
            0,
            self.plan_limit_bytes - self.current_cycle_used_bytes,
        ):
            raise ValueError("foundation_data_budget_left_inconsistent")
        if self.day_used_bytes is None:
            if self.daily_remaining_bytes is not None:
                raise ValueError(
                    "foundation_data_budget_daily_remaining_requires_day_usage"
                )
        elif self.daily_remaining_bytes != max(
            0,
            self.daily_allowance_bytes - self.day_used_bytes,
        ):
            raise ValueError("foundation_data_budget_daily_remaining_inconsistent")
        _optional_nonnegative_int(
            self.router_quota_limit_bytes,
            field="router_quota_limit_bytes",
        )
        _optional_nonnegative_int(
            self.router_quota_left_bytes,
            field="router_quota_left_bytes",
        )

    def ngui_values(self) -> dict[str, int | None]:
        return {
            "traffic.budget_cycle_day": self.cycle_start_day,
            "traffic.budget_day_used_bytes": self.day_used_bytes,
            "traffic.budget_left_bytes": self.left_bytes,
            "traffic.budget_limit_bytes": self.plan_limit_bytes,
            "traffic.budget_month_used_bytes": self.current_cycle_used_bytes,
            "traffic.budget_total_used_bytes": self.total_used_bytes,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "scope_id": self.scope_id,
            "plan_configured": self.plan_configured,
            "plan_enabled": self.plan_enabled,
            "plan_limit_bytes": self.plan_limit_bytes,
            "cycle_start_day": self.cycle_start_day,
            "cycle_length_days": self.cycle_length_days,
            "day_used_bytes": self.day_used_bytes,
            "current_cycle_used_bytes": self.current_cycle_used_bytes,
            "total_used_bytes": self.total_used_bytes,
            "left_bytes": self.left_bytes,
            "daily_allowance_bytes": self.daily_allowance_bytes,
            "daily_remaining_bytes": self.daily_remaining_bytes,
            "router_quota_limit_bytes": self.router_quota_limit_bytes,
            "router_quota_left_bytes": self.router_quota_left_bytes,
            "observed_at": self.observed_at,
            "sample_sequence": self.sample_sequence,
            "period_accounting_qualified": self.period_accounting_qualified,
            "period_accounting_status": self.period_accounting_status,
        }


def project_foundation_data_budget(
    plan: TrafficPlan,
    observation: TrafficCounterObservation,
    *,
    plan_configured: bool,
) -> FoundationDataBudgetProjection:
    """Combine one data plan with one traffic-counter observation."""

    if type(plan) is not TrafficPlan:
        raise TypeError("foundation_data_budget_exact_traffic_plan_required")
    if type(observation) is not TrafficCounterObservation:
        raise TypeError("foundation_data_budget_typed_observation_required")
    plan_limit_bytes = max(
        0,
        int(round(float(plan.monthly_allowance_gb) * BYTES_PER_GIB)),
    )
    if observation.period_accounting_qualified:
        if (
            observation.current_cycle_download_bytes is None
            or observation.current_cycle_upload_bytes is None
        ):
            raise ValueError("foundation_data_budget_qualified_period_values_required")
        current_cycle_used_bytes: int | None = (
            observation.current_cycle_download_bytes
            + observation.current_cycle_upload_bytes
        )
        day_used_bytes = observation.day_used_bytes
    else:
        current_cycle_used_bytes = None
        day_used_bytes = None
    total_used_bytes = (
        None
        if (
            observation.total_download_bytes is None
            or observation.total_upload_bytes is None
        )
        else observation.total_download_bytes + observation.total_upload_bytes
    )
    cycle_length_days = _cycle_length_days(
        plan.cycle_start_day,
        observed_at=observation.observed_at,
    )
    daily_allowance_bytes = plan_limit_bytes // cycle_length_days
    return FoundationDataBudgetProjection(
        scope_id=plan.scope_id,
        plan_configured=bool(plan_configured),
        plan_enabled=plan_limit_bytes > 0,
        plan_limit_bytes=plan_limit_bytes,
        cycle_start_day=plan.cycle_start_day,
        cycle_length_days=cycle_length_days,
        day_used_bytes=day_used_bytes,
        current_cycle_used_bytes=current_cycle_used_bytes,
        total_used_bytes=total_used_bytes,
        left_bytes=(
            None
            if current_cycle_used_bytes is None
            else max(0, plan_limit_bytes - current_cycle_used_bytes)
        ),
        daily_allowance_bytes=daily_allowance_bytes,
        daily_remaining_bytes=(
            None
            if day_used_bytes is None
            else max(0, daily_allowance_bytes - day_used_bytes)
        ),
        router_quota_limit_bytes=observation.router_quota_limit_bytes,
        router_quota_left_bytes=observation.router_quota_left_bytes,
        observed_at=observation.observed_at,
        sample_sequence=observation.sample_sequence,
        period_accounting_qualified=observation.period_accounting_qualified,
        period_accounting_status=observation.period_accounting_status,
    )


def _cycle_length_days(cycle_day: int, *, observed_at: float) -> int:
    current = datetime.fromtimestamp(float(observed_at)).date()
    this_start = _cycle_date(current.year, current.month, cycle_day)
    if current < this_start:
        previous_year = current.year if current.month > 1 else current.year - 1
        previous_month = current.month - 1 if current.month > 1 else 12
        start = _cycle_date(previous_year, previous_month, cycle_day)
        end = this_start
    else:
        start = this_start
        next_year = current.year if current.month < 12 else current.year + 1
        next_month = current.month + 1 if current.month < 12 else 1
        end = _cycle_date(next_year, next_month, cycle_day)
    return max(1, (end - start).days)


def _cycle_date(year: int, month: int, cycle_day: int) -> date:
    return date(
        int(year),
        int(month),
        min(int(cycle_day), calendar.monthrange(int(year), int(month))[1]),
    )


__all__ = (
    "BYTES_PER_GIB",
    "FOUNDATION_DATA_BUDGET_CLIENT_ID",
    "FOUNDATION_DATA_BUDGET_NGUI_SOURCE_IDS",
    "FOUNDATION_DATA_BUDGET_OWNER",
    "FOUNDATION_DATA_BUDGET_SCHEMA",
    "FoundationDataBudgetProjection",
    "TrafficCounterObservation",
    "project_foundation_data_budget",
)
