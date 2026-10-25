"""Read-only Foundation boundary for exact-scope operator data budgets."""

from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from typing import Any, Mapping

from arch4.core_contracts.accepted_generation_root import (
    CoreAcceptedGenerationRoot,
)
from arch4.core_contracts.accepted_current_facts import CoreAcceptedCurrentFacts
from arch4.customer_contracts.settings import TrafficPlan, traffic_plan_scope_id
from arch4.foundation.data_budget import (
    FoundationDataBudgetProjection,
    TrafficCounterObservation,
    project_foundation_data_budget,
)
from arch4.foundation.operational_service_contracts import (
    OperatorScopeRequest,
    OperatorScopeResult,
)
from arch4.foundation.operator_settings import FoundationOperatorSettingsStore
from arch4.foundation.traffic_accounting import (
    FoundationTrafficAccountingAuthority,
    read_accounted_traffic,
)


FOUNDATION_OPERATOR_DATA_BUDGET_READER_OWNER = (
    "54guc.foundation.operator_data_budget_reader"
)
QUALIFIED_PERIOD_ACCOUNTING_STATUSES = frozenset(
    {
        "counter_rebased",
        "counter_source_rebased",
        "subscription_rebased",
        "native",
        "ok",
        "plan_rebased",
        "updated",
    }
)


def _period_accounting_qualified(status: object) -> bool:
    return str(status or "").strip().lower() in QUALIFIED_PERIOD_ACCOUNTING_STATUSES


