"""Implementations of optional Foundation operational services."""

from __future__ import annotations

import logging
from pathlib import Path
from time import time
from typing import Callable
import webbrowser

from arch4.license_credential_store import (
    load_license_credentials,
    save_license_credentials,
)
from arch4.customer_contracts.settings import (
    TrafficPlan,
)
from arch4.licensing import (
    generate_token,
    license_tier_from_nonce,
    parse_license_entry,
    validate_license,
)
from arch4.modem.device_probe import scan_local_network
from arch4.open_source_manifest import load_open_source_manifest
from arch4.foundation.site_report_job import SiteReportContext, run_site_report_job
from arch4.core_contracts.site_report_history import (
    SiteReportHistoryProjection,
)
from arch4.support_bundle_context import SupportBundleContext
from arch4.support_bundle_job import run_support_bundle_job
from arch4.updater import (
    UPDATE_CHANNEL_UNPUBLISHED,
    check_for_update,
    load_update_channel,
)
from arch4.foundation.operator_settings import FoundationOperatorSettingsStore
from arch4.foundation.data_budget import (
    FoundationDataBudgetProjection,
    TrafficCounterObservation,
)
from arch4.foundation.operator_data_budget_reader import (
    FoundationOperatorDataBudgetReader,
)

from .operational_service_contracts import (
    DiscoveryEntry,
    DiscoveryRequest,
    DiscoveryResult,
    LicenseImportRequest,
    LicenseImportResult,
    LicensePurchaseRequest,
    LicensePurchaseResult,
    LicenseSaveRequest,
    LicenseSaveResult,
    LicenseStatusRequest,
    OpenSourceInventoryResult,
    OperatorScopeRequest,
    OperatorScopeResult,
    ReportRequest,
    ReportResult,
    SupportRequest,
    SupportResult,
    TrafficPlanSaveRequest,
    TrafficPlanSaveResult,
    UpdateCheckResult,
)


log = logging.getLogger("arch4.foundation.operational_services")


_REPORT_HOURS = {
    "1h": 1.0,
    "6h": 6.0,
    "12h": 12.0,
    "1d": 24.0,
    "1w": 168.0,
    "14d": 336.0,
}

_PUBLIC_VERSION_COMPONENT_NAMES = (
    "Python",
    "PySide6",
    "Qt",
    "pyqtgraph",
    "NumPy",
    "requests",
    "RouterOS-api",
    "Liberation Mono",
)


class ReportService:
    def __init__(
        self,
        context: SiteReportContext,
        *,
        history_projector: Callable[[float], SiteReportHistoryProjection]
        | None = None,
    ) -> None:
        if type(context) is not SiteReportContext:
            raise TypeError("report_service_requires_site_report_context")
        if history_projector is not None and not callable(history_projector):
            raise TypeError("report_service_history_projector_invalid")
        self._context = context
        self._history_projector = history_projector

    def generate(self, request: ReportRequest) -> ReportResult:
        try:
            hours = _REPORT_HOURS[str(request.window)]
        except KeyError as exc:
            raise ValueError("unsupported site-report window") from exc
        generated_at = float(time())
        if self._history_projector is None:
            history = SiteReportHistoryProjection.unavailable(
                requested_hours=hours,
                generated_at=generated_at,
                reason="product_intelligence_not_attached",
            )
        else:
            try:
                history = self._history_projector(hours)
                if type(history) is not SiteReportHistoryProjection:
                    raise TypeError("site_report_history_projection_required")
            except Exception as exc:
                log.warning(
                    "Site Report Intelligence history unavailable: %s",
                    type(exc).__name__,
                )
                history = SiteReportHistoryProjection.unavailable(
                    requested_hours=hours,
                    generated_at=generated_at,
                    reason=(
                        "product_intelligence_history_unavailable:"
                        + type(exc).__name__
                    ),
                )
        path = run_site_report_job(
            context=self._context,
            hours=hours,
            history=history,
            open_browser=bool(request.open_browser),
        )
        return ReportResult(path=str(path))


