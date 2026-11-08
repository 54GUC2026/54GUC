"""Typed contracts for optional Foundation operational services."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Callable, Mapping, Protocol

from arch4.foundation.data_budget import (
    FoundationDataBudgetProjection,
    TrafficCounterObservation,
)

TRAFFIC_PLAN_SAVE_RESULT_SCHEMA = "FoundationTrafficPlanSaveResult.v1"
TRAFFIC_PLAN_SAVE_RESULT_OWNER = "54guc.foundation.operator_config"


@dataclass(frozen=True, slots=True)
class ReportRequest:
    window: str
    open_browser: bool = True


@dataclass(frozen=True, slots=True)
class ReportResult:
    path: str


class ReportServicePort(Protocol):
    def generate(self, request: ReportRequest) -> ReportResult: ...


@dataclass(frozen=True, slots=True)
class SupportRequest:
    probe_mode: bool = False
    runtime_mode: str = ""
    profile: str = ""
    modem_identity: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class SupportResult:
    path: str


class SupportServicePort(Protocol):
    def create(
        self,
        request: SupportRequest,
        *,
        progress: Callable[[float, str], None] | None = None,
    ) -> SupportResult: ...


@dataclass(frozen=True, slots=True)
class LicensePurchaseRequest:
    identity: str
    identity_kind: str = "device"


@dataclass(frozen=True, slots=True)
class LicensePurchaseResult:
    token: str
    identity_kind: str


@dataclass(frozen=True, slots=True)
class LicenseSaveRequest:
    combined_license: str
    identity: str
    identity_kind: str = "device"


@dataclass(frozen=True, slots=True)
class LicenseSaveResult:
    identity_kind: str
    tier: str


@dataclass(frozen=True, slots=True)
class LicenseStatusRequest:
    serial: str
    entered_license: str = ""
    dev_full_access: bool = False


@dataclass(frozen=True, slots=True)
class LicenseImportRequest:
    identity: str
    nonce: str
    key: str
    email: str = ""
    tier: str = ""


@dataclass(frozen=True, slots=True)
class LicenseImportResult:
    disposition: str
    committed: bool


class LicenseServicePort(Protocol):
    @property
    def storage_root(self) -> str: ...

    def purchase_token(
        self, request: LicensePurchaseRequest
    ) -> LicensePurchaseResult: ...

    def save(self, request: LicenseSaveRequest) -> LicenseSaveResult: ...

    def status(self, request: LicenseStatusRequest) -> str: ...

    def import_if_missing(
        self, request: LicenseImportRequest
    ) -> LicenseImportResult: ...


@dataclass(frozen=True, slots=True)
class OperatorScopeRequest:
    backend: str
    provider_name: str
    provider_plmn: str
    profile_id: str
    subscription_key: str


@dataclass(frozen=True, slots=True)
class OperatorScopeResult:
    backend: str
    provider_name: str
    provider_plmn: str
    profile_id: str
    subscription_key: str
    scope_id: str


@dataclass(frozen=True, slots=True)
class TrafficPlanSaveRequest:
    limit_gb: float
    cycle_day: int
    scope: OperatorScopeResult


@dataclass(frozen=True, slots=True)
class TrafficPlanSaveResult:
    scope_id: str
    limit_gb: float
    cycle_day: int
    saved_at: float
    schema: str = TRAFFIC_PLAN_SAVE_RESULT_SCHEMA
    owner: str = TRAFFIC_PLAN_SAVE_RESULT_OWNER

    def __post_init__(self) -> None:
        scope_id = str(self.scope_id or "").strip()
        if not scope_id:
            raise ValueError("traffic_plan_save_result_scope_required")
        limit_gb = float(self.limit_gb)
        if not isfinite(limit_gb) or limit_gb < 0.0 or limit_gb > 100_000.0:
            raise ValueError("traffic_plan_save_result_limit_invalid")
        cycle_day = int(self.cycle_day)
        if not 1 <= cycle_day <= 31:
            raise ValueError("traffic_plan_save_result_cycle_day_invalid")
        saved_at = float(self.saved_at)
        if not isfinite(saved_at) or saved_at <= 0.0:
            raise ValueError("traffic_plan_save_result_timestamp_invalid")
        if self.schema != TRAFFIC_PLAN_SAVE_RESULT_SCHEMA:
            raise ValueError("traffic_plan_save_result_schema_invalid")
        if self.owner != TRAFFIC_PLAN_SAVE_RESULT_OWNER:
            raise ValueError("traffic_plan_save_result_owner_invalid")
        object.__setattr__(self, "scope_id", scope_id)
        object.__setattr__(self, "limit_gb", limit_gb)
        object.__setattr__(self, "cycle_day", cycle_day)
        object.__setattr__(self, "saved_at", saved_at)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "scope_id": self.scope_id,
            "limit_gb": self.limit_gb,
            "cycle_day": self.cycle_day,
            "saved_at": self.saved_at,
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
    ) -> "TrafficPlanSaveResult":
        values = dict(payload or {})
        expected_fields = {
            "schema",
            "owner",
            "scope_id",
            "limit_gb",
            "cycle_day",
            "saved_at",
        }
        if set(values) != expected_fields:
            raise ValueError("traffic_plan_save_result_fields_mismatch")
        return cls(
            schema=str(values["schema"]),
            owner=str(values["owner"]),
            scope_id=str(values["scope_id"]),
            limit_gb=float(values["limit_gb"]),
            cycle_day=int(values["cycle_day"]),
            saved_at=float(values["saved_at"]),
        )


class OperatorConfigServicePort(Protocol):
    def apn_profiles(self, operation: str, payload: Mapping[str, object]) -> dict[str, object]: ...

    @property
    def storage_root(self) -> str: ...

    def resolve_scope(self, request: OperatorScopeRequest) -> OperatorScopeResult: ...

    def load_traffic_plan(
        self, request: OperatorScopeRequest
    ) -> Mapping[str, object]: ...

    def save_traffic_plan(
        self, request: TrafficPlanSaveRequest
    ) -> TrafficPlanSaveResult: ...

    def project_data_budget(
        self,
        request: OperatorScopeRequest,
        observation: TrafficCounterObservation,
    ) -> FoundationDataBudgetProjection: ...


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    seed_host: str = ""


@dataclass(frozen=True, slots=True)
class DiscoveryEntry:
    host: str
    category: str
    detected_backend: str
    backend_family: str
    confidence: float
    open_ports: tuple[int, ...]
    note: str


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    mode: str
    seed_host: str
    scanned_host_count: int
    entries: tuple[DiscoveryEntry, ...]


class DiscoveryServicePort(Protocol):
    def scan(self, request: DiscoveryRequest) -> DiscoveryResult: ...


@dataclass(frozen=True, slots=True)
class OpenSourceInventoryResult:
    rendered_text: str


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    status: str
    available: bool
    current: str
    latest: str = ""
    url: str = ""
    message: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"unpublished", "current", "update_available"}:
            raise ValueError("update check status invalid")
        if not str(self.current or "").strip():
            raise ValueError("update check current version required")
        if self.status == "update_available":
            if not self.available or not str(self.latest or "").strip():
                raise ValueError("available update requires latest version")
        elif self.available:
            raise ValueError("non-available update status cannot be available")
        if self.status == "unpublished" and not str(self.message or "").strip():
            raise ValueError("unpublished update channel requires message")


class ExternalActionServicePort(Protocol):
    def open_speed_test(self) -> bool: ...

    def open_source_inventory(self) -> OpenSourceInventoryResult: ...

    def check_updates(self) -> UpdateCheckResult: ...


@dataclass(frozen=True, slots=True)
class OperationalServicePorts:
    report: ReportServicePort
    support: SupportServicePort
    license: LicenseServicePort
    operator_config: OperatorConfigServicePort
    discovery: DiscoveryServicePort
    external: ExternalActionServicePort


__all__ = [
    "DiscoveryEntry",
    "DiscoveryRequest",
    "DiscoveryResult",
    "DiscoveryServicePort",
    "ExternalActionServicePort",
    "LicenseImportRequest",
    "LicenseImportResult",
    "LicensePurchaseRequest",
    "LicensePurchaseResult",
    "LicenseSaveRequest",
    "LicenseSaveResult",
    "LicenseServicePort",
    "LicenseStatusRequest",
    "OpenSourceInventoryResult",
    "OperationalServicePorts",
    "OperatorConfigServicePort",
    "OperatorScopeRequest",
    "OperatorScopeResult",
    "ReportRequest",
    "ReportResult",
    "ReportServicePort",
    "SupportRequest",
    "SupportResult",
    "SupportServicePort",
    "TRAFFIC_PLAN_SAVE_RESULT_OWNER",
    "TRAFFIC_PLAN_SAVE_RESULT_SCHEMA",
    "TrafficPlanSaveRequest",
    "TrafficPlanSaveResult",
    "UpdateCheckResult",
]