def _optional_counter(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _billing_scope_id(request: OperatorScopeRequest) -> str:
    """Stable device/provider accounting scope without mutable profile identity."""

    backend = str(request.backend or "").strip().lower()
    provider = str(request.provider_plmn or request.provider_name or "").strip()
    if not backend or not provider:
        raise RuntimeError("foundation_traffic_accounting_billing_scope_unavailable")
    if not request.subscription_key:
        raise RuntimeError("foundation_traffic_accounting_subscription_unavailable")
    return f"backend={backend}|provider={provider}|sim={request.subscription_key}"


class FoundationOperatorDataBudgetReader:
    """Read operator settings without providing a write API."""

    def __init__(self, storage_root: str | Path) -> None:
        self._storage_root = Path(storage_root).resolve()
        self._store = FoundationOperatorSettingsStore(self._storage_root)

    @property
    def storage_root(self) -> str:
        return str(self._storage_root)

    @staticmethod
    def resolve_scope(request: OperatorScopeRequest) -> OperatorScopeResult:
        backend = str(request.backend or "").strip().lower()
        provider_name = str(request.provider_name or "").strip()
        provider_plmn = str(request.provider_plmn or "").strip()
        profile_id = str(request.profile_id or "").strip()
        if not backend or not profile_id or not (provider_name or provider_plmn):
            raise RuntimeError(
                "traffic plan requires exact modem/provider/profile scope"
            )
        scope_id = traffic_plan_scope_id(
            subscription_key=request.subscription_key,
            modem_backend=backend,
            provider_name=provider_name,
            provider_plmn=provider_plmn,
            profile_id=profile_id,
        )
        if not scope_id:
            raise RuntimeError("traffic plan exact scope could not be constructed")
        return OperatorScopeResult(
            subscription_key=request.subscription_key,
            backend=backend,
            provider_name=provider_name,
            provider_plmn=provider_plmn,
            profile_id=profile_id,
            scope_id=scope_id,
        )

    def load_traffic_plan(self, request: OperatorScopeRequest) -> dict[str, object]:
        scope = self.resolve_scope(request)
        return self._store.load_traffic_plan(scope.scope_id)

    def project_data_budget(
        self,
        request: OperatorScopeRequest,
        observation: TrafficCounterObservation,
    ) -> FoundationDataBudgetProjection:
        if type(observation) is not TrafficCounterObservation:
            raise TypeError("operator_config_typed_traffic_observation_required")
        plan, plan_configured = self.exact_traffic_plan(request)
        return project_foundation_data_budget(
            plan,
            observation,
            plan_configured=plan_configured,
        )

    def exact_traffic_plan(
        self,
        request: OperatorScopeRequest,
    ) -> tuple[TrafficPlan, bool]:
        scope = self.resolve_scope(request)
        settings = self._store.load_traffic_plan(scope.scope_id)
        if str(settings.get("scope_id") or "").strip() != scope.scope_id:
            raise RuntimeError("operator_config_traffic_plan_scope_mismatch")
        plan = TrafficPlan(
            subscription_key=scope.subscription_key,
            scope_id=scope.scope_id,
            modem_backend=scope.backend,
            provider_name=scope.provider_name,
            provider_plmn=scope.provider_plmn,
            profile_id=scope.profile_id,
            monthly_allowance_gb=float(settings["monthly_allowance_gb"]),
            cycle_start_day=int(settings["cycle_start_day"]),
            override_enabled=bool(settings["override_enabled"]),
        )
        return plan, bool(settings["scoped"])


def _available_current_fact(
    current_facts: CoreAcceptedCurrentFacts,
    path: str,
) -> tuple[object, object] | None:
    try:
        fact = current_facts.exact(path)
    except KeyError:
        return None
    if fact.disposition != "available":
        return None
    envelope = fact.value.to_mapping()
    if envelope.get("present") is not True:
        raise RuntimeError(
            "foundation_data_budget_current_fact_envelope_invalid"
        )
    return envelope["value"], fact


def project_operator_data_budget_from_accepted_root(
    reader: FoundationOperatorDataBudgetReader,
    accepted_root: CoreAcceptedGenerationRoot,
    *,
    traffic_accounting: FoundationTrafficAccountingAuthority | None = None,
) -> FoundationDataBudgetProjection | None:
    """Build the budget view from accepted Core facts without reopening Matrix storage."""

    if type(reader) is not FoundationOperatorDataBudgetReader:
        raise TypeError("foundation_data_budget_operator_reader_required")
    if not isinstance(accepted_root, CoreAcceptedGenerationRoot):
        raise TypeError("foundation_data_budget_accepted_root_required")
    return project_operator_data_budget_from_accepted_facts(
        reader,
        accepted_root.current_facts,
        traffic_accounting=traffic_accounting,
    )


def project_operator_data_budget_from_accepted_facts(
    reader: FoundationOperatorDataBudgetReader,
    current_facts: CoreAcceptedCurrentFacts,
    *,
    traffic_accounting: FoundationTrafficAccountingAuthority | None = None,
    accounting_read_root: str | Path | None = None,
    as_of: float | None = None,
) -> FoundationDataBudgetProjection | None:
    """Build the same budget view from a saved accepted-current record."""

    if type(reader) is not FoundationOperatorDataBudgetReader:
        raise TypeError("foundation_data_budget_operator_reader_required")
    if not isinstance(current_facts, CoreAcceptedCurrentFacts):
        raise TypeError("foundation_data_budget_accepted_current_facts_required")
    paths = {
        "backend": "identity.backend",
        "provider_name": "identity.provider_name",
        "provider_plmn": "identity.provider_plmn",
        "profile_id": "identity.profile_id",
        "sim_network": "identity.sim_network",
        "serial": "identity.serial",
        "imei": "identity.imei",
        "traffic_valid": "traffic.traffic_valid",
        "traffic_invalid_reason": "traffic.traffic_invalid_reason",
        "traffic_accounting_status": "traffic.traffic_accounting_status",
        "traffic_accounting_source": "traffic.traffic_accounting_source",
        "traffic_counter_period_started_at": "traffic.traffic_counter_period_started_at",
        "traffic_counter_interface_started_at": "traffic.traffic_counter_interface_started_at",
        "traffic_quota_start_day": "traffic.traffic_quota_start_day",
        "traffic_day_used_bytes": "traffic.traffic_day_used_bytes",
        "traffic_month_download_bytes": (
            "traffic.traffic_month_download_bytes"
        ),
        "traffic_month_upload_bytes": (
            "traffic.traffic_month_upload_bytes"
        ),
        "traffic_total_download_bytes": (
            "traffic.traffic_total_download_bytes"
        ),
        "traffic_total_upload_bytes": (
            "traffic.traffic_total_upload_bytes"
        ),
        "traffic_quota_limit_bytes": "traffic.traffic_quota_limit_bytes",
        "traffic_quota_left_bytes": "traffic.traffic_quota_left_bytes",
        "traffic_device_download_counter_bytes": (
            "traffic.traffic_device_download_counter_bytes"
        ),
        "traffic_device_upload_counter_bytes": (
            "traffic.traffic_device_upload_counter_bytes"
        ),
    }
    projected: dict[str, object] = {}
    evidence: dict[str, object] = {}
    for name, path in paths.items():
        current = _available_current_fact(current_facts, path)
        if current is None:
            projected[name] = None
            continue
        projected[name], evidence[name] = current

    if not (
        str(projected["backend"] or "").strip()
        and str(projected["profile_id"] or "").strip()
        and (
            str(projected["provider_name"] or "").strip()
            or str(projected["provider_plmn"] or "").strip()
        )
    ):
        return None
    traffic_available = bool(
        projected["traffic_valid"] is True
        and not str(projected["traffic_invalid_reason"] or "").strip()
    )
    timing_fact = (
        evidence.get("traffic_valid")
        if traffic_available
        else evidence.get("backend")
    )
    sample_sequence = int(
        getattr(timing_fact, "source_sequence", 0)
    )
    sample_ts = float(getattr(timing_fact, "sampled_at", 0.0))
    if sample_sequence <= 0 or sample_ts <= 0.0:
        return None
    accounting_status = (
        str(projected["traffic_accounting_status"] or "").strip()
        if traffic_available
        else str(projected["traffic_invalid_reason"] or "unavailable").strip()
    )
    period_counters_available = all(
        projected[name] is not None
        for name in (
            "traffic_month_download_bytes",
            "traffic_month_upload_bytes",
        )
    )
    period_qualified = traffic_available and period_counters_available and (
        not accounting_status
        or _period_accounting_qualified(accounting_status)
    )
    sim = projected["sim_network"]
    subscription = sim.get("subscription_key") if isinstance(sim, Mapping) else None
    if not subscription:
        return None
    request = OperatorScopeRequest(
        subscription_key=subscription,
        backend=str(projected["backend"] or ""),
        provider_name=str(projected["provider_name"] or ""),
        provider_plmn=str(projected["provider_plmn"] or ""),
        profile_id=str(projected["profile_id"] or ""),
    )
    plan, plan_configured = reader.exact_traffic_plan(request)
    if period_qualified and plan_configured and projected["traffic_quota_start_day"] != plan.cycle_start_day:
        # Native calendar-month counters do not prove a user-selected cycle.
        # Qualified raw-counter accounting below owns that period instead.
        period_qualified = False
        accounting_status = "native_period_does_not_match_plan"
    observation = TrafficCounterObservation(
        day_used_bytes=_optional_counter(
            projected["traffic_day_used_bytes"] if traffic_available else None
        ),
        current_cycle_download_bytes=_optional_counter(
            projected["traffic_month_download_bytes"]
            if traffic_available
            else None
        ),
        current_cycle_upload_bytes=_optional_counter(
            projected["traffic_month_upload_bytes"]
            if traffic_available
            else None
        ),
        total_download_bytes=_optional_counter(
            projected["traffic_total_download_bytes"]
            if traffic_available
            else None
        ),
        total_upload_bytes=_optional_counter(
            projected["traffic_total_upload_bytes"]
            if traffic_available
            else None
        ),
        router_quota_limit_bytes=(
            projected["traffic_quota_limit_bytes"]
            if traffic_available
            else None
        ),
        router_quota_left_bytes=(
            projected["traffic_quota_left_bytes"]
            if traffic_available
            else None
        ),
        observed_at=sample_ts,
        sample_sequence=sample_sequence,
        period_accounting_qualified=period_qualified,
        period_accounting_status=accounting_status,
    )
    if not period_qualified and plan_configured and accounting_read_root is not None and traffic_accounting is None:
        # Saved accounting has its own exact scope and timestamp. A temporarily
        # missing NEW counter must not hide an already qualified usage record.
        # This branch never advances the mutable counter stream.
        serial = str(projected["serial"] or "").strip()
        imei = str(projected["imei"] or "").strip()
        device_key = "serial:" + serial if serial else "imei:" + imei if imei else ""
        if device_key:
            saved = read_accounted_traffic(
                accounting_read_root, billing_scope_id=_billing_scope_id(request),
                device_identity_key=device_key, cycle_start_day=plan.cycle_start_day,
                source_namespace=current_facts.matrix_identity.namespace,
                counter_source=str(projected["traffic_accounting_source"] or ""),
                as_of=float(as_of if as_of is not None else sample_ts),
                router_quota_limit_bytes=observation.router_quota_limit_bytes,
                router_quota_left_bytes=observation.router_quota_left_bytes,
            )
            if saved is not None:
                return reader.project_data_budget(request, saved)
    if (
        not period_qualified
        and plan_configured
        and traffic_accounting is not None
        and traffic_available
        and projected["traffic_device_download_counter_bytes"] is not None
        and projected["traffic_device_upload_counter_bytes"] is not None
    ):
        raw_timing = evidence.get("traffic_device_download_counter_bytes")
        raw_sequence = int(getattr(raw_timing, "source_sequence", 0))
        raw_sampled_at = float(getattr(raw_timing, "sampled_at", 0.0))
        raw_namespace = getattr(raw_timing, "acquisition_namespace", None)
        # A cumulative pair, its source and optional period proof must describe
        # one physical read. Matrix can legitimately retain individual cells;
        # joining different receipts here would invent traffic.
        pair_names = ["traffic_device_upload_counter_bytes", "traffic_accounting_source"]
        for hint in ("traffic_counter_period_started_at", "traffic_counter_interface_started_at"):
            if projected[hint] is not None:
                pair_names.append(hint)
        provenance = ("source_sequence", "sampled_at", "acquisition_attempt_id", "acquisition_namespace")
        if any(
            any(getattr(evidence.get(name), field, None) != getattr(raw_timing, field, None)
                for field in provenance)
            for name in pair_names
        ):
            return reader.project_data_budget(request, replace(
                observation, period_accounting_qualified=False,
                period_accounting_status="counter_receipt_mismatch"))
        serial = str(projected["serial"] or "").strip()
        imei = str(projected["imei"] or "").strip()
        device_identity_key = (
            "serial:" + serial if serial else "imei:" + imei if imei else ""
        )
        if not device_identity_key:
            raise RuntimeError(
                "foundation_device_counter_identity_evidence_required"
            )
        if raw_sequence <= 0 or raw_sampled_at <= 0.0:
            raise RuntimeError(
                "foundation_device_counter_acquisition_evidence_required"
            )
        # A qualified native period already proves the selected cycle.
        # Plan entry must not replace it with a new zero-based raw ledger.
        observation = traffic_accounting.observe(
            billing_scope_id=_billing_scope_id(request),
            device_identity_key=device_identity_key,
            cycle_start_day=plan.cycle_start_day,
            download_counter_bytes=int(
                projected["traffic_device_download_counter_bytes"]
            ),
            upload_counter_bytes=int(
                projected["traffic_device_upload_counter_bytes"]
            ),
            observed_at=raw_sampled_at,
            sample_sequence=raw_sequence,
            source_namespace=raw_namespace,
            counter_source=str(projected["traffic_accounting_source"] or ""),
            router_quota_limit_bytes=_optional_counter(
                projected["traffic_quota_limit_bytes"]
            ),
            router_quota_left_bytes=_optional_counter(
                projected["traffic_quota_left_bytes"]
            ),
            counter_period_started_at=(
                float(projected["traffic_counter_period_started_at"])
                if projected["traffic_counter_period_started_at"] is not None else None
            ),
            counter_interface_started_at=(
                float(projected["traffic_counter_interface_started_at"])
                if projected["traffic_counter_interface_started_at"] is not None else None
            ),
        )
    return reader.project_data_budget(
        request,
        observation,
    )


__all__ = (
    "FOUNDATION_OPERATOR_DATA_BUDGET_READER_OWNER",
    "FoundationOperatorDataBudgetReader",
    "project_operator_data_budget_from_accepted_facts",
    "project_operator_data_budget_from_accepted_root",
)