class SupportService:
    def __init__(self, context: SupportBundleContext) -> None:
        if type(context) is not SupportBundleContext:
            raise TypeError("support_service_requires_launch_context")
        self._context = context

    def create(
        self,
        request: SupportRequest,
        *,
        progress: Callable[[float, str], None] | None = None,
        prepare_history: Callable[[float, Path], None] | None = None,
    ) -> SupportResult:
        from arch4.windows_native_launcher import create_support_bundle

        context = SupportBundleContext.from_runtime_paths(
            app_root=self._context.app_root,
            runtime_dir=self._context.runtime_dir,
            memory_dir=self._context.memory_dir,
            profile=str(request.profile or self._context.profile),
            source=self._context.source,
        )
        path = run_support_bundle_job(
            context,
            exporter=create_support_bundle,
            probe_mode=bool(request.probe_mode),
            runtime_mode=str(request.runtime_mode or ""),
            modem_identity=dict(request.modem_identity or {}),
            progress=progress,
            prepare_history=prepare_history,
        )
        return SupportResult(path=str(path))


class LicenseService:
    def __init__(self, app_root: str | Path) -> None:
        self._app_root = Path(app_root).resolve()

    @property
    def storage_root(self) -> str:
        return str(self._app_root)

    def purchase_token(self, request: LicensePurchaseRequest) -> LicensePurchaseResult:
        identity = str(request.identity or "").strip()
        if not identity:
            raise RuntimeError("connected modem serial or IMEI is required")
        return LicensePurchaseResult(
            token=generate_token(identity),
            identity_kind=str(request.identity_kind or "device").strip() or "device",
        )

    def save(self, request: LicenseSaveRequest) -> LicenseSaveResult:
        nonce, key = parse_license_entry(str(request.combined_license or ""))
        if not nonce or not key:
            raise ValueError(
                "invalid format; expected NONCE-KEY or generator block with Nonce and Key"
            )
        identity = str(request.identity or "").strip()
        if not identity:
            raise RuntimeError("connect the modem and wait for serial or IMEI")
        if not validate_license(identity, nonce, key):
            raise ValueError("license is invalid for the connected modem")
        current = load_license_credentials(self._app_root) or {}
        tier = license_tier_from_nonce(nonce)
        save_license_credentials(
            identity=identity,
            nonce=nonce,
            key=key,
            email=str(current.get("email") or ""),
            tier=tier,
            config_dir=self._app_root,
        )
        return LicenseSaveResult(
            identity_kind=(str(request.identity_kind or "device").strip() or "device"),
            tier=tier,
        )

    def status(self, request: LicenseStatusRequest) -> str:
        if request.dev_full_access:
            return "Licensed"
        serial = str(request.serial or "").strip()
        credential = load_license_credentials(self._app_root)
        credential_present = credential is not None
        stored_nonce = ""
        stored_key = ""
        if credential is not None:
            stored_nonce = str(credential.get("nonce") or "").strip()
            stored_key = str(credential.get("key") or "").strip()
            if stored_key:
                if not serial or not stored_nonce:
                    return "Loaded; identity check pending"
                if validate_license(serial, stored_nonce, stored_key):
                    return "Licensed"

        nonce = ""
        key = ""
        entered = str(request.entered_license or "").strip()
        if entered:
            nonce, key = parse_license_entry(entered)
            if not nonce or not key:
                return "Invalid license"
        else:
            nonce = stored_nonce
            key = stored_key
        if not key:
            return "Invalid license" if credential_present else "Not licensed"
        if not serial or not nonce:
            return "Loaded; identity check pending"
        return "Licensed" if validate_license(serial, nonce, key) else "Invalid license"

    def import_if_missing(self, request: LicenseImportRequest) -> LicenseImportResult:
        if load_license_credentials(self._app_root) is not None:
            return LicenseImportResult(
                disposition="preserved_existing_credentials",
                committed=False,
            )
        save_license_credentials(
            identity=str(request.identity or "").strip(),
            nonce=str(request.nonce or "").strip(),
            key=str(request.key or "").strip(),
            email=str(request.email or "").strip(),
            tier=str(request.tier or "").strip(),
            config_dir=self._app_root,
        )
        return LicenseImportResult(
            disposition="committed_by_license_service",
            committed=True,
        )


class OperatorConfigService:
    def __init__(self, app_root: str | Path) -> None:
        from threading import RLock
        self._settings_lock = RLock()
        self._app_root = Path(app_root).resolve()
        self._store = FoundationOperatorSettingsStore(self._app_root)
        self._reader = FoundationOperatorDataBudgetReader(self._app_root)

    def apn_profiles(self, operation: str, payload: Mapping[str, object]) -> dict[str, object]:
        from arch4.foundation.apn_profiles import public_apn_library, save_apn_choice
        with self._settings_lock:
            if operation == "load" and not payload:
                return {"library": public_apn_library(self._store.load())}
            if operation == "save":
                return save_apn_choice(self._store, payload)
            raise ValueError("apn_profile_operation_invalid")

    @property
    def storage_root(self) -> str:
        return str(self._app_root)

    def resolve_scope(self, request: OperatorScopeRequest) -> OperatorScopeResult:
        return self._reader.resolve_scope(request)

    def load_traffic_plan(self, request: OperatorScopeRequest) -> dict[str, object]:
        return self._reader.load_traffic_plan(request)

    def save_traffic_plan(
        self, request: TrafficPlanSaveRequest
    ) -> TrafficPlanSaveResult:
        scope = self.resolve_scope(
            OperatorScopeRequest(
                subscription_key=request.scope.subscription_key,
                backend=request.scope.backend,
                provider_name=request.scope.provider_name,
                provider_plmn=request.scope.provider_plmn,
                profile_id=request.scope.profile_id,
            )
        )
        if scope.scope_id != str(request.scope.scope_id or "").strip():
            raise RuntimeError("traffic plan scope receipt does not match save payload")
        with self._settings_lock:
            self._save_traffic_plan(request, scope)
        return TrafficPlanSaveResult(
            scope_id=scope.scope_id,
            limit_gb=float(request.limit_gb),
            cycle_day=int(request.cycle_day),
            saved_at=time(),
        )

    def _save_traffic_plan(self, request, scope):
        self._store.save_traffic_plan(
            TrafficPlan(
                subscription_key=scope.subscription_key,
                scope_id=scope.scope_id,
                modem_backend=scope.backend,
                provider_name=scope.provider_name,
                provider_plmn=scope.provider_plmn,
                profile_id=scope.profile_id,
                monthly_allowance_gb=float(request.limit_gb),
                cycle_start_day=int(request.cycle_day),
                override_enabled=True,
            )
        )

    def project_data_budget(
        self,
        request: OperatorScopeRequest,
        observation: TrafficCounterObservation,
    ) -> FoundationDataBudgetProjection:
        return self._reader.project_data_budget(request, observation)


class DiscoveryService:
    def scan(self, request: DiscoveryRequest) -> DiscoveryResult:
        result = scan_local_network(seed_host=str(request.seed_host or ""))
        return DiscoveryResult(
            mode=result.mode,
            seed_host=result.seed_host,
            scanned_host_count=result.scanned_host_count,
            entries=tuple(
                DiscoveryEntry(
                    host=entry.host,
                    category=entry.category,
                    detected_backend=entry.detected_backend,
                    backend_family=entry.backend_family,
                    confidence=float(entry.confidence),
                    open_ports=tuple(entry.open_ports),
                    note=entry.note,
                )
                for entry in result.entries
            ),
        )


class ExternalActionService:
    def __init__(self, app_root: str | Path, *, current_version: str) -> None:
        self._app_root = Path(app_root).resolve()
        self._current_version = str(current_version or "").strip()
        if not self._current_version:
            raise ValueError("external action service requires current version")

    def open_speed_test(self) -> bool:
        return bool(webbrowser.open("https://www.speedtest.net/"))

    def open_source_inventory(self) -> OpenSourceInventoryResult:
        public_names = frozenset(_PUBLIC_VERSION_COMPONENT_NAMES)
        public_order = {
            name: index for index, name in enumerate(_PUBLIC_VERSION_COMPONENT_NAMES)
        }
        components = tuple(
            component
            for component in load_open_source_manifest(self._app_root)
            if component.name in public_names
        )
        rendered = "\n".join(
            f"{component.name} — {component.license}"
            for component in sorted(
                components,
                key=lambda item: public_order.get(item.name, len(public_order)),
            )
        )
        return OpenSourceInventoryResult(rendered_text=rendered)

    def check_updates(self) -> UpdateCheckResult:
        channel = load_update_channel(self._app_root)
        if channel.status == UPDATE_CHANNEL_UNPUBLISHED:
            return UpdateCheckResult(
                status="unpublished",
                available=False,
                current=self._current_version,
                message=channel.message,
            )
        result = check_for_update(self._current_version, channel=channel)
        if result is None:
            return UpdateCheckResult(
                status="current",
                available=False,
                current=self._current_version,
                message="The public update channel was checked successfully.",
            )
        return UpdateCheckResult(
            status="update_available",
            available=True,
            current=self._current_version,
            latest=result.tag_name,
            url=result.download_url or result.html_url,
            message="A newer public release is available.",
        )


__all__ = [
    "DiscoveryService",
    "ExternalActionService",
    "LicenseService",
    "OperatorConfigService",
    "ReportService",
    "SupportService",
]
